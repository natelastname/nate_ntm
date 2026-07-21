"""One macro test for the complete nate-ntm runtime path.

This test intentionally uses the real production adapters. It requires:

* the project-local ``nate-oha`` executable; and
* a running ``mcp_agent_mail`` HTTP server at the configured Agent Mail URL.

When either external service is unavailable the test skips with a concrete
reason. No fake adapter, mock transport, or manually fabricated agent metadata
is used.
"""

from __future__ import annotations

import asyncio
import shutil
import socket
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import acp
import pytest
from acp.connection import StreamDirection, StreamEvent

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import NateOhaAcpClient
from nate_ntm.runtime.acp_types import SessionNotification
from nate_ntm.runtime.adapters import create_runtime_adapters
from nate_ntm.runtime.daemon import RuntimeDaemon
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.swarm_acp_client import SwarmACPClient
from nate_ntm.runtime.swarm_acp_mux import SwarmACPMux
from nate_ntm.runtime.swarm_acp_server import (
    ConnectionExternalACPConnection,
    SwarmACPConnection,
    SwarmACPServerSession,
)


@dataclass
class _Callbacks:
    notifications: list[SessionNotification] = field(default_factory=list)

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        self.notifications.append(SessionNotification(session_id=session_id, update=update))


async def _start_swarm_server(
    daemon: RuntimeDaemon,
    client: NateOhaAcpClient,
) -> tuple[asyncio.AbstractServer, asyncio.Future[SwarmACPMux]]:
    mux_future: asyncio.Future[SwarmACPMux] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        external = ConnectionExternalACPConnection()
        session = SwarmACPServerSession(
            daemon=daemon,
            agent_client=client,
            external_connection=external,
            external_session_id="external-1",
        )
        if not mux_future.done():
            mux_future.set_result(session.mux)

        connection = SwarmACPConnection(
            session=session,
            writer=writer,
            reader=reader,
            receive_timeout=10.0,
        )
        external.bind(connection)

        async def serve(_: SwarmACPServerSession) -> None:
            await connection.main_loop()

        async def close() -> None:
            await connection.close()
            writer.close()
            await writer.wait_closed()

        await session.run_connection(serve, close_transport=close)

    return await asyncio.start_server(handle, "127.0.0.1", 0), mux_future


def _config(tmp_path: Path) -> RuntimeConfig:
    project = tmp_path / "project"
    project.mkdir()
    repo_root = Path(__file__).resolve().parents[3]
    return load_runtime_config(
        project_path=project,
        env={
            "NATE_NTM_PROJECT_DIR": str(project),
            "NATE_NTM_NATE_OHA_CONFIG": str(
                repo_root / "nate-oha-profiles" / "profile1.json"
            ),
            "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
            "NATE_NTM_AGENT_MAIL_ENABLED": "true",
            "NATE_NTM_AGENT_MAIL_PROJECT": str(project),
            "NATE_NTM_AGENT_MAIL_URL": "http://127.0.0.1:8765/api",
        },
    )


def _require_external_services(config: RuntimeConfig) -> None:
    if shutil.which(config.nate_oha_executable) is None:
        pytest.skip(f"{config.nate_oha_executable!r} is not installed")

    parsed = urlparse(config.agent_mail_upstream_url or "http://127.0.0.1:8765/api")
    host = parsed.hostname or "127.0.0.1"
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=1.0):
            pass
    except OSError:
        pytest.skip(f"mcp_agent_mail is not reachable at {host}:{port}")


def _notification_texts(callbacks: _Callbacks, start: int) -> list[str]:
    texts: list[str] = []
    for notification in callbacks.notifications[start:]:
        payload = notification.update.model_dump(mode="json", by_alias=True)
        content = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            texts.append(content["text"])
    return texts


async def _wait_for_text(callbacks: _Callbacks, expected: str, start: int) -> None:
    async with asyncio.timeout(20.0):
        while not any(expected in text for text in _notification_texts(callbacks, start)):
            await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_real_runtime_create_swarm_acp_and_resume(tmp_path: Path) -> None:
    """Exercise the complete supported runtime architecture in one scenario."""

    config = _config(tmp_path)
    _require_external_services(config)

    adapters = create_runtime_adapters(config)
    daemon = RuntimeDaemon.create(config, agent_count=2, adapters=adapters)
    assert set(daemon.swarm_state.agents) == {"agent-1", "agent-2"}
    assert daemon.swarm_state.agent_mail_project_id == str(config.project_path)

    store = MetadataStore(config=config)
    for agent_id in daemon.swarm_state.agents:
        metadata = store.load_agent_state(agent_id)
        agent_mail = metadata.nate_oha_config.features.agent_mail
        assert agent_mail.enabled is True
        assert agent_mail.agent_identity
        assert agent_mail.credentials_ref

    internal = daemon.acp_client
    assert isinstance(internal, NateOhaAcpClient)
    for agent_id in daemon.swarm_state.agents:
        await internal.start_agent(agent_id, metadata=store.load_agent_state(agent_id))
    daemon.start()

    server, mux_future = await _start_swarm_server(daemon, internal)
    host, port = server.sockets[0].getsockname()[:2]
    callbacks = _Callbacks()
    wire: list[tuple[str, Any]] = []

    def observe(event: StreamEvent) -> None:
        if event.direction is not StreamDirection.INCOMING:
            return
        message = event.message
        if "id" in message and "method" not in message:
            wire.append(("response", message.get("result")))
        elif "method" in message and "id" not in message:
            wire.append(("notification", message["method"]))

    external = await SwarmACPClient.connect(
        callbacks,
        host,
        port,
        session_id="external-1",
        receive_timeout=10.0,
        observers=[observe],
    )
    mux = await asyncio.wait_for(mux_future, timeout=5.0)

    try:
        status = await external.swarm_status()
        assert status.attached_agent_id is None
        assert len(status.swarm["agents"]) == 2

        detail = await external.agent_detail("agent-1")
        assert detail.attached is False
        assert detail.agent["agent_mail_identity"]
        assert detail.agent["conversation_id"]
        assert "events" not in detail.model_dump()

        await external.attach("agent-1")
        first = "end-to-end prompt for agent one"
        start = len(callbacks.notifications)
        response = await external.prompt_text(first)
        assert response.stop_reason == "end_turn"
        await _wait_for_text(callbacks, first, start)

        attach_response_index = next(
            index
            for index, item in enumerate(wire)
            if item[0] == "response"
            and isinstance(item[1], dict)
            and item[1].get("attached_agent_id") == "agent-1"
        )
        first_update_index = next(
            index
            for index, item in enumerate(wire)
            if item == ("notification", acp.CLIENT_METHODS["session_update"])
        )
        assert first_update_index > attach_response_index

        await external.interrupt()
        await external.attach("agent-2")
        second = "end-to-end prompt for agent two"
        start = len(callbacks.notifications)
        await external.prompt_text(second)
        await _wait_for_text(callbacks, second, start)
        assert mux.attached_agent_id == "agent-2"

        assert (await external.detach()).detached is True
        assert mux.attached_agent_id is None
    finally:
        await external.close()
        server.close()
        await server.wait_closed()
        for agent_id in daemon.swarm_state.agents:
            await internal.stop_agent(agent_id)
        daemon.request_shutdown()
        daemon.mark_stopped()

    persisted = {
        agent_id: store.load_agent_state(agent_id).conversation_id
        for agent_id in daemon.swarm_state.agents
    }
    assert all(persisted.values())

    resumed = RuntimeDaemon.resume(config, adapters=create_runtime_adapters(config))
    assert set(resumed.swarm_state.agents) == set(persisted)
    for agent_id, conversation_id in persisted.items():
        assert resumed.get_agent_detail(agent_id)["conversation_id"] == conversation_id
