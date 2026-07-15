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
from nate_ntm.runtime.metadata_store import AgentMetadata


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

    sentinel_cfg = object()
    meta = AgentMetadata(
        agent_id="agent-1",
        display_name="Agent One",
        conversation_id="conv-123",
        nate_oha_config=sentinel_cfg,  # type: ignore[arg-type]
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

    assert calls["config"] is sentinel_cfg
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
    meta = AgentMetadata(agent_id="agent-1", display_name="Agent One")

    with pytest.raises(AcpClientError) as excinfo:
        client._build_command("agent-1", meta)

    msg = str(excinfo.value)
    assert "metadata.nate_oha_config" in msg
    assert "persisted Nate OHA configuration" in msg


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

    meta = AgentMetadata(
        agent_id="agent-1",
        display_name="Agent One",
        conversation_id="conv-123",
    )

    env = client._build_env("agent-1", meta)

    # Correlation variables should be present.
    assert env["NATE_NTM_PROJECT_PATH"] == str(config.project_path)
    assert env["NATE_NTM_SWARM_ID"] == config.swarm_id
    assert env["NATE_NTM_AGENT_ID"] == "agent-1"
    assert env["NATE_NTM_AGENT_CONVERSATION_ID"] == "conv-123"

    # A default model is always supplied unless explicitly overridden.
    assert env["LLM_MODEL"] == "openai/gpt-4o"


@dataclass
class _AgentMailConfigStub:
    enabled: bool = True
    project: str | None = None
    agent_identity: str | None = None
    credentials_ref: str | None = None
    upstream_url: str | None = None


@dataclass
class _FeaturesStub:
    agent_mail: _AgentMailConfigStub | None = None


@dataclass
class _NateOhaConfigStub:
    features: _FeaturesStub | None = None


def test_build_env_derives_agent_mail_from_persisted_config(tmp_path: Path, monkeypatch) -> None:
    """Agent Mail env vars are derived from metadata.nate_oha_config.features.

    When the persisted Nate OHA config includes an enabled Agent Mail
    feature, :meth:`_build_env` must translate it into the corresponding
    ``AGENT_MAIL_*`` variables regardless of any legacy runtime config or
    metadata fields.
    """

    monkeypatch.setattr(os, "environ", {})

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_mail_cfg = _AgentMailConfigStub(
        enabled=True,
        project="proj-1",
        agent_identity="agent@example.com",
        credentials_ref="token-123",
        upstream_url="https://mail.example.com/mcp",
    )
    nate_oha_cfg = _NateOhaConfigStub(features=_FeaturesStub(agent_mail=agent_mail_cfg))

    # Legacy Agent Mail hints are intentionally ignored when a config-driven
    # Agent Mail section is present.
    meta = AgentMetadata(
        agent_id="agent-1",
        display_name="Agent One",
        agent_mail_identity="legacy-identity",
        agent_mail_credentials_ref="legacy-token",
        nate_oha_config=nate_oha_cfg,  # type: ignore[arg-type]
    )

    env = client._build_env("agent-1", meta)

    assert env["AGENT_MAIL_PROJECT"] == "proj-1"
    assert env["AGENT_MAIL_AGENT"] == "agent@example.com"
    assert env["AGENT_MAIL_TOKEN"] == "token-123"
    assert env["AGENT_MAIL_UPSTREAM_URL"] == "https://mail.example.com/mcp"


def test_build_env_raises_when_legacy_agent_mail_without_config(tmp_path: Path, monkeypatch) -> None:
    """Legacy Agent Mail hints without a config section are rejected.

    Older persistence formats that relied on RuntimeConfig + AgentMetadata
    fields without a persisted NateOhaConfig are no longer supported. When
    such legacy configuration is present, :meth:`_build_env` raises
    :class:`AcpClientError` instead of attempting a best-effort launch.
    """

    monkeypatch.setattr(os, "environ", {})

    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)

    env = {
        "NATE_NTM_PROJECT_DIR": str(project_root),
        "NATE_NTM_AGENT_MAIL_PROJECT": "legacy-project",
        "NATE_NTM_AGENT_MAIL_URL": "https://mail.example.com/mcp",
    }
    config = load_runtime_config(env=env)
    client = NateOhaAcpClient(config=config)

    meta = AgentMetadata(
        agent_id="agent-1",
        display_name="Agent One",
        agent_mail_identity="legacy-identity",
        agent_mail_credentials_ref="legacy-token",
        # No nate_oha_config.features.agent_mail section.
    )

    with pytest.raises(AcpClientError) as excinfo:
        client._build_env("agent-1", meta)

    msg = str(excinfo.value)
    assert "Agent Mail metadata is present" in msg
    assert "NateOhaConfig.features.agent_mail" in msg
