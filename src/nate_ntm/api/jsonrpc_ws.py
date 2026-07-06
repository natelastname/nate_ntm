"""Legacy WebSocket JSON-RPC server for the runtime control API.

This module provides a small asyncio-based WebSocket server that exposes
:class:`RuntimeApiServer` over a localhost-only JSON-RPC 2.0 interface,
matching the contract in
``specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md``.

The unified FastAPI/uvicorn control surface implemented in
:mod:`nate_ntm.api.runtime_api` is the primary entrypoint used by the
runtime runner and CLI. This module is retained as a lower-level,
transport-specific alternative for tests and specialised tooling that
still expect a pure WebSocket JSON-RPC endpoint.

Key responsibilities:

* Accept incoming WebSocket connections from local clients.
* Receive JSON-RPC request objects and dispatch them via
  :func:`nate_ntm.api.jsonrpc.dispatch_request`.
* Send JSON-RPC responses back to the client.
* Track ``events.subscribe`` / ``events.unsubscribe`` requests so that
  live :class:`~nate_ntm.runtime.events.AgentEvent` instances can be
  fanned out as ``events.notify`` notifications using
  :func:`nate_ntm.api.jsonrpc.build_events_notify_messages`.

Transport and lifetime management are deliberately minimal; callers that
instantiate :class:`JsonRpcWebSocketServer` are expected to own this
server instance and its event loop.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field
from typing import Any, Dict, Mapping

import socket

import uvicorn
from fastapi import WebSocket, WebSocketDisconnect
from fastapi_jsonrpc import API

from .jsonrpc import JSONRPC_VERSION, build_events_notify_messages, dispatch_request
from .server import RuntimeApiServer

__all__ = ["JsonRpcWebSocketServer"]


def _make_error_response(
    *, code: int, message: str, response_id: Any | None = None, data: Dict[str, Any] | None = None
) -> Dict[str, Any]:
    """Build a JSON-RPC error envelope.

    This mirrors the structure used by :mod:`nate_ntm.api.jsonrpc` for
    consistency but is kept local to avoid exposing additional helpers
    from that module.
    """

    error: Dict[str, Any] = {"code": code, "message": message}
    if data:
        error["data"] = data

    return {"jsonrpc": JSONRPC_VERSION, "error": error, "id": response_id}


@dataclass(slots=True)
class JsonRpcWebSocketServer:
    """Async WebSocket JSON-RPC server bound to a :class:`RuntimeApiServer`.

    Parameters
    ----------
    api_server:
        The in-process :class:`RuntimeApiServer` instance that implements
        the control API handlers.

    host, port:
        Bind address for the WebSocket server. The MVP assumes a
        localhost-only binding; passing ``port=0`` allows the OS to pick
        an ephemeral port (useful in tests).
    """

    api_server: RuntimeApiServer
    host: str = "127.0.0.1"
    port: int = 0

    # Underlying FastAPI/JSON-RPC application and uvicorn server
    _app: API | None = field(default=None, init=False, repr=False)
    _uvicorn_server: uvicorn.Server | None = field(default=None, init=False, repr=False)
    _server_task: asyncio.Task | None = field(default=None, init=False, repr=False)
    _bound_port: int = field(default=0, init=False, repr=False)
    _sockets: list[socket.socket] | None = field(default=None, init=False, repr=False)

    # Mapping from subscription_id -> WebSocket connection and the
    # inverse mapping from connection -> set[subscription_id]. These are
    # used to route ``events.notify`` notifications to the correct
    # clients.
    _subscription_clients: Dict[str, WebSocket] = field(
        default_factory=dict, init=False, repr=False
    )
    _client_subscriptions: Dict[WebSocket, set[str]] = field(
        default_factory=dict, init=False, repr=False
    )

    async def start(self) -> None:
        """Start the WebSocket server.

        This coroutine binds the listening socket and waits until the
        underlying uvicorn server has completed its startup sequence
        before returning. Callers are responsible for running the event
        loop (for example via :func:`asyncio.run`).
        """

        if self._app is None:
            self._create_app()

        assert self._app is not None
        config = uvicorn.Config(
            self._app,
            host=self.host,
            port=self.port,
            log_level="info",
        )
        if not config.loaded:
            config.load()

        # Bind a socket so we can discover the effective port when
        # ``port=0`` was requested, then run uvicorn's startup sequence
        # against that socket.
        sock = config.bind_socket()
        self._bound_port = int(sock.getsockname()[1])
        self._sockets = [sock]

        self._uvicorn_server = uvicorn.Server(config)
        # Mirror the setup performed in ``Server._serve`` so that
        # ``startup`` can run correctly when called directly.
        self._uvicorn_server.lifespan = config.lifespan_class(config)
        await self._uvicorn_server.startup(sockets=self._sockets)

        # Run the uvicorn main loop in the background; the caller owns
        # the event loop.
        loop = asyncio.get_running_loop()
        self._server_task = loop.create_task(self._uvicorn_server.main_loop())

    async def stop(self) -> None:
        """Stop the WebSocket server and close all active connections."""

        server = self._uvicorn_server
        self._uvicorn_server = None

        # Ask the uvicorn main loop to exit and wait for it to finish.
        task = self._server_task
        self._server_task = None

        if server is not None and task is not None:
            server.should_exit = True
            await task

            sockets = self._sockets or []
            await server.shutdown(sockets=sockets)

        self._sockets = None

        # Clear subscription tracking state.
        self._subscription_clients.clear()
        self._client_subscriptions.clear()
        self._bound_port = 0

    @property
    def bound_port(self) -> int:
        """Return the effective TCP port the server is bound to.

        When ``port=0`` was passed to the constructor, this returns the
        OS-assigned ephemeral port. Otherwise it simply returns the
        configured port.
        """

        return self._bound_port or self.port

    # Internal wiring ----------------------------------------------------

    def _create_app(self) -> None:
        """Create the FastAPI/JSON-RPC application for this server."""

        app = API(title="nate_ntm runtime control API")

        # Expose a single WebSocket endpoint at ``/`` that speaks the
        # same JSON-RPC over WebSocket protocol as the original
        # websockets-based server.
        app.add_api_websocket_route("/", self._handle_client, name="jsonrpc_ws")

        self._app = app

    async def _handle_client(self, websocket: WebSocket) -> None:
        """Per-connection handler for JSON-RPC messages.

        The current implementation is deliberately simple:

        * Accept the WebSocket connection.
        * Each received text message is parsed as JSON.
        * Valid JSON-RPC request objects are dispatched via
          :func:`dispatch_request`.
        * Responses are sent back on the same WebSocket.
        * ``events.subscribe`` / ``events.unsubscribe`` calls update the
          subscription-to-connection mappings used by
          :meth:`publish_event`.
        """

        await websocket.accept()
        try:
            while True:
                try:
                    raw_message = await websocket.receive_text()
                except WebSocketDisconnect:
                    # Client disconnected.
                    break

                try:
                    request = json.loads(raw_message)
                except json.JSONDecodeError:
                    error = _make_error_response(
                        code=1000,
                        message="Invalid JSON payload",
                        response_id=None,
                    )
                    await websocket.send_text(json.dumps(error))
                    continue

                if not isinstance(request, Mapping):
                    error = _make_error_response(
                        code=1000,
                        message="Request must be a JSON object",
                        response_id=request.get("id") if isinstance(request, dict) else None,
                    )
                    await websocket.send_text(json.dumps(error))
                    continue

                method = request.get("method")
                response = dispatch_request(self.api_server, request)

                # Track subscriptions based on the JSON-RPC method.
                if method == "events.subscribe" and "result" in response:
                    sub_id = response["result"].get("subscription_id")
                    if isinstance(sub_id, str):
                        self._subscription_clients[sub_id] = websocket
                        self._client_subscriptions.setdefault(websocket, set()).add(sub_id)
                elif method == "events.unsubscribe" and "result" in response:
                    params = request.get("params") or {}
                    sub_id_val = params.get("subscription_id")
                    if isinstance(sub_id_val, str):
                        self._subscription_clients.pop(sub_id_val, None)
                        subs = self._client_subscriptions.get(websocket)
                        if subs is not None:
                            subs.discard(sub_id_val)

                await websocket.send_text(json.dumps(response))
        finally:
            await self._cleanup_client(websocket)

    async def _cleanup_client(self, websocket: WebSocket) -> None:
        """Remove all subscriptions associated with a disconnected client."""

        subs = self._client_subscriptions.pop(websocket, set())
        for sub_id in subs:
            self._subscription_clients.pop(sub_id, None)
            # Keep the in-process subscription registry tidy as well.
            self.api_server.unsubscribe_events(sub_id)

    async def publish_event(self, event: "AgentEvent") -> None:  # pragma: no cover - covered via tests
        """Publish a single :class:`AgentEvent` to matching subscribers.

        This is a thin async wrapper around
        :func:`build_events_notify_messages`. It looks up the owning
        WebSocket connection for each subscription and sends a
        JSON-RPC 2.0 ``events.notify`` notification frame.
        """

        from ..runtime.events import AgentEvent as _AgentEvent

        if not isinstance(event, _AgentEvent):
            raise TypeError("publish_event expects an AgentEvent instance")

        messages = build_events_notify_messages(self.api_server, event)

        # Send each notification to the WebSocket associated with its
        # subscription, if still connected.
        for msg in messages:
            params = msg.get("params") or {}
            sub_id = params.get("subscription_id")
            if not isinstance(sub_id, str):
                continue

            ws = self._subscription_clients.get(sub_id)
            if ws is None:
                continue

            try:
                await ws.send_text(json.dumps(msg))
            except RuntimeError:
                # Connection might already be closed; ignore.
                continue
