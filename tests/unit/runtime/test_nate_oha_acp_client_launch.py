from __future__ import annotations

"""Unit tests for NateOhaAcpClient launch semantics.

These tests focus on how :class:`NateOhaAcpClient` constructs the nate-oha
command line and launch environment from persisted agent metadata.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict

import os

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import AcpClientError, NateOhaAcpClient
from nate_ntm.runtime.swarm_state import AgentState
from nate_oha.config import build_default_config


def _make_config(tmp_path: Path) -> RuntimeConfig:
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_root),
        # Explicitly set the executable so tests can assert on argv[0].
        "NATE_NTM_NATE_OHA_EXECUTABLE": "nate-oha",
    }

    return load_runtime_config(env=env)


def test_build_command_uses_persisted_nate_oha_config_and_conversation(tmp_path: Path, monkeypatch) -> None:
    """_build_command launches from metadata.nate_oha_config and forwards resume.

    When a persisted :class:`NateOhaConfig` is attached to the agent's
    metadata, :meth:`NateOhaAcpClient._build_command` must materialise that
    config and pass the resulting path via ``--config``. Any existing
    conversation ID on the metadata is forwarded via ``--resume``.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    nate_oha_cfg = build_default_config()
    meta = AgentState(
        agent_id="agent-1",
        display_name="Agent One",
        conversation_id="conv-123",
        nate_oha_config=nate_oha_cfg,
    )

    calls: Dict[str, object] = {}

    def fake_materialize_nate_oha_config(*, config: object) -> Path:  # type: ignore[override]
        calls["config"] = config
        materialized = tmp_path / "materialized" / "nate-oha-config.json"
        materialized.parent.mkdir(parents=True, exist_ok=True)
        materialized.write_text("{}", encoding="utf-8")
        return materialized

    monkeypatch.setattr(
        "nate_ntm.runtime.acp_client.materialize_nate_oha_config",
        fake_materialize_nate_oha_config,
    )

    argv = client._build_command("agent-1", meta)

    assert calls["config"] is nate_oha_cfg
    assert argv[:4] == ["nate-oha", "acp", "--config", str(tmp_path / "materialized" / "nate-oha-config.json")]
    assert argv[-2:] == ["--resume", "conv-123"]
    assert client._temp_config_dirs["agent-1"] == str((tmp_path / "materialized").resolve())


def test_build_command_requires_persisted_config(tmp_path: Path) -> None:
    """_build_command fails fast when metadata lacks nate_oha_config.

    Callers are expected to derive and persist an effective NateOhaConfig for
    each agent before attempting to launch nate-oha.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    @dataclass
    class MinimalMetadata:
        agent_id: str
        display_name: str
        conversation_id: str = ""
        # Intentionally omit ``nate_oha_config`` so that _build_command
        # treats this as missing persisted configuration.

    meta = MinimalMetadata(agent_id="agent-1", display_name="Agent One")

    with pytest.raises(AcpClientError) as excinfo:
        # Type hint for ``metadata`` is :class:`AgentState`, but the
        # implementation only relies on attribute access and rejects any
        # object lacking ``nate_oha_config``. Using a minimal stub here
        # allows the test to exercise the failure path without
        # constructing an invalid AgentState.
        client._build_command("agent-1", meta)  # type: ignore[arg-type]

    msg = str(excinfo.value)
    assert "metadata.nate_oha_config" in msg
    assert "persisted nate-oha configuration" in msg


def test_build_env_sets_conversation_id_and_correlation(tmp_path: Path, monkeypatch) -> None:
    """_build_env adds correlation variables including conversation ID.

    The environment inherited from the parent process is augmented with
    non-secret correlation identifiers so that downstream tooling and logs
    can associate nate-oha subprocesses with their originating swarm and
    agent.
    """

    # Start from a clean environment to make assertions deterministic.
    monkeypatch.setattr(os, "environ", {})

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    meta = AgentState(
        agent_id="agent-1",
        display_name="Agent One",
        conversation_id="conv-123",
        nate_oha_config=build_default_config(),
    )

    env = client._build_env("agent-1", meta)

    # Correlation variables should be present.
    assert env["NATE_NTM_PROJECT_PATH"] == str(config.project_path)
    assert env["NATE_NTM_SWARM_ID"] == config.swarm_id
    assert env["NATE_NTM_AGENT_ID"] == "agent-1"
    assert env["NATE_NTM_AGENT_CONVERSATION_ID"] == "conv-123"

    # A default model is always supplied unless explicitly overridden.
    assert env["LLM_MODEL"] == "openai/gpt-4o"

    # Milestone 2 removes any Agent Mail environment translation; configuration
    # must flow via NateOhaConfig instead.
    assert not any(name.startswith("AGENT_MAIL_") for name in env)



