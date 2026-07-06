from __future__ import annotations

"""Unified FastAPI runtime control API.

This module provides the *primary* ASGI application for the nate_ntm
runtime control API. It exposes two transport surfaces on a single
FastAPI/uvicorn app:

* ``POST /jsonrpc`` – HTTP JSON-RPC 2.0 endpoint implemented via
  :mod:`fastapi_jsonrpc`. This is the main command surface for
  ``runtime.*``, ``swarm.*``, ``agent.*``, and ``events.*`` methods.
* ``WS /events`` – WebSocket endpoint used to stream runtime and agent
  events to subscribed clients.

The application is bound to an in-process :class:`RuntimeApiServer`
instance, which in turn delegates to :class:`RuntimeDaemon` for its
behaviour.

The HTTP JSON-RPC layer lets ``fastapi-jsonrpc`` own request parsing,
routing, validation, and error shaping wherever practical, while the
WebSocket layer focuses on delivering JSON-RPC-style ``events.notify``
notifications to clients that have registered subscriptions via the
``events.subscribe`` method.

Typical usage::

    from nate_ntm.api.runtime_api import create_runtime_api_app
    from nate_ntm.api.server import RuntimeApiServer
    from nate_ntm.runtime.daemon import RuntimeDaemon

    daemon = RuntimeDaemon.resume(config)
    api_server = RuntimeApiServer(daemon=daemon)
    app = create_runtime_api_app(api_server)

The resulting ``app`` can be served under uvicorn and is the same
application used by the CLI/runtime runner helpers.
"""

import json
import logging
from typing import Any, Dict, Iterable, List, Mapping, Optional, Set

from fastapi import WebSocket, WebSocketDisconnect
from fastapi_jsonrpc import API, BaseError, Entrypoint

from .jsonrpc import JSONRPC_VERSION, build_events_notify_messages
from .server import RuntimeApiServer
from ..runtime.events import AgentEvent

__all__ = ["create_runtime_api_app", "AgentNotFoundError", "RuntimeStateConflictError"]

logger = logging.getLogger(__name__)


class AgentNotFoundError(BaseError):
    """JSON-RPC error raised when an ``agent_id`` is unknown.

    This mirrors the ``code=1001`` error used by the legacy
    :func:`nate_ntm.api.jsonrpc.dispatch_request` implementation for
    ``agent.get_detail``.
    """

    CODE = 1001
    MESSAGE = "Agent not found"


class RuntimeStateConflictError(BaseError):
    """JSON-RPC error used for runtime state conflicts.

    This corresponds to the ``1100`` error range in the runtime API
    contract and is typically raised when a shutdown is requested from
    an invalid state.
    """

    CODE = 1100
    MESSAGE = "Runtime state conflict"


def _create_entrypoint(api_server: RuntimeApiServer, path: str = "/jsonrpc") -> Entrypoint:
    """Create a :class:`fastapi_jsonrpc.Entrypoint` bound to ``api_server``.

    The entrypoint exposes the MVP JSON-RPC surface described in
    ``specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md``:

    * ``runtime.get_status``
    * ``runtime.shutdown``
    * ``swarm.get_overview``
    * ``agent.get_detail``
    * ``events.subscribe``
    * ``events.unsubscribe``
    """

    ep = Entrypoint(path)

    @ep.method(name="runtime.get_status")
    def runtime_get_status() -> Dict[str, Any]:
        return api_server.get_runtime_status()

    @ep.method(name="swarm.get_overview")
    def swarm_get_overview() -> Dict[str, Any]:
        return api_server.get_swarm_overview()

    @ep.method(name="runtime.shutdown", errors=[RuntimeStateConflictError])
    def runtime_shutdown(timeout_seconds: int = 30) -> Dict[str, Any]:
        try:
            return api_server.shutdown_runtime(timeout_seconds=timeout_seconds)
        except RuntimeError as exc:
            # Map daemon state conflicts onto a structured JSON-RPC error.
            raise RuntimeStateConflictError({"detail": str(exc)})

    @ep.method(name="agent.get_detail", errors=[AgentNotFoundError])
    def agent_get_detail(
        agent_id: str,
        max_events: int = 100,
    ) -> Dict[str, Any]:
        try:
            return api_server.get_agent_detail(agent_id=agent_id, max_events=max_events)
        except KeyError:
            raise AgentNotFoundError({"agent_id": agent_id})

    @ep.method(name="events.subscribe")
    def events_subscribe(
        agent_ids: Optional[List[str]] = None,
        include_runtime: bool = True,
    ) -> Dict[str, Any]:
        return api_server.subscribe_events(
            agent_ids=agent_ids,
            include_runtime=include_runtime,
        )

    @ep.method(name="events.unsubscribe")
    def events_unsubscribe(subscription_id: str) -> Dict[str, Any]:
        return api_server.unsubscribe_events(subscription_id)

    return ep


def create_runtime_api_app(api_server: RuntimeApiServer) -> API:
    """Create the unified FastAPI/JSON-RPC runtime control application.

    The returned :class:`fastapi_jsonrpc.API` instance is a fully
    configured ASGI app that exposes:

    * ``POST /jsonrpc`` for HTTP JSON-RPC commands.
    * ``WS   /events`` for streaming ``events.notify`` notifications.

    Event subscriptions are registered via the ``events.subscribe``
    method on the HTTP JSON-RPC surface. WebSocket clients then attach
    to one or more ``subscription_id`` values by sending a small JSON
    handshake message after connecting::

        {"subscription_id": "sub-001"}

    or::

        {"subscription_ids": ["sub-001", "sub-002"]}

    Incoming events are bridged from :class:`RuntimeApiServer` via
    :func:`nate_ntm.api.jsonrpc.build_events_notify_messages` and
    delivered as JSON-RPC 2.0 notification envelopes on the
    ``/events`` WebSocket.
    """

    app = API(title="nate_ntm runtime control API")

    # ------------------------------------------------------------------
    # HTTP JSON-RPC entrypoint
    # ------------------------------------------------------------------
    entrypoint = _create_entrypoint(api_server, path="/jsonrpc")
    app.bind_entrypoint(entrypoint)

    # ------------------------------------------------------------------
    # WebSocket event streaming state
    # ------------------------------------------------------------------
    # Mapping from subscription_id -> set[WebSocket] and the inverse;
    # these are used purely for transport-level routing of
    # ``events.notify`` notifications. The underlying subscription
    # registry and filtering logic remain owned by RuntimeApiServer.
    app.state.subscription_clients = {}  # type: Dict[str, Set[WebSocket]]
    app.state.client_subscriptions = {}  # type: Dict[WebSocket, Set[str]]

    async def _attach_subscriptions(websocket: WebSocket, subscription_ids: Iterable[str]) -> None:
        """Record that ``websocket`` is interested in the given IDs."""

        client_map: Dict[WebSocket, Set[str]] = app.state.client_subscriptions
        subscription_map: Dict[str, Set[WebSocket]] = app.state.subscription_clients

        client_set = client_map.setdefault(websocket, set())
        for sub_id in subscription_ids:
            sub_id_str = str(sub_id)
            client_set.add(sub_id_str)
            subscription_map.setdefault(sub_id_str, set()).add(websocket)

    async def _detach_client(websocket: WebSocket) -> None:
        """Remove all subscription bindings for ``websocket``."""

        client_map: Dict[WebSocket, Set[str]] = app.state.client_subscriptions
        subscription_map: Dict[str, Set[WebSocket]] = app.state.subscription_clients

        subs = client_map.pop(websocket, set())
        for sub_id in subs:
            clients = subscription_map.get(sub_id)
            if not clients:
                continue
            clients.discard(websocket)
            if not clients:
                subscription_map.pop(sub_id, None)

    # ------------------------------------------------------------------
    # WebSocket /events endpoint
    # ------------------------------------------------------------------
    @app.websocket("/events")
    async def events_websocket(websocket: WebSocket) -> None:  # pragma: no cover - behaviour tested via integration tests
        """Attach a WebSocket client to one or more subscriptions.

        The protocol is intentionally small:

        * Client connects to ``/events``.
        * Client sends a single JSON object indicating which
          ``subscription_id``\(s) it is interested in.
        * Server records the mapping and then treats the connection as
          receive-only, delivering JSON-RPC ``events.notify``
          notifications whenever matching events occur.

        Example client handshake payloads::

            {"subscription_id": "sub-001"}

            {"subscription_ids": ["sub-001", "sub-002"]}
        """

        await websocket.accept()

        try:
            # Expect an initial handshake frame describing the desired
            # subscription(s).
            try:
                raw = await websocket.receive_text()
            except WebSocketDisconnect:
                return

            try:
                payload = json.loads(raw)
            except json.JSONDecodeError:
                # Invalid handshake; close with a generic protocol error.
                await websocket.close(code=1003)
                return

            sub_ids: List[str] = []
            if isinstance(payload.get("subscription_id"), str):
                sub_ids = [payload["subscription_id"]]
            elif isinstance(payload.get("subscription_ids"), list):
                raw_ids = payload["subscription_ids"]
                sub_ids = [str(v) for v in raw_ids if isinstance(v, (str, int))]

            if not sub_ids:
                # No usable subscription identifiers provided.
                await websocket.close(code=1008)
                return

            await _attach_subscriptions(websocket, sub_ids)

            # Keep the connection open until the client disconnects.
            while True:
                try:
                    # We currently ignore any subsequent client frames
                    # and treat this as a server-initiated stream.
                    await websocket.receive_text()
                except WebSocketDisconnect:
                    break
        finally:
            await _detach_client(websocket)

    # ------------------------------------------------------------------
    # Event publication helper
    # ------------------------------------------------------------------
    async def publish_event(event: AgentEvent) -> None:
        """Publish a single :class:`AgentEvent` to attached clients.

        This helper is attached to ``app.state.publish_event`` so that
        the runtime runner can bridge events from the scheduler into the
        WebSocket transport without being aware of the underlying
        routing details.
        """

        if not isinstance(event, AgentEvent):  # Defensive type check.
            raise TypeError("publish_event expects an AgentEvent instance")

        logger.debug(
            "publish_event_called",
            extra={
                "agent_id": event.agent_id,
                "event_type": event.type,
            },
        )

        messages = build_events_notify_messages(api_server, event)
        logger.debug(
            "publish_event_notifications_built",
            extra={"notification_count": len(messages)},
        )

        subscription_map: Dict[str, Set[WebSocket]] = app.state.subscription_clients
        client_map: Dict[WebSocket, Set[str]] = app.state.client_subscriptions

        for msg in messages:
            params = msg.get("params") or {}
            sub_id = params.get("subscription_id")
            if not isinstance(sub_id, str):
                continue

            clients = subscription_map.get(sub_id)
            if not clients:
                continue

            text = json.dumps({"jsonrpc": JSONRPC_VERSION, **msg}) if "jsonrpc" not in msg else json.dumps(msg)

            disconnected: List[WebSocket] = []
            for ws in list(clients):
                try:
                    await ws.send_text(text)
                except RuntimeError:
                    # Connection might already be closed; mark for
                    # cleanup and continue.
                    disconnected.append(ws)

            # Clean up any connections that failed during send.
            for ws in disconnected:
                clients.discard(ws)
                subs = client_map.get(ws)
                if subs is not None:
                    subs.discard(sub_id)
                    if not subs:
                        client_map.pop(ws, None)

        # Remove empty subscription buckets.
        empty_ids = [sid for sid, clients in subscription_map.items() if not clients]
        for sid in empty_ids:
            subscription_map.pop(sid, None)

    app.state.publish_event = publish_event

    return app
