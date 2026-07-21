from __future__ import annotations

import json
from pathlib import Path

import pytest

from nate_ntm.config.runtime_config import load_runtime_config
from nate_ntm.runtime.nate_oha_launch import (
    build_effective_nate_oha_config,
    build_nate_oha_launch_spec,
    materialize_nate_oha_config,
)
from nate_ntm.runtime.swarm_state import AgentState
from nate_oha.config import build_default_config


def _base_config(project: Path) -> Path:
    path = project / "nate-oha.json"
    path.write_text(build_default_config().model_dump_json(), encoding="utf-8")
    return path


def test_generated_nate_oha_config_and_launch_spec_are_complete(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    base = _base_config(project)
    config = load_runtime_config(
        project_path=project,
        nate_oha_config_path=base,
        nate_oha_runtime_mode="agent",
        llm_model="gpt-test",
        prompt_soul_content="ship it",
        agent_mail_enabled=True,
        agent_mail_project="mail-project",
        agent_mail_upstream_url="https://mail.invalid/mcp",
        env={},
    )

    effective = build_effective_nate_oha_config(
        config=config,
        agent_mail_identity="agent-one",
        agent_mail_credentials_ref="registration-token",
    )
    agent_mail = effective.features.agent_mail
    assert effective.runtime.mode == "agent"
    assert effective.llm.model == "gpt-test"
    assert effective.prompt.soul_content == "ship it"
    assert agent_mail.enabled is True
    assert agent_mail.project == Path("mail-project")
    assert agent_mail.agent_identity == "agent-one"
    assert agent_mail.credentials_ref == "registration-token"
    assert agent_mail.upstream_url == "https://mail.invalid/mcp"

    metadata = AgentState(
        agent_id="agent-1",
        display_name="Agent One",
        conversation_id="conversation-1",
        nate_oha_config=effective,
    )
    argv = list(build_nate_oha_launch_spec(config=config, metadata=metadata).to_argv())
    assert argv[:4] == ["nate-oha", "acp", "--config", str(base)]
    assert argv[4:6] == ["--resume", "conversation-1"]
    assert "runtime.mode=agent" in argv
    assert "llm.model=gpt-test" in argv

    materialized = json.loads(materialize_nate_oha_config(config=effective).read_text())
    assert materialized["features"]["agent_mail"]["agent_identity"] == "agent-one"
    assert "conversation-1" not in json.dumps(materialized)


@pytest.mark.parametrize(
    ("config_path", "runtime_mode", "message"),
    [
        (None, "agent", "nate_oha_config_path"),
        ("base", None, "nate_oha_runtime_mode"),
    ],
)
def test_effective_config_rejects_missing_required_runtime_input(
    tmp_path: Path,
    config_path: str | None,
    runtime_mode: str | None,
    message: str,
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    base = _base_config(project)
    config = load_runtime_config(
        project_path=project,
        nate_oha_config_path=base if config_path else None,
        nate_oha_runtime_mode=runtime_mode,
        env={},
    )

    with pytest.raises(ValueError, match=message):
        build_effective_nate_oha_config(config=config)
