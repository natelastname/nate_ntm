from __future__ import annotations

"""Typed client for nate-ntm's external Swarm ACP endpoint."""

import asyncio
from collections.abc import Callable, Iterable
from typing import Any, TypeAlias

from acp import connect_to_agent, text_block
from acp.client.connection import ClientSideConnection
from acp.connection import StreamEvent
from acp.interfaces import Client
from acp.schema import (
    AudioContentBlock,
    EmbeddedResourceContentBlock,
    ImageContentBlock,
    PromptResponse,
    ResourceContentBlock,
    TextContentBlock,
)
from pydantic import BaseModel, ConfigDict

__all__ = [
    "AgentDetailResult",
    "AttachResult",
    "DetachResult",
    "SwarmACPClient",
    "SwarmStatusResult",
]

PromptBlock: TypeAlias = (
    TextContentBlock
    | ImageContentBlock
    | AudioContentBlock
    | ResourceContentBlock
    | EmbeddedResourceContentBlock
)


class _ExtensionResult(BaseModel):
    model_config = ConfigDict(extra="allow")


class AttachResult(_ExtensionResult):
    attached_agent_id: str


class DetachResult(_ExtensionResult):
    detached: bool


class SwarmStatusResult(_ExtensionResult):
    attached_agent_id: str | None
    swarm: dict[str, Any]


class AgentDetailResult(_ExtensionResult):
    attached: bool
    agent: dict[str, Any]


class SwarmACPClient:
    """Client-side API for one external Swarm ACP session."""

    def __init__(
        self,
        connection: ClientSideConnection,
        writer: asyncio.StreamWriter,
        *,
        session_id: str,
    ) -> None:
        self._connection = connection
        self._writer = writer
        self.session_id = session_id
        self._closed = False

    @classmethod
    async def connect(
        cls,
        client: Client,
        host: str,
        port: int,
        *,
        session_id: str,
        receive_timeout: float | None = None,
        observers: Iterable[Callable[[StreamEvent], None]] = (),
    ) -> SwarmACPClient:
        reader, writer = await asyncio.open_connection(host, port)
        connection = connect_to_agent(
            client,
            writer,
            reader,
            receive_timeout=receive_timeout,
            observers=list(observers),
        )
        return cls(connection, writer, session_id=session_id)

    async def attach(self, agent_id: str) -> AttachResult:
        return AttachResult.model_validate(
            await self._connection.ext_method("attach", {"agent_id": agent_id})
        )

    async def detach(self) -> DetachResult:
        return DetachResult.model_validate(
            await self._connection.ext_method("detach", {})
        )

    async def swarm_status(self) -> SwarmStatusResult:
        return SwarmStatusResult.model_validate(
            await self._connection.ext_method("swarm_status", {})
        )

    async def agent_detail(self, agent_id: str) -> AgentDetailResult:
        return AgentDetailResult.model_validate(
            await self._connection.ext_method("agent_detail", {"agent_id": agent_id})
        )

    async def prompt(self, prompt: list[PromptBlock]) -> PromptResponse:
        return await self._connection.prompt(
            session_id=self.session_id,
            prompt=prompt,
        )

    async def prompt_text(self, text: str) -> PromptResponse:
        return await self.prompt([text_block(text)])

    async def interrupt(self) -> None:
        await self._connection.cancel(session_id=self.session_id)

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        await self._connection.close()
        self._writer.close()
        await self._writer.wait_closed()

    async def __aenter__(self) -> SwarmACPClient:
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()
