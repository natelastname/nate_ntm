from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator

import pytest
from acp.schema import BaseModel

from nate_ntm.runtime.acp_update_stream import (
    AcpSessionUpdateStream,
    AgentSessionNotActive,
    ReceivedSessionUpdate,
)
from nate_ntm.runtime.swarm_acp_mux import SwarmACPMux


class _Update(BaseModel):
    text: str


@dataclass
class _SwarmState:
    agents: dict[str, object]


class _Daemon:
    def __init__(self, *agent_ids: str) -> None:
        self.swarm_state = _SwarmState({agent_id: object() for agent_id in agent_ids})

    def get_swarm_status(self) -> dict[str, object]:
        return {}

    def get_agent_detail(self, agent_id: str) -> dict[str, object]:
        if agent_id not in self.swarm_state.agents:
            raise KeyError(agent_id)
        return {"agent_id": agent_id}


class _AgentClient:
    def __init__(self, *agent_ids: str) -> None:
        self.streams = {
            agent_id: AcpSessionUpdateStream() for agent_id in agent_ids
        }
        self.fail_on_subscribe: set[str] = set()

    def subscribe_acp_updates(self, agent_id: str):
        if agent_id in self.fail_on_subscribe:
            return self._failed_subscription(agent_id)
        return self.streams[agent_id].subscribe()

    @asynccontextmanager
    async def _failed_subscription(
        self, agent_id: str
    ) -> AsyncIterator[AsyncIterator[ReceivedSessionUpdate]]:
        raise AgentSessionNotActive(agent_id)
        yield  # pragma: no cover

    async def prompt(self, agent_id: str, prompt: str) -> str | None:
        return None

    async def interrupt(self, agent_id: str) -> None:
        return None


class _External:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.updates: list[_Update] = []

    async def session_update(self, *, session_id: str, update: BaseModel) -> None:
        if self.error is not None:
            raise self.error
        self.updates.append(update)


def _mux(client: _AgentClient, external: _External) -> SwarmACPMux:
    return SwarmACPMux(
        daemon=_Daemon(*client.streams),  # type: ignore[arg-type]
        agent_client=client,
        external_connection=external,
        external_session_id="external",
    )


async def _acknowledge(_: str) -> None:
    return None


def _publish(stream: AcpSessionUpdateStream, text: str) -> None:
    stream.publish(_Update(text=text), received_at=datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_obsolete_attachment_cannot_clear_new_attachment() -> None:
    client = _AgentClient("agent-a", "agent-b")
    mux = _mux(client, _External())

    old = await mux.prepare_attach("agent-a")
    await mux.activate_attachment(old)
    new = await mux.prepare_attach("agent-b")
    await mux.activate_attachment(new)

    await mux._finished(old.token)  # type: ignore[arg-type]

    assert mux.attached_agent_id == "agent-b"
    await mux.close()


@pytest.mark.asyncio
async def test_external_write_failure_fails_and_cleans_attachment() -> None:
    client = _AgentClient("agent-a")
    error = RuntimeError("external connection failed")
    mux = _mux(client, _External(error))
    await mux.attach("agent-a", acknowledge=_acknowledge)

    _publish(client.streams["agent-a"], "update")

    with pytest.raises(RuntimeError, match="external connection failed"):
        await mux.wait_failed()

    async with asyncio.timeout(1):
        while mux.attached_agent_id is not None:
            await asyncio.sleep(0)

    await mux.close()


@pytest.mark.asyncio
async def test_failed_prepare_leaves_mux_unattached() -> None:
    client = _AgentClient("agent-a")
    client.fail_on_subscribe.add("agent-a")
    mux = _mux(client, _External())

    with pytest.raises(AgentSessionNotActive):
        await mux.prepare_attach("agent-a")

    assert mux.attached_agent_id is None
    await mux.close()
