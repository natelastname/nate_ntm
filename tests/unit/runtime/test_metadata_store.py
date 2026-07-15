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
from nate_oha.config import build_default_config
from nate_ntm.runtime.swarm_state import SwarmState


def _make_config(project_root: Path) -> RuntimeConfig:
    project_root.mkdir(parents=True, exist_ok=True)
    return load_runtime_config(project_path=project_root)


def test_swarm_and_agent_metadata_round_trip_with_default_layout(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    store = MetadataStore(config=config)

    # Build a concrete NateOhaConfig instance using the upstream helper. This
    # exercises round-trip persistence of the nested configuration through
    # SwarmState/AgentState without depending on a particular JSON profile
    # layout.
    nate_oha_cfg = build_default_config()

    agent = AgentMetadata(
        agent_id="agent-1",
        display_name="Agent One",
        role="navigator",
        agent_mail_identity="mail-1",
        agent_mail_credentials_ref="cred-1",
        conversation_id="conv-1",
        restart_policy={"max_restarts": 3},
        last_known_status="Idle",
        nate_oha_config=nate_oha_cfg,
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

    # Layout expectations under .nate_ntm/: a single swarm.json file.
    metadata_dir = config.metadata_dir
    swarm_path = metadata_dir / "swarm.json"

    assert metadata_dir.is_dir()
    assert swarm_path.is_file()

    # Load metadata back and verify core fields and relationships.
    loaded_swarm = store.load_swarm_metadata()
    loaded_agent = store.load_agent_metadata("agent-1")

    assert loaded_swarm.swarm_id == config.swarm_id
    assert loaded_swarm.project_path == config.project_path
    assert loaded_swarm.agent_mail_project_id == "mail-project-1"
    assert loaded_swarm.created_at == created
    # ``last_updated_at`` is expected to be bumped when per-agent metadata
    # is saved via :meth:`MetadataStore.save_agent_metadata`.
    assert loaded_swarm.last_updated_at >= created
    assert list(loaded_swarm.agents.keys()) == ["agent-1"]

    loaded_swarm_agent = loaded_swarm.agents["agent-1"]
    assert loaded_swarm_agent.agent_id == agent.agent_id
    assert loaded_swarm_agent.display_name == agent.display_name
    assert loaded_swarm_agent.role == agent.role
    assert loaded_swarm_agent.agent_mail_identity == agent.agent_mail_identity
    assert loaded_swarm_agent.agent_mail_credentials_ref == agent.agent_mail_credentials_ref
    assert loaded_swarm_agent.conversation_id == agent.conversation_id
    assert loaded_swarm_agent.last_known_status == agent.last_known_status

    assert loaded_agent.agent_id == agent.agent_id
    assert loaded_agent.display_name == agent.display_name
    assert loaded_agent.role == agent.role
    assert loaded_agent.agent_mail_identity == agent.agent_mail_identity
    assert loaded_agent.agent_mail_credentials_ref == agent.agent_mail_credentials_ref
    assert loaded_agent.conversation_id == agent.conversation_id
    assert loaded_agent.last_known_status == agent.last_known_status

    # Nate OHA configuration should round-trip via SwarmState/AgentState.
    assert loaded_agent.nate_oha_config is not None
    assert loaded_swarm_agent.nate_oha_config is not None
    assert type(loaded_agent.nate_oha_config) is type(nate_oha_cfg)
    assert loaded_agent.nate_oha_config.model_dump() == nate_oha_cfg.model_dump()
    assert loaded_swarm_agent.nate_oha_config.model_dump() == nate_oha_cfg.model_dump()

    # load_all_agent_metadata should also see the same record.
    all_agents = store.load_all_agent_metadata()
    assert list(all_agents.keys()) == ["agent-1"]
    assert all_agents["agent-1"].agent_id == agent.agent_id
    assert all_agents["agent-1"].nate_oha_config is not None
    assert all_agents["agent-1"].nate_oha_config.model_dump() == nate_oha_cfg.model_dump()


def test_load_swarm_metadata_validates_project_path_mismatch(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    # Write a swarm.json with an incorrect project_path directly to disk
    # to exercise validation at load time. Use the SwarmState helper so the
    # on-disk shape matches the production layout.
    metadata_dir = config.metadata_dir
    metadata_dir.mkdir(parents=True, exist_ok=True)
    swarm_path = metadata_dir / "swarm.json"

    wrong_project = project.parent / "other-project"
    created = datetime(2026, 7, 3, 12, 0, 0)

    state = SwarmState(
        swarm_id=config.swarm_id,
        project_path=wrong_project,
        agent_mail_project_id="mail-project-1",
        created_at=created,
        last_updated_at=created,
        config_version=None,
        agents={},
        runtime_options={},
    )

    swarm_path.write_text(state.to_json(indent=2), encoding="utf-8")

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

    # Seed minimal swarm metadata so that save_agent_metadata has a swarm
    # state to update.
    created = datetime(2026, 7, 3, 12, 0, 0)
    swarm = SwarmMetadata(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=created,
        last_updated_at=created,
    )
    store.save_swarm_metadata(swarm)

    agent = AgentMetadata(agent_id="agent-1", display_name="Agent One")

    # First write
    store.save_agent_metadata(agent)

    # Overwrite with updated content
    updated = AgentMetadata(agent_id="agent-1", display_name="Agent One Updated")
    store.save_agent_metadata(updated)

    swarm_path = config.metadata_dir / "swarm.json"
    with swarm_path.open("r", encoding="utf-8") as f:
        data = json.load(f)

    agents_data = data.get("agents") or {}
    assert "agent-1" in agents_data
    assert agents_data["agent-1"]["agent_id"] == "agent-1"
    assert agents_data["agent-1"]["display_name"] == "Agent One Updated"

    # Ensure no leftover temporary files in the metadata directory.
    temp_files = [p for p in config.metadata_dir.iterdir() if p.suffix == ".tmp"]
    assert temp_files == []


def test_metadata_store_treats_missing_swarm_metadata_as_empty(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    store = MetadataStore(config=config)

    agents = store.load_all_agent_metadata()
    assert agents == {}
