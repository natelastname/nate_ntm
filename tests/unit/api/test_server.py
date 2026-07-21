from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from nate_ntm.api.server import RuntimeApiServer
from nate_ntm.config.runtime_config import load_runtime_config
from nate_ntm.runtime.daemon import RuntimeDaemon, StartupMode
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.state import AgentRuntimeState, AgentStatus, RuntimeState, RuntimeStatus
from nate_ntm.runtime.swarm_state import AgentState, SwarmState
from nate_oha.config import build_default_config


def _daemon(tmp_path: Path) -> RuntimeDaemon:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    config = load_runtime_config(project_path=project)
    return RuntimeDaemon(
        config=config,
        metadata_store=MetadataStore(config=config),
        swarm_state=SwarmState(
            swarm_id=config.swarm_id,
            project_path=config.project_path,
            agent_mail_project_id="mail-project-1",
            created_at=datetime(2026, 7, 3, 12, 0, 0),
            last_updated_at=datetime(2026, 7, 3, 12, 0, 0),
        ),
        state=RuntimeState(config=config),
        startup_mode=StartupMode.RESUME,
    )


def test_status_and_swarm_overview_delegate_to_daemon(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    metadata = AgentState(
        agent_id="agent-1",
        display_name="Agent One",
        nate_oha_config=build_default_config(),
    )
    daemon.swarm_state = daemon.swarm_state.model_copy(
        update={"agents": {metadata.agent_id: metadata}}
    )
    daemon.state.agents[metadata.agent_id] = AgentRuntimeState(
        agent_id=metadata.agent_id,
        status=AgentStatus.RUNNING,
    )
    daemon.state.status = RuntimeStatus.RUNNING
    server = RuntimeApiServer(daemon)

    status = server.get_runtime_status()
    overview = server.get_swarm_overview()

    assert status["status"] == RuntimeStatus.RUNNING.value
    assert status["agent_counts"]["running"] == 1
    assert overview["agents"][0]["agent_id"] == metadata.agent_id
    assert overview["agents"][0]["status"] == AgentStatus.RUNNING.value


def test_shutdown_requires_running_runtime(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    server = RuntimeApiServer(daemon)

    with pytest.raises(RuntimeError):
        server.shutdown_runtime()

    daemon.state.status = RuntimeStatus.RUNNING
    assert server.shutdown_runtime() == {
        "accepted": True,
        "status": RuntimeStatus.SHUTTING_DOWN.value,
    }
    assert daemon.state.shutdown_requested is True


def test_agent_detail_returns_agent_only(tmp_path: Path) -> None:
    daemon = _daemon(tmp_path)
    config = build_default_config()
    config.features.agent_mail.enabled = True
    config.features.agent_mail.agent_identity = "mail-1"
    metadata = AgentState(
        agent_id="agent-1",
        display_name="Agent One",
        conversation_id="conv-1",
        nate_oha_config=config,
    )
    daemon.swarm_state = daemon.swarm_state.model_copy(
        update={"agents": {metadata.agent_id: metadata}}
    )
    daemon.state.agents[metadata.agent_id] = AgentRuntimeState(
        agent_id=metadata.agent_id,
        status=AgentStatus.RUNNING,
    )

    detail = RuntimeApiServer(daemon).get_agent_detail(metadata.agent_id)

    assert detail == {
        "agent_id": "agent-1",
        "display_name": "Agent One",
        "status": AgentStatus.RUNNING.value,
        "agent_mail_identity": "mail-1",
        "conversation_id": "conv-1",
        "last_error": None,
    }
    assert "events" not in detail


def test_agent_detail_rejects_unknown_agent(tmp_path: Path) -> None:
    with pytest.raises(KeyError):
        RuntimeApiServer(_daemon(tmp_path)).get_agent_detail("missing")


def test_server_exposes_no_event_api(tmp_path: Path) -> None:
    server = RuntimeApiServer(_daemon(tmp_path))
    assert not hasattr(server, "subscribe_events")
    assert not hasattr(server, "unsubscribe_events")
    assert not hasattr(server, "build_agent_event_notifications")
