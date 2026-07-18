"""Unit tests for AgentSupervisor (runtime/agents.py).

These tests cover the initial T016 scaffolding for linking
AgentState/SwarmState to AgentRuntimeState and AgentEventStream without
introducing real subprocess management.
"""

from __future__ import annotations

from datetime import datetime

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.agents import AgentSupervisor
from nate_ntm.runtime.events import AgentEventStream
from nate_ntm.runtime.swarm_state import AgentState, SwarmState
from nate_ntm.runtime.state import AgentRuntimeState, AgentStatus, RuntimeState
from nate_oha.config import build_default_config


def _make_runtime_state(config: RuntimeConfig) -> RuntimeState:
    return RuntimeState(config=config)



def _make_config(project_root) -> RuntimeConfig:
    project_root.mkdir(parents=True, exist_ok=True)
    return load_runtime_config(project_path=project_root)


def _make_swarm_state(
    config: RuntimeConfig, *, agents: dict[str, AgentState] | None = None
) -> SwarmState:
    now = datetime(2026, 7, 3, 12, 0, 0)
    return SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="",
        created_at=now,
        last_updated_at=now,
        agents=agents or {},
    )


def _make_agent(agent_id: str, display_name: str) -> AgentState:
    """Construct a minimal AgentState with an embedded NateOhaConfig.

    AgentSupervisor tests do not depend on concrete NateOhaConfig contents,
    but the production model requires this field. Using build_default_config
    keeps the helper self-contained and avoids coupling to runtime config
    resolution.
    """

    return AgentState(
        agent_id=agent_id,
        display_name=display_name,
        nate_oha_config=build_default_config(),
    )


def test_iter_configured_agents_yields_metadata_objects(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    # Attach two agents to the swarm state.
    a1 = _make_agent("a1", "Agent One")
    a2 = _make_agent("a2", "Agent Two")
    swarm = _make_swarm_state(config, agents={"a1": a1, "a2": a2})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)

    agents = list(supervisor.iter_configured_agents())
    assert agents == [a1, a2]


def test_ensure_agent_runtime_state_creates_new_entry_with_event_stream(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    metadata = _make_agent("agent-1", "Agent One")
    swarm = _make_swarm_state(config, agents={metadata.agent_id: metadata})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)

    runtime_state = supervisor.ensure_agent_runtime_state(metadata)

    # The runtime entry should be present and reference the same agent_id.
    assert "agent-1" in state.agents
    assert runtime_state is state.agents["agent-1"]
    assert runtime_state.agent_id == "agent-1"

    # Newly created entries should start in STARTING state for US1.
    assert runtime_state.status is AgentStatus.STARTING

    # And they should have an AgentEventStream bound to the same agent.
    assert isinstance(runtime_state.event_stream, AgentEventStream)
    assert runtime_state.event_stream.agent_id == "agent-1"


def test_ensure_agent_runtime_state_returns_existing_entry_without_overwrite(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    metadata = _make_agent("agent-1", "Agent One")
    swarm = _make_swarm_state(config, agents={metadata.agent_id: metadata})

    # Seed state with a pre-existing runtime entry using a different status.
    preexisting = AgentRuntimeState(
        agent_id="agent-1",
        status=AgentStatus.RUNNING,
        last_error="boom",
    )
    state.agents["agent-1"] = preexisting

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)

    result = supervisor.ensure_agent_runtime_state(metadata)

    # The same instance should be returned and kept in RUNNING state.
    assert result is preexisting
    assert result.status is AgentStatus.RUNNING
    assert result.last_error == "boom"


def test_ensure_agents_registered_populates_state_for_all_metadata(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    a1 = _make_agent("a1", "Agent One")
    a2 = _make_agent("a2", "Agent Two")
    swarm = _make_swarm_state(config, agents={"a1": a1, "a2": a2})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)

    supervisor.ensure_agents_registered()

    assert set(state.agents.keys()) == {"a1", "a2"}
    assert state.agents["a1"].status is AgentStatus.STARTING
    assert state.agents["a2"].status is AgentStatus.STARTING


def test_ensure_agents_registered_preserves_existing_runtime_entries(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    a1 = _make_agent("a1", "Agent One")
    a2 = _make_agent("a2", "Agent Two")
    swarm = _make_swarm_state(config, agents={"a1": a1, "a2": a2})

    # Pre-seed runtime state for one of the agents.
    preexisting = AgentRuntimeState(
        agent_id="a1",
        status=AgentStatus.RUNNING,
    )
    state.agents["a1"] = preexisting

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)

    supervisor.ensure_agents_registered()

    # `a1` should be preserved, and `a2` should be added.
    assert state.agents["a1"] is preexisting
    assert state.agents["a1"].status is AgentStatus.RUNNING

    assert "a2" in state.agents
    assert state.agents["a2"].status is AgentStatus.STARTING


def test_launch_all_agents_transitions_starting_to_idle_and_sets_subprocess(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    a1 = _make_agent("a1", "Agent One")
    a2 = _make_agent("a2", "Agent Two")
    swarm = _make_swarm_state(config, agents={"a1": a1, "a2": a2})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)

    # Initially there should be no runtime entries.
    assert state.agents == {}

    supervisor.launch_all_agents()

    # All configured agents should now have runtime state entries.
    assert set(state.agents.keys()) == {"a1", "a2"}

    # New agents should be marked Idle with a subprocess handle and event stream.
    for runtime_state in state.agents.values():
        assert runtime_state.status is AgentStatus.IDLE
        assert runtime_state.subprocess_handle is not None
        assert isinstance(runtime_state.event_stream, AgentEventStream)




def test_mark_agent_failed_sets_status_and_last_error_and_emits_event(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    metadata = _make_agent("agent-1", "Agent One")
    swarm = _make_swarm_state(config, agents={metadata.agent_id: metadata})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)
    supervisor.ensure_agents_registered()

    runtime_state = state.agents["agent-1"]
    assert runtime_state.status is AgentStatus.STARTING
    assert runtime_state.last_error is None

    supervisor.mark_agent_failed("agent-1", error="boom")

    assert runtime_state.status is AgentStatus.FAILED
    assert runtime_state.last_error == "boom"

    # An event should have been appended to the agent's stream.
    stream = runtime_state.event_stream
    assert isinstance(stream, AgentEventStream)
    events = list(stream)
    assert len(events) == 1
    event = events[0]
    assert event.type == "AgentFailed"
    assert event.payload.get("last_error") == "boom"



def test_restart_agent_clears_error_and_marks_idle_and_emits_event(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    metadata = _make_agent("agent-1", "Agent One")
    swarm = _make_swarm_state(config, agents={metadata.agent_id: metadata})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)
    supervisor.ensure_agents_registered()

    runtime_state = state.agents["agent-1"]
    runtime_state.status = AgentStatus.FAILED
    runtime_state.last_error = "boom"

    supervisor.restart_agent("agent-1")

    assert runtime_state.status is AgentStatus.IDLE
    assert runtime_state.last_error is None
    assert runtime_state.subprocess_handle is not None

    # AgentFailed + AgentRestarted events should both be recorded if the
    # test marks the agent as failed first.
    stream = runtime_state.event_stream
    assert isinstance(stream, AgentEventStream)
    events = list(stream)
    assert any(e.type == "AgentRestarted" for e in events)


def test_launch_all_agents_is_idempotent_and_preserves_non_starting_status(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    a1 = _make_agent("a1", "Agent One")
    a2 = _make_agent("a2", "Agent Two")
    swarm = _make_swarm_state(config, agents={"a1": a1, "a2": a2})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)

    supervisor.launch_all_agents()

    # Capture subprocess handles from the first launch.
    first_handles = {
        agent_id: runtime_state.subprocess_handle
        for agent_id, runtime_state in state.agents.items()
    }

    # Simulate scheduler or runtime changing one agent's status away from STARTING.
    state.agents["a1"].status = AgentStatus.RUNNING

    supervisor.launch_all_agents()

    # Subprocess handles should be stable across calls (idempotent behavior).
    second_handles = {
        agent_id: runtime_state.subprocess_handle
        for agent_id, runtime_state in state.agents.items()
    }
    assert second_handles == first_handles

    # Non-STARTING statuses should be preserved.
    assert state.agents["a1"].status is AgentStatus.RUNNING
    assert state.agents["a2"].status is AgentStatus.IDLE

