"""Unit tests for the Typer-based CLI runtime start command (T009, T010).

These tests exercise the `nate_ntm.cli.runtime_start` command in a
side-effect-light way using Typer's `CliRunner`. The goal is to validate
argument parsing and the wiring to `RuntimeDaemon` startup semantics
without running a real long-lived daemon.
"""

from __future__ import annotations

from pathlib import Path
import os

from typer.testing import CliRunner

from nate_ntm.cli import app
from nate_ntm.config.runtime_config import load_runtime_config
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.swarm_state import SwarmState


runner = CliRunner()


def _make_cli_env() -> dict[str, str]:
    """Return a test-local environment forcing FAKE adapters.

    The runtime start CLI should be unit-testable without requiring a
    live nate-oha binary or Agent Mail service. These tests therefore
    force FAKE adapters via environment variables and avoid consulting
    any repository-level .env configuration.
    """

    env = dict(os.environ)
    env.update(
        {
            "NATE_NTM_ADAPTER_MODE": "fake",
            "NATE_NTM_AGENT_MAIL_ADAPTER": "fake",
            "NATE_NTM_ACP_ADAPTER": "fake",
        }
    )
    return env



def _init_project_with_metadata(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    # Use the same FAKE-adapter environment as the CLI invocations so
    # that metadata initialisation and runtime startup share consistent
    # adapter selection semantics and do not accidentally depend on
    # repository-level .env configuration.
    env = _make_cli_env()

    config = load_runtime_config(project_path=project, env=env)
    store = MetadataStore(config=config)

    from datetime import datetime

    now = datetime(2026, 7, 3, 12, 0, 0)
    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        # For FAKE adapters the Agent Mail project identifier is not
        # enforced, but we record the project path so that future REAL
        # runs have a stable key to compare against.
        agent_mail_project_id=str(config.project_path),
        created_at=now,
        last_updated_at=now,
    )
    store.save_swarm_state(swarm)

    return project


def _init_project_without_metadata(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    # Do not create any metadata; this represents a fresh project.
    return project


def test_runtime_start_resume_with_existing_metadata_succeeds(tmp_path: Path) -> None:
    project = _init_project_with_metadata(tmp_path)

    result = runner.invoke(
        app,
        ["runtime", "start", "--project", str(project), "--mode", "resume"],
        env=_make_cli_env(),
    )

    assert result.exit_code == 0


def test_runtime_start_resume_without_metadata_fails(tmp_path: Path) -> None:
    project = _init_project_without_metadata(tmp_path)

    result = runner.invoke(
        app,
        ["runtime", "start", "--project", str(project), "--mode", "resume"],
        env=_make_cli_env(),
    )

    assert result.exit_code != 0


def test_runtime_start_create_with_existing_metadata_fails(tmp_path: Path) -> None:
    project = _init_project_with_metadata(tmp_path)

    result = runner.invoke(
        app,
        ["runtime", "start", "--project", str(project), "--mode", "create"],
        env=_make_cli_env(),
    )

    assert result.exit_code != 0




def test_runtime_start_create_without_metadata_succeeds_and_writes_swarm(tmp_path: Path) -> None:
    project = _init_project_without_metadata(tmp_path)

    result = runner.invoke(
        app,
        ["runtime", "start", "--project", str(project), "--mode", "create"],
        env=_make_cli_env(),
    )

    assert result.exit_code == 0

    # Swarm state should now exist and be loadable.
    config = load_runtime_config(project_path=project, env=_make_cli_env())
    store = MetadataStore(config=config)
    swarm_path = store.metadata_dir / "swarm.json"
    assert swarm_path.is_file()
    swarm = store.load_swarm_state()
    assert swarm.project_path == config.project_path
    assert swarm.swarm_id == config.swarm_id




def test_runtime_start_resume_rejects_agents_option(tmp_path: Path) -> None:
    project = _init_project_with_metadata(tmp_path)

    result = runner.invoke(
        app,
        [
            "runtime",
            "start",
            "--project",
            str(project),
            "--mode",
            "resume",
            "--agents",
            "2",
        ],
        env=_make_cli_env(),
    )

    # Typer should treat this as a usage error because --agents is only
    # supported for create mode.
    assert result.exit_code != 0



def test_runtime_start_create_rejects_zero_agents_option(tmp_path: Path) -> None:
    project = _init_project_without_metadata(tmp_path)

    result = runner.invoke(
        app,
        [
            "runtime",
            "start",
            "--project",
            str(project),
            "--mode",
            "create",
            "--agents",
            "0",
        ],
        env=_make_cli_env(),
    )

    # Zero is not a meaningful agent count for create mode.
    assert result.exit_code != 0



def test_runtime_start_create_rejects_negative_agents_option(tmp_path: Path) -> None:
    project = _init_project_without_metadata(tmp_path)

    result = runner.invoke(
        app,
        [
            "runtime",
            "start",
            "--project",
            str(project),
            "--mode",
            "create",
            "--agents",
            "-1",
        ],
        env=_make_cli_env(),
    )

    # Negative values are rejected explicitly by the CLI.
    assert result.exit_code != 0


def test_runtime_start_with_control_api_delegates_to_runner(monkeypatch, tmp_path: Path) -> None:
    """When --with-control-api is set, CLI uses the runtime runner.

    This ensures that the Typer command delegates to
    :func:`run_runtime_with_control_api` with the correct startup mode
    instead of directly constructing a :class:`RuntimeDaemon` and
    performing the short start → shutdown cycle.
    """

    project = _init_project_without_metadata(tmp_path)

    called: dict[str, object] = {}

    def fake_run_runtime_with_control_api(config, mode, *args, **kwargs):  # type: ignore[override]
        called["config"] = config
        called["mode"] = mode
        called["agent_count"] = kwargs.get("agent_count")

    # Patch the runner entrypoint used by the CLI.
    monkeypatch.setattr(
        "nate_ntm.cli.run_runtime_with_control_api",
        fake_run_runtime_with_control_api,
    )

    result = runner.invoke(
        app,
        [
            "runtime",
            "start",
            "--project",
            str(project),
            "--mode",
            "create",
            "--with-control-api",
        ],
        env=_make_cli_env(),
    )

    assert result.exit_code == 0
    assert "config" in called and "mode" in called
    # The CLI should have mapped the string mode onto StartupMode.CREATE.
    from nate_ntm.runtime.daemon import StartupMode

    assert called["mode"] is StartupMode.CREATE
    # No --agents flag was provided, so the CLI should have passed
    # ``agent_count=None`` to the runner.
    assert called["agent_count"] is None



def test_runtime_start_with_control_api_passes_agents_to_runner(monkeypatch, tmp_path: Path) -> None:
    project = _init_project_without_metadata(tmp_path)

    called: dict[str, object] = {}

    def fake_run_runtime_with_control_api(config, mode, *args, **kwargs):  # type: ignore[override]
        called["config"] = config
        called["mode"] = mode
        called["agent_count"] = kwargs.get("agent_count")

    monkeypatch.setattr(
        "nate_ntm.cli.run_runtime_with_control_api",
        fake_run_runtime_with_control_api,
    )

    result = runner.invoke(
        app,
        [
            "runtime",
            "start",
            "--project",
            str(project),
            "--mode",
            "create",
            "--agents",
            "3",
            "--with-control-api",
        ],
        env=_make_cli_env(),
    )

    assert result.exit_code == 0
    from nate_ntm.runtime.daemon import StartupMode

    assert called["mode"] is StartupMode.CREATE
    assert called["agent_count"] == 3




def test_runtime_start_forwards_adapter_and_nate_oha_cli_flags_to_config_loader(
    monkeypatch, tmp_path: Path
) -> None:
    """Adapter and nate-oha-related CLI flags are forwarded to the loader.

    This exercises the wiring from the Typer command down to the
    configuration loader without requiring a real runtime or metadata
    store. The runner entrypoint is patched so that the test remains
    side-effect-light.
    """

    project = _init_project_without_metadata(tmp_path)
    nate_oha_config = project / "nate-oha.json"
    nate_oha_config.write_text("{}", encoding="utf-8")

    called: dict[str, object] = {}

    class DummyConfig:
        pass

    def fake_load_runtime_config(
        *,
        project_path,
        adapter_mode=None,
        agent_mail_adapter=None,
        acp_adapter=None,
        **kwargs,
    ):  # type: ignore[override]
        called["project_path"] = project_path
        called["adapter_mode"] = adapter_mode
        called["agent_mail_adapter"] = agent_mail_adapter
        called["acp_adapter"] = acp_adapter
        called["extra"] = kwargs
        return DummyConfig()

    def fake_run_runtime_with_control_api(config, mode, *args, **kwargs):  # type: ignore[override]
        called["config"] = config
        called["mode"] = mode

    monkeypatch.setattr("nate_ntm.cli.load_runtime_config", fake_load_runtime_config)
    monkeypatch.setattr(
        "nate_ntm.cli.run_runtime_with_control_api",
        fake_run_runtime_with_control_api,
    )

    result = runner.invoke(
        app,
        [
            "runtime",
            "start",
            "--project",
            str(project),
            "--mode",
            "create",
            "--adapter-mode",
            "fake",
            "--agent-mail-adapter",
            "fake-mail",
            "--acp-adapter",
            "fake-acp",
            "--nate-oha-config",
            str(nate_oha_config),
            "--nate-oha-runtime-mode",
            "echo",
            "--llm-model",
            "gpt-cli",
            "--llm-api-key",
            "cli-key",
            "--prompt-soul-content",
            "Hello from CLI",
            "--with-control-api",
        ],
    )

    assert result.exit_code == 0
    assert called["project_path"] == project
    assert called["adapter_mode"] == "fake"
    assert called["agent_mail_adapter"] == "fake-mail"
    assert called["acp_adapter"] == "fake-acp"

    extra = called["extra"]
    assert extra["nate_oha_config_path"] == nate_oha_config.resolve()
    assert extra["nate_oha_runtime_mode"] == "echo"
    assert extra["llm_model"] == "gpt-cli"
    assert extra["llm_api_key"] == "cli-key"
    assert extra["prompt_soul_content"] == "Hello from CLI"

    assert isinstance(called["config"], DummyConfig)

def test_runtime_start_default_mode_resume_is_applied(tmp_path: Path) -> None:
    project = _init_project_with_metadata(tmp_path)

    # No --mode flag: should behave like --mode resume and succeed.
    result = runner.invoke(
        app,
        ["runtime", "start", "--project", str(project)],
    )

    assert result.exit_code == 0
