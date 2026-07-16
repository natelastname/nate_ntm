"""Unit tests for the RuntimeApiServer skeleton (T011).

These tests are intentionally minimal and focus on the association
between the server and a `RuntimeDaemon` instance; networking is not yet
implemented.
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime

import pytest

from nate_ntm.config.runtime_config import load_runtime_config
from nate_ntm.runtime.daemon import RuntimeDaemon, StartupMode
from nate_ntm.runtime.events import AgentEvent, AgentEventSource, AgentEventStream
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.swarm_state import AgentState, SwarmState
from nate_ntm.runtime.state import AgentRuntimeState, AgentStatus, RuntimeState, RuntimeStatus
from nate_ntm.api.server import RuntimeApiServer


def _make_daemon(tmp_path: Path) -> RuntimeDaemon:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    config = load_runtime_config(project_path=project)

    # Construct a minimal in-memory daemon without pre-existing on-disk state.
    # This is sufficient for testing the `RuntimeApiServer` association.
    store = MetadataStore(config=config)
    state = RuntimeState(config=config)
    from datetime import datetime

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


def test_runtime_api_server_get_runtime_status_delegates_to_daemon(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)

    # Seed minimal runtime state.
    daemon.state.agents = {
        "a1": AgentRuntimeState(agent_id="a1", status=AgentStatus.RUNNING),
        "a2": AgentRuntimeState(agent_id="a2", status=AgentStatus.IDLE),
    }
    daemon.state.status = RuntimeStatus.RUNNING

    server = RuntimeApiServer(daemon=daemon)

    payload = server.get_runtime_status()

    assert payload["status"] == RuntimeStatus.RUNNING.value
    assert payload["project_path"] == str(daemon.config.project_path)
    assert payload["swarm_id"] == daemon.swarm_state.swarm_id

    counts = payload["agent_counts"]
    assert counts["total"] == 2
    assert counts["running"] == 1
    assert counts["idle"] == 1



def test_runtime_api_server_get_swarm_overview_delegates_to_daemon(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)

    # Attach durable state and runtime state for a single agent.
    base_swarm = daemon.swarm_state
    agent_meta = AgentState(agent_id="agent-1", display_name="Agent One")
    daemon.swarm_state = base_swarm.model_copy(update={"agents": {"agent-1": agent_meta}})

    daemon.state.agents = {
        "agent-1": AgentRuntimeState(agent_id="agent-1", status=AgentStatus.RUNNING)
    }
    daemon.state.status = RuntimeStatus.RUNNING

    server = RuntimeApiServer(daemon=daemon)

    overview = server.get_swarm_overview()

    assert overview["swarm_id"] == daemon.swarm_state.swarm_id
    assert overview["runtime_status"] == RuntimeStatus.RUNNING.value
    assert overview["agent_counts"]["total"] == 1

    agents = overview["agents"]
    assert len(agents) == 1
    agent = agents[0]
    assert agent["agent_id"] == "agent-1"
    assert agent["display_name"] == "Agent One"
    assert agent["status"] == AgentStatus.RUNNING.value


def test_runtime_api_server_binds_daemon(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)
    server = RuntimeApiServer(daemon=daemon)

    assert server.daemon is daemon

    # Stubbed start/stop should be callable without side effects.
    server.start()


def test_runtime_api_server_shutdown_runtime_transitions_status_and_returns_payload(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)

    # Put the daemon into a running state so shutdown is permitted.
    daemon.state.status = RuntimeStatus.RUNNING

    server = RuntimeApiServer(daemon=daemon)

    payload = server.shutdown_runtime(timeout_seconds=5)

    assert payload == {
        "accepted": True,
        "status": RuntimeStatus.SHUTTING_DOWN.value,
    }
    assert daemon.state.shutdown_requested is True
    assert daemon.state.status is RuntimeStatus.SHUTTING_DOWN


def test_runtime_api_server_shutdown_runtime_rejects_when_not_running(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)
    # Default status is ``Starting`` for a fresh RuntimeState.
    assert daemon.state.status is RuntimeStatus.STARTING

    server = RuntimeApiServer(daemon=daemon)

    with pytest.raises(RuntimeError) as excinfo:
        server.shutdown_runtime(timeout_seconds=5)

    msg = str(excinfo.value)
    assert "Starting" in msg

    server.stop()


def test_runtime_api_server_get_agent_detail_returns_metadata_and_events(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)

    # Attach durable state and runtime state for a single agent, including an
    # in-memory event stream with a couple of events.
    base_swarm = daemon.swarm_state
    agent_meta = AgentState(
        agent_id="agent-1",
        display_name="Agent One",
        agent_mail_identity="mail-1",
        conversation_id="conv-1",
    )
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
    e2 = AgentEvent(
        event_id="e2",
        timestamp=datetime(2026, 7, 3, 12, 0, 1),
        agent_id="agent-1",
        source=AgentEventSource.ACP,
        type="TestEvent2",
        payload={"k": "v2"},
    )
    stream.append(e1)
    stream.append(e2)

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

    detail = server.get_agent_detail(agent_id="agent-1", max_events=10)

    agent = detail["agent"]
    assert agent["agent_id"] == "agent-1"
    assert agent["display_name"] == "Agent One"
    assert agent["status"] == AgentStatus.RUNNING.value
    assert agent["agent_mail_identity"] == "mail-1"
    assert agent["conversation_id"] == "conv-1"
    assert agent["last_error"] is None

    events = detail["events"]
    assert len(events) == 2
    assert {e["event_id"] for e in events} == {"e1", "e2"}


def test_runtime_api_server_get_agent_detail_unknown_agent_raises(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)
    server = RuntimeApiServer(daemon=daemon)

    # No metadata or runtime state has been attached for this agent ID.
    try:
        _ = server.get_agent_detail(agent_id="missing-agent", max_events=10)
    except KeyError as exc:
        msg = str(exc)
        assert "missing-agent" in msg
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected KeyError for unknown agent_id")


def test_runtime_api_server_subscribe_events_creates_subscription(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)
    server = RuntimeApiServer(daemon=daemon)

    result = server.subscribe_events(agent_ids=["a1", "a2"], include_runtime=True)

    sub_id = result["subscription_id"]
    assert sub_id.startswith("sub-")
    assert sub_id in server._subscriptions  # type: ignore[attr-defined]

    stored = server._subscriptions[sub_id]  # type: ignore[attr-defined]
    assert stored["agent_ids"] == ("a1", "a2")
    assert stored["include_runtime"] is True



def test_runtime_api_server_unsubscribe_events_is_idempotent(tmp_path: Path) -> None:
    daemon = _make_daemon(tmp_path)
    server = RuntimeApiServer(daemon=daemon)

    first = server.subscribe_events(agent_ids=["a1"], include_runtime=False)
    sub_id = first["subscription_id"]
    assert sub_id in server._subscriptions  # type: ignore[attr-defined]

    # First unsubscribe removes the subscription and reports success.
    result1 = server.unsubscribe_events(sub_id)
    assert result1 == {"unsubscribed": True}
    assert sub_id not in server._subscriptions  # type: ignore[attr-defined]

    # Second unsubscribe for the same ID is a no-op but still reports success.
    result2 = server.unsubscribe_events(sub_id)
    assert result2 == {"unsubscribed": True}



def test_runtime_api_server_build_agent_event_notifications_filters_by_agent_id(tmp_path: Path) -> None:
    """Event routing honors per-subscription agent_id filters.

    Subscriptions with an empty ``agent_ids`` list receive all events; those
    with a non-empty list only receive events for matching agents.
    """

    daemon = _make_daemon(tmp_path)
    server = RuntimeApiServer(daemon=daemon)

    # Three subscriptions: global, one bound to a1, one bound to a2.
    sub_all = server.subscribe_events(agent_ids=[], include_runtime=True)["subscription_id"]
    sub_a1 = server.subscribe_events(agent_ids=["a1"], include_runtime=True)["subscription_id"]
    sub_a2 = server.subscribe_events(agent_ids=["a2"], include_runtime=True)["subscription_id"]

    event = AgentEvent(
        event_id="e1",
        timestamp=datetime(2026, 7, 3, 12, 0, 0),
        agent_id="a1",
        source=AgentEventSource.RUNTIME,
        type="TestEvent",
        payload={"k": "v"},
    )

    payload = server.build_agent_event_notifications(event)
    notifications = payload["notifications"]

    # We expect notifications for the global subscription and the a1-specific
    # subscription, but not for the a2-specific one.
    by_sub = {n["subscription_id"]: n for n in notifications}

    assert set(by_sub.keys()) == {sub_all, sub_a1}

    # Each notification should contain the serialized event payload.
    for n in notifications:
        assert n["event"]["event_id"] == "e1"
        assert n["event"]["agent_id"] == "a1"

