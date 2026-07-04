"""Unit tests for the project-local metadata store (T004, T012, T038).

These tests focus on the JSON layout under ``.nate_ntm/`` and basic
load/save validation semantics. Higher-level "create vs resume" logic is
covered elsewhere.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import json

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.metadata_store import (
    AgentMetadata,
    MetadataStore,
    SwarmMetadata,
)


def _make_config(project_root: Path) -> RuntimeConfig:
    project_root.mkdir(parents=True, exist_ok=True)
    return load_runtime_config(project_path=project_root)


def test_swarm_and_agent_metadata_round_trip_with_default_layout(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    store = MetadataStore(config=config)

    agent = AgentMetadata(
        agent_id="agent-1",
        display_name="Agent One",
        role="navigator",
        agent_mail_identity="mail-1",
        agent_mail_credentials_ref="cred-1",
        conversation_id="conv-1",
        launch_config={"cmd": "python -m agent1"},
        model="model-x",
        task_description="Do important work",
        restart_policy={"max_restarts": 3},
        last_known_status="Idle",
    )

    created = datetime(2026, 7, 3, 12, 0, 0)
    swarm = SwarmMetadata(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=created,
        last_updated_at=created,
        config_version="v1",
        agents={agent.agent_id: agent},
        runtime_options={"poll_interval_seconds": 5},
    )

    # Save metadata
    store.save_swarm_metadata(swarm)
    store.save_agent_metadata(agent)

    # Layout expectations under .nate_ntm/
    metadata_dir = config.metadata_dir
    swarm_path = metadata_dir / "swarm.json"
    agents_dir = metadata_dir / "agents"
    agent_path = agents_dir / "agent-1.json"

    assert metadata_dir.is_dir()
    assert swarm_path.is_file()
    assert agents_dir.is_dir()
    assert agent_path.is_file()

    # Load metadata back and verify core fields and relationships.
    loaded_swarm = store.load_swarm_metadata()
    loaded_agent = store.load_agent_metadata("agent-1")

    assert loaded_swarm.swarm_id == config.swarm_id
    assert loaded_swarm.project_path == config.project_path
    assert loaded_swarm.agent_mail_project_id == "mail-project-1"
    assert loaded_swarm.created_at == created
    assert loaded_swarm.last_updated_at == created
    assert list(loaded_swarm.agents.keys()) == ["agent-1"]
    assert loaded_swarm.agents["agent-1"] == agent

    assert loaded_agent == agent

    # load_all_agent_metadata should also see the same record.
    all_agents = store.load_all_agent_metadata()
    assert list(all_agents.keys()) == ["agent-1"]
    assert all_agents["agent-1"] == agent


def test_load_swarm_metadata_validates_project_path_mismatch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    # Write a swarm.json with an incorrect project_path directly to disk
    # to exercise validation at load time.
    metadata_dir = config.metadata_dir
    metadata_dir.mkdir(parents=True, exist_ok=True)
    swarm_path = metadata_dir / "swarm.json"

    wrong_project = project.parent / "other-project"
    payload = {
        "swarm_id": config.swarm_id,
        "project_path": str(wrong_project),
        "agent_mail_project_id": "mail-project-1",
        "created_at": "2026-07-03T12:00:00",
        "last_updated_at": "2026-07-03T12:00:00",
        "config_version": None,
        "agents": [],
        "runtime_options": {},
    }

    swarm_path.write_text(json.dumps(payload), encoding="utf-8")

    store = MetadataStore(config=config)

    with pytest.raises(ValueError) as excinfo:
        _ = store.load_swarm_metadata()

    msg = str(excinfo.value)
    assert "project_path" in msg
    assert str(wrong_project) in msg or "does not match" in msg


def test_load_agent_metadata_missing_file_raises(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    store = MetadataStore(config=config)

    with pytest.raises(FileNotFoundError):
        _ = store.load_agent_metadata("missing-agent")


def test_save_agent_metadata_overwrites_existing_file_atomically(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    store = MetadataStore(config=config)

    agent = AgentMetadata(agent_id="agent-1", display_name="Agent One")

    # First write
    store.save_agent_metadata(agent)

    # Overwrite with updated content
    updated = AgentMetadata(agent_id="agent-1", display_name="Agent One Updated")
    store.save_agent_metadata(updated)

    agent_path = config.metadata_dir / "agents" / "agent-1.json"
    with agent_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    assert data["agent_id"] == "agent-1"
    assert data["display_name"] == "Agent One Updated"

    # Ensure no leftover temporary files in the agents directory.
    temp_files = [p for p in agent_path.parent.iterdir() if p.suffix == ".tmp"]
    assert temp_files == []


def test_metadata_store_treats_missing_agents_dir_as_empty(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    store = MetadataStore(config=config)

    agents = store.load_all_agent_metadata()
    assert agents == {}
