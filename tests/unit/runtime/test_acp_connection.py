from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import pytest

from nate_ntm.runtime.acp_connection import open_nate_oha_acp_client
from nate_ntm.runtime.acp_protocol_client import NateNtmAcpProtocolClient
from nate_ntm.runtime.events import AgentEventSource


class DummyProcess:
    def __init__(self) -> None:
        self.terminated = False


@pytest.mark.asyncio
async def test_open_nate_oha_acp_client_wires_spawn_and_client_side(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Helper uses spawn_stdio_transport + ClientSideConnection wiring.

    This test exercises the high-level composition without requiring a
    real nate-oha binary or ACP server. It stubs the ACP SDK's
    ``spawn_stdio_transport`` and ``ClientSideConnection`` helpers and
    verifies that:

    * the subprocess command/env/cwd are passed through correctly;
    * a :class:`NateNtmAcpProtocolClient` instance is constructed for the
      given ``agent_id``;
    * the returned connection and process objects are those produced by
      the stubs; and
    * exiting the context closes the client connection and triggers the
      stubbed subprocess shutdown path.
    """

    events: list[Any] = []

    def _event_sink(event: Any) -> None:
        events.append(event)

    dummy_process = DummyProcess()
    captured: dict[str, Any] = {}

    @asynccontextmanager
    async def fake_spawn_stdio_transport(
        command: str,
        *args: str,
        env: dict[str, str] | None = None,
        cwd: Path | None = None,
        **_kwargs: Any,
    ):
        captured["command"] = command
        captured["args"] = list(args)
        captured["env"] = env
        captured["cwd"] = cwd

        # The concrete types of reader/writer do not matter here because
        # ClientSideConnection is stubbed below; we use simple sentinel
        # objects.
        reader = object()
        writer = object()

        try:
            yield reader, writer, dummy_process
        finally:
            dummy_process.terminated = True

    class DummyConnection:
        """Minimal stand-in for acp.client.ClientSideConnection.

        The real implementation wires JSON-RPC framing and exposes
        high-level request helpers. For this test we only care that the
        runtime passes the expected client instance and streams through
        to the constructor and that ``close`` is awaited on teardown.
        """

        def __init__(self, client: Any, writer: Any, reader: Any, **kwargs: Any) -> None:
            self.client = client
            self.writer = writer
            self.reader = reader
            self.kwargs = kwargs
            self.closed = False

        async def close(self) -> None:  # pragma: no cover - trivial
            self.closed = True

    # Patch the ACP helpers used by open_nate_oha_acp_client.
    import acp
    import acp.client as acp_client_module

    monkeypatch.setattr(acp, "spawn_stdio_transport", fake_spawn_stdio_transport, raising=True)
    monkeypatch.setattr(acp_client_module, "ClientSideConnection", DummyConnection, raising=True)

    command = ["nate-oha", "acp", "--config", "conf.json"]
    env = {"FOO": "bar"}

    async with open_nate_oha_acp_client(
        command=command,
        env=env,
        cwd=tmp_path,
        agent_id="agent-1",
        event_sink=_event_sink,
    ) as (connection, process, protocol_client):
        # The helper should have constructed our dummy connection and
        # returned the dummy process instance created above.
        assert isinstance(connection, DummyConnection)
        assert process is dummy_process
        assert isinstance(protocol_client, NateNtmAcpProtocolClient)

        # The protocol client should be bound to the requested agent id.
        # We rely on the implementation detail that the agent id is
        # stored on a private attribute; this is acceptable for focused
        # unit tests.
        assert getattr(protocol_client, "_agent_id") == "agent-1"

        # Exercise the event path once to ensure the sink is wired up.
        await protocol_client.session_update("session-123", {"foo": "bar"})

    # After the context exits, the dummy connection should have been
    # closed and the fake subprocess teardown path invoked.
    assert connection.closed
    assert dummy_process.terminated

    # The event emitted by ``session_update`` should have been routed
    # through to the sink and carry basic ACP metadata.
    assert len(events) == 1
    event = events[0]
    assert event.agent_id == "agent-1"
    assert event.source is AgentEventSource.ACP
    assert event.payload["session_id"] == "session-123"
    assert event.payload["update"] == {"foo": "bar"}

    # Finally, verify that the subprocess helper was invoked with the
    # expected command, arguments, environment, and working directory.
    assert captured["command"] == "nate-oha"
    assert captured["args"] == ["acp", "--config", "conf.json"]
    assert captured["env"] is env
    assert captured["cwd"] == tmp_path
