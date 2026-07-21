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
    events: list[dict[str, Any]]


class SwarmACPClient:
    """Client-side API for one external Swarm ACP session.

    Standard ACP operations are delegated to the SDK's
    :class:`ClientSideConnection`, which constructs and validates the ACP
    schema models. nate-ntm's reserved swarm controls use the SDK's extension
    method support and validate their results into stable Pydantic models.
    """

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
        """Connect to an already-running Swarm ACP TCP endpoint."""

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
        result = await self._connection.ext_method("attach", {"agent_id": agent_id})
        return AttachResult.model_validate(result)

    async def detach(self) -> DetachResult:
        result = await self._connection.ext_method("detach", {})
        return DetachResult.model_validate(result)

    async def swarm_status(self) -> SwarmStatusResult:
        result = await self._connection.ext_method("swarm_status", {})
        return SwarmStatusResult.model_validate(result)

    async def agent_detail(
        self,
        agent_id: str,
        *,
        max_events: int | None = None,
    ) -> AgentDetailResult:
        params: dict[str, Any] = {"agent_id": agent_id}
        if max_events is not None:
            params["max_events"] = max_events
        result = await self._connection.ext_method("agent_detail", params)
        return AgentDetailResult.model_validate(result)

    async def prompt(self, prompt: list[PromptBlock]) -> PromptResponse:
        return await self._connection.prompt(session_id=self.session_id, prompt=prompt)

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

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.close()
