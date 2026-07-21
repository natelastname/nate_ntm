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
