from __future__ import annotations

"""Macro integration tests for the external Swarm ACP transport."""

import asyncio
import os
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator

import acp
import pytest
import pytest_asyncio
from acp.connection import StreamDirection, StreamEvent

from nate_ntm.config.runtime_config import AdapterKind, RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import AcpAgentSession, NateOhaAcpClient
from nate_ntm.runtime.acp_types import SessionNotification
from nate_ntm.runtime.adapters import create_runtime_adapters
from nate_ntm.runtime.daemon import RuntimeDaemon
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.nate_oha_launch import build_effective_nate_oha_config
from nate_ntm.runtime.swarm_acp_client import SwarmACPClient
from nate_ntm.runtime.swarm_acp_mux import SwarmACPMux
from nate_ntm.runtime.swarm_acp_server import (
    ConnectionExternalACPConnection,
    SwarmACPConnection,
    SwarmACPServerSession,
)
from nate_ntm.runtime.swarm_state import AgentState, SwarmState


@dataclass
class RealSwarm:
    daemon: RuntimeDaemon
    acp_client: NateOhaAcpClient
    agent_a: str
    agent_b: str


@dataclass
class RecordingCallbacks:
    notifications: list[SessionNotification] = field(default_factory=list)

    async def session_update(self, session_id: str, update: Any, **_: Any) -> None:
        self.notifications.append(SessionNotification(session_id=session_id, update=update))


@dataclass
class ConnectedSwarm:
    swarm: RealSwarm
    server: asyncio.AbstractServer
    client: SwarmACPClient
    callbacks: RecordingCallbacks
    wire_events: list[tuple[str, Any]]
    mux: SwarmACPMux


def _config(tmp_path: Path) -> RuntimeConfig:
    project = tmp_path / "project"
    project.mkdir()
    root = Path(__file__).resolve().parents[3]
    env = dict(os.environ)
    env.update(
        {
            "NATE_NTM_PROJECT_DIR": str(project),
            "NATE_NTM_ADAPTER_MODE": AdapterKind.REAL.value,
            "NATE_NTM_NATE_OHA_CONFIG": str(root / "nate-oha-profiles/profile1.json"),
            "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
        }
    )
    return load_runtime_config(project_path=project, env=env)


@pytest_asyncio.fixture
async def real_swarm(tmp_path: Path) -> AsyncIterator[RealSwarm]:
    config = _config(tmp_path)
    store = MetadataStore(config=config)
    nate_oha_config = build_effective_nate_oha_config(config=config)
    agent_a, agent_b = "swarm-real-a", "swarm-real-b"
    states = {
        agent_id: AgentState(
            agent_id=agent_id,
            display_name=display_name,
            conversation_id="",
            nate_oha_config=nate_oha_config,
        )
        for agent_id, display_name in (
            (agent_a, "Swarm Real Agent A"),
            (agent_b, "Swarm Real Agent B"),
        )
    }
    now = datetime.utcnow()
    store.save_swarm_state(
        SwarmState(
            swarm_id=config.swarm_id,
            project_path=config.project_path,
            agent_mail_project_id=str(config.project_path),
            created_at=now,
            last_updated_at=now,
            agents=states,
        )
    )

    adapters = create_runtime_adapters(config)
    daemon = RuntimeDaemon.resume(config, adapters=adapters)
    client = daemon.acp_client
    assert isinstance(client, NateOhaAcpClient)
    await client.start_agent_async(agent_a, metadata=states[agent_a])
    await client.start_agent_async(agent_b, metadata=states[agent_b])
    daemon.start()

    try:
        yield RealSwarm(daemon, client, agent_a, agent_b)
    finally:
        for agent_id in (agent_a, agent_b):
            try:
                await client.stop_agent_async(agent_id, timeout=10.0)
            except Exception:
                pass
        daemon.request_shutdown()
        daemon.mark_stopped()


async def _start_server(
    daemon: RuntimeDaemon,
) -> tuple[asyncio.AbstractServer, asyncio.Future[SwarmACPMux]]:
    agent_client = daemon.acp_client
    assert isinstance(agent_client, NateOhaAcpClient)
    mux_future: asyncio.Future[SwarmACPMux] = asyncio.get_running_loop().create_future()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        external = ConnectionExternalACPConnection()
        session = SwarmACPServerSession(
            daemon=daemon,
            agent_client=agent_client,
            external_connection=external,
            external_session_id="external-1",
        )
        mux_future.set_result(session.mux)
        connection = SwarmACPConnection(
            session=session,
            writer=writer,
            reader=reader,
            receive_timeout=5.0,
        )
        external.bind(connection)

        async def serve(_: SwarmACPServerSession) -> None:
            await connection.main_loop()

        async def close() -> None:
            await connection.close()
            writer.close()
            await writer.wait_closed()

        await session.run_connection(serve, close_transport=close)

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    return server, mux_future


@pytest_asyncio.fixture
async def connected_swarm(real_swarm: RealSwarm) -> AsyncIterator[ConnectedSwarm]:
    server, mux_future = await _start_server(real_swarm.daemon)
    host, port = server.sockets[0].getsockname()[:2]
    callbacks = RecordingCallbacks()
    wire_events: list[tuple[str, Any]] = []

    def observe(event: StreamEvent) -> None:
        if event.direction is not StreamDirection.INCOMING:
            return
        message = event.message
        if "id" in message and "method" not in message:
            wire_events.append(("response", message.get("result")))
        elif "method" in message and "id" not in message:
            wire_events.append(("notification", message["method"], message.get("params")))

    client = await SwarmACPClient.connect(
        callbacks,
        host,
        port,
        session_id="external-1",
        receive_timeout=5.0,
        observers=[observe],
    )
    mux = await asyncio.wait_for(mux_future, 5.0)
    connected = ConnectedSwarm(real_swarm, server, client, callbacks, wire_events, mux)
    try:
        yield connected
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
        assert mux._closed is True  # type: ignore[attr-defined]
        assert mux._attachment is None  # type: ignore[attr-defined]


def _texts(callbacks: RecordingCallbacks, start: int) -> list[str]:
    values = []
    for notification in callbacks.notifications[start:]:
        payload = notification.update.model_dump(mode="json", by_alias=True)
        content = payload.get("content") if isinstance(payload, dict) else None
        if isinstance(content, dict) and isinstance(content.get("text"), str):
            values.append(content["text"])
    return values


async def _wait_for_text(callbacks: RecordingCallbacks, text: str, start: int) -> None:
    async with asyncio.timeout(15.0):
        while not any(text in value for value in _texts(callbacks, start)):
            await asyncio.sleep(0.05)


def _text(value: Any) -> str:
    if isinstance(value, list) and value:
        return str(getattr(value[0], "text", ""))
    return "" if value is None else str(value)


@pytest.mark.asyncio
async def test_attach_prompt_interrupt_detach(connected_swarm: ConnectedSwarm) -> None:
    swarm, client = connected_swarm.swarm, connected_swarm.client
    assert (await client.swarm_status()).attached_agent_id is None

    detail = await client.agent_detail(swarm.agent_a)
    assert detail.attached is False
    assert detail.agent == swarm.daemon.get_agent_detail(swarm.agent_a)
    assert "events" not in detail.model_dump()

    prompt_calls: list[tuple[str, str]] = []
    interrupt_calls: list[str] = []
    original_prompt = swarm.acp_client.prompt
    original_interrupt = swarm.acp_client.interrupt

    async def prompt(agent_id: str, value: Any = None) -> str | None:
        text = _text(value)
        prompt_calls.append((agent_id, text))
        return await original_prompt(agent_id, text)

    async def interrupt(agent_id: str) -> None:
        interrupt_calls.append(agent_id)
        await original_interrupt(agent_id)

    swarm.acp_client.prompt = prompt  # type: ignore[assignment]
    swarm.acp_client.interrupt = interrupt  # type: ignore[assignment]
    try:
        await client.attach(swarm.agent_a)
        text = "hello through swarm"
        start = len(connected_swarm.callbacks.notifications)
        await client.prompt_text(text)
        await _wait_for_text(connected_swarm.callbacks, text, start)
        assert prompt_calls[-1] == (swarm.agent_a, text)
        await client.interrupt()
        async with asyncio.timeout(10.0):
            while not interrupt_calls:
                await asyncio.sleep(0.05)
        assert interrupt_calls[-1] == swarm.agent_a
    finally:
        swarm.acp_client.prompt = original_prompt  # type: ignore[assignment]
        swarm.acp_client.interrupt = original_interrupt  # type: ignore[assignment]

    assert (await client.detach()).detached is True
    baseline = len(connected_swarm.callbacks.notifications)
    await swarm.acp_client.prompt(swarm.agent_a, "direct-after-detach")
    await asyncio.sleep(0.1)
    assert len(connected_swarm.callbacks.notifications) == baseline
    assert isinstance(swarm.acp_client._sessions[swarm.agent_a], AcpAgentSession)


@pytest.mark.asyncio
async def test_switching_preserves_attach_order(connected_swarm: ConnectedSwarm) -> None:
    swarm, client = connected_swarm.swarm, connected_swarm.client
    await client.attach(swarm.agent_a)
    start = len(connected_swarm.callbacks.notifications)
    await client.prompt_text("agent-a")
    await _wait_for_text(connected_swarm.callbacks, "agent-a", start)

    attach_index = next(
        i
        for i, event in enumerate(connected_swarm.wire_events)
        if event[0] == "response"
        and isinstance(event[1], dict)
        and event[1].get("attached_agent_id") == swarm.agent_a
    )
    update_index = next(
        i
        for i, event in enumerate(connected_swarm.wire_events)
        if event[0] == "notification"
        and event[1] == acp.CLIENT_METHODS["session_update"]
    )
    assert update_index > attach_index

    await client.attach(swarm.agent_b)
    start = len(connected_swarm.callbacks.notifications)
    await client.prompt_text("agent-b")
    await _wait_for_text(connected_swarm.callbacks, "agent-b", start)
    assert connected_swarm.mux.attached_agent_id == swarm.agent_b


@pytest.mark.asyncio
async def test_unknown_agent_error_mapping(connected_swarm: ConnectedSwarm) -> None:
    with pytest.raises(acp.RequestError) as exc_info:
        await connected_swarm.client.attach("missing-agent")
    assert isinstance(exc_info.value.data, dict)
    assert exc_info.value.data.get("mux_code") == "MUX_UNKNOWN_AGENT"
