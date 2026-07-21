from __future__ import annotations

"""Swarm ACP server adapter primitives for Epic 009.

This module provides the *production* adapter surface that binds
:class:`SwarmACPMux` to concrete ACP server connections.

For User Story 1 (US1) the focus is intentionally narrow:

* create exactly one :class:`SwarmACPMux` per external ACP session; and
* expose a minimal per-session API that:

  - executes the three-stage `_attach` transaction
    (prepare → acknowledgment → activate);
  - dispatches `_detach` to :meth:`SwarmACPMux.detach`;
  - routes ordinary prompt and interrupt operations through the mux; and
  - serialises `_attach`, `_detach`, and session shutdown for one
    external session.

Higher-level concerns such as reserved-control parsing, logical error
mapping, and structured connection lifetime are covered by later tasks
(T019, T020, T025 in ``specs/009-swarm-acp-mux/tasks.md``) and build on
this minimal session primitive.
"""

import asyncio
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from .daemon import RuntimeDaemon
from .swarm_acp_mux import ExternalACPConnection, SwarmACPMux, SwarmAgentClient

__all__ = ["SwarmACPServerSession"]


@dataclass(slots=True)
class SwarmACPServerSession:
    """Adapter-owned state for a single external ACP session.

    A new :class:`SwarmACPServerSession` instance must be created for
    each external ACP session. Construction wires a dedicated
    :class:`SwarmACPMux` for that session and provides a small, explicit
    API used by the server adapter's request handlers.

    This type deliberately **does not** own the concrete ACP transport
    or wire protocol. Callers supply an :class:`ExternalACPConnection`
    that exposes the ``session_update`` method used by the mux for
    forwarding typed :class:`SessionUpdate` objects.

    Concurrency model (US1 / contract §1):

    * For a given external session, the adapter MUST treat `_attach`,
      `_detach`, and mux/connection shutdown as a single-threaded control
      stream.
    * This is enforced here via ``_control_lock`` so that no second
      `_attach` / `_detach` / shutdown sequence overlaps an in-flight
      attachment transaction.
    * Ordinary agent-directed operations (prompt/interrupt) are routed
      directly through the mux and may be subject to adapter-specific
      concurrency rules in later tasks.
    """

    daemon: RuntimeDaemon
    agent_client: SwarmAgentClient
    external_connection: ExternalACPConnection
    external_session_id: str

    mux: SwarmACPMux = field(init=False)

    # Per-session control lock used to serialize `_attach`, `_detach`,
    # and shutdown. This is layered on top of the mux's own internal
    # lifecycle lock and follows the serialization requirements from the
    # session contract (see ``specs/009-swarm-acp-mux/contracts``).
    _control_lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        """Create the per-session :class:`SwarmACPMux`.

        Exactly one mux is instantiated for each external ACP session
        and it MUST NOT be shared across sessions (spec §3, contract §1).
        """

        self.mux = SwarmACPMux(
            daemon=self.daemon,
            agent_client=self.agent_client,
            external_connection=self.external_connection,
            external_session_id=self.external_session_id,
        )

    # ------------------------------------------------------------------
    # Reserved swarm-control operations (US1 subset)
    # ------------------------------------------------------------------

    async def attach(
        self,
        agent_id: str,
        *,
        acknowledge: Callable[[str], Awaitable[None]],
    ) -> None:
        """Perform the logical `_attach` operation for this session.

        This method encodes the three-stage attachment transaction
        described in spec §8.1–§8.3 and the session contract §3.3 using
        :meth:`SwarmACPMux.attach`:

        1. ``prepare_attach(agent_id)`` establishes the internal Epic 008
           subscription via
           :meth:`SwarmAgentClient.subscribe_acp_updates`.
        2. The provided ``acknowledge`` callback is awaited to write the
           `_attach` success acknowledgment back to the external client.
        3. ``activate_attachment(prepared)`` begins forwarding retained
           and live typed updates, releasing the forwarding gate only
           after acknowledgment has succeeded.

        Acknowledgment failures or cancellation roll back only newly
        prepared attachments; pre-existing healthy attachments reused
        idempotently remain intact. These semantics are enforced by the
        mux itself; this adapter layer is responsible for serialising the
        transaction so that no second `_attach` or `_detach` overlaps
        steps (1)–(3).
        """

        async with self._control_lock:
            await self.mux.attach(agent_id, acknowledge=acknowledge)

    async def detach(self) -> None:
        """Perform the logical `_detach` operation for this session.

        Detach is serialised with `_attach` and shutdown for the same
        external session to preserve the single-threaded control-stream
        invariant. The underlying mux implements idempotent detach
        semantics and ensures that only its own subscription is removed
        from the agent's :class:`AcpSessionUpdateStream`.
        """

        async with self._control_lock:
            await self.mux.detach()

    # ------------------------------------------------------------------
    # Ordinary agent-directed operations
    # ------------------------------------------------------------------

    async def prompt(self, text: str) -> str | None:
        """Send a prompt to the currently attached agent via the mux.

        Preconditions (enforced by :class:`SwarmACPMux`):

        * the mux must be open; and
        * an agent must be attached.

        Violations raise :class:`SwarmACPMuxClosedError` or
        :class:`NoAttachedAgentError` respectively and are mapped to
        logical error codes by higher-level adapter logic in later
        tasks.
        """

        return await self.mux.prompt(text)

    async def interrupt(self) -> None:
        """Request an interrupt for the currently attached agent via the mux."""

        await self.mux.interrupt()

    # ------------------------------------------------------------------
    # Session shutdown / cleanup
    # ------------------------------------------------------------------

    async def close(self) -> None:
        """Close the mux and serialise shutdown with attach/detach.

        This helper is intended to be invoked when the external ACP
        session terminates. It ensures that:

        * no new `_attach` / `_detach` begins while shutdown is in
          progress; and
        * the underlying mux is always closed exactly once.

        Mapping to wire-level behaviour (for example, terminating the
        concrete ACP transport) is owned by higher-level server code and
        is implemented in later tasks.
        """

        async with self._control_lock:
            await self.mux.close()

    async def __aenter__(self) -> "SwarmACPServerSession":
        # Allow connection handlers to use the session as an async
        # context manager in later user stories. The mux is fully
        # initialised at construction time so we simply return ``self``.
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.close()
