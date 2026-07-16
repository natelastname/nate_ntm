"""Unit tests for the JSON-RPC dispatcher helpers.

These tests exercise the transport-agnostic dispatcher defined in
``src/nate_ntm/api/jsonrpc.py`` and ensure that JSON-RPC request objects
are mapped correctly onto :class:`RuntimeApiServer` handlers with the
expected success and error envelopes.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from nate_ntm.api.jsonrpc import JSONRPC_VERSION, dispatch_request
from nate_ntm.api.server import RuntimeApiServer
from nate_ntm.config.runtime_config import load_runtime_config
from nate_ntm.runtime.daemon import RuntimeDaemon, StartupMode
from nate_ntm.runtime.events import AgentEvent, AgentEventSource, AgentEventStream
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.swarm_state import AgentState, SwarmState
from nate_ntm.runtime.state import AgentRuntimeState, AgentStatus, RuntimeState, RuntimeStatus


def _make_daemon(tmp_path: Path) -> RuntimeDaemon:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    config = load_runtime_config(project_path=project)

    # Construct a minimal in-memory daemon without pre-existing on-disk state.
    store = MetadataStore(config=config)
    state = RuntimeState(config=config)

    now = datetime(2026, 7, 3, 12, 0, 0)
    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
    )

    return RuntimeDaemon(
        config=config,
        metadata_store=store,
        swarm_state=swarm,
        state=state,
        startup_mode=StartupMode.RESUME,
    )


def test_runtime_get_status_jsonrpc_envelope(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)
    daemon.state.status = RuntimeStatus.RUNNING

    server = RuntimeApiServer(daemon=daemon)

    request = {
        "jsonrpc": JSONRPC_VERSION,
        "method": "runtime.get_status",
        "params": {},
        "id": 1,
    }

    response = dispatch_request(server, request)

    assert response["jsonrpc"] == JSONRPC_VERSION
    assert response["id"] == 1
    assert "result" in response
    result = response["result"]
    assert result["status"] == RuntimeStatus.RUNNING.value
    assert result["project_path"] == str(daemon.config.project_path)
    assert result["swarm_id"] == daemon.swarm_state.swarm_id


def test_agent_get_detail_success_and_unknown_agent_error(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)

    # Attach durable state and runtime state for a single agent with an
    # in-memory event stream to exercise serialization.
    base_swarm = daemon.swarm_state
    agent_meta = AgentState(agent_id="agent-1", display_name="Agent One")
    daemon.swarm_state = base_swarm.model_copy(update={"agents": {"agent-1": agent_meta}})

    stream = AgentEventStream(agent_id="agent-1", max_events=10)
    e1 = AgentEvent(
        event_id="e1",
        timestamp=datetime(2026, 7, 3, 12, 0, 0),
        agent_id="agent-1",
        source=AgentEventSource.RUNTIME,
        type="TestEvent1",
        payload={"k": "v1"},
    )
    stream.append(e1)

    daemon.state.agents = {
        "agent-1": AgentRuntimeState(
            agent_id="agent-1",
            status=AgentStatus.RUNNING,
            last_error=None,
            event_stream=stream,
        )
    }
    daemon.state.status = RuntimeStatus.RUNNING

    server = RuntimeApiServer(daemon=daemon)

    # Successful detail request for the known agent.
    request_ok = {
        "jsonrpc": JSONRPC_VERSION,
        "method": "agent.get_detail",
        "params": {"agent_id": "agent-1", "max_events": 10},
        "id": 5,
    }

    response_ok = dispatch_request(server, request_ok)
    assert "result" in response_ok
    detail = response_ok["result"]
    assert detail["agent"]["agent_id"] == "agent-1"
    assert detail["events"][0]["event_id"] == "e1"

    # Unknown agent should map to a JSON-RPC error with code 1001.
    request_missing = {
        "jsonrpc": JSONRPC_VERSION,
        "method": "agent.get_detail",
        "params": {"agent_id": "missing-agent", "max_events": 10},
        "id": 6,
    }

    response_missing = dispatch_request(server, request_missing)
    assert "error" in response_missing
    error = response_missing["error"]
    assert error["code"] == 1001
    assert "Agent not found" in error["message"]
    assert error["data"]["agent_id"] == "missing-agent"


def test_runtime_shutdown_state_conflict_error(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)
    # Default status is Starting; shutdown should conflict.
    assert daemon.state.status is RuntimeStatus.STARTING

    server = RuntimeApiServer(daemon=daemon)

    request = {
        "jsonrpc": JSONRPC_VERSION,
        "method": "runtime.shutdown",
        "params": {"timeout_seconds": 5},
        "id": 10,
    }

    response = dispatch_request(server, request)
    assert "error" in response
    error = response["error"]
    assert error["code"] == 1100
    assert "Runtime state conflict" in error["message"]
    assert "Cannot shutdown runtime" in error["data"]["detail"]


def test_unknown_method_and_invalid_params_errors(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)
    server = RuntimeApiServer(daemon=daemon)

    # Unknown method.
    request_unknown = {
        "jsonrpc": JSONRPC_VERSION,
        "method": "does.not.exist",
        "params": {},
        "id": 20,
    }

    response_unknown = dispatch_request(server, request_unknown)
    assert "error" in response_unknown
    err_unknown = response_unknown["error"]
    assert err_unknown["code"] == 1000
    assert "Unknown method" in err_unknown["message"]

    # Missing required parameter for events.unsubscribe.
    request_bad_params = {
        "jsonrpc": JSONRPC_VERSION,
        "method": "events.unsubscribe",
        "params": {},
        "id": 21,
    }

    response_bad_params = dispatch_request(server, request_bad_params)
    assert "error" in response_bad_params
    err_bad = response_bad_params["error"]
    assert err_bad["code"] == 1000
    assert "Missing required parameter" in err_bad["message"]


def test_events_subscribe_and_unsubscribe_via_jsonrpc(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)
    server = RuntimeApiServer(daemon=daemon)

    # Subscribe to events for a single agent.
    request_sub = {
        "jsonrpc": JSONRPC_VERSION,
        "method": "events.subscribe",
        "params": {"agent_ids": ["a1"], "include_runtime": True},
        "id": 30,
    }

    response_sub = dispatch_request(server, request_sub)
    assert "result" in response_sub
    result_sub = response_sub["result"]
    sub_id = result_sub["subscription_id"]
    assert sub_id.startswith("sub-")

    # Unsubscribe using the returned subscription_id.
    request_unsub = {
        "jsonrpc": JSONRPC_VERSION,
        "method": "events.unsubscribe",
        "params": {"subscription_id": sub_id},
        "id": 31,
    }

    response_unsub = dispatch_request(server, request_unsub)
    assert "result" in response_unsub
    assert response_unsub["result"] == {"unsubscribed": True}


def test_invalid_jsonrpc_version_yields_error(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)
    server = RuntimeApiServer(daemon=daemon)

    request = {
        "jsonrpc": "1.0",
        "method": "runtime.get_status",
        "params": {},
        "id": 40,
    }

    response = dispatch_request(server, request)
    assert "error" in response
    error = response["error"]
    assert error["code"] == 1000
    assert "Invalid jsonrpc version" in error["message"]


def test_build_events_notify_messages_from_agent_event(tmp_path: Path) -> None:
    """JSON-RPC notifications mirror events.notify contract for AgentEvent.

    This covers the bridge from the in-process subscription registry to
    JSON-RPC notification envelopes without requiring a real transport
    layer.
    """

    daemon = _make_daemon(tmp_path)
    server = RuntimeApiServer(daemon=daemon)

    # Create two overlapping subscriptions: one for a specific agent and
    # one wildcard subscription for all agents.
    sub_specific = server.subscribe_events(agent_ids=["agent-1"], include_runtime=False)
    sub_all = server.subscribe_events(agent_ids=None, include_runtime=False)

    # Emit an event for agent-1; both subscriptions should match.
    event = AgentEvent(
        event_id="e1",
        timestamp=datetime(2026, 7, 3, 12, 0, 0),
        agent_id="agent-1",
        source=AgentEventSource.RUNTIME,
        type="TurnStarted",
        payload={"info": "test"},
    )

    from nate_ntm.api.jsonrpc import build_events_notify_messages

    messages = build_events_notify_messages(server, event)

    # One notification per matching subscription.
    assert len(messages) == 2
    method_set = {msg["method"] for msg in messages}
    assert method_set == {"events.notify"}

    # Notifications must follow the JSON-RPC 2.0 notification shape and
    # not include an ``id`` field.
    for msg in messages:
        assert msg["jsonrpc"] == JSONRPC_VERSION
        assert "id" not in msg
        params = msg["params"]
        assert params["event"]["event_id"] == "e1"
        assert params["event"]["agent_id"] == "agent-1"

    sub_ids = {msg["params"]["subscription_id"] for msg in messages}
    assert sub_specific["subscription_id"] in sub_ids
    assert sub_all["subscription_id"] in sub_ids

