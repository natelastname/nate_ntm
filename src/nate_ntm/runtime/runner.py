"""Helpers for running a RuntimeDaemon with its WebSocket control API.

This module provides small orchestration helpers that wire together

* :class:`~nate_ntm.runtime.daemon.RuntimeDaemon`
* :class:`~nate_ntm.api.server.RuntimeApiServer`
* :class:`~nate_ntm.api.jsonrpc_ws.JsonRpcWebSocketServer`

into a single in-process runtime suitable for the MVP quickstart
scenarios (US1).

The helpers are intentionally minimal and synchronous at the top level so
that they can be used from the Typer-based CLI while still exposing
async-capable building blocks for tests and future event-loop plumbing.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Optional

from ..api.jsonrpc_ws import JsonRpcWebSocketServer
from ..api.server import RuntimeApiServer
from ..config.runtime_config import RuntimeConfig
from .daemon import RuntimeDaemon, StartupMode
from .events import AgentEvent

__all__ = [
    "RuntimeControlContext",
    "create_runtime_control_context",
    "serve_runtime_control_api",
    "run_runtime_with_control_api_async",
    "run_runtime_with_control_api",
]


@dataclass(slots=True)
class RuntimeControlContext:
    """Bundle owning a :class:`RuntimeDaemon` and its control API server.

    Parameters
    ----------
    config:
        The resolved :class:`RuntimeConfig` for this runtime instance.

    mode:
        Startup mode used to construct the daemon (``create`` or
        ``resume``).

    daemon:
        The in-process :class:`RuntimeDaemon` instance.

    api_server:
        The :class:`RuntimeApiServer` bound to ``daemon``.

    ws_server:
        The :class:`JsonRpcWebSocketServer` exposing ``api_server`` over
        a localhost-only WebSocket JSON-RPC interface.
    """

    config: RuntimeConfig
    mode: StartupMode
    daemon: RuntimeDaemon
    api_server: RuntimeApiServer
    ws_server: JsonRpcWebSocketServer


def create_runtime_control_context(
    config: RuntimeConfig,
    mode: StartupMode,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
) -> RuntimeControlContext:
    """Construct a :class:`RuntimeControlContext` for ``config`` and ``mode``.

    This helper performs the synchronous wiring needed to:

    * Create or resume a :class:`RuntimeDaemon`.
    * Attach a :class:`RuntimeApiServer` to the daemon.
    * Prepare a :class:`JsonRpcWebSocketServer` bound to the configured
      control API host/port.

    The returned context does **not** start the WebSocket server or mark
    the daemon as running; call :func:`serve_runtime_control_api` to do
    so under an event loop.
    """

    if mode is StartupMode.CREATE:
        daemon = RuntimeDaemon.create(config)
    elif mode is StartupMode.RESUME:
        daemon = RuntimeDaemon.resume(config)
    else:  # pragma: no cover - defensive against future enum variants
        raise ValueError(f"Unsupported startup mode: {mode!r}")

    api_server = RuntimeApiServer(daemon=daemon)

    ws_host = host or config.control_api_host
    ws_port = port if port is not None else config.control_api_port

    ws_server = JsonRpcWebSocketServer(api_server=api_server, host=ws_host, port=ws_port)

    # Wire the runtime's AgentEvent pipeline into the WebSocket control API.
    #
    # The AgentSupervisor exposes an ``on_agent_event`` callback that is
    # invoked whenever a new :class:`AgentEvent` is appended to an agent's
    # in-memory event stream. Here we install a small bridge that forwards
    # those events to :meth:`JsonRpcWebSocketServer.publish_event`, which in
    # turn emits ``events.notify`` JSON-RPC notifications to subscribed
    # clients.
    scheduler = daemon.scheduler
    if scheduler is not None:
        supervisor = getattr(scheduler, "agent_supervisor", None)

        if supervisor is not None:
            def _on_agent_event(event: AgentEvent) -> None:
                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    # If no event loop is running (for example, in pure
                    # in-process unit tests), we still record events in the
                    # per-agent streams but skip streaming notifications.
                    return

                # Schedule asynchronous publication of the event without
                # blocking the caller.
                loop.create_task(ws_server.publish_event(event))

            supervisor.on_agent_event = _on_agent_event

    return RuntimeControlContext(
        config=config,
        mode=mode,
        daemon=daemon,
        api_server=api_server,
        ws_server=ws_server,
    )


async def serve_runtime_control_api(
    ctx: RuntimeControlContext,
    *,
    poll_interval: float = 0.1,
) -> None:
    """Start the WebSocket control API and run until shutdown is requested.

    This coroutine is responsible for the *lifetime* of the control API
    server for a single runtime instance. It:

    * Starts the underlying :class:`JsonRpcWebSocketServer`.
    * Marks the :class:`RuntimeDaemon` as running via :meth:`start`.
    * Polls :attr:`RuntimeState.shutdown_requested` until a graceful
      shutdown has been requested (for example, via the
      ``runtime.shutdown`` control API method).
    * On exit, stops the WebSocket server and marks the daemon as
      stopped.

    The caller owns the asyncio event loop and is expected to manage
    cancellation or process-level signals as appropriate.
    """

    await ctx.ws_server.start()

    try:
        # Transition the daemon into the Running state. This will, in
        # turn, allow the scheduler to register and "launch" configured
        # agents in dev-mode.
        ctx.daemon.start()

        # Simple polling loop driven by the RuntimeState flag. More
        # sophisticated event-loop and signal handling can be introduced
        # later without changing this basic contract.
        while not ctx.daemon.state.shutdown_requested:
            await asyncio.sleep(poll_interval)
    finally:
        # Always attempt to stop the WebSocket server and mark the
        # daemon as fully stopped, even if an error or cancellation
        # occurs while serving requests.
        try:
            await ctx.ws_server.stop()
        finally:
            ctx.daemon.mark_stopped()


async def run_runtime_with_control_api_async(
    config: RuntimeConfig,
    mode: StartupMode,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    poll_interval: float = 0.1,
) -> None:
    """Async helper to run a runtime and its control API to completion.

    This is a convenience wrapper that constructs a
    :class:`RuntimeControlContext` and delegates to
    :func:`serve_runtime_control_api`. It is suitable for use in tests or
    higher-level orchestration code that already manages an asyncio
    event loop.
    """

    ctx = create_runtime_control_context(config, mode, host=host, port=port)
    await serve_runtime_control_api(ctx, poll_interval=poll_interval)


def run_runtime_with_control_api(
    config: RuntimeConfig,
    mode: StartupMode,
    *,
    host: Optional[str] = None,
    port: Optional[int] = None,
    poll_interval: float = 0.1,
) -> None:
    """Run a runtime daemon and its WebSocket control API to completion.

    This synchronous helper is intended for use from the Typer-based CLI
    and other non-async entrypoints. It drives the underlying coroutine
    via :func:`asyncio.run` and returns once a graceful shutdown has been
    requested and processed.
    """

    asyncio.run(
        run_runtime_with_control_api_async(
            config,
            mode,
            host=host,
            port=port,
            poll_interval=poll_interval,
        )
    )
