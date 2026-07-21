from __future__ import annotations

from pathlib import Path

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config


def test_defaults(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = load_runtime_config(project_path=project)

    assert isinstance(config, RuntimeConfig)
    assert config.project_path == project.resolve()
    assert config.metadata_dir == (project / ".nate_ntm").resolve()
    assert config.control_api_host == "127.0.0.1"
    assert config.control_api_port == 8765
    assert config.swarm_id == "default"
    assert config.nate_oha_executable == "nate-oha"
    assert config.nate_oha_config_path is None
    assert config.agent_mail_enabled is None


def test_environment_overrides(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = load_runtime_config(
        env={
            "NATE_NTM_PROJECT_DIR": str(project),
            "NATE_NTM_METADATA_DIR": ".custom",
            "NATE_NTM_CONTROL_HOST": "127.0.0.2",
            "NATE_NTM_CONTROL_PORT": "9999",
            "NATE_NTM_SWARM_ID": "test-swarm",
            "NATE_NTM_NATE_OHA_CONFIG": "config/base.json",
            "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
            "NATE_NTM_LLM_MODEL": "gpt-test",
            "NATE_NTM_AGENT_MAIL_ENABLED": "true",
        }
    )

    assert config.metadata_dir == (project / ".custom").resolve()
    assert config.control_api_host == "127.0.0.2"
    assert config.control_api_port == 9999
    assert config.swarm_id == "test-swarm"
    assert config.nate_oha_config_path == (project / "config/base.json").resolve()
    assert config.nate_oha_runtime_mode == "echo"
    assert config.llm_model == "gpt-test"
    assert config.agent_mail_enabled is True


def test_explicit_values_override_environment(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = load_runtime_config(
        project_path=project,
        swarm_id="explicit",
        agent_mail_enabled=False,
        env={
            "NATE_NTM_PROJECT_DIR": str(project),
            "NATE_NTM_SWARM_ID": "environment",
            "NATE_NTM_AGENT_MAIL_ENABLED": "true",
        },
    )

    assert config.swarm_id == "explicit"
    assert config.agent_mail_enabled is False


def test_metadata_must_be_under_or_adjacent_to_project(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    adjacent = tmp_path / ".metadata"
    assert load_runtime_config(project_path=project, metadata_dir=adjacent).metadata_dir == adjacent

    outside = tmp_path.parent / "outside" / ".metadata"
    with pytest.raises(ValueError):
        load_runtime_config(project_path=project, metadata_dir=outside)


@pytest.mark.parametrize("raw, expected", [("true", True), ("1", True), ("false", False), ("0", False)])
def test_boolean_parsing(tmp_path: Path, raw: str, expected: bool) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = load_runtime_config(
        env={
            "NATE_NTM_PROJECT_DIR": str(project),
            "NATE_NTM_AGENT_MAIL_ENABLED": raw,
        }
    )
    assert config.agent_mail_enabled is expected


def test_invalid_boolean_raises(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError):
        load_runtime_config(
            env={
                "NATE_NTM_PROJECT_DIR": str(project),
                "NATE_NTM_AGENT_MAIL_ENABLED": "maybe",
            }
        )


@pytest.mark.parametrize("port", ["not-an-int", 0, 1024, 70000])
def test_invalid_port_raises(tmp_path: Path, port: object) -> None:
    project = tmp_path / "project"
    project.mkdir()
    with pytest.raises(ValueError):
        load_runtime_config(project_path=project, control_api_port=port)  # type: ignore[arg-type]


def test_invalid_project_raises(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        load_runtime_config(project_path=tmp_path / "missing")


def test_dotenv_loading(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    project = tmp_path / "project"
    project.mkdir()
    (tmp_path / ".env").write_text(
        "NATE_NTM_PROJECT_DIR=project\nNATE_NTM_SWARM_ID=dotenv\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)

    config = load_runtime_config()
    assert config.project_path == project.resolve()
    assert config.swarm_id == "dotenv"
