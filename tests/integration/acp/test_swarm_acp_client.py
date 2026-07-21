from __future__ import annotations

import asyncio
from typing import Any

import acp
import pytest
from acp import schema as acp_schema
from acp.connection import Connection

from nate_ntm.runtime.swarm_acp_client import SwarmACPClient


class _ClientCallbacks:
    async def request_permission(self, session_id, tool_call, options, **kwargs):
        return {"outcome": {"outcome": "cancelled"}}

    async def session_update(self, session_id, update, **kwargs):
        return None


@pytest.mark.asyncio
async def test_swarm_acp_client_uses_typed_sdk_operations_over_tcp() -> None:
    calls: list[tuple[str, Any]] = []
    handlers: set[asyncio.Task[None]] = set()

    async def handler(method: str, params: Any | None, is_notification: bool) -> Any:
        calls.append((method, params))
        if method == "_attach":
            return {"attached_agent_id": params["agent_id"]}
        if method == "_detach":
            return {"detached": True}
        if method == "_swarm_status":
            return {"attached_agent_id": "agent-a", "swarm": {"swarm_id": "default"}}
        if method == "_agent_detail":
            return {
                "attached": True,
                "agent": {"agent_id": params["agent_id"]},
                "events": [{"kind": "started"}],
            }
        if method == acp.AGENT_METHODS["session_prompt"]:
            request = acp_schema.PromptRequest.model_validate(params)
            assert request.prompt[0].text == "hello"
            return acp_schema.PromptResponse(stop_reason="end_turn").model_dump(
                mode="json", by_alias=True, exclude_none=True
            )
        if method == acp.AGENT_METHODS["session_cancel"]:
            acp_schema.CancelNotification.model_validate(params)
            return None
        raise acp.RequestError.method_not_found(method)

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        conn = Connection(handler, writer, reader, listening=False, receive_timeout=5.0)
        try:
            await conn.main_loop()
        finally:
            await conn.close()
            writer.close()
            await writer.wait_closed()

    server = await asyncio.start_server(
        lambda reader, writer: handlers.add(
            asyncio.create_task(handle_client(reader, writer))
        ),
        host="127.0.0.1",
        port=0,
    )
    host, port = server.sockets[0].getsockname()[:2]

    client = await SwarmACPClient.connect(
        _ClientCallbacks(), host, port, session_id="external-1", receive_timeout=5.0
    )
    try:
        assert (await client.attach("agent-a")).attached_agent_id == "agent-a"
        assert (await client.swarm_status()).swarm["swarm_id"] == "default"
        assert (await client.agent_detail("agent-a", max_events=10)).events == [
            {"kind": "started"}
        ]
        assert (await client.prompt_text("hello")).stop_reason == "end_turn"
        await client.interrupt()
        assert (await client.detach()).detached is True
    finally:
        await client.close()
        server.close()
        await server.wait_closed()
        if handlers:
            await asyncio.gather(*handlers)

    methods = [method for method, _ in calls]
    assert methods == [
        "_attach",
        "_swarm_status",
        "_agent_detail",
        acp.AGENT_METHODS["session_prompt"],
        acp.AGENT_METHODS["session_cancel"],
        "_detach",
    ]
