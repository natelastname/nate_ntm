from __future__ import annotations

"""Tests for :mod:`nate_ntm.runtime.nate_oha_launch`.

These tests exercise the :class:`NateOhaLaunchSpec` helper, ensuring that
nate-oha launches are constructed from a base JSON configuration plus
runtime-specific overrides (FR-012/FR-013) and that the resulting argv is
stable and easy to assert on.
"""

from pathlib import Path
from types import SimpleNamespace


from nate_ntm.config.runtime_config import load_runtime_config
from nate_ntm.runtime.swarm_state import AgentState
from nate_ntm.runtime.nate_oha_launch import (
    NateOhaLaunchSpec,
    build_effective_nate_oha_config,
    build_nate_oha_launch_spec,
)


def _make_base_paths(tmp_path: Path) -> tuple[Path, Path]:
    project_root = tmp_path / "project"
    project_root.mkdir()
    base_config = project_root / "nate-oha-config.json"
    base_config.write_text("{}", encoding="utf-8")
    return project_root, base_config


def test_to_argv_minimal_includes_config_and_runtime_mode(tmp_path: Path) -> None:
    """Minimal launch spec still passes --config and runtime.mode.

    This corresponds to the simplest echo-mode launch where only the base
    configuration path and runtime mode are required.
    """

    cwd, base_config = _make_base_paths(tmp_path)

    spec = NateOhaLaunchSpec(
        executable="nate-oha",
        base_config=base_config,
        cwd=cwd,
        runtime_mode="echo",
    )

    argv = list(spec.to_argv())

    # Leading portion should be ``nate-oha acp --config <path>``.
    assert argv[:4] == ["nate-oha", "acp", "--config", str(base_config)]

    # No resume flag when no conversation ID is provided.
    assert "--resume" not in argv

    # We always set runtime.mode explicitly.
    assert "--set" in argv
    assert "runtime.mode=echo" in argv


def test_to_argv_includes_resume_when_conversation_id_present(tmp_path: Path) -> None:
    """When a conversation ID is supplied, it is passed via --resume."""

    cwd, base_config = _make_base_paths(tmp_path)

    spec = NateOhaLaunchSpec(
        executable="nate-oha",
        base_config=base_config,
        cwd=cwd,
        runtime_mode="agent",
        conversation_id="conv-123",
    )

    argv = list(spec.to_argv())

    assert "--resume" in argv
    idx = argv.index("--resume")
    assert argv[idx + 1] == "conv-123"


def test_to_argv_includes_model_api_key_and_prompt(tmp_path: Path) -> None:
    """Model, API key, and prompt souls are translated into --set overrides."""

    cwd, base_config = _make_base_paths(tmp_path)

    spec = NateOhaLaunchSpec(
        executable="nate-oha",
        base_config=base_config,
        cwd=cwd,
        runtime_mode="agent",
        model="gpt-test-1",
        api_key="secret-key",
        prompt_soul_content="Hello, world!",
    )

    argv = list(spec.to_argv())

    assert "--set" in argv
    # The individual overrides must appear somewhere in the argument vector.
    assert "llm.model=gpt-test-1" in argv
    assert "llm.api_key=secret-key" in argv
    assert "prompt.soul_content=Hello, world!" in argv


def test_to_argv_agent_mail_disabled_sets_flag_false(tmp_path: Path) -> None:
    """Explicitly disabled Agent Mail emits a false-enabled flag only."""

    cwd, base_config = _make_base_paths(tmp_path)

    spec = NateOhaLaunchSpec(
        executable="nate-oha",
        base_config=base_config,
        cwd=cwd,
        runtime_mode="echo",
        agent_mail_enabled=False,
    )

    argv = list(spec.to_argv())

    # Agent Mail must be explicitly disabled.
    assert "features.agent_mail.enabled=false" in argv

    # No other Agent Mail configuration paths should be present.
    assert all(
        not item.startswith("features.agent_mail.")
        or item == "features.agent_mail.enabled=false"
        for item in argv
    )


def test_to_argv_agent_mail_enabled_sets_all_fields(tmp_path: Path) -> None:
    """Enabled Agent Mail emits project, identity, credentials, and upstream."""

    cwd, base_config = _make_base_paths(tmp_path)

    spec = NateOhaLaunchSpec(
        executable="nate-oha",
        base_config=base_config,
        cwd=cwd,
        runtime_mode="agent",
        agent_mail_enabled=True,
        agent_mail_project="proj-1",
        agent_mail_agent_identity="agent@example.com",
        agent_mail_credentials_ref="cred-ref-1",
        agent_mail_upstream_url="https://mail.example.com/mcp",
    )

    argv = list(spec.to_argv())

    assert "features.agent_mail.enabled=true" in argv
    assert "features.agent_mail.project=proj-1" in argv
    assert "features.agent_mail.agent_identity=agent@example.com" in argv
    assert "features.agent_mail.credentials_ref=cred-ref-1" in argv
    assert "features.agent_mail.upstream_url=https://mail.example.com/mcp" in argv


def test_to_argv_extra_overrides_reject_conflicts_with_structured_fields(tmp_path: Path) -> None:
    """extra_overrides must not silently override structured configuration paths."""

    cwd, base_config = _make_base_paths(tmp_path)

    spec = NateOhaLaunchSpec(
        executable="nate-oha",
        base_config=base_config,
        cwd=cwd,
        runtime_mode="echo",
        extra_overrides={"runtime.mode": "agent"},
    )

    # Attempting to render argv should raise an error because runtime.mode is a
    # structured field and cannot be replaced via extra_overrides.
    import pytest

    with pytest.raises(ValueError) as excinfo:
        list(spec.to_argv())

    msg = str(excinfo.value)
    assert "extra_overrides may not override structured configuration path" in msg


def test_to_argv_extra_overrides_support_additional_paths(tmp_path: Path) -> None:
    """extra_overrides can still be used for additional, non-typed paths."""

    cwd, base_config = _make_base_paths(tmp_path)

    spec = NateOhaLaunchSpec(
        executable="nate-oha",
        base_config=base_config,
        cwd=cwd,
        runtime_mode="echo",
        extra_overrides={"custom.flag": "on"},
    )

    argv = list(spec.to_argv())

    assert "runtime.mode=echo" in argv
    assert "custom.flag=on" in argv


def test_to_argv_is_deterministic(tmp_path: Path) -> None:
    """The same specification yields the same argv regardless of mapping order."""

    cwd, base_config = _make_base_paths(tmp_path)

    # Two specs with logically identical data but different extra_overrides
    # construction order must produce identical argv sequences.
    spec_a = NateOhaLaunchSpec(
        executable="nate-oha",
        base_config=base_config,
        cwd=cwd,
        runtime_mode="agent",
        extra_overrides={"a": "1", "b": "2"},
    )
    spec_b = NateOhaLaunchSpec(
        executable="nate-oha",
        base_config=base_config,
        cwd=cwd,
        runtime_mode="agent",
        extra_overrides={"b": "2", "a": "1"},
    )

    argv_a = list(spec_a.to_argv())
    argv_b = list(spec_b.to_argv())

    assert argv_a == argv_b


def test_build_nate_oha_launch_spec_minimal(tmp_path: Path) -> None:
    """Minimal config produces a basic launch spec without resume or Agent Mail.

    This covers the core mapping from :class:`RuntimeConfig` and
    :class:`AgentState` into :class:`NateOhaLaunchSpec` for the case
    where only the executable, base config, and runtime mode are
    configured.
    """

    project_dir = tmp_path / "project_minimal"
    project_dir.mkdir()

    # Reuse the existing helper to create a base config under the
    # project directory so that the relative path used in the
    # environment matches what :func:`load_runtime_config` expects.
    _project_root, base_config = _make_base_paths(project_dir)

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_dir),
        "NATE_NTM_NATE_OHA_EXECUTABLE": "nate-oha",
        "NATE_NTM_NATE_OHA_CONFIG": str(base_config.relative_to(project_dir)),
        "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
    }

    config = load_runtime_config(env=env)

    meta = SimpleNamespace(agent_id="agent-1", display_name="Agent One", conversation_id="")

    spec = build_nate_oha_launch_spec(config=config, metadata=meta)
    assert isinstance(spec, NateOhaLaunchSpec)

    # Basic fields should be populated from the runtime config.
    assert spec.executable == "nate-oha"
    assert spec.base_config == base_config.resolve()
    assert spec.cwd == project_dir.resolve()
    assert spec.runtime_mode == "echo"

    # No conversation ID or Agent Mail configuration when metadata and
    # config do not supply them.
    assert spec.conversation_id is None
    assert spec.agent_mail_enabled is None
    assert spec.agent_mail_project is None
    assert spec.agent_mail_agent_identity is None
    assert spec.agent_mail_credentials_ref is None
    assert spec.agent_mail_upstream_url is None

    argv = list(spec.to_argv())

    # Command prefix and --config should match the spec fields.
    assert argv[:4] == [
        "nate-oha",
        "acp",
        "--config",
        str(base_config.resolve()),
    ]

    # No --resume argument should be present when there is no
    # conversation ID.
    assert "--resume" not in argv

    # With only runtime.mode configured, exactly one --set argument
    # should be present and it should target runtime.mode.
    assert argv[4:] == ["--set", "runtime.mode=echo"]


def test_build_nate_oha_launch_spec_with_conversation_and_agent_mail(tmp_path: Path) -> None:
    """Full config yields resume flag and Agent Mail overrides.

    This exercises the mapping for the common case where:

    * a conversation ID has been persisted in :class:`AgentState`,
    * LLM and prompt overrides are configured,
    * Agent Mail integration is explicitly enabled for the swarm.
    """

    project_dir = tmp_path / "project_full"
    project_dir.mkdir()

    _project_root, base_config = _make_base_paths(project_dir)

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_dir),
        "NATE_NTM_NATE_OHA_EXECUTABLE": "nate-oha",
        "NATE_NTM_NATE_OHA_CONFIG": str(base_config.relative_to(project_dir)),
        "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
        "NATE_NTM_LLM_MODEL": "gpt-test-1",
        "NATE_NTM_LLM_API_KEY": "secret-key",
        "NATE_NTM_PROMPT_SOUL_CONTENT": "Hello from test",
        "NATE_NTM_AGENT_MAIL_ENABLED": "true",
        "NATE_NTM_AGENT_MAIL_PROJECT": "test-project",
        "NATE_NTM_AGENT_MAIL_URL": "https://agent-mail.invalid/mcp",
    }

    config = load_runtime_config(env=env)

    meta = SimpleNamespace(
        agent_id="agent-1",
        display_name="Agent One",
        agent_mail_identity="agent-mail-identity",
        agent_mail_credentials_ref="secret-token-ref",
        conversation_id="conv-123",
    )

    spec = build_nate_oha_launch_spec(config=config, metadata=meta)

    # Conversation and Agent Mail fields should be propagated into the
    # launch spec.
    assert spec.conversation_id == "conv-123"
    assert spec.agent_mail_enabled is True
    assert spec.agent_mail_project == "test-project"
    assert spec.agent_mail_agent_identity == "agent-mail-identity"
    assert spec.agent_mail_credentials_ref == "secret-token-ref"
    assert spec.agent_mail_upstream_url == "https://agent-mail.invalid/mcp"

    # LLM and prompt configuration come from the runtime config.
    assert spec.model == "gpt-test-1"
    assert spec.api_key == "secret-key"
    assert spec.prompt_soul_content == "Hello from test"

    argv = list(spec.to_argv())

    # The prefix and resume flag should be present.
    assert argv[:6] == [
        "nate-oha",
        "acp",
        "--config",
        str(base_config.resolve()),
        "--resume",
        "conv-123",
    ]

    # The remaining arguments should be a deterministic sequence of
    # --set path=value pairs covering runtime.mode, LLM, prompt, and
    # Agent Mail configuration. The order is defined by sorting the
    # configuration paths lexicographically.
    expected_tail = [
        "--set",
        "features.agent_mail.agent_identity=agent-mail-identity",
        "--set",
        "features.agent_mail.credentials_ref=secret-token-ref",
        "--set",
        "features.agent_mail.enabled=true",
        "--set",
        "features.agent_mail.project=test-project",
        "--set",
        "features.agent_mail.upstream_url=https://agent-mail.invalid/mcp",
        "--set",
        "llm.api_key=secret-key",
        "--set",
        "llm.model=gpt-test-1",
        "--set",
        "prompt.soul_content=Hello from test",
        "--set",
        "runtime.mode=echo",
    ]

    assert argv[6:] == expected_tail


def test_build_effective_nate_oha_config_uses_launch_spec_overrides(
    tmp_path: Path,
    monkeypatch,
) -> None:
    """build_effective_nate_oha_config reuses the launch-spec override mapping.

    This ensures that the effective nate-oha configuration seen by the
    runtime is derived from the same base-config-plus-overrides contract
    that drives the CLI argv construction.
    """

    project_dir = tmp_path / "project_effective_config"
    project_dir.mkdir()

    _project_root, base_config = _make_base_paths(project_dir)

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_dir),
        "NATE_NTM_NATE_OHA_EXECUTABLE": "nate-oha",
        "NATE_NTM_NATE_OHA_CONFIG": str(base_config.relative_to(project_dir)),
        "NATE_NTM_NATE_OHA_RUNTIME_MODE": "agent",
        "NATE_NTM_LLM_MODEL": "gpt-test-1",
        "NATE_NTM_LLM_API_KEY": "secret-key",
        "NATE_NTM_PROMPT_SOUL_CONTENT": "Hello from test",
        "NATE_NTM_AGENT_MAIL_ENABLED": "true",
        "NATE_NTM_AGENT_MAIL_PROJECT": "test-project",
        "NATE_NTM_AGENT_MAIL_URL": "https://agent-mail.invalid/mcp",
    }

    config = load_runtime_config(env=env)
    meta = SimpleNamespace(
        agent_id="agent-1",
        display_name="Agent One",
        agent_mail_identity="agent-mail-identity",
        agent_mail_credentials_ref="secret-token-ref",
        conversation_id="conv-123",
    )

    spec = build_nate_oha_launch_spec(config=config, metadata=meta)
    expected_overrides = sorted(spec.iter_overrides())

    calls: dict[str, object] = {}
    sentinel = object()

    def fake_load_nate_oha_config(base_config_path, overrides=None):
        calls["base_config_path"] = base_config_path
        calls["overrides"] = list(overrides) if overrides is not None else None
        return sentinel

    monkeypatch.setattr(
        "nate_ntm.runtime.nate_oha_launch.load_nate_oha_config",
        fake_load_nate_oha_config,
    )

    result = build_effective_nate_oha_config(config=config, metadata=meta)

    assert result is sentinel
    assert "base_config_path" in calls
    assert "overrides" in calls
    assert Path(calls["base_config_path"]) == spec.base_config
    assert calls["overrides"] is not None
    assert sorted(calls["overrides"]) == expected_overrides

