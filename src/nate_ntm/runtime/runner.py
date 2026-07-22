"""Run a RuntimeDaemon with its control API and external ACP TCP server."""

from __future__ import annotations

import asyncio
import socket
from dataclasses import dataclass, field

import uvicorn
from fastapi_jsonrpc import API

from ..api.runtime_api import create_runtime_api_app
from ..api.server import RuntimeApiServer
from ..config.runtime_config import RuntimeConfig
from .adapters import RuntimeAdapters, create_runtime_adapters
from .daemon import RuntimeDaemon, StartupMode
from .swarm_acp_tcp import SwarmACPTCPServer

__all__ = [
    "RuntimeControlContext",
    "create_runtime_control_context",
    "serve_runtime_control_api",
    "run_runtime_with_control_api_async",
    "run_runtime_with_control_api",
]


@dataclass(slots=True)
class RuntimeControlContext:
    config: RuntimeConfig
    mode: StartupMode
    daemon: RuntimeDaemon
    api_server: RuntimeApiServer
    app: API
    host: str
    port: int
    acp_host: str
    acp_port: int
    acp_server: SwarmACPTCPServer
    bound_port: int = 0
    _uvicorn_server: uvicorn.Server | None = field(default=None, repr=False)
    _server_task: asyncio.Task[None] | None = field(default=None, repr=False)
    _sockets: list[socket.socket] | None = field(default=None, repr=False)


def create_runtime_control_context(
    config: RuntimeConfig,
    mode: StartupMode,
    *,
    host: str | None = None,
    port: int | None = None,
    acp_host: str = "127.0.0.1",
    acp_port: int = 8766,
    agent_count: int | None = None,
    adapters: RuntimeAdapters | None = None,
) -> RuntimeControlContext:
    adapters = adapters or create_runtime_adapters(config)
    if mode is StartupMode.CREATE:
        daemon = RuntimeDaemon.create(
            config,
            agent_count=agent_count,
            adapters=adapters,
        )
    elif mode is StartupMode.RESUME:
        daemon = RuntimeDaemon.resume(config, adapters=adapters)
    else:
        raise ValueError(f"Unsupported startup mode: {mode!r}")

    api_server = RuntimeApiServer(daemon=daemon)
    acp_server = SwarmACPTCPServer(
        daemon=daemon,
        agent_client=adapters.acp,
        host=acp_host,
        port=acp_port,
    )
    return RuntimeControlContext(
        config=config,
        mode=mode,
        daemon=daemon,
        api_server=api_server,
        app=create_runtime_api_app(api_server),
        host=host or config.control_api_host,
        port=port if port is not None else config.control_api_port,
        acp_host=acp_host,
        acp_port=acp_port,
        acp_server=acp_server,
    )


async def _start_api_server(ctx: RuntimeControlContext) -> None:
    if ctx._uvicorn_server is not None:
        return

    config = uvicorn.Config(
        ctx.app,
        host=ctx.host,
        port=ctx.port,
        log_level="info",
    )
    if not config.loaded:
        config.load()

    sock = config.bind_socket()
    ctx.bound_port = int(sock.getsockname()[1])
    ctx._sockets = [sock]
    server = uvicorn.Server(config)
    ctx._uvicorn_server = server
    server.lifespan = config.lifespan_class(config)
    await server.startup(sockets=ctx._sockets)
    ctx._server_task = asyncio.create_task(server.main_loop())


async def _stop_api_server(ctx: RuntimeControlContext) -> None:
    server = ctx._uvicorn_server
    task = ctx._server_task
    ctx._uvicorn_server = None
    ctx._server_task = None
    if server is not None and task is not None:
        server.should_exit = True
        await task
        await server.shutdown(sockets=ctx._sockets or [])
    ctx._sockets = None
    ctx.bound_port = 0


async def serve_runtime_control_api(
    ctx: RuntimeControlContext,
    *,
    poll_interval: float = 0.1,
) -> None:
    await _start_api_server(ctx)
    await ctx.acp_server.start()
    try:
        ctx.daemon.start()
        while not ctx.daemon.state.shutdown_requested:
            await asyncio.sleep(poll_interval)
    finally:
        try:
            await ctx.acp_server.close()
        finally:
            try:
                await _stop_api_server(ctx)
            finally:
                ctx.daemon.mark_stopped()


async def run_runtime_with_control_api_async(
    config: RuntimeConfig,
    mode: StartupMode,
    *,
    host: str | None = None,
    port: int | None = None,
    acp_host: str = "127.0.0.1",
    acp_port: int = 8766,
    poll_interval: float = 0.1,
    agent_count: int | None = None,
    adapters: RuntimeAdapters | None = None,
) -> None:
    ctx = create_runtime_control_context(
        config,
        mode,
        host=host,
        port=port,
        acp_host=acp_host,
        acp_port=acp_port,
        agent_count=agent_count,
        adapters=adapters,
    )
    await serve_runtime_control_api(ctx, poll_interval=poll_interval)


def run_runtime_with_control_api(
    config: RuntimeConfig,
    mode: StartupMode,
    *,
    host: str | None = None,
    port: int | None = None,
    acp_host: str = "127.0.0.1",
    acp_port: int = 8766,
    poll_interval: float = 0.1,
    agent_count: int | None = None,
    adapters: RuntimeAdapters | None = None,
) -> None:
    asyncio.run(
        run_runtime_with_control_api_async(
            config,
            mode,
            host=host,
            port=port,
            acp_host=acp_host,
            acp_port=acp_port,
            poll_interval=poll_interval,
            agent_count=agent_count,
            adapters=adapters,
        )
    )
