from __future__ import annotations

"""Unit tests for :mod:`nate_ntm.runtime.swarm_acp_server` (US1/US2).

These tests exercise the production Swarm ACP server adapter surface in
``src/nate_ntm/runtime/swarm_acp_server.py``. For User Story 2 they
focus on:

* routing of reserved swarm-control operations through :class:`SwarmACPMux`;
* ensuring reserved controls are never forwarded to an attached agent;
* treating underscore-prefixed agent output as ordinary updates; and
* mapping mux/domain failures to stable logical ``MUX_*`` error codes.

The tests deliberately reuse the in-memory fakes from
``test_swarm_acp_mux.py`` so that both the mux and adapter are exercised
against the same synthetic Epic 008 stream implementation.
"""

import asyncio
import logging
from typing import Mapping

import pytest

from nate_ntm.runtime.acp_update_stream import AcpSessionUpdateStream, AgentSessionNotActive
from nate_ntm.runtime.swarm_acp_mux import (
    NoAttachedAgentError,
    StaleAttachmentError,
    SwarmACPMuxClosedError,
    UnknownAgentError,
    UnsupportedReservedUpdateError,
)
from nate_ntm.runtime.swarm_acp_server import SwarmACPServerSession

from .test_swarm_acp_mux import (
    _DummyUpdate,
    _FakeAgentClient,
    _FakeDaemon,
    _FakeExternalConnection,
    _anext_with_timeout,
    _publish,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_session(
    *,
    durable_agents: Mapping[str, object] | None = None,
    agent_client: _FakeAgentClient | None = None,
    external: _FakeExternalConnection | None = None,
) -> SwarmACPServerSession:
    """Construct a SwarmACPServerSession wired to the standard fakes."""

    agent_ids = list(durable_agents.keys()) if durable_agents is not None else []
    daemon = _FakeDaemon(agent_ids=agent_ids)
    if agent_client is None:
        agent_client = _FakeAgentClient()
    if external is None:
        external = _FakeExternalConnection()

    return SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=agent_client,
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="session-1",
    )


# ---------------------------------------------------------------------------
# T017 [US2] Reserved-control routing and error mapping
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swarm_status_routed_through_mux_and_does_not_call_agent() -> None:
    daemon = _FakeDaemon(agent_ids=["agent-1"], swarm_status={"status": "ok"})
    agent_client = _FakeAgentClient()
    external = _FakeExternalConnection()

    session = SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=agent_client,
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="session-1",
    )

    result = await session.handle_reserved_control({"op": "_swarm_status", "payload": {}})

    # Adapter should simply reuse the mux-level view.
    assert result == {"attached_agent_id": None, "swarm": daemon.get_swarm_status()}

    # Reserved operations must not be forwarded to the agent.
    assert agent_client.prompts == []
    assert agent_client.interrupts == []


@pytest.mark.asyncio
async def test_agent_detail_routed_through_mux_and_preserves_max_events() -> None:
    agent_id = "agent-1"
    agent_detail = {
        "agent": {"agent_id": agent_id},
        "events": ["e1", "e2", "e3", "e4"],
    }

    daemon = _FakeDaemon(agent_ids=[agent_id], agent_details={agent_id: agent_detail})
    agent_client = _FakeAgentClient()
    external = _FakeExternalConnection()

    session = SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=agent_client,
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="session-1",
    )

    result = await session.handle_reserved_control(
        {"op": "_agent_detail", "payload": {"agent_id": agent_id, "max_events": 2}}
    )

    # Adapter should reuse mux-level view semantics and honour max_events.
    assert result["agent"] is agent_detail["agent"]
    assert result["events"] == agent_detail["events"][:2]
    assert daemon.max_events_calls == [(agent_id, 2)]

    # Reserved operations must not be forwarded to the agent.
    assert agent_client.prompts == []
    assert agent_client.interrupts == []


@pytest.mark.asyncio
async def test_handle_reserved_control_rejects_attach_and_requires_explicit_adapter_path() -> None:
    agent_id = "agent-1"
    stream = AcpSessionUpdateStream()
    agent_client = _FakeAgentClient()
    agent_client.add_agent(agent_id, stream=stream)
    daemon = _FakeDaemon(agent_ids=[agent_id])
    external = _FakeExternalConnection()

    session = SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=agent_client,
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="session-1",
    )

    with pytest.raises(UnsupportedReservedUpdateError) as exc_info:
        await session.handle_reserved_control({"op": "_attach", "payload": {"agent_id": agent_id}})

    code = SwarmACPServerSession.map_mux_error(exc_info.value)
    assert code == "MUX_INVALID_REQUEST"


@pytest.mark.asyncio
async def test_attach_helper_runs_ack_before_forwarding_and_attaches_agent() -> None:
    agent_id = "agent-1"
    stream = AcpSessionUpdateStream()
    agent_client = _FakeAgentClient()
    agent_client.add_agent(agent_id, stream=stream)
    daemon = _FakeDaemon(agent_ids=[agent_id])
    external = _FakeExternalConnection()

    session = SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=agent_client,
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="session-1",
    )

    ack_payloads: list[dict[str, str]] = []

    async def acknowledge(attached_id: str) -> None:
        # Simulate writing a success response while verifying that no
        # updates have been forwarded yet, even if new updates are
        # published during acknowledgment.
        assert external.calls == []
        ack_payloads.append({"attached_agent_id": attached_id})
        u1 = _publish(stream, "u1")
        # Give the stream a moment to deliver `u1` to the mux's
        # internal subscription while forwarding remains gated.
        await asyncio.sleep(0.05)
        assert external.calls == []
        # Silence unused-variable warnings.
        assert u1 is not None

    await session.attach(agent_id, acknowledge=acknowledge)

    assert ack_payloads == [{"attached_agent_id": agent_id}]
    assert session.mux.attached_agent_id == agent_id

    # After `attach` completes, forwarding must have been activated
    # and the previously published update delivered.
    await external.wait_for_calls(1)
    forwarded = [u for (_sid, u) in external.calls]
    assert getattr(forwarded[0], "label", None) == "u1"


@pytest.mark.asyncio
async def test_detach_reserved_control_is_idempotent_and_returns_detached_true() -> None:
    agent_id = "agent-1"
    stream = AcpSessionUpdateStream()
    agent_client = _FakeAgentClient()
    agent_client.add_agent(agent_id, stream=stream)
    daemon = _FakeDaemon(agent_ids=[agent_id])
    external = _FakeExternalConnection()

    session = SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=agent_client,
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="session-1",
    )

    # First detach without any prior attachment.
    result1 = await session.handle_reserved_control({"op": "_detach", "payload": {}})
    assert result1 == {"detached": True}

    # Attach then detach again; detach must remain idempotent.
    ack_called = asyncio.Event()

    async def acknowledge(agent: str) -> None:
        assert agent == agent_id
        ack_called.set()

    await session.attach(agent_id, acknowledge=acknowledge)
    assert ack_called.is_set()

    result2 = await session.handle_reserved_control({"op": "_detach", "payload": {}})
    assert result2 == {"detached": True}
    assert session.mux.attached_agent_id is None


@pytest.mark.asyncio
async def test_malformed_reserved_request_maps_to_mux_invalid_request() -> None:
    daemon = _FakeDaemon(agent_ids=["agent-1"])
    agent_client = _FakeAgentClient()
    external = _FakeExternalConnection()

    session = SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=agent_client,
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="session-1",
    )

    # Missing `op` field.
    with pytest.raises(Exception) as exc_info:
        await session.handle_reserved_control({"payload": {}})

    code = SwarmACPServerSession.map_mux_error(exc_info.value)
    assert code == "MUX_INVALID_REQUEST"


@pytest.mark.asyncio
async def test_unknown_reserved_operation_maps_to_mux_invalid_request() -> None:
    daemon = _FakeDaemon(agent_ids=["agent-1"])
    agent_client = _FakeAgentClient()
    external = _FakeExternalConnection()

    session = SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=agent_client,
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="session-1",
    )

    with pytest.raises(UnsupportedReservedUpdateError) as exc_info:
        await session.handle_reserved_control({"op": "_unknown", "payload": {}})

    code = SwarmACPServerSession.map_mux_error(exc_info.value)
    assert code == "MUX_INVALID_REQUEST"


def test_map_mux_error_codes_for_known_domain_errors() -> None:
    assert SwarmACPServerSession.map_mux_error(SwarmACPMuxClosedError("closed")) == "MUX_CLOSED"
    assert (
        SwarmACPServerSession.map_mux_error(NoAttachedAgentError("no attachment"))
        == "MUX_NO_ATTACHED_AGENT"
    )
    assert (
        SwarmACPServerSession.map_mux_error(UnknownAgentError("unknown")) == "MUX_UNKNOWN_AGENT"
    )
    assert (
        SwarmACPServerSession.map_mux_error(AgentSessionNotActive("inactive"))
        == "MUX_AGENT_SESSION_NOT_ACTIVE"
    )
    assert (
        SwarmACPServerSession.map_mux_error(StaleAttachmentError("stale")) == "MUX_STALE_ATTACHMENT"
    )
    # Unsupported reserved operations and parse/validation errors map to MUX_INVALID_REQUEST.
    assert (
        SwarmACPServerSession.map_mux_error(UnsupportedReservedUpdateError("bad op"))
        == "MUX_INVALID_REQUEST"
    )
    assert SwarmACPServerSession.map_mux_error(ValueError("bad payload")) == "MUX_INVALID_REQUEST"


def test_map_mux_error_logs_and_maps_unexpected_errors_to_internal_error(caplog: pytest.LogCaptureFixture) -> None:
    err = RuntimeError("boom")

    with caplog.at_level(logging.ERROR):
        code = SwarmACPServerSession.map_mux_error(err)

    assert code == "MUX_INTERNAL_ERROR"
    # The log should mention that an unhandled adapter error occurred.
    assert any("Unhandled error in SwarmACPServerSession" in record.getMessage() for record in caplog.records)


@pytest.mark.asyncio
async def test_reserved_operations_never_call_prompt_or_interrupt() -> None:
    agent_id = "agent-1"
    stream = AcpSessionUpdateStream()
    agent_client = _FakeAgentClient()
    agent_client.add_agent(agent_id, stream=stream)
    dummy_detail = {"agent": {"agent_id": agent_id}, "events": []}
    daemon = _FakeDaemon(agent_ids=[agent_id], agent_details={agent_id: dummy_detail})
    external = _FakeExternalConnection()

    session = SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=agent_client,
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="session-1",
    )

    # Establish an attachment so that agent-directed operations would be valid.
    ack_called = asyncio.Event()

    async def acknowledge(agent: str) -> None:
        assert agent == agent_id
        ack_called.set()

    await session.attach(agent_id, acknowledge=acknowledge)
    assert ack_called.is_set()

    # Clear any prompt/interrupt calls recorded during attachment.
    agent_client.prompts.clear()
    agent_client.interrupts.clear()

    # Invoke each reserved operation; none must route to the agent client.
    await session.handle_reserved_control({"op": "_swarm_status", "payload": {}})
    await session.handle_reserved_control({"op": "_agent_detail", "payload": {"agent_id": agent_id}})
    await session.handle_reserved_control({"op": "_detach", "payload": {}})

    assert agent_client.prompts == []
    assert agent_client.interrupts == []


@pytest.mark.asyncio
async def test_underscore_prefixed_agent_output_is_forwarded_unchanged() -> None:
    agent_id = "agent-1"
    stream = AcpSessionUpdateStream()
    agent_client = _FakeAgentClient()
    agent_client.add_agent(agent_id, stream=stream)
    daemon = _FakeDaemon(agent_ids=[agent_id])
    external = _FakeExternalConnection()

    session = SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=agent_client,
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="session-1",
    )

    ack_called = asyncio.Event()

    async def acknowledge(agent: str) -> None:
        assert agent == agent_id
        ack_called.set()

    await session.attach(agent_id, acknowledge=acknowledge)
    assert ack_called.is_set()

    # Publish two updates, one of which looks like a reserved operation
    # name. From the mux/adapter's perspective this is ordinary agent
    # output and must be forwarded unchanged.
    special = _publish(stream, "_looks_like_reserved")
    normal = _publish(stream, "normal")

    await external.wait_for_calls(2)
    forwarded = [u for (_sid, u) in external.calls]

    assert forwarded[0] is special
    assert forwarded[1] is normal

    assert getattr(forwarded[0], "label", None) == "_looks_like_reserved"
    assert getattr(forwarded[1], "label", None) == "normal"


# ---------------------------------------------------------------------------
# T023 [US3] Adapter lifetime and first-completion race
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_run_connection_normal_inbound_completion_cancels_failure_watcher_and_closes_mux_and_transport() -> None:
    """Normal inbound completion wins the race and cancels the watcher.

    When inbound processing completes successfully, the failure watcher must be
    cancelled and awaited, the mux must be closed, and the concrete transport
    close callback must be invoked exactly once.
    """

    session = _make_session(durable_agents={})

    # Start an independent failure watcher so we can observe its cancellation
    # behaviour when the connection shuts down.
    external_watcher = asyncio.create_task(session.mux.wait_failed())
    await asyncio.sleep(0)
    assert not external_watcher.done()

    inbound_calls: list[str] = []

    async def serve_inbound(s: SwarmACPServerSession) -> None:
        assert s is session
        inbound_calls.append("start")
        # Normal completion without error.

    close_calls: list[str] = []

    async def close_transport() -> None:
        close_calls.append("closed")

    await session.run_connection(serve_inbound, close_transport=close_transport)

    assert inbound_calls == ["start"]

    # After the connection ends, the mux must be closed and the transport
    # close callback must have been invoked exactly once.
    assert session.mux._closed is True  # type: ignore[attr-defined]
    assert close_calls == ["closed"]

    # ``wait_failed`` watchers must be cancelled when the mux is closed.
    assert session.mux._failure.cancelled()  # type: ignore[attr-defined]
    with pytest.raises(asyncio.CancelledError):
        await external_watcher


@pytest.mark.asyncio
async def test_run_connection_inbound_failure_cancels_failure_watcher_and_closes_mux_and_transport() -> None:
    """Inbound failure wins the race and cancels the watcher.

    When inbound processing fails, its exception must be propagated, the
    failure watcher must be cancelled and awaited, the mux closed, and the
    transport close callback invoked.
    """

    session = _make_session(durable_agents={})

    # Independent watcher to observe cancellation behaviour.
    external_watcher = asyncio.create_task(session.mux.wait_failed())
    await asyncio.sleep(0)
    assert not external_watcher.done()

    inbound_error = RuntimeError("inbound failure")

    async def serve_inbound(s: SwarmACPServerSession) -> None:
        assert s is session
        raise inbound_error

    close_calls: list[str] = []

    async def close_transport() -> None:
        close_calls.append("closed")

    with pytest.raises(RuntimeError) as exc_info:
        await session.run_connection(serve_inbound, close_transport=close_transport)

    assert exc_info.value is inbound_error
    assert session.mux._closed is True  # type: ignore[attr-defined]
    assert close_calls == ["closed"]

    # ``wait_failed`` watchers (including the independent one) must be
    # cancelled when the mux is closed as part of connection teardown.
    assert session.mux._failure.cancelled()  # type: ignore[attr-defined]
    with pytest.raises(asyncio.CancelledError):
        await external_watcher


@pytest.mark.asyncio
async def test_run_connection_forwarding_failure_cancels_inbound_and_closes_mux_and_transport() -> None:
    """Forwarding failure wins the race and cancels inbound processing.

    When forwarding to the external connection fails, the mux must report the
    failure via :meth:`wait_failed`, run_connection must cancel inbound
    processing, propagate the failure to the caller, close the mux, and invoke
    the transport close callback.
    """

    agent_id = "agent-1"
    stream = AcpSessionUpdateStream()
    agent_client = _FakeAgentClient()
    agent_client.add_agent(agent_id, stream=stream)

    class _FailingExternal(_FakeExternalConnection):
        def __init__(self) -> None:  # pragma: no cover - trivial initialiser
            super().__init__()
            self.failures: list[BaseException] = []

        async def session_update(self, *, session_id: str, update: _DummyUpdate) -> None:  # type: ignore[override]
            exc = RuntimeError("synthetic forwarding failure")
            self.failures.append(exc)
            raise exc

    external = _FailingExternal()
    session = _make_session(durable_agents={agent_id: object()}, agent_client=agent_client, external=external)

    inbound_started = asyncio.Event()
    inbound_cancelled = asyncio.Event()

    async def serve_inbound(s: SwarmACPServerSession) -> None:
        assert s is session
        inbound_started.set()

        async def acknowledge(attached_id: str) -> None:
            assert attached_id == agent_id
            # Immediate acknowledgment; no-op for the test.
            return None

        await s.attach(agent_id, acknowledge=acknowledge)

        # Publish an update that will trigger a forwarding failure in the mux's
        # forwarding task.
        _publish(stream, "u1")

        # Block until cancelled by run_connection.
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            inbound_cancelled.set()
            raise

    close_called = asyncio.Event()

    async def close_transport() -> None:
        close_called.set()

    with pytest.raises(RuntimeError) as exc_info:
        await session.run_connection(serve_inbound, close_transport=close_transport)

    # The propagated error must be the synthetic forwarding failure raised by
    # the external connection.
    assert "synthetic forwarding failure" in str(exc_info.value)
    assert external.failures, "Expected at least one recorded forwarding failure"

    # Inbound processing must have started and then been cancelled by the
    # run_connection race winner.
    assert inbound_started.is_set()
    assert inbound_cancelled.is_set()

    # Cleanup must close both mux and transport.
    assert close_called.is_set()
    assert session.mux._closed is True  # type: ignore[attr-defined]

