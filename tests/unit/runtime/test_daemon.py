"""Unit tests for the RuntimeDaemon entrypoint and startup semantics.

Covers Phase 2 tasks T008 and T037 at the Python API level:

* Explicit `create` vs `resume` precondition checks.
* Construction of `RuntimeDaemon` in `resume` mode from existing
  metadata.
* Basic lifecycle transitions for `start`, `request_shutdown`, and
  `mark_stopped`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.daemon import (
    MetadataAlreadyExistsError,
    MetadataMissingError,
    RuntimeDaemon,
    RuntimeStartupError,
    StartupMode,
    check_startup_preconditions,
)
from nate_ntm.runtime.metadata_store import MetadataStore, SwarmMetadata
from nate_ntm.runtime.state import RuntimeStatus


def _make_config(project_root: Path) -> RuntimeConfig:
    project_root.mkdir(parents=True, exist_ok=True)
    return load_runtime_config(project_path=project_root)


def _write_minimal_swarm_metadata(config: RuntimeConfig) -> None:
    store = MetadataStore(config=config)
    now = datetime(2026, 7, 3, 12, 0, 0)
    swarm = SwarmMetadata(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
    )
    store.save_swarm_metadata(swarm)


def test_check_startup_preconditions_create_fails_if_metadata_exists(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    _write_minimal_swarm_metadata(config)

    with pytest.raises(MetadataAlreadyExistsError) as excinfo:
        check_startup_preconditions(config, StartupMode.CREATE)

    msg = str(excinfo.value)
    assert "Swarm metadata already exists" in msg


def test_check_startup_preconditions_resume_fails_if_metadata_missing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    with pytest.raises(MetadataMissingError) as excinfo:
        check_startup_preconditions(config, StartupMode.RESUME)

    msg = str(excinfo.value)
    assert "Swarm metadata not found" in msg


def test_runtime_daemon_resume_constructs_state_from_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_metadata(config)

    daemon = RuntimeDaemon.resume(config)

    assert daemon.config is config
    assert daemon.metadata_store.metadata_dir == config.metadata_dir
    assert daemon.swarm_metadata.swarm_id == config.swarm_id
    assert daemon.swarm_metadata.project_path == config.project_path

    assert daemon.state.config is config
    assert daemon.state.status is RuntimeStatus.STARTING
    assert daemon.startup_mode is StartupMode.RESUME
    assert daemon.started_at is None


def test_runtime_daemon_start_and_shutdown_transitions(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_metadata(config)
    daemon = RuntimeDaemon.resume(config)

    # Initially in STARTING state
    assert daemon.state.status is RuntimeStatus.STARTING
    assert daemon.state.shutdown_requested is False

    # After start(), runtime should be RUNNING and started_at set.
    daemon.start()
    assert daemon.state.status is RuntimeStatus.RUNNING
    assert isinstance(daemon.started_at, datetime)

    # Idempotent start() when already running should not fail.
    daemon.start()
    assert daemon.state.status is RuntimeStatus.RUNNING

    # Request shutdown moves to SHUTTING_DOWN from RUNNING.
    daemon.request_shutdown()
    assert daemon.state.shutdown_requested is True
    assert daemon.state.status is RuntimeStatus.SHUTTING_DOWN

    # Mark fully stopped.
    daemon.mark_stopped()
    assert daemon.state.status is RuntimeStatus.STOPPED


def test_runtime_daemon_start_rejects_invalid_transition(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_metadata(config)
    daemon = RuntimeDaemon.resume(config)

    # Move the state to STOPPED manually to simulate prior lifecycle.
    daemon.state.status = RuntimeStatus.STOPPED

    with pytest.raises(RuntimeStartupError):
        daemon.start()
