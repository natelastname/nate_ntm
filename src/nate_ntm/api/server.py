"""Runtime control API server skeleton.

For Phase 2 (T011), this module defines the small in-process surface
that the runtime daemon and control API share. The unified FastAPI app
in :mod:`nate_ntm.api.runtime_api` exposes this surface over HTTP
JSON-RPC (``POST /jsonrpc``) plus an ``/events`` WebSocket endpoint,
but :class:`RuntimeApiServer` itself is intentionally transport-agnostic.

A minimal :class:`RuntimeApiServer` class is provided with concrete
handlers and an in-memory subscription registry that can be exercised
directly in unit and CLI tests without binding to a specific ASGI
implementation.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, Protocol

from ..runtime.daemon import RuntimeDaemon
from ..runtime.events import AgentEvent
from ..runtime.state import RuntimeStatus

__all__ = ["RuntimeApiServer", "SupportsRuntimeDaemon"]


class SupportsRuntimeDaemon(Protocol):
    """Protocol capturing the subset of RuntimeDaemon used by the API.

    This keeps the server layer decoupled from the concrete daemon
    implementation while still enabling type checking.
    """

    @property
    def daemon(self) -> RuntimeDaemon:  # pragma: no cover - structural only
        ...


@dataclass(slots=True)
class RuntimeApiServer:
    """In-process implementation of the runtime control API (T011).

    This class owns the JSON-RPC method handlers used by the control API:

    * Inspect and control the runtime (``runtime.get_status``,
      ``runtime.shutdown``).
    * Inspect swarm and agents (``swarm.get_overview``,
      ``agent.get_detail``).
    * Manage an in-memory subscription registry for event streaming
      (``events.subscribe``, ``events.unsubscribe``, ``events.notify``).

    Transport concerns (HTTP, WebSocket, ASGI lifecycle) live in
    :mod:`nate_ntm.api.runtime_api`; this class intentionally remains a
    thin, synchronous wrapper around :class:`RuntimeDaemon`.
    """

    daemon: RuntimeDaemon

    # Internal subscription registry for ``events.subscribe``/``events.unsubscribe``.
    _subscriptions: Dict[str, Dict[str, Any]] = field(
        default_factory=dict, init=False, repr=False
    )
    _next_subscription_id: int = field(default=1, init=False, repr=False)

    def start(self) -> None:
        """Start accepting API connections (stub).

        The actual implementation will be async and will integrate with
        the runtime event loop.
        """

        # Stub: nothing to do yet.
        return

    def stop(self) -> None:
        """Stop the API server and release any resources (stub)."""

        # Stub: nothing to do yet.
        return

    # Handlers -----------------------------------------------------------

    def get_runtime_status(self) -> Dict[str, Any]:
        """Return high-level runtime status for ``runtime.get_status``.

        For the MVP this is a thin wrapper over the
        :class:`RuntimeDaemon` introspection APIs. The FastAPI layer in
        :mod:`nate_ntm.api.runtime_api` exposes this method via JSON-RPC.
        """

        return self.daemon.get_runtime_status()

    def get_swarm_overview(self) -> Dict[str, Any]:
        """Return swarm overview data for ``swarm.get_overview``.

        This mirrors the result shape defined in
        ``contracts/runtime-api.md`` by delegating to the
        :class:`RuntimeDaemon`.
        """

        return self.daemon.get_swarm_status()

    def shutdown_runtime(self, timeout_seconds: int = 30) -> Dict[str, Any]:
        """Request a graceful runtime shutdown for ``runtime.shutdown``.

        This mirrors the high-level contract in
        ``specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md`` by
        delegating to :meth:`RuntimeDaemon.request_shutdown` and returning a
        small acknowledgement payload.

        In the JSON-RPC layer this result is returned as the ``result``
        payload for a ``runtime.shutdown`` call.
        """

        if self.daemon.state.status is not RuntimeStatus.RUNNING:
            raise RuntimeError(
                f"Cannot shutdown runtime from status "
                f"{self.daemon.state.status.value!r}"
            )

        self.daemon.request_shutdown()

        return {
            "accepted": True,
            "status": self.daemon.state.status.value,
        }

    def subscribe_events(
        self,
        *,
        agent_ids: list[str] | None = None,
        include_runtime: bool = True,
    ) -> Dict[str, Any]:
        """Register an event subscription for ``events.subscribe``.

        This is a minimal, in-memory subscription registry suitable for the
        MVP. The FastAPI control API calls this method when handling
        ``events.subscribe`` requests and maps the returned
        ``subscription_id`` onto specific ``/events`` WebSocket clients.
        """

        if agent_ids is None:
            agent_ids = []

        subscription_id = f"sub-{self._next_subscription_id:03d}"
        self._next_subscription_id += 1

        # Store a small descriptor for future routing; concrete notification
        # delivery is handled by the ``/events`` WebSocket endpoint.
        self._subscriptions[subscription_id] = {
            "agent_ids": tuple(agent_ids),
            "include_runtime": bool(include_runtime),
        }

        return {"subscription_id": subscription_id}

    def unsubscribe_events(self, subscription_id: str) -> Dict[str, Any]:
        """Terminate a subscription for ``events.unsubscribe``.

        This is intentionally idempotent: attempting to unsubscribe an
        unknown ``subscription_id`` still returns ``{\"unsubscribed\": true}``.
        """

        self._subscriptions.pop(subscription_id, None)
        return {"unsubscribed": True}

    # ------------------------------------------------------------------
    # Event routing helpers
    # ------------------------------------------------------------------

    def build_agent_event_notifications(self, event: AgentEvent) -> Dict[str, Any]:
        """Build notification payloads for an :class:`AgentEvent`.

        This is a small, in-process helper that applies the current
        subscription filters to a single agent-scoped event and returns a
        JSON-serializable structure mirroring the ``events.notify``
        contract:

        .. code-block:: json

            {
              "notifications": [
                {
                  "subscription_id": "sub-001",
                  "event": { ... AgentEvent.to_dict() ... }
                },
                ...
              ]
            }

        The FastAPI/WebSocket layer in :mod:`nate_ntm.api.runtime_api` uses
        this payload to fan out notifications to connected clients.
        """

        event_payload = event.to_dict()
        notifications: list[Dict[str, Any]] = []

        for subscription_id, desc in self._subscriptions.items():
            agent_ids = desc.get("agent_ids") or ()

            # Empty ``agent_ids`` means "all agents".
            if agent_ids and event.agent_id not in agent_ids:
                continue

            notifications.append(
                {
                    "subscription_id": subscription_id,
                    "event": event_payload,
                }
            )

        return {"notifications": notifications}



    def get_agent_detail(self, agent_id: str, max_events: int = 100) -> Dict[str, Any]:
        """Return detailed information for a single agent.

        This corresponds to the ``agent.get_detail`` method in
        ``contracts/runtime-api.md`` and delegates to the
        :class:`RuntimeDaemon` for its implementation.
        """

        return self.daemon.get_agent_detail(agent_id=agent_id, max_events=max_events)
