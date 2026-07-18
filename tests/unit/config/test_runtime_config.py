"""Tests for :mod:`nate_ntm.config.runtime_config`.

These tests exercise the `RuntimeConfig` model and the `load_runtime_config`
loader, focusing on path resolution, defaults, and environment overrides as
outlined in Bead SRO-B1 and the feature plan.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from nate_ntm.config.runtime_config import AdapterKind, RuntimeConfig, load_runtime_config


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
    assert config.adapter_mode is AdapterKind.FAKE
    assert config.agent_mail_adapter is None
    assert config.acp_adapter is None



def test_load_runtime_config_nate_oha_and_llm_defaults(tmp_path: Path) -> None:
    """New nate-oha and LLM-related fields have sensible defaults.

    This exercises the additional configuration surface introduced for the
    nate-oha ACP integration (Epic 005) without requiring any environment
    variables or explicit arguments.
    """

    project_dir = tmp_path / "project_nate_oha_defaults"
    project_dir.mkdir()

    config = load_runtime_config(project_path=project_dir)

    # nate-oha-related defaults
    assert config.nate_oha_executable == "nate-oha"
    assert config.nate_oha_config_path is None
    assert config.nate_oha_runtime_mode is None

    # LLM / prompt defaults
    assert config.llm_model is None
    assert config.llm_api_key is None
    assert config.prompt_soul_content is None

    # Agent Mail enablement flag defaults to "unspecified" so that
    # higher-level components can apply their own behavior.
    assert config.agent_mail_enabled is None




def test_load_runtime_config_adapter_mode_and_overrides_from_env(tmp_path: Path) -> None:
    """Adapter selection fields can be driven from environment variables.

    This exercises the T100 behavior where adapter mode and per-adapter
    overrides are resolved from ``NATE_NTM_*`` environment variables when
    explicit arguments are not provided.
    """

    project_dir = tmp_path / "project_env_adapters"
    project_dir.mkdir()

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_dir),
        "NATE_NTM_ADAPTER_MODE": "real",
        "NATE_NTM_AGENT_MAIL_ADAPTER": "fake",
        "NATE_NTM_ACP_ADAPTER": "fake",
    }

    config = load_runtime_config(env=env)

    # env-driven adapter selection should be normalized into AdapterKind values
    assert config.adapter_mode is AdapterKind.REAL
    assert config.agent_mail_adapter is AdapterKind.FAKE
    assert config.acp_adapter is AdapterKind.FAKE



def test_load_runtime_config_adapter_args_override_env(tmp_path: Path) -> None:
    """Explicit adapter arguments override any environment variables.

    This ensures that CLI- or caller-supplied adapter selection takes
    precedence over ``NATE_NTM_*`` variables.
    """

    project_dir = tmp_path / "project_args_override"
    project_dir.mkdir()

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_dir),
        "NATE_NTM_ADAPTER_MODE": "fake",
        "NATE_NTM_AGENT_MAIL_ADAPTER": "fake",
        "NATE_NTM_ACP_ADAPTER": "fake",
    }

    config = load_runtime_config(
        env=env,
        adapter_mode="real",
        agent_mail_adapter="real",
        acp_adapter="real",
    )

    assert config.adapter_mode is AdapterKind.REAL
    assert config.agent_mail_adapter is AdapterKind.REAL
    assert config.acp_adapter is AdapterKind.REAL



def test_load_runtime_config_invalid_adapter_env_raises(tmp_path: Path) -> None:
    """Invalid adapter kinds from environment raise ``ValueError``.

    Error messages should identify the originating field so that
    misconfiguration is easy to diagnose.
    """

    project_dir = tmp_path / "project_invalid_adapter_env"
    project_dir.mkdir()

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_dir),
        "NATE_NTM_ADAPTER_MODE": "bogus",
    }

    with pytest.raises(ValueError) as excinfo:
        load_runtime_config(env=env)

    msg = str(excinfo.value)
    # The helper annotates env-derived fields as ``env:<name>``.
    assert "env:adapter_mode" in msg
    assert "bogus" in msg



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



def test_load_runtime_config_nate_oha_fields_from_env(tmp_path: Path) -> None:
    """nate-oha and LLM fields can be driven entirely from the environment."""

    project_dir = tmp_path / "project_nate_oha_env"
    project_dir.mkdir()

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_dir),
        "NATE_NTM_NATE_OHA_EXECUTABLE": "nate-oha",
        "NATE_NTM_NATE_OHA_CONFIG": "config/base.json",
        "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
        "NATE_NTM_LLM_MODEL": "gpt-test-1",
        "NATE_NTM_LLM_API_KEY": "secret-key",
        "NATE_NTM_PROMPT_SOUL_CONTENT": "Hello from env",
        "NATE_NTM_AGENT_MAIL_ENABLED": "true",
    }

    config = load_runtime_config(env=env)

    assert config.project_path == project_dir.resolve()
    assert config.nate_oha_executable == "nate-oha"
    assert config.nate_oha_config_path == (project_dir / "config/base.json").resolve()
    assert config.nate_oha_runtime_mode == "echo"
    assert config.llm_model == "gpt-test-1"
    assert config.llm_api_key == "secret-key"
    assert config.prompt_soul_content == "Hello from env"
    assert config.agent_mail_enabled is True


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("1", True),
        ("true", True),
        ("TRUE", True),
        ("yes", True),
        ("on", True),
        ("0", False),
        ("false", False),
        ("FALSE", False),
        ("no", False),
        ("off", False),
    ],
)
def test_load_runtime_config_agent_mail_enabled_env_parsing(
    tmp_path: Path, raw: str, expected: bool
) -> None:
    """Boolean Agent Mail enabled flag is parsed from a variety of forms."""

    project_dir = tmp_path / "agent_mail_enabled_env"
    project_dir.mkdir()

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_dir),
        "NATE_NTM_AGENT_MAIL_ENABLED": raw,
    }

    config = load_runtime_config(env=env)

    assert config.agent_mail_enabled is expected


@pytest.mark.parametrize("raw", ["", "2", "maybe", "truthy"])
def test_load_runtime_config_agent_mail_enabled_invalid_env_raises(
    tmp_path: Path, raw: str
) -> None:
    """Invalid boolean strings for the enabled flag raise ValueError."""

    project_dir = tmp_path / "agent_mail_enabled_invalid"
    project_dir.mkdir()

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_dir),
        "NATE_NTM_AGENT_MAIL_ENABLED": raw,
    }

    with pytest.raises(ValueError) as excinfo:
        load_runtime_config(env=env)

    msg = str(excinfo.value)
    assert "env:agent_mail_enabled" in msg


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



def test_load_runtime_config_uses_dotenv_when_env_missing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """When ``env`` is None, values from a local .env file are used.

    This exercises the optional python-dotenv-based loading path. The test
    is skipped entirely when python-dotenv is not installed.
    """

    pytest.importorskip("dotenv")

    project_dir = tmp_path / "project_from_dotenv"
    project_dir.mkdir()

    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text(
        "\n".join(
            [
                "NATE_NTM_PROJECT_DIR=project_from_dotenv",
                "NATE_NTM_SWARM_ID=from-dotenv",
            ]
        )
    )

    monkeypatch.chdir(tmp_path)

    config = load_runtime_config()

    assert config.project_path == project_dir.resolve()
    assert config.swarm_id == "from-dotenv"



def test_load_runtime_config_env_overrides_dotenv(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Real environment variables override values from .env when both are set."""

    pytest.importorskip("dotenv")

    project_dir = tmp_path / "project_from_dotenv"
    project_dir.mkdir()

    (tmp_path / ".env").write_text(
        "\n".join(
            [
                "NATE_NTM_PROJECT_DIR=project_from_dotenv",
                "NATE_NTM_SWARM_ID=from-dotenv",
            ]
        )
    )

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("NATE_NTM_SWARM_ID", "from-os-environ")

    config = load_runtime_config()

    assert config.project_path == project_dir.resolve()
    assert config.swarm_id == "from-os-environ"

