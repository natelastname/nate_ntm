"""Tests for :mod:`nate_ntm.config.runtime_config`.

These tests exercise the `RuntimeConfig` model and the `load_runtime_config`
loader, focusing on path resolution, defaults, and environment overrides as
outlined in Bead SRO-B1 and the feature plan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config


def test_load_runtime_config_basic_defaults(tmp_path: Path) -> None:
    """Basic load with explicit project path uses sane defaults.

    - project_path is normalized and must exist
    - metadata_dir defaults to ``<project_path>/.nate_ntm``
    - control API host and port use localhost-only defaults
    - swarm_id defaults to "default"
    """

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    config = load_runtime_config(project_path=project_dir)

    assert isinstance(config, RuntimeConfig)
    assert config.project_path == project_dir.resolve()
    assert config.metadata_dir == (project_dir / ".nate_ntm").resolve()
    assert config.control_api_host == "127.0.0.1"
    assert isinstance(config.control_api_port, int)
    assert 1024 < config.control_api_port < 65536
    assert config.swarm_id == "default"


def test_load_runtime_config_metadata_dir_validation(tmp_path: Path) -> None:
    """`metadata_dir` must be under or adjacent to the project path.

    - Under: ``project_dir/.nate_ntm`` or any subdirectory
    - Adjacent: shares the same parent directory as project_dir
    - Anything else should raise ``ValueError``
    """

    root = tmp_path
    project_dir = root / "project"
    project_dir.mkdir()

    # Under the project directory is allowed
    metadata_under = project_dir / ".runtime_meta"
    config = load_runtime_config(project_path=project_dir, metadata_dir=metadata_under)
    assert config.metadata_dir == metadata_under.resolve()

    # Adjacent (same parent) is allowed
    metadata_adjacent = root / ".nate_ntm_project"
    config = load_runtime_config(project_path=project_dir, metadata_dir=metadata_adjacent)
    assert config.metadata_dir == metadata_adjacent.resolve()

    # Outside (different parent) is rejected
    outside_root = root.parent / "other_root"
    outside_root.mkdir()
    metadata_outside = outside_root / ".nate_ntm_elsewhere"

    with pytest.raises(ValueError):
        load_runtime_config(project_path=project_dir, metadata_dir=metadata_outside)


def test_load_runtime_config_uses_environment_when_args_missing(tmp_path: Path) -> None:
    """Environment variables are used when explicit args are omitted.

    This keeps the loader pluggable for future CLI integration while
    remaining easy to unit test by passing an explicit ``env`` mapping.
    """

    project_dir = tmp_path / "env_project"
    project_dir.mkdir()

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_dir),
        "NATE_NTM_METADATA_DIR": str(project_dir / ".custom_meta"),
        "NATE_NTM_CONTROL_HOST": "127.0.0.2",
        "NATE_NTM_CONTROL_PORT": "9999",
        "NATE_NTM_SWARM_ID": "test-swarm",
    }

    config = load_runtime_config(env=env)

    assert config.project_path == project_dir.resolve()
    assert config.metadata_dir == (project_dir / ".custom_meta").resolve()
    assert config.control_api_host == "127.0.0.2"
    assert config.control_api_port == 9999
    assert config.swarm_id == "test-swarm"


def test_load_runtime_config_invalid_project_path_raises(tmp_path: Path) -> None:
    """A non-existent project path should raise ``ValueError``.

    This protects downstream components that assume an existing project
    directory and `.nate_ntm/` layout.
    """

    missing_dir = tmp_path / "does-not-exist"
    assert not missing_dir.exists()

    with pytest.raises(ValueError):
        load_runtime_config(project_path=missing_dir)


@pytest.mark.parametrize("port_value", ["not-an-int", "0", "70000", -1])
def test_load_runtime_config_invalid_port_raises(tmp_path: Path, port_value: object) -> None:
    """Invalid port values from args or environment raise ``ValueError``."""

    project_dir = tmp_path / "project"
    project_dir.mkdir()

    # Environment-based invalid port
    env = {
        "NATE_NTM_PROJECT_DIR": str(project_dir),
        "NATE_NTM_CONTROL_PORT": str(port_value),
    }

    with pytest.raises(ValueError):
        load_runtime_config(env=env)

    # Argument-based invalid port
    with pytest.raises(ValueError):
        load_runtime_config(project_path=project_dir, control_api_port=port_value)  # type: ignore[arg-type]
