from __future__ import annotations

"""Integration tests: Swarm ACP server over a real ACP transport (US3 Addendum).

These tests exercise the concrete JSON-RPC adapter path that binds
:class:`SwarmACPServerSession` and :class:`SwarmACPMux` to an ACP
:class:`acp.connection.Connection` using the production
:class:`ConnectionExternalACPConnection` and :class:`SwarmACPConnection`
helpers.

The goal, per T027.2 in ``specs/009-swarm-acp-mux/tasks.md``, is to
validate at the wire level that:

* reserved requests such as ``_swarm_status``, ``_agent_detail``,
  ``_attach``, and ``_detach`` are decoded and encoded correctly; 
* the ``_attach`` success response is observed *before* any retained or
  live agent updates reach the external client; 
* ordinary ``session/prompt`` and ``session/cancel`` operations reach
  only the currently attached agent; 
* switching and detaching change routing as specified by the mux
  contract; 
* mux/domain failures are surfaced to the client as ACP
  :class:`RequestError` instances carrying the expected logical
  ``MUX_*`` codes in ``error.data``; and 
* connection shutdown leaves no forwarding tasks or Epic 008
  subscriptions active for the external session.

These tests deliberately reuse the real Epic 008 typed update path:

``AcpAgentSession.update_stream``
    → :meth:`NateOhaAcpClient.subscribe_acp_updates`
    → :class:`SwarmACPMux`
    → :class:`ConnectionExternalACPConnection`
    → :class:`SwarmACPConnection` / :class:`acp.connection.Connection`.

No alternative telemetry paths are introduced; all agent output flows
through the typed update layer defined in Epic 008.
"""

import asyncio
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Iterable

import acp
import pytest
import pytest_asyncio
from acp import schema as acp_schema
from acp.agent.router import AGENT_METHODS
from acp.connection import Connection, StreamDirection, StreamEvent

from nate_ntm.config.runtime_config import (
    AdapterKind,
    RuntimeConfig,
    load_runtime_config,
)
from nate_ntm.runtime.acp_client import AcpAgentSession, NateOhaAcpClient
from nate_ntm.runtime.acp_types import SessionNotification
from nate_ntm.runtime.acp_update_stream import (
    AcpSessionUpdateStream,
    ReceivedSessionUpdate,
)
from nate_ntm.runtime.adapters import create_runtime_adapters
from nate_ntm.runtime.daemon import RuntimeDaemon
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.nate_oha_launch import build_effective_nate_oha_config
from nate_ntm.runtime.swarm_acp_mux import SwarmACPMux
from nate_ntm.runtime.swarm_acp_server import (
    ConnectionExternalACPConnection,
    SwarmACPConnection,
    SwarmACPServerSession,
)
from nate_ntm.runtime.swarm_state import AgentState, SwarmState

# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


@dataclass
class RealSwarm:
    """Fixture payload for a REAL nate-oha–backed swarm."""

    config: RuntimeConfig
    store: MetadataStore
    daemon: RuntimeDaemon
    acp_client: NateOhaAcpClient
    agent_a: str
    agent_b: str


@dataclass
class ConnectedSwarm:
    """Fixture payload for an external ACP client connected to the swarm."""

    swarm: RealSwarm
    server: asyncio.AbstractServer
    client: "_RecordingClient"
    mux: SwarmACPMux


def _make_real_echo_config(tmp_path: Path) -> RuntimeConfig:
    """Construct a :class:`RuntimeConfig` for REAL nate-oha echo tests."""

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[3]
    base_config = repo_root / "nate-oha-profiles" / "profile1.json"

    env = dict(os.environ)
    env.update(
        {
            "NATE_NTM_PROJECT_DIR": str(project),
            "NATE_NTM_ADAPTER_MODE": AdapterKind.REAL.value,
            "NATE_NTM_NATE_OHA_CONFIG": str(base_config),
            "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
        }
    )

    return load_runtime_config(project_path=project, env=env)


@pytest_asyncio.fixture
async def real_swarm(tmp_path: Path) -> AsyncIterator[RealSwarm]:
    """Provision a REAL swarm with two nate-oha echo agents.

    The fixture creates durable :class:`AgentState` records, persists a
    :class:`SwarmState`, constructs production adapters, resumes a
    :class:`RuntimeDaemon`, and starts ACP sessions for two agents (A and
    B). All agent output still flows exclusively through the typed
    :class:`AcpSessionUpdateStream` telemetry pipeline.
    """

    config = _make_real_echo_config(tmp_path)
    store = MetadataStore(config=config)
    now = datetime.utcnow()

    agent_a = "swarm-real-a"
    agent_b = "swarm-real-b"

    nate_oha_cfg = build_effective_nate_oha_config(config=config)

    meta_a = AgentState(
        agent_id=agent_a,
        display_name="Swarm Real Agent A",
        conversation_id="",  # Force a new ACP session on first run.
        nate_oha_config=nate_oha_cfg,
    )
    meta_b = AgentState(
        agent_id=agent_b,
        display_name="Swarm Real Agent B",
        conversation_id="",
        nate_oha_config=nate_oha_cfg,
    )

    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id=str(config.project_path),
        created_at=now,
        last_updated_at=now,
        agents={meta_a.agent_id: meta_a, meta_b.agent_id: meta_b},
    )
    store.save_swarm_state(swarm)

    adapters = create_runtime_adapters(config)
    assert isinstance(adapters.acp, NateOhaAcpClient)

    daemon = RuntimeDaemon.resume(config, adapters=adapters)
    acp_client = daemon.acp_client
    assert isinstance(acp_client, NateOhaAcpClient)

    # Start REAL ACP sessions for both agents.
    meta_a_loaded = store.load_agent_state(agent_a)
    meta_b_loaded = store.load_agent_state(agent_b)

    await acp_client.start_agent_async(agent_a, metadata=meta_a_loaded)
    await acp_client.start_agent_async(agent_b, metadata=meta_b_loaded)

    # Mark runtime as running for status/detail views.
    daemon.start()

    swarm_fixture = RealSwarm(
        config=config,
        store=store,
        daemon=daemon,
        acp_client=acp_client,
        agent_a=agent_a,
        agent_b=agent_b,
    )

    try:
        yield swarm_fixture
    finally:
        # Clean shutdown of REAL ACP sessions and runtime. These calls are
        # intentionally tolerant of failures so that test assertions about
        # earlier behaviour are not masked by teardown issues.
        try:
            await acp_client.stop_agent_async(agent_a, timeout=10.0)
        except Exception:
            pass
        try:
            await acp_client.stop_agent_async(agent_b, timeout=10.0)
        except Exception:
            pass

        daemon.request_shutdown()
        daemon.mark_stopped()


@pytest_asyncio.fixture
async def connected_swarm(real_swarm: RealSwarm) -> AsyncIterator[ConnectedSwarm]:
    """Start a Swarm ACP server and external client for a REAL swarm."""

    server, mux_future = await _start_swarm_acp_server_for_daemon(daemon=real_swarm.daemon)
    host, port = server.sockets[0].getsockname()[:2]

    client = await _RecordingClient.connect(host, port)
    mux = await asyncio.wait_for(mux_future, timeout=5.0)

    connected = ConnectedSwarm(
        swarm=real_swarm,
        server=server,
        client=client,
        mux=mux,
    )

    try:
        yield connected
    finally:
        try:
            await client.close()
        finally:
            server.close()
            await server.wait_closed()

        # After transport shutdown the mux must be closed with no active
        # forwarding attachment.
        assert connected.mux._closed is True  # type: ignore[attr-defined]
        assert connected.mux._attachment is None  # type: ignore[attr-defined]


class _RecordingClient:
    """Thin wrapper around :class:`acp.connection.Connection` for tests.

    The client records all `session/update` notifications and a coarse
    event log so that tests can assert on wire-level ordering between the
    `_attach` success response and subsequent forwarded updates.

    It also owns the underlying TCP transport so that tests can shut down
    cleanly without leaking :class:`asyncio.StreamWriter` instances when
    the event loop closes.
    """

    def __init__(
        self,
        conn: Connection,
        writer: asyncio.StreamWriter,
    ) -> None:
        self._conn = conn
        self._writer = writer
        # High-level, decoded notifications and coarse-grained client events.
        self.notifications: list[SessionNotification] = []
        self.events: list[tuple[str, Any]] = []
        # Low-level stream events observed directly at the JSON-RPC layer.
        # Each entry is either:
        #
        #   ("response", result_obj)
        #   ("notification", method, params)
        #
        # captured in the exact order the Connection processes incoming
        # messages. Tests use this to assert wire-level ordering between the
        # `_attach` response and subsequent `session/update` notifications.
        self.stream_events: list[tuple[str, Any]] = []

    @classmethod
    async def connect(cls, host: str, port: int) -> "_RecordingClient":
        reader, writer = await asyncio.open_connection(host, port)

        async def handler(method: str, params: Any | None, is_notification: bool) -> Any:
            if is_notification and method == acp.CLIENT_METHODS["session_update"]:
                notif = SessionNotification.model_validate(params or {})
                self_ref.notifications.append(notif)
                self_ref.events.append(("update", notif))
                return None

            # No server-initiated requests or other notifications are
            # expected in these tests.
            if not is_notification:
                raise acp.RequestError.method_not_found(method)

            return None

        # Use a small receive timeout to ensure tests fail promptly if the
        # server stops responding.
        self_ref: "_RecordingClient"

        def observer(event: StreamEvent) -> None:
            # Capture the exact order of incoming responses and notifications
            # as they cross the JSON-RPC boundary. This is used to assert
            # that the `_attach` success response is observed before any
            # forwarded `session/update` notifications.
            if event.direction is not StreamDirection.INCOMING:
                return
            message = event.message
            if "id" in message and "method" not in message:
                # Ordinary JSON-RPC response.
                self_ref.stream_events.append(("response", message.get("result")))
            elif "method" in message and "id" not in message:
                # Server-initiated notification.
                self_ref.stream_events.append(("notification", message["method"], message.get("params")))

        conn = Connection(
            handler=handler,
            writer=writer,
            reader=reader,
            receive_timeout=5.0,
            observers=[observer],
        )
        self_ref = cls(conn, writer)
        return self_ref

    # ----- High-level ACP operations ---------------------------------

    async def request(self, method: str, params: dict[str, Any] | None = None) -> Any:
        result = await self._conn.send_request(method, params or {})
        # Record only the reserved control responses we care about.
        if method.startswith("_"):
            self.events.append(("reserved_result", method, result))
        elif method in (AGENT_METHODS["session_prompt"], AGENT_METHODS["session_cancel"]):
            self.events.append(("agent_result", method, result))
        return result

    async def notify(self, method: str, params: dict[str, Any] | None = None) -> None:
        await self._conn.send_notification(method, params or {})

    async def attach(self, agent_id: str) -> dict[str, Any]:
        result = await self.request("_attach", {"agent_id": agent_id})
        self.events.append(("attach_result", result))
        return result

    async def detach(self) -> dict[str, Any]:
        return await self.request("_detach", {})

    async def swarm_status(self) -> dict[str, Any]:
        return await self.request("_swarm_status", {})

    async def agent_detail(self, agent_id: str, max_events: int) -> dict[str, Any]:
        return await self.request("_agent_detail", {"agent_id": agent_id, "max_events": max_events})

    async def prompt(self, text: str) -> dict[str, Any]:
        """Send a minimal but valid ACP PromptRequest for this test session."""

        method = AGENT_METHODS["session_prompt"]
        request = acp_schema.PromptRequest(
            session_id="session-1",
            prompt=[acp_schema.TextContentBlock(type="text", text=text)],
        )
        params = request.model_dump(mode="json", by_alias=True, exclude_none=True)
        return await self.request(method, params)

    async def interrupt(self) -> None:
        """Send a minimal but valid ACP CancelNotification for this test session."""

        method = AGENT_METHODS["session_cancel"]
        cancel = acp_schema.CancelNotification(session_id="session-1")
        params = cancel.model_dump(mode="json", by_alias=True, exclude_none=True)
        await self.notify(method, params)

    async def close(self) -> None:
        await self._conn.close()
        # Ensure the underlying TCP transport is closed so that the
        # event loop does not report unclosed StreamWriter instances
        # when pytest tears it down.
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            # During error-path tests the loop may already be shutting
            # down; failures here must not mask the real assertions.
            pass



def _extract_text_from_notifications(client: "_RecordingClient", start: int = 0) -> list[str]:
    """Return any text content values from forwarded session/update notifications.

    This helper inspects the typed :class:`SessionNotification` objects
    recorded by :class:`_RecordingClient` and extracts any text payloads
    embedded in their ``content`` fields. It is intentionally tolerant of
    non-text updates and unknown payload shapes.
    """

    texts: list[str] = []
    for notif in client.notifications[start:]:
        update = notif.update
        try:
            payload = update.model_dump(mode="json", by_alias=True)  # type: ignore[call-arg]
        except Exception:
            continue
        if not isinstance(payload, dict):
            continue
        content = payload.get("content")
        if isinstance(content, dict) and content.get("type") == "text":
            text_val = content.get("text")
            if isinstance(text_val, str):
                texts.append(text_val)
    return texts


async def _wait_for_text(
    client: "_RecordingClient",
    expected: str,
    *,
    start: int,
    timeout: float = 10.0,
) -> None:
    """Wait until ``expected`` appears in forwarded session/update text."""

    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        texts = _extract_text_from_notifications(client, start)
        if any(expected in t for t in texts):
            return
        now_time = loop.time()
        if now_time >= deadline:
            raise AssertionError(
                f"Timed out waiting for {expected!r} in session/update notifications; saw {texts!r}"
            )
        await asyncio.sleep(0.05)




async def _start_swarm_acp_server_for_daemon(
    *,
    daemon: RuntimeDaemon,
) -> tuple[asyncio.AbstractServer, "asyncio.Future[SwarmACPMux]"]:
    """Start a single-connection Swarm ACP server bound to a real daemon.

    This helper mirrors :func:`_start_swarm_acp_server` but reuses the
    production :class:`RuntimeDaemon` and its REAL
    :class:`NateOhaAcpClient`/ACP wiring instead of constructing synthetic
    :class:`AcpAgentSession` instances. It is used by the end-to-end
    macro test that launches real nate-oha processes for two agents and
    exercises attachment + switching over a concrete ACP transport.
    """

    acp_client = daemon.acp_client
    assert isinstance(acp_client, NateOhaAcpClient)

    loop = asyncio.get_running_loop()
    mux_future: asyncio.Future[SwarmACPMux] = loop.create_future()

    async def handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        external = ConnectionExternalACPConnection()
        server_session = SwarmACPServerSession(
            daemon=daemon,  # type: ignore[arg-type]
            agent_client=acp_client,  # type: ignore[arg-type]
            external_connection=external,  # type: ignore[arg-type]
            external_session_id="external-1",
        )
        if not mux_future.done():
            mux_future.set_result(server_session.mux)

        conn = SwarmACPConnection(
            session=server_session,
            writer=writer,
            reader=reader,
            receive_timeout=5.0,
        )
        external.bind(conn)

        async def serve_inbound(sess: SwarmACPServerSession) -> None:
            assert sess is server_session
            await conn.main_loop()

        async def close_transport() -> None:
            try:
                await conn.close()
            finally:
                writer.close()
                await writer.wait_closed()

        await server_session.run_connection(serve_inbound, close_transport=close_transport)

    server = await asyncio.start_server(handle_client, host="127.0.0.1", port=0)
    return server, mux_future


# ---------------------------------------------------------------------------
# T027.2 [US3] Macro-level adapter behaviour over real ACP transport
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swarm_acp_server_transport_attach_prompt_and_detach(connected_swarm: ConnectedSwarm) -> None:
    """Happy-path attach, prompt, interrupt, and detach over REAL ACP transport.

    This scenario validates reserved controls, prompt/interrupt routing,
    and detach semantics using a REAL :class:`RuntimeDaemon` with
    nate-oha echo agents behind :class:`NateOhaAcpClient`.
    """

    swarm = connected_swarm.swarm
    daemon = swarm.daemon
    acp_client = swarm.acp_client
    agent_id = swarm.agent_a
    client = connected_swarm.client
    mux = connected_swarm.mux

    # ------------------------------------------------------------------
    # Reserved controls before attachment
    # ------------------------------------------------------------------

    status = await client.swarm_status()
    expected_swarm = daemon.get_swarm_status()
    assert status["attached_agent_id"] is None
    assert status["swarm"] == expected_swarm

    max_events = 5
    detail = await client.agent_detail(agent_id=agent_id, max_events=max_events)
    expected_detail = daemon.get_agent_detail(agent_id=agent_id, max_events=max_events)
    assert detail["attached"] is False
    assert detail["agent"] == expected_detail["agent"]
    assert detail["events"] == expected_detail["events"]

    # ------------------------------------------------------------------
    # Wrap REAL prompt/interrupt so we can observe routing decisions
    # ------------------------------------------------------------------

    prompt_calls: list[tuple[str, str]] = []
    interrupt_calls: list[str] = []

    orig_prompt = acp_client.prompt
    orig_interrupt = acp_client.interrupt

    async def logging_prompt(agent: str, text: str | None = None) -> str | None:
        value = "" if text is None else text
        prompt_calls.append((agent, value))
        return await orig_prompt(agent, value)  # type: ignore[arg-type]

    async def logging_interrupt(agent: str) -> None:
        interrupt_calls.append(agent)
        await orig_interrupt(agent)  # type: ignore[arg-type]

    acp_client.prompt = logging_prompt  # type: ignore[assignment]
    acp_client.interrupt = logging_interrupt  # type: ignore[assignment]

    try:
        # ------------------------------------------------------------------
        # Attach and send a prompt via the external ACP client
        # ------------------------------------------------------------------

        attach_result = await client.attach(agent_id)
        assert attach_result == {"attached_agent_id": agent_id}
        assert mux.attached_agent_id == agent_id

        prompt_text = "attach-prompt-detach: hello from external client"
        start_idx = len(client.notifications)
        prompt_result = await client.prompt(prompt_text)
        pr = acp_schema.PromptResponse.model_validate(prompt_result)
        assert pr.stop_reason == "end_turn"

        # The adapter surfaces any textual reply in field_meta.swarm_output.
        if pr.field_meta is not None:
            swarm_output = str(pr.field_meta.get("swarm_output", ""))
            # In echo mode we expect the marker to appear somewhere.
            assert prompt_text.split()[0] in swarm_output or prompt_text in swarm_output

        await _wait_for_text(client, prompt_text, start=start_idx, timeout=15.0)

        # The REAL adapter's prompt should have been invoked for the attached agent.
        assert (agent_id, prompt_text) in prompt_calls

        # ------------------------------------------------------------------
        # Interrupt while still attached to agent_id
        # ------------------------------------------------------------------

        await client.interrupt()

        async def _wait_for_interrupt() -> None:
            while not interrupt_calls:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_wait_for_interrupt(), timeout=10.0)
        assert interrupt_calls[-1] == agent_id
    finally:
        # Restore the REAL adapter methods so later tests see unwrapped behaviour.
        acp_client.prompt = orig_prompt  # type: ignore[assignment]
        acp_client.interrupt = orig_interrupt  # type: ignore[assignment]

    # ------------------------------------------------------------------
    # Detach stops forwarding but leaves the REAL agent session intact
    # ------------------------------------------------------------------

    result1 = await client.detach()
    result2 = await client.detach()
    assert result1 == {"detached": True}
    assert result2 == {"detached": True}
    assert mux.attached_agent_id is None

    # After detaching, prompt the same agent directly via the runtime-owned
    # NateOhaAcpClient. The resulting updates should *not* be forwarded to
    # the external ACP client, but the underlying session must remain
    # active from the adapter's perspective.
    session = acp_client._sessions.get(agent_id)  # type: ignore[attr-defined]
    assert isinstance(session, AcpAgentSession)
    baseline_notifications = len(client.notifications)

    direct_text = "attach-prompt-detach: direct-after-detach"
    await acp_client.prompt(agent_id, direct_text)

    # Give the mux a brief window; no new notifications should arrive.
    await asyncio.sleep(0.1)
    assert len(client.notifications) == baseline_notifications

    # The underlying AcpAgentSession remains managed by NateOhaAcpClient.
    assert session.status in {"starting", "running", "waiting"}




@pytest.mark.asyncio
async def test_swarm_acp_server_transport_switching_reroutes_prompt_and_updates(
    connected_swarm: ConnectedSwarm,
) -> None:
    """Attachment switching reroutes both updates and prompt/interrupt.

    This lifts the T027.2 switching scenario onto the REAL runtime path,
    exercising two nate-oha echo agents behind :class:`NateOhaAcpClient`.
    """

    swarm = connected_swarm.swarm
    acp_client = swarm.acp_client
    agent_a = swarm.agent_a
    agent_b = swarm.agent_b
    client = connected_swarm.client
    mux = connected_swarm.mux

    # Wrap REAL prompt/interrupt to log routing decisions while still
    # exercising the nate-oha / ACP SDK path.
    prompt_calls: list[tuple[str, str]] = []
    interrupt_calls: list[str] = []

    orig_prompt = acp_client.prompt
    orig_interrupt = acp_client.interrupt

    async def logging_prompt(agent_id: str, prompt: str | None = None) -> str | None:
        # Accept both raw text and ACP SDK-style content blocks. When the
        # server adapter passes through the PromptRequest ``prompt`` field
        # directly, it may arrive here as a list of TextContentBlock
        # objects; normalise to the first text block's ``text`` value.
        if isinstance(prompt, list) and prompt:
            first = prompt[0]
            text = getattr(first, "text", "")
        else:
            text = "" if prompt is None else prompt
        prompt_calls.append((agent_id, text))
        return await orig_prompt(agent_id, text)  # type: ignore[arg-type]

    async def logging_interrupt(agent_id: str) -> None:
        interrupt_calls.append(agent_id)
        await orig_interrupt(agent_id)  # type: ignore[arg-type]

    acp_client.prompt = logging_prompt  # type: ignore[assignment]
    acp_client.interrupt = logging_interrupt  # type: ignore[assignment]

    try:
        # ------------------------------
        # Attach to agent A and send a prompt
        # ------------------------------
        attach_a = await client.attach(agent_a)
        assert attach_a == {"attached_agent_id": agent_a}
        assert mux.attached_agent_id == agent_a

        prompt_text_a = "switching: hello from agent A via swarm"
        start_idx_a = len(client.notifications)
        await client.prompt(prompt_text_a)
        await _wait_for_text(client, prompt_text_a, start=start_idx_a, timeout=15.0)

        # The REAL adapter's prompt should have been invoked for agent A.
        assert (agent_a, prompt_text_a) in prompt_calls

        # At the JSON-RPC layer, the `_attach` success response must still be
        # observed before any forwarded `session/update` notifications.
        attach_response_index: int | None = None
        first_update_index: int | None = None
        for i, event in enumerate(client.stream_events):
            kind = event[0]
            if kind == "response" and attach_response_index is None:
                result = event[1]
                if isinstance(result, dict) and result.get("attached_agent_id") == agent_a:
                    attach_response_index = i
            elif kind == "notification" and first_update_index is None:
                method = event[1]
                if method == acp.CLIENT_METHODS["session_update"]:
                    first_update_index = i
            if attach_response_index is not None and first_update_index is not None:
                break

        assert attach_response_index is not None
        assert first_update_index is not None
        assert first_update_index > attach_response_index

        # ------------------------------
        # Switch attachment to agent B and send another prompt
        # ------------------------------
        attach_b = await client.attach(agent_b)
        assert attach_b == {"attached_agent_id": agent_b}
        assert mux.attached_agent_id == agent_b

        prompt_text_b = "switching: hello from agent B via swarm"
        start_idx_b = len(client.notifications)
        await client.prompt(prompt_text_b)
        await _wait_for_text(client, prompt_text_b, start=start_idx_b, timeout=15.0)

        # Prompts should have been routed to A then B in order.
        assert (agent_a, prompt_text_a) in prompt_calls
        assert (agent_b, prompt_text_b) in prompt_calls

        # ------------------------------
        # Interrupt while attached to B
        # ------------------------------
        await client.interrupt()

        async def _wait_for_interrupt() -> None:
            while not interrupt_calls:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_wait_for_interrupt(), timeout=10.0)
        # The last interrupt must target the currently attached agent (B).
        assert interrupt_calls[-1] == agent_b
    finally:
        acp_client.prompt = orig_prompt  # type: ignore[assignment]
        acp_client.interrupt = orig_interrupt  # type: ignore[assignment]


@pytest.mark.asyncio
async def test_swarm_acp_server_transport_error_mapping_and_shutdown(
    connected_swarm: ConnectedSwarm,
) -> None:
    """Mux/domain failures surface as ACP errors with logical mux codes.

    An invalid `_attach` request for an unknown agent must result in an
    ACP :class:`RequestError` whose ``error.data.mux_code`` matches the
    mux session contract.
    """

    client = connected_swarm.client

    # Attempt to attach to an unknown agent; this should map the
    # underlying UnknownAgentError to MUX_UNKNOWN_AGENT and surface an
    # ACP RequestError with that logical code in error.data.
    with pytest.raises(acp.RequestError) as exc_info:
        await client.attach("missing-agent")

    err = exc_info.value
    assert isinstance(err.data, dict)
    assert err.data.get("mux_code") == "MUX_UNKNOWN_AGENT"


# ---------------------------------------------------------------------------
# Epic 009 real-path integration: Swarm ACP server over REAL runtime
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swarm_acp_server_real_runtime_two_agents_switch_and_shutdown(tmp_path: Path) -> None:
    """End-to-end Swarm ACP server over REAL nate-oha echo agents.

    This test lifts the T027.2 transport assertions into the full Epic 009
    real-path scenario:

    * A REAL :class:`RuntimeDaemon` is constructed in ``adapter_mode=REAL``
      with two nate-oha echo agents in durable swarm state.
    * The REAL :class:`NateOhaAcpClient` starts ACP sessions for both agents
      via :meth:`start_agent_async`, wiring typed
      :class:`AcpSessionUpdateStream` telemetry from nate-oha into the
      runtime.
    * A single-connection Swarm ACP server is started using the production
      :class:`SwarmACPServerSession`, :class:`ConnectionExternalACPConnection`,
      and :class:`SwarmACPConnection` helpers.
    * An external ACP client attaches first to agent A, then switches to
      agent B, sending real ``session/prompt`` and ``session/cancel``
      operations over JSON-RPC.
    * Agent-produced typed updates flow through the canonical Epic 008
      pipeline into :class:`SwarmACPMux` and are forwarded as
      ``session/update`` notifications to the external client.
    * Prompt and interrupt routing is observed via thin logging wrappers
      around the REAL :class:`NateOhaAcpClient` methods (which still call the
      underlying ACP SDK), and connection/runtime cleanup is asserted at the
      end of the test.
    """

    # ------------------------------
    # REAL runtime + swarm metadata
    # ------------------------------

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[3]
    base_config = repo_root / "nate-oha-profiles" / "profile1.json"

    env = dict(os.environ)
    env.update(
        {
            "NATE_NTM_PROJECT_DIR": str(project),
            "NATE_NTM_ADAPTER_MODE": AdapterKind.REAL.value,
            "NATE_NTM_NATE_OHA_CONFIG": str(base_config),
            "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
        }
    )

    config = load_runtime_config(project_path=project, env=env)

    store = MetadataStore(config=config)
    now = datetime(2026, 7, 3, 12, 0, 0)

    agent_a = "swarm-real-a"
    agent_b = "swarm-real-b"

    nate_oha_cfg = build_effective_nate_oha_config(config=config)

    meta_a = AgentState(
        agent_id=agent_a,
        display_name="Swarm Real Agent A",
        conversation_id="",  # Force a new ACP session on first run.
        nate_oha_config=nate_oha_cfg,
    )
    meta_b = AgentState(
        agent_id=agent_b,
        display_name="Swarm Real Agent B",
        conversation_id="",
        nate_oha_config=nate_oha_cfg,
    )

    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id=str(config.project_path),
        created_at=now,
        last_updated_at=now,
        agents={meta_a.agent_id: meta_a, meta_b.agent_id: meta_b},
    )
    store.save_swarm_state(swarm)

    adapters = create_runtime_adapters(config)
    assert isinstance(adapters.acp, NateOhaAcpClient)
    adapters.acp.executable = "nate-oha"  # type: ignore[attr-defined]

    daemon = RuntimeDaemon.resume(config, adapters=adapters)
    acp_client = daemon.acp_client
    assert isinstance(acp_client, NateOhaAcpClient)

    # ------------------------------
    # Start REAL ACP sessions for both agents
    # ------------------------------

    meta_a_loaded = store.load_agent_state(agent_a)
    meta_b_loaded = store.load_agent_state(agent_b)

    await acp_client.start_agent_async(agent_a, metadata=meta_a_loaded)
    await acp_client.start_agent_async(agent_b, metadata=meta_b_loaded)

    # ------------------------------
    # Start Swarm ACP server bound to the REAL daemon
    # ------------------------------

    server, mux_future = await _start_swarm_acp_server_for_daemon(daemon=daemon)
    host, port = server.sockets[0].getsockname()[:2]

    try:
        client = await _RecordingClient.connect(host, port)
        mux = await asyncio.wait_for(mux_future, timeout=5.0)

        # Wrap REAL prompt/interrupt to log routing decisions while still
        # exercising the nate-oha / ACP SDK path.
        prompt_calls: list[tuple[str, str]] = []
        interrupt_calls: list[str] = []

        orig_prompt = acp_client.prompt
        orig_interrupt = acp_client.interrupt

        async def logging_prompt(agent_id: str, prompt: str | None = None) -> str | None:
            # Accept both raw text and ACP SDK-style content blocks. When the
            # server adapter passes through the PromptRequest ``prompt`` field
            # directly, it arrives here as a list of TextContentBlock objects;
            # for logging and delegation we normalise this to the first text
            # block's ``text`` value.
            if isinstance(prompt, list) and prompt:
                first = prompt[0]
                text = getattr(first, "text", "")
            else:
                text = "" if prompt is None else prompt
            prompt_calls.append((agent_id, text))
            # Delegate to the REAL NateOhaAcpClient implementation using the
            # normalised text value.
            return await orig_prompt(agent_id, text)  # type: ignore[arg-type]

        async def logging_interrupt(agent_id: str) -> None:
            interrupt_calls.append(agent_id)
            await orig_interrupt(agent_id)  # type: ignore[arg-type]

        acp_client.prompt = logging_prompt  # type: ignore[assignment]
        acp_client.interrupt = logging_interrupt  # type: ignore[assignment]

        def _extract_text_from_notifications(start: int = 0) -> list[str]:
            texts: list[str] = []
            for notif in client.notifications[start:]:
                update = notif.update
                try:
                    payload = update.model_dump(mode="json", by_alias=True)  # type: ignore[call-arg]
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                content = payload.get("content")
                if isinstance(content, dict) and content.get("type") == "text":
                    text_val = content.get("text")
                    if isinstance(text_val, str):
                        texts.append(text_val)
            return texts

        async def _wait_for_text(expected: str, *, start: int, timeout: float = 10.0) -> None:
            """Wait until ``expected`` appears in forwarded session/update text."""

            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while True:
                texts = _extract_text_from_notifications(start)
                if any(expected in t for t in texts):
                    return
                now_time = loop.time()
                if now_time >= deadline:
                    raise AssertionError(
                        f"Timed out waiting for {expected!r} in session/update notifications; "
                        f"saw {texts!r}"
                    )
                await asyncio.sleep(0.05)

        # ------------------------------
        # Attach to agent A and send a prompt
        # ------------------------------

        attach_a = await client.attach(agent_a)
        assert attach_a == {"attached_agent_id": agent_a}
        assert mux.attached_agent_id == agent_a

        prompt_text_a = "hello from agent A via swarm"
        start_idx_a = len(client.notifications)
        await client.prompt(prompt_text_a)
        await _wait_for_text(prompt_text_a, start=start_idx_a, timeout=15.0)

        # The REAL adapter's prompt should have been invoked for agent A.
        assert (agent_a, prompt_text_a) in prompt_calls

        # At the JSON-RPC layer, the `_attach` success response must still be
        # observed before any forwarded `session/update` notifications, even
        # when using REAL nate-oha processes behind the mux.
        attach_response_index: int | None = None
        first_update_index: int | None = None
        for i, event in enumerate(client.stream_events):
            kind = event[0]
            if kind == "response" and attach_response_index is None:
                result = event[1]
                if isinstance(result, dict) and result.get("attached_agent_id") == agent_a:
                    attach_response_index = i
            elif kind == "notification" and first_update_index is None:
                method = event[1]
                if method == acp.CLIENT_METHODS["session_update"]:
                    first_update_index = i
            if attach_response_index is not None and first_update_index is not None:
                break

        assert attach_response_index is not None
        assert first_update_index is not None
        assert first_update_index > attach_response_index

        # ------------------------------
        # Switch attachment to agent B and send another prompt
        # ------------------------------

        attach_b = await client.attach(agent_b)
        assert attach_b == {"attached_agent_id": agent_b}
        assert mux.attached_agent_id == agent_b

        prompt_text_b = "hello from agent B via swarm"
        start_idx_b = len(client.notifications)
        await client.prompt(prompt_text_b)
        await _wait_for_text(prompt_text_b, start=start_idx_b, timeout=15.0)

        # Prompts should have been routed to A then B in order.
        assert (agent_a, prompt_text_a) in prompt_calls
        assert (agent_b, prompt_text_b) in prompt_calls

        # ------------------------------
        # Interrupt while attached to B
        # ------------------------------

        await client.interrupt()

        async def _wait_for_interrupt() -> None:
            while not interrupt_calls:
                await asyncio.sleep(0.05)

        await asyncio.wait_for(_wait_for_interrupt(), timeout=10.0)
        # The last interrupt must target the currently attached agent (B).
        assert interrupt_calls[-1] == agent_b

        await client.close()
    finally:
        server.close()
        await server.wait_closed()

    # After connection shutdown the mux must be closed with no active
    # forwarding attachment.
    assert mux._closed is True  # type: ignore[attr-defined]
    assert mux._attachment is None  # type: ignore[attr-defined]

    # ------------------------------
    # Clean shutdown of REAL ACP sessions
    # ------------------------------

    try:
        await acp_client.stop_agent_async(agent_a, timeout=10.0)
        await acp_client.stop_agent_async(agent_b, timeout=10.0)
    finally:
        # Ensure session records reflect termination and no active
        # subscription context remains for either agent.
        session_a = acp_client._sessions.get(agent_a)  # type: ignore[attr-defined]
        session_b = acp_client._sessions.get(agent_b)  # type: ignore[attr-defined]
        if session_a is not None:
            assert session_a.status == "terminated"
        if session_b is not None:
            assert session_b.status == "terminated"

        assert agent_a not in acp_client._session_contexts  # type: ignore[attr-defined]
        assert agent_b not in acp_client._session_contexts  # type: ignore[attr-defined]

