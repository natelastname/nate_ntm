"""ACP client adapters for the swarm runtime.

The nate_ntm runtime owns all ACP (Agent Control Protocol) integrations
for agents in a swarm. This module defines the
:class:`BaseAcpClient` abstraction that the runtime and scheduler use to
interact with ACP-backed agent runtimes.

Concrete implementation in this branch:

* :class:`NateOhaAcpClient` – the nate-oha-backed ACP adapter used as the
  canonical implementation for the nate_ntm runtime in all modes.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, AsyncIterator, Callable, Dict, Literal, Mapping, Optional, Set

from acp.meta import PROTOCOL_VERSION
from acp.schema import TextContentBlock

from ..config.runtime_config import RuntimeConfig
from .acp_connection import open_nate_oha_acp_client
from .acp_protocol_client import NATE_NTM_CLIENT_CAPABILITIES
from .acp_types import SessionUpdate
from .acp_update_stream import AcpSessionUpdateStream, AgentSessionNotActive, ReceivedSessionUpdate, StreamClosedError
from .events import AgentEvent, AgentEventSource
from .metadata_store import MetadataStore
from .nate_oha_launch import materialize_nate_oha_config
from .swarm_state import AgentState

__all__ = [
    "AcpClientError",
    "AcpAgentStatus",
    "AcpAgentSession",
    "BaseAcpClient",
    "NateOhaAcpClient",
]


logger = logging.getLogger(__name__)

class AcpClientError(RuntimeError):
    """Base error type for ACP adapter failures."""



@dataclass(slots=True)
class AcpAgentStatus:
    """Lightweight adapter-level status for a single agent.

    Instances of this type are returned by :meth:`BaseAcpClient.get_status`
    and are intended to be easy to map onto :class:`AgentRuntimeState` and
    runtime API payloads.
    """

    agent_id: str
    """Identifier of the agent this status belongs to."""

    state: str
    """Adapter-level lifecycle state (for example ``"idle"`` or ``"running"``)."""

    last_exit_code: int | None = None
    """Exit code from the most recent process run, if applicable."""

    last_error: str | None = None
    """Summary of the last error observed for this agent, if any."""

    restart_count: int = 0
    """Number of restarts attempted for this agent, when tracked."""



@dataclass(slots=True)
class AcpAgentSession:
    """Runtime-owned state for a live ACP-backed agent session.

    This structure captures the resources and identifiers needed to
    supervise a single nate-oha / ACP session from the runtime's
    perspective. It is intentionally narrow and does not expose ACP SDK
    types directly; concrete adapters such as :class:`NateOhaAcpClient`
    are responsible for storing whatever rich SDK objects are required
    in the opaque ``process``, ``connection``, and ``protocol_client``
    fields.

    The fields are aligned with the active-session model described in
    ``specs/005-nate-oha-migration/spec-appendix-B.md`` and the T017
    task in ``tasks.md``.
    """

    agent_id: str
    """Identifier of the agent this session belongs to."""

    conversation_id: str
    """Opaque, ACP-owned conversation/session identifier.

    The runtime treats this as an opaque token obtained from the nate-oha
    ACP runtime (for example via ``session/new``) and persists it only for
    resume and correlation purposes.
    """

    process: Any
    """Process or process-group handle used for supervision.

    In early implementations this may be a :class:`subprocess.Popen`
    instance; longer term it is expected to be a small adapter type such
    as ``AgentProcess``.
    """

    connection: Any
    """Client-side ACP connection handle.

    This will typically be an instance of the ACP SDK's
    ``ClientSideConnection`` type once the integration is wired in.
    """

    protocol_client: Any
    """Callback client responsible for handling ACP events.

    Concrete implementations are expected to store a
    ``NateNtmAcpProtocolClient``-like object here, which exposes methods
    such as ``session_update(...)`` and publishes typed ACP updates into
    :attr:`update_stream`.
    """

    # Per-session typed ACP update stream owned by this concrete session.
    update_stream: AcpSessionUpdateStream = field(default_factory=AcpSessionUpdateStream)

    status: str = "starting"
    """Adapter-level lifecycle status for this session.

    Typical values include ``"starting"``, ``"running"``,
    ``"stopping"``, ``"terminated"``, and ``"failed"``.
    """

    stderr_task: Any | None = None
    """Background task responsible for draining stderr diagnostics."""

    exit_monitor_task: Any | None = None
    """Background task responsible for monitoring process exit."""


@dataclass(slots=True)
class NateOhaProcessRecord:
    """In-memory supervision record for a nate-oha subprocess.

    This mirrors the conceptual model described in
    ``specs/002-nate-oha-acp-adapter/data-model.md`` section 2.1.
    ``NateOhaAcpClient`` maintains one record per nate-oha–backed agent.
    """

    agent_id: str
    pid: int | None = None
    status: Literal["starting", "running", "stopping", "terminated", "failed"] = "starting"
    last_start_time: datetime | None = None
    last_exit_code: int | None = None
    last_error: str | None = None
    restart_count: int = 0


class BaseAcpClient:
    """Runtime-facing contract for ACP-backed agent execution.

    Implementations are responsible for:

    * Owning ACP/runtime lifecycle for managed agents (process launch,
      ACP session initialization, shutdown, and status reporting).
    * Providing an agent-centric interface to ACP that can be driven
      from the runtime scheduler and daemon.

    Long-term, the public contract is expected to be expressed in terms
    of asynchronous, agent-lifecycle operations (see T016 in
    ``specs/005-nate-oha-migration/tasks.md``). The initial
    implementation exposes these via ``*_async`` methods so they can
    coexist with the pre-existing synchronous API during the migration:

    .. code-block:: python

        async def start_agent_async(...)
        async def prompt(...)
        async def interrupt(...)
        async def stop_agent_async(...)
        def get_status(...)

    Earlier iterations of the ACP client exposed additional
    conversation/turn helpers such as ``ensure_conversation`` and
    ``start_turn``. These have been superseded by the agent-centric
    async lifecycle above (see Epic 005) and are no longer part of the
    public ACP client contract. New runtime code and adapters should
    model work in terms of the agent-lifecycle operations listed
    above.

    Concrete implementations are expected to be **runtime-owned** and
    reused for the lifetime of the process.
    """

    #: Optional callback invoked when adapter-level events occur for an
    #: agent. Implementations SHOULD invoke this for significant ACP or
    #: process lifecycle events when configured.
    on_event: Callable[[AgentEvent], None] | None = None

    # The following methods define the public contract. Concrete
    # implementations *must* override them.

    def start_agent(self, agent_id: str, *, metadata: AgentState) -> None:  # pragma: no cover - abstract
        """Launch or attach to the ACP runtime backing ``agent_id``.

        Implementations are free to decide how much work is performed
        synchronously here (for example, spawning a subprocess and
        performing an initial health check) as long as they satisfy the
        process launch contract described in the feature spec.

        Longer term this operation is expected to be expressed as an
        asynchronous agent-lifecycle method (see T016); this synchronous
        variant remains the stable API until the async interface is
        introduced and call sites are migrated.
        """

        raise NotImplementedError

    # ------------------------------------------------------------------
    # Agent-lifecycle async API (T016)
    # ------------------------------------------------------------------

    async def start_agent_async(self, agent_id: str, *, metadata: AgentState) -> None:  # pragma: no cover - abstract
        """Asynchronously launch or attach to the ACP runtime for ``agent_id``.

        Implementations SHOULD override this method to provide an
        awaitable agent-lifecycle entrypoint. The default implementation
        delegates to :meth:`start_agent` so that existing synchronous
        adapters remain usable during the migration.
        """

        self.start_agent(agent_id, metadata=metadata)

    async def stop_agent_async(self, agent_id: str, *, timeout: float) -> None:  # pragma: no cover - abstract
        """Asynchronously request a graceful stop for the ACP runtime.

        Implementations SHOULD override this method to provide an
        awaitable shutdown helper. The default implementation delegates
        to :meth:`stop_agent`.
        """

        self.stop_agent(agent_id, timeout=timeout)

    async def prompt(self, agent_id: str, prompt: str | None = None) -> str | None:  # pragma: no cover - abstract
        """Asynchronously prompt the agent and return an adapter-defined ID.

        Concrete implementations are expected to map this onto the
        appropriate ACP operation for initiating new work in the
        agent-centric model.
        """

        raise NotImplementedError

    async def interrupt(self, agent_id: str) -> None:  # pragma: no cover - abstract
        """Request cancellation or interruption of in-flight work for ``agent_id``.

        The exact behavior is adapter-specific and may be a no-op for
        simple dev-mode implementations.
        """

        raise NotImplementedError

    def stop_agent(self, agent_id: str, *, timeout: float) -> None:  # pragma: no cover - abstract
        """Request a graceful stop for the ACP runtime backing ``agent_id``.

        Implementations should enforce a bounded timeout and apply any
        configured restart or escalation policy on timeout.
        """

        raise NotImplementedError

    def get_status(self, agent_id: str) -> AcpAgentStatus:  # pragma: no cover - abstract
        """Return a lightweight status snapshot for ``agent_id``.

        The returned :class:`AcpAgentStatus` is intended to be easy to map
        onto :class:`AgentRuntimeState` and the runtime API payloads.
        """

        raise NotImplementedError


@dataclass(slots=True)
class NateOhaAcpClient(BaseAcpClient):
    """Production ACP adapter that launches and supervises the nate-oha runtime.

    NateOhaAcpClient is the canonical production implementation of
    :class:`BaseAcpClient` for the nate_ntm runtime. It owns the lifecycle of
    a dedicated nate-oha process per managed agent and reports adapter-level
    status via :class:`AcpAgentStatus`.

    The initial implementation focuses on the process-supervision contract
    described in the Feature 002 spec. Conversation semantics and ACP event
    streaming are added in subsequent tasks.
    """

    config: RuntimeConfig

    #: Executable used to launch the nate-oha CLI. This may be overridden in
    #: tests or deployment-specific configuration if needed.
    executable: str = "nate-oha"

    #: Maximum time to wait for initial nate-oha readiness checks.
    startup_timeout: float = 15.0

    #: Default timeout for graceful shutdown requests.

    def __post_init__(self) -> None:
        """Initialise adapter defaults from :class:`RuntimeConfig`.

        The nate-oha CLI executable used for launches is always taken from the
        associated :class:`RuntimeConfig` so that tests and deployments can
        override it via ``nate_oha_executable`` or the corresponding
        environment variable.
        """

        # Align the launch executable with the resolved runtime configuration.
        # Callers may still override ``self.executable`` after construction if
        # needed for advanced scenarios.
        self.executable = self.config.nate_oha_executable


    shutdown_timeout: float = 10.0

    # Internal process supervision state, keyed by ``agent_id``.
    _processes: Dict[str, NateOhaProcessRecord] = field(default_factory=dict, init=False)

    # Live subprocess handles keyed by ``agent_id``. These are used for
    # shutdown and basic health checks and are not exposed outside the
    # adapter.
    _process_handles: Dict[str, subprocess.Popen] = field(default_factory=dict, init=False)

    # Active ACP sessions keyed by ``agent_id``. These records are populated
    # by the async lifecycle helpers once the ACP SDK wiring is in place.
    _sessions: Dict[str, AcpAgentSession] = field(default_factory=dict, init=False)

    # Underlying async context managers that own the ACP stdio transport and
    # subprocess lifetime for each agent. ``start_agent_async`` calls
    # ``__aenter__`` on these and ``stop_agent_async`` is responsible for
    # invoking ``__aexit__``.
    _session_contexts: Dict[str, Any] = field(default_factory=dict, init=False)

    # Cache of per-agent conversation identifiers for this adapter instance.

    # Materialized nate-oha configuration directories keyed by ``agent_id``.
    # These are created on-demand when a launch requires a config file and
    # are cleaned up when the corresponding agent is stopped.
    _temp_config_dirs: Dict[str, str] = field(default_factory=dict, init=False)


    # Cached result of the version/compatibility check (FR-013).

    # ------------------------------------------------------------------
    # Event emission and async streaming helpers
    # ------------------------------------------------------------------

    def _on_session_update(
        self,
        agent_id: str,
        session_id: str,
        update: SessionUpdate,
        received_at: datetime,
    ) -> None:
        """Internal hook for typed ACP ``session/update`` notifications.

        This method is wired into :func:`open_nate_oha_acp_client` via
        :class:`NateNtmAcpProtocolClient` and is responsible for forwarding
        each typed :class:`SessionUpdate` into the owning
        :class:`AcpAgentSession`'s :class:`AcpSessionUpdateStream`.
        """

        session = self._sessions.get(agent_id)
        if session is None:
            # The update refers to an agent that no longer has an active
            # session in this adapter. Log a warning and drop the update.
            logger.warning(
                "acp_session_update_for_unknown_agent",
                extra={"agent_id": agent_id, "session_id": session_id},
            )
            raise AgentSessionNotActive(
                f"Received ACP session update for inactive agent {agent_id!r}"
            )

        # When the session already has a bound conversation identifier,
        # reject updates that refer to a different ACP session. This avoids
        # accidentally publishing stale callbacks from a replaced session
        # into the active stream. During the brief window before a new
        # session ID is recorded we accept all updates for the agent.
        bound_session_id = (session.conversation_id or "").strip()
        if bound_session_id and bound_session_id != session_id:
            logger.warning(
                "acp_session_update_for_stale_session",
                extra={
                    "agent_id": agent_id,
                    "expected_session_id": bound_session_id,
                    "actual_session_id": session_id,
                },
            )
            return

        try:
            session.update_stream.publish(update, received_at=received_at)
        except StreamClosedError:
            # The stream has already been closed, typically because the
            # session is shutting down. Treat this as benign but log at
            # debug level for diagnostics.
            logger.debug(
                "acp_update_after_stream_closed",
                extra={"agent_id": agent_id, "session_id": session_id},
            )
        except Exception as exc:  # pragma: no cover - defensive
            # Any unexpected failure when publishing to the stream is treated
            # as terminal for that stream so that subscribers observe a
            # consistent closure signal.
            session.update_stream.close(exc)
            logger.error(
                "acp_update_stream_publish_error",
                extra={"agent_id": agent_id, "session_id": session_id},
            )
            raise


    def _emit_event(self, event: AgentEvent) -> None:
        """Deliver an AgentEvent to the runtime callback, if configured.

        This hook is used for process-lifecycle notifications and any
        adapter-level telemetry that still flows through the generic
        :class:`AgentEvent` pipeline.
        """

        if self.on_event is not None:
            self.on_event(event)

    @asynccontextmanager
    async def subscribe_acp_updates(self, agent_id: str) -> AsyncIterator[AsyncIterator[ReceivedSessionUpdate]]:
        """Subscribe to the typed ACP update stream for ``agent_id``.

        The returned async iterator yields :class:`ReceivedSessionUpdate`
        objects drawn from the per-session
        :class:`~nate_ntm.runtime.acp_update_stream.AcpSessionUpdateStream`
        owned by the active :class:`AcpAgentSession`.

        This API is the preferred integration surface for ACP telemetry in
        new code paths. It supersedes the legacy :meth:`subscribe_events`
        interface, which remains for compatibility during the migration
        away from :class:`AgentEvent`.
        """

        session = self._sessions.get(agent_id)
        if session is None or session.status not in {"starting", "running"}:
            raise AgentSessionNotActive(
                f"No active ACP session for agent {agent_id!r}"
            )

        stream = session.update_stream
        async with stream.subscribe() as updates:
            yield updates

    async def iter_acp_updates(self, agent_id: str) -> AsyncIterator[ReceivedSessionUpdate]:
        """Yield typed ACP updates for ``agent_id`` as they arrive.

        This is a thin convenience wrapper around
        :meth:`subscribe_acp_updates`. Each call registers an independent
        subscription that receives a replay of retained history followed by
        live updates for the given agent.
        """

        async with self.subscribe_acp_updates(agent_id) as updates:
            async for item in updates:
                yield item

    _version_checked: bool = field(default=False, init=False)
    _detected_version: str | None = field(default=None, init=False)

    # Namespace used to derive deterministic conversation IDs from runtime
    # context (swarm_id + project path + agent_id).

    # ------------------------------------------------------------------
    # BaseAcpClient API
    # ------------------------------------------------------------------

    def start_agent(self, agent_id: str, *, metadata: AgentState) -> None:
        """Launch the nate-oha ACP process backing ``agent_id``.

        This implementation follows the nate-oha process-launch contract at a
        high level:

        * Ensure the nate-oha binary is compatible via :meth:`_check_version`.
        * Spawn a dedicated nate-oha process for the agent.
        * Create/update the in-memory :class:`NateOhaProcessRecord`.
        * Perform a lightweight startup check and transition the record to a
          running or failed state.
        """

        self._check_version()

        # Avoid spawning duplicate processes for the same agent when one is
        # already starting or running. Restart semantics are implemented in
        # later tasks.
        existing = self._processes.get(agent_id)
        if existing is not None and existing.status in {"starting", "running"}:
            return

        cmd = self._build_command(agent_id, metadata)
        env = self._build_env(agent_id, metadata)

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(self.config.project_path),
                env=env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except OSError as exc:  # pragma: no cover - defensive
            message = str(exc)
            record = NateOhaProcessRecord(
                agent_id=agent_id,
                pid=None,
                status="failed",
                last_start_time=datetime.utcnow(),
                last_exit_code=None,
                last_error=message,
                restart_count=existing.restart_count if existing else 0,
            )
            self._processes[agent_id] = record
            # Best-effort cleanup of any materialized nate-oha config created
            # for this launch attempt.
            self._cleanup_temp_config(agent_id)
            raise AcpClientError(
                f"Failed to launch nate-oha process for agent {agent_id!r}: {message}"
            ) from exc

        record = NateOhaProcessRecord(
            agent_id=agent_id,
            pid=proc.pid,
            status="starting",
            last_start_time=datetime.utcnow(),
            last_exit_code=existing.last_exit_code if existing else None,
            last_error=None,
            restart_count=existing.restart_count if existing else 0,
        )
        self._processes[agent_id] = record
        self._process_handles[agent_id] = proc

        # Emit a simple process-started event.
        self._emit_event(
            self._make_process_event(
                agent_id=agent_id,
                event_type="nate_oha_process_started",
                payload={"pid": proc.pid},
            )
        )

        # Minimal readiness check: if the process has already exited, treat
        # startup as failed; otherwise consider it running.
        retcode = proc.poll()
        if retcode is not None:
            record.status = "failed"
            record.last_exit_code = retcode
            record.last_error = f"nate-oha exited during startup with code {retcode}"

            self._emit_event(
                self._make_process_event(
                    agent_id=agent_id,
                    event_type="nate_oha_process_start_failed",
                    payload={"exit_code": retcode},
                )
            )

            raise AcpClientError(
                f"nate-oha process for agent {agent_id!r} exited during startup with code {retcode}"
            )

        record.status = "running"
        self._emit_event(
            self._make_process_event(
                agent_id=agent_id,
                event_type="nate_oha_process_ready",
                payload={"pid": proc.pid},
            )
        )

    # ------------------------------------------------------------------
    # Agent-lifecycle async API (ACP SDK-backed)
    # ------------------------------------------------------------------

    async def start_agent_async(self, agent_id: str, *, metadata: AgentState) -> None:
        """Asynchronously launch or attach to the nate-oha ACP runtime.

        This implementation wires the nate-oha subprocess into the official
        ACP SDK using :func:`open_nate_oha_acp_client` and establishes an ACP
        session for ``agent_id`` via :class:`acp.client.ClientSideConnection`.

        The synchronous :meth:`start_agent` method continues to provide the
        process-supervision contract used by existing tests and callers;
        :meth:`start_agent_async` is the entrypoint for the new ACP SDK-based
        lifecycle and maintains an :class:`AcpAgentSession` record for the
        live connection.
        """

        # Avoid spawning duplicate sessions when one is already starting or
        # running. Restart semantics are implemented in later tasks.
        session = self._sessions.get(agent_id)
        if session is not None and session.status in {"starting", "running"}:
            return

        # Construct the nate-oha command and environment using the existing
        # helpers so that process-launch semantics remain consistent with the
        # synchronous implementation.
        cmd = self._build_command(agent_id, metadata)
        env = self._build_env(agent_id, metadata)

        # ``open_nate_oha_acp_client`` is an async context manager that binds
        # the nate-oha subprocess stdio to an ACP client connection. We call
        # ``__aenter__`` manually here so that the session can outlive this
        # method and be shut down later by :meth:`stop_agent_async`.
        cm = open_nate_oha_acp_client(
            command=cmd,
            env=env,
            cwd=self.config.project_path,
            agent_id=agent_id,
            on_session_update=self._on_session_update,
            capabilities=NATE_NTM_CLIENT_CAPABILITIES,
        )

        try:
            connection, process, protocol_client = await cm.__aenter__()
        except Exception as exc:  # pragma: no cover - defensive
            # Ensure we do not leak a partially entered context on failure.
            with_context = hasattr(cm, "__aexit__")
            if with_context:
                try:
                    await cm.__aexit__(type(exc), exc, exc.__traceback__)
                except Exception:
                    # Suppress secondary errors during cleanup; the original
                    # exception is what we surface to callers.
                    pass
            # Best-effort cleanup of any materialized nate-oha config created
            # for this launch attempt.
            self._cleanup_temp_config(agent_id)
            raise AcpClientError(
                f"Failed to establish ACP connection for agent {agent_id!r}: {exc}"
            ) from exc

        # Record the context so that ``stop_agent_async`` can close the ACP
        # connection and subprocess using the SDK's defensive shutdown
        # semantics.
        self._session_contexts[agent_id] = cm

        # Create a provisional AcpAgentSession record before initializing the
        # ACP connection so that any ``session/update`` notifications observed
        # during initialization or during session (load/new) are published into
        # a live :class:`AcpSessionUpdateStream`.
        provisional_conversation_id = (metadata.conversation_id or "").strip()
        session = AcpAgentSession(
            agent_id=agent_id,
            conversation_id=provisional_conversation_id,
            process=process,
            connection=connection,
            protocol_client=protocol_client,
            status="starting",
            stderr_task=None,
            exit_monitor_task=None,
        )
        self._sessions[agent_id] = session

        # Initialize the ACP connection and negotiate capabilities.
        await connection.initialize(
            protocol_version=PROTOCOL_VERSION,
            client_capabilities=NATE_NTM_CLIENT_CAPABILITIES,
        )

        # Determine whether we are resuming an existing conversation or
        # creating a new one. For nate-oha / ACP, the authoritative
        # conversation identifier is the opaque ``session_id`` returned by
        # the ACP "session/new" operation. When a non-empty
        # ``metadata.conversation_id`` is present we treat it as a previously
        # persisted ACP session identifier and attach via ``load_session``;
        # otherwise we create a fresh session and persist the returned
        # ``session_id`` back into :class:`AgentState` so it can be reused
        # on ``--resume`` and in subsequent launches.
        conversation_id = provisional_conversation_id
        if conversation_id:
            # Attach to an existing, persisted ACP session.
            await connection.load_session(
                cwd=str(self.config.project_path),
                session_id=conversation_id,
            )
        else:
            # Create a fresh ACP session for this agent and treat the returned
            # ``session_id`` as the conversation identifier for runtime
            # purposes. The new identifier is persisted into the metadata
            # store and cached for future calls.
            new_session = await connection.new_session(
                cwd=str(self.config.project_path),
            )
            conversation_id = new_session.session_id

            # Persist the ACP-assigned session identifier into per-agent
            # state so that later runs (including ``--resume``) can reuse
            # it. We deliberately perform a best-effort update here: if the
            # agent record does not yet exist we seed it from the in-memory
            # :class:`AgentState` instance supplied by the caller.
            store = MetadataStore(config=self.config)
            try:
                existing_state = store.load_agent_state(agent_id)
            except FileNotFoundError:
                existing_state = metadata

            updated_state = existing_state.model_copy(update={"conversation_id": conversation_id})
            store.save_agent_state(updated_state)

        # Update the in-memory session record now that we know the
        # authoritative conversation identifier and mark it as running.
        session.conversation_id = conversation_id
        session.status = "running"

    async def stop_agent_async(self, agent_id: str, *, timeout: float) -> None:
        """Asynchronously request a graceful stop for the ACP runtime.

        When an ACP-backed session has been established via
        :meth:`start_agent_async`, this method closes the ACP connection and
        underlying subprocess using the context manager returned by
        :func:`open_nate_oha_acp_client`. If no such session exists, it
        falls back to the synchronous :meth:`stop_agent` implementation so
        existing call sites and tests continue to behave as before.
        """

        ctx = self._session_contexts.pop(agent_id, None)
        session = self._sessions.pop(agent_id, None)

        if ctx is None or session is None:
            # No async session is active; delegate to the synchronous
            # process-supervision implementation.
            self.stop_agent(agent_id, timeout=timeout)
            return

        error: BaseException | None = None
        try:
            # The context manager owns both the ACP connection and the
            # subprocess shutdown semantics.
            await ctx.__aexit__(None, None, None)
        except Exception as exc:  # pragma: no cover - defensive
            error = exc
            raise AcpClientError(
                f"Failed to stop ACP session for agent {agent_id!r}: {exc}"
            ) from exc
        finally:
            # Ensure the per-session typed update stream is closed so that
            # callers observe a consistent terminal state for this session.
            try:
                session.update_stream.close(error)
            except Exception:
                # Stream closure must not mask shutdown errors; log and
                # continue with cleanup.
                logger.debug(
                    "acp_update_stream_close_error",
                    extra={"agent_id": agent_id},
                )

            # Regardless of whether shutdown succeeds or fails, clean up any
            # temporary nate-oha configuration created for the agent.
            self._cleanup_temp_config(agent_id)

        # Mark the in-memory session as terminated so any future status
        # queries can distinguish between running and stopped agents.
        session.status = "terminated"
        self._sessions[agent_id] = session

    async def prompt(self, agent_id: str, prompt: str | None = None) -> str | None:
        """Asynchronously send a user prompt into the active ACP session.

        This assumes :meth:`start_agent_async` has already established an ACP
        session for ``agent_id``. The prompt is delivered via
        :class:`acp.client.ClientSideConnection.prompt`, and any resulting ACP
        session updates are translated into :class:`AgentEvent` instances by
        the associated :class:`NateNtmAcpProtocolClient`.

        The return value is currently ``None``; callers should observe
        adapter-level behavior through the emitted events rather than
        relying on a concrete turn identifier.
        """

        session = self._sessions.get(agent_id)
        if session is None or session.status not in {"starting", "running"}:
            raise AcpClientError(
                f"prompt: no active ACP session for agent {agent_id!r}; "
                "call start_agent_async(...) first"
            )

        connection = session.connection
        session_id = session.conversation_id

        text = "" if prompt is None else prompt

        # Build a single text content block for the prompt.
        block = TextContentBlock(type="text", text=text)

        # Delegate to the ACP SDK. The NateNtmAcpProtocolClient associated
        # with this connection will receive any resulting session updates and
        # emit AgentEvent instances via the configured event sink.
        await connection.prompt(session_id, [block])

        # There is no stable "turn ID" exposed by the ACP prompt API today,
        # so we return None. If a useful identifier becomes available in
        # PromptResponse in the future, this method can be updated to surface
        # it without changing the async signature.
        return None

    def start_turn(self, agent_id: str, prompt: str | None = None) -> str:  # pragma: no cover - placeholder
        """Start a new ACP turn for ``agent_id``.

        Turn semantics for nate-oha-backed agents are implemented in
        follow-up tasks; this placeholder exists so that tests can be written
        against the intended interface.
        """

        raise NotImplementedError("NateOhaAcpClient.start_turn is not implemented yet")

    def stop_agent(self, agent_id: str, *, timeout: float) -> None:
        """Request a graceful stop for the nate-oha process backing ``agent_id``.

        The method attempts a graceful termination first (``SIGTERM`` via
        :meth:`subprocess.Popen.terminate`) and escalates to a forced kill if
        the process does not exit within ``timeout`` seconds. Adapter-level
        status and process records are updated accordingly.
        """

        record = self._processes.get(agent_id)
        proc = self._process_handles.get(agent_id)

        # If we have no subprocess handle, treat this as a no-op but ensure the
        # status reflects a non-running agent for subsequent calls.
        if record is None or proc is None or record.pid is None:
            if record is None:
                self._processes[agent_id] = NateOhaProcessRecord(
                    agent_id=agent_id,
                    pid=None,
                    status="terminated",
                    last_start_time=None,
                    last_exit_code=None,
                    last_error=None,
                    restart_count=0,
                )
            else:
                record.status = "terminated"

            # Clean up any temporary nate-oha configuration for the agent.
            self._cleanup_temp_config(agent_id)
            return

        # If the process has already exited, just normalize the status.
        retcode = proc.poll()
        if retcode is not None:
            record.last_exit_code = retcode
            record.status = "terminated" if retcode == 0 else "failed"
            if retcode != 0 and not record.last_error:
                record.last_error = f"nate-oha exited with code {retcode}"

            self._process_handles.pop(agent_id, None)

            event_type = (
                "nate_oha_process_exited" if retcode == 0 else "nate_oha_process_crashed"
            )
            self._emit_event(
                self._make_process_event(
                    agent_id=agent_id,
                    event_type=event_type,
                    payload={"exit_code": retcode},
                )
            )
            self._cleanup_temp_config(agent_id)
            return

        record.status = "stopping"
        try:
            proc.terminate()
            try:
                retcode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                retcode = proc.wait(timeout=timeout)
        except OSError as exc:  # pragma: no cover - defensive
            record.status = "failed"
            record.last_error = f"Failed to stop nate-oha process: {exc}"
            self._emit_event(
                self._make_process_event(
                    agent_id=agent_id,
                    event_type="nate_oha_process_stop_failed",
                    payload={"error": str(exc)},
                )
            )
            self._cleanup_temp_config(agent_id)
            raise AcpClientError(
                f"Failed to stop nate-oha process for agent {agent_id!r}: {exc}"
            ) from exc

        record.last_exit_code = retcode
        record.status = "terminated" if retcode == 0 else "failed"
        if retcode != 0 and not record.last_error:
            record.last_error = f"nate-oha exited with code {retcode}"

        self._process_handles.pop(agent_id, None)

        event_type = "nate_oha_process_exited" if retcode == 0 else "nate_oha_process_crashed"
        self._emit_event(
            self._make_process_event(
                agent_id=agent_id,
                event_type=event_type,
                payload={"exit_code": retcode},
            )
        )
        self._cleanup_temp_config(agent_id)

    def get_status(self, agent_id: str) -> AcpAgentStatus:
        """Return a lightweight status snapshot for ``agent_id``.

        When no nate-oha process has been started for ``agent_id``, the
        adapter reports an ``"idle"`` state. Otherwise the status is derived
        from the corresponding :class:`NateOhaProcessRecord`.
        """

        record = self._processes.get(agent_id)
        if record is None:
            return AcpAgentStatus(
                agent_id=agent_id,
                state="idle",
                last_exit_code=None,
                last_error=None,
                restart_count=0,
            )

        return AcpAgentStatus(
            agent_id=agent_id,
            state=record.status,
            last_exit_code=record.last_exit_code,
            last_error=record.last_error,
            restart_count=record.restart_count,
        )

    def _build_command(self, agent_id: str, metadata: AgentState) -> list[str]:
        """Construct the nate-oha ``acp`` command line for an agent.

        This helper always launches nate-oha from the persisted effective
        :class:`NateOhaConfig` attached to :class:`AgentState`. The
        configuration is materialised into a temporary JSON file via
        :func:`materialize_nate_oha_config` and passed to the CLI via
        ``--config``. When :attr:`AgentState.conversation_id` is non-empty,
        the same value is forwarded via ``--resume`` so that ACP can resume the
        existing session.

        If ``metadata.nate_oha_config`` is not set, an :class:`AcpClientError`
        is raised; callers must ensure that an effective nate-oha
        configuration has been derived and persisted for each agent before
        launch.
        """

        cfg = getattr(metadata, "nate_oha_config", None)
        conversation_id = metadata.conversation_id or None

        # Preferred path: launch from the persisted effective nate-oha config.
        if cfg is None:
            raise AcpClientError(
                "NateOhaAcpClient._build_command requires metadata.nate_oha_config to be set; "
                f"no persisted nate-oha configuration found for agent {agent_id!r}."
            )

        config_path = materialize_nate_oha_config(config=cfg)
        # Track the temporary directory so it can be cleaned up when the
        # agent is stopped.
        self._temp_config_dirs[agent_id] = str(config_path.parent)

        argv: list[str] = [self.executable, "acp", "--config", str(config_path)]
        if conversation_id:
            argv.extend(["--resume", conversation_id])
        return argv

    def _build_env(self, agent_id: str, metadata: AgentState) -> Dict[str, str]:
        """Return the environment used to launch nate-oha.

        The base environment is inherited from the current process with a
        small set of nate_ntm-specific variables added for correlation.

        Milestone 2 intentionally **removes** any translation of
        NateOhaConfig Agent Mail settings into ``AGENT_MAIL_*`` environment
        variables. All Agent Mail configuration must be provided via the
        JSON configuration file materialised from :class:`NateOhaConfig`;
        this helper is limited to non-secret correlation identifiers such
        as project, swarm, agent, and conversation identifiers plus a
        default model hint.
        """

        # Start from the current process environment and treat it purely as
        # a base for non-secret settings and unrelated variables.
        env: Dict[str, str] = dict(os.environ)

        # Runtime correlation variables used by nate_ntm and downstream
        # tooling. These are non-secret and safe to set by default.
        env.setdefault("NATE_NTM_PROJECT_PATH", str(self.config.project_path))
        env.setdefault("NATE_NTM_SWARM_ID", self.config.swarm_id)
        env.setdefault("NATE_NTM_AGENT_ID", agent_id)

        # Default model selection for nate-oha. The child environment must
        # always have an explicit LLM model configured so that we do not
        # rely on nate-oha's internal default. Callers who need a different
        # model may override this via the parent environment before
        # constructing the runtime configuration.
        env.setdefault("LLM_MODEL", "openai/gpt-4o")

        # Correlate subprocess-level logs and diagnostics with the ACP
        # session by forwarding the persisted conversation identifier when
        # present. The identifier itself is treated as opaque and must not
        # be modified here.
        if metadata.conversation_id:
            env.setdefault("NATE_NTM_AGENT_CONVERSATION_ID", metadata.conversation_id)

        return env

    def _cleanup_temp_config(self, agent_id: str) -> None:
        """Best-effort removal of any materialized nate-oha config.

        Temporary configuration directories are created when launching agents
        from a persisted :class:`NateOhaConfig` (see
        :func:`materialize_nate_oha_config`). They are strictly runtime
        artifacts and must never be treated as durable project metadata.
        """

        tmpdir = self._temp_config_dirs.pop(agent_id, None)
        if not tmpdir:
            return

        try:
            shutil.rmtree(tmpdir, ignore_errors=True)
        except Exception:  # pragma: no cover - defensive
            # Failure to clean up a temporary directory should not fail the
            # overall agent shutdown path, but we log a warning for
            # observability.
            logger.warning(
                "nate_oha_temp_config_cleanup_failed",
                extra={"agent_id": agent_id, "path": tmpdir},
            )


    def _make_process_event(
        self,
        *,
        agent_id: str,
        event_type: str,
        payload: Mapping[str, Any],
    ) -> AgentEvent:
        """Construct a process-lifecycle :class:`AgentEvent` for callbacks."""

        return AgentEvent(
            event_id=f"{agent_id}:{event_type}:{uuid.uuid4()}",
            timestamp=datetime.utcnow(),
            agent_id=agent_id,
            source=AgentEventSource.ACP,
            type=event_type,
            payload=payload,
        )

    # ------------------------------------------------------------------
    # Version and compatibility checks (FR-013, T211)
    # ------------------------------------------------------------------

    def _check_version(self) -> None:
        """Verify that the installed ``nate-oha`` meets minimum requirements.

        This helper runs a lightweight self-check command (by default
        ``nate-oha --help``) and parses its output to ensure that a
        supported version of nate-oha is installed. If the check fails or an
        incompatible version is detected, :class:`AcpClientError` is raised
        with a clear diagnostic.

        The use of ``--help`` rather than a dedicated ``--version`` flag
        matches the current nate-oha CLI, which prints its version in a
        banner line (for example ``OpenHands SDK v1.28.1``) and exits
        successfully.
        """

        if self._version_checked:
            return

        cmd = [self.executable, "--help"]
        try:
            proc = subprocess.run(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
        except OSError as exc:  # pragma: no cover - defensive
            raise AcpClientError(
                f"nate-oha executable {self.executable!r} not found or not executable"
            ) from exc

        if proc.returncode != 0:
            message = (proc.stderr or proc.stdout or "").strip()
            if not message:
                message = f"exit code {proc.returncode}"
            raise AcpClientError(
                f"nate-oha version check failed (command: {' '.join(cmd)}): {message}"
            )

        output = (proc.stdout or proc.stderr or "").strip()
        if not output:
            raise AcpClientError(
                f"nate-oha version check produced no output (command: {' '.join(cmd)})"
            )

        version_tuple = self._parse_semver(output)

        # Enforce a minimum version if configured via environment. This keeps
        # the runtime flexible while still satisfying FR-013.
        min_version_str = os.environ.get("NATE_OHA_MIN_VERSION", "").strip()

        if version_tuple is None:
            # If nate-oha does not report a semantic version but exits
            # successfully, treat the version as "unknown" unless a minimum
            # version has been explicitly configured. This allows environments
            # that suppress the banner (for example via
            # ``OPENHANDS_SUPPRESS_BANNER=1``) to pass the compatibility check
            # while still enforcing strict versioning when
            # ``NATE_OHA_MIN_VERSION`` is set.
            if min_version_str:
                raise AcpClientError(
                    "nate-oha version check did not report a semantic version "
                    f"and NATE_OHA_MIN_VERSION={min_version_str!r} is set; cannot "
                    f"verify compatibility (output was: {output!r})."
                )

            self._version_checked = True
            self._detected_version = None
            return

        if min_version_str:
            min_tuple = self._parse_semver(min_version_str)
            if min_tuple is None:
                raise AcpClientError(
                    "Invalid NATE_OHA_MIN_VERSION value "
                    f"{min_version_str!r}; expected a semantic version such as '0.5.0'."
                )

            if self._compare_versions(version_tuple, min_tuple) < 0:
                current_str = ".".join(str(p) for p in version_tuple)
                raise AcpClientError(
                    "Installed nate-oha version "
                    f"{current_str} is below the minimum required version {min_version_str}."
                )

        # Record that we've successfully validated the version.
        self._version_checked = True
        self._detected_version = ".".join(str(p) for p in version_tuple)

    @staticmethod
    def _parse_semver(text: str) -> tuple[int, int, int] | None:
        """Extract the first ``MAJOR.MINOR.PATCH`` version from ``text``.

        Returns a tuple of integers on success or ``None`` if no semantic
        version can be found.
        """

        match = re.search(r"(\d+)\.(\d+)\.(\d+)", text)
        if not match:
            return None

        return int(match.group(1)), int(match.group(2)), int(match.group(3))

    @staticmethod
    def _compare_versions(a: tuple[int, int, int], b: tuple[int, int, int]) -> int:
        """Return -1, 0, or 1 depending on version ordering."""

        return (a > b) - (a < b)

