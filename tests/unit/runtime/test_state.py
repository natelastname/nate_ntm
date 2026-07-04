"""Unit tests for RuntimeState and AgentRuntimeState.

These tests focus on the in-memory data structures only; scheduler and
I/O concerns are covered elsewhere.
"""

from __future__ import annotations

from pathlib import Path

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.state import AgentRuntimeState, AgentStatus, RuntimeState, RuntimeStatus


def _make_config(tmp_path: Path) -> RuntimeConfig:
    return load_runtime_config(project_path=tmp_path)


def test_runtime_state_defaults(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = RuntimeState(config=config)

    assert state.config is config
    assert state.status is RuntimeStatus.STARTING
    assert state.shutdown_requested is False
    assert state.agents == {}


def test_agent_runtime_state_defaults() -> None:
    agent = AgentRuntimeState(agent_id="agent-1")

    assert agent.agent_id == "agent-1"
    assert agent.status is AgentStatus.STARTING
    assert agent.current_turn_id is None
    assert agent.last_error is None
    assert agent.subprocess_handle is None
    assert agent.acp_connection is None


def test_runtime_state_can_register_and_access_agents(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = RuntimeState(config=config)

    agent = AgentRuntimeState(agent_id="agent-1")
    state.agents[agent.agent_id] = agent

    assert state.get_agent("agent-1") is agent
    assert state.get_agent("missing") is None


def test_set_agent_status_updates_existing_agent(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = RuntimeState(config=config)

    agent = AgentRuntimeState(agent_id="agent-1")
    state.agents[agent.agent_id] = agent

    state.set_agent_status("agent-1", AgentStatus.RUNNING)
    assert agent.status is AgentStatus.RUNNING


def test_set_agent_status_raises_for_unknown_agent(tmp_path: Path) -> None:
    config = _make_config(tmp_path)
    state = RuntimeState(config=config)

    try:
        state.set_agent_status("unknown", AgentStatus.RUNNING)
    except KeyError as exc:
        assert "unknown" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("Expected KeyError for unknown agent_id")
