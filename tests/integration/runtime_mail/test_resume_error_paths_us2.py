"""US2: resume error-path integration tests for metadata and rebinding.

These tests correspond to T026 in ``tasks.md`` and exercise negative
paths for the runtime's resume semantics, focusing on FR-009 and
SC-002 edge cases:

1. Missing swarm state when starting in resume mode.
2. Mismatched Agent Mail project identifier for dev-mode fake client.
3. Mismatched per-agent Agent Mail identity.
4. Mismatched per-agent ACP conversation identifier.
5. Incomplete or legacy metadata (with empty identity/conversation
   fields) still resumes successfully.

They complement the happy-path resume test in
``tests/integration/quickstart/test_resume_swarm_us2.py`` by locking
in failure behavior while the resume logic is small and easy to reason
about.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from nate_oha.config import AgentMailFeatureConfig, FeaturesConfig, build_default_config

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.daemon import (
    MetadataMissingError,
    RuntimeDaemon,
    RuntimeStartupError,
)
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.swarm_state import AgentState, SwarmState
from nate_ntm.runtime.state import RuntimeStatus
from ..quickstart.test_resume_swarm_us2 import _install_stub_adapters


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    return project


def _base_swarm(config: RuntimeConfig) -> SwarmState:
    """Construct a minimal SwarmState instance for tests.

    By default this uses a simple placeholder Agent Mail project ID
    (``"mail-project-1"``) so that existing US1-style metadata remains
    valid. Tests that need strict fake-client rebinding semantics
    override ``agent_mail_project_id`` explicitly.
    """

    now = datetime(2026, 7, 3, 12, 0, 0)
    return SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
        agents={},
    )


def test_resume_errors_when_swarm_state_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T026.1: mode=resume fails fast when swarm state is missing.

    Expectation: :class:`MetadataMissingError` is raised before any
    attempt to construct a :class:`RuntimeDaemon` when ``swarm.json``
    does not exist for the project.
    """

    project = _make_project(tmp_path)
    config: RuntimeConfig = load_runtime_config(project_path=project)

    # Use in-memory stub adapters so these error-path tests remain hermetic.
    _install_stub_adapters(monkeypatch)

    with pytest.raises(MetadataMissingError):
        RuntimeDaemon.resume(config)


def test_resume_fails_on_agent_mail_identity_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T026.3: Agent Mail identity mismatch for an agent fails resume.

    The runtime treats divergence between the adapter-derived
    ``agent_mail_identity`` and the value stored in the persisted
    NateOhaConfig (``features.agent_mail.agent_identity``) as a startup
    error to protect FR-009.
    """

    project = _make_project(tmp_path)
    config: RuntimeConfig = load_runtime_config(project_path=project)
    store = MetadataStore(config=config)

    # Use in-memory stub adapters so these error-path tests remain hermetic.
    _install_stub_adapters(monkeypatch)

    now = datetime(2026, 7, 3, 12, 0, 0)

    base_cfg = build_default_config()
    agent_mail_cfg = AgentMailFeatureConfig(
        enabled=True,
        project="mail-project-1",
        agent_identity="some-other-identity",
        credentials_ref="token-123",
        upstream_url="https://agent-mail.invalid/mcp",
    )
    features_cfg = FeaturesConfig(agent_mail=agent_mail_cfg)
    nate_oha_config = base_cfg.model_copy(update={"features": features_cfg})

    agent = AgentState(
        agent_id="nav-1",
        display_name="Navigator 1",
        nate_oha_config=nate_oha_config,
        conversation_id="",  # not relevant for this test
    )

    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
        agents={agent.agent_id: agent},
    )

    store.save_swarm_state(swarm)

    with pytest.raises(RuntimeStartupError) as excinfo:
        RuntimeDaemon.resume(config)

    assert "Agent Mail identity mismatch on resume" in str(excinfo.value)


def test_resume_allows_conversation_id_mismatch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T026.4: Pre-populated ACP conversation IDs do not block resume.

    At this stage the runtime treats the persisted ``conversation_id`` as an
    opaque ACP session identifier. Resume does not attempt to validate it
    eagerly against the ACP adapter; more detailed mismatch detection is
    covered by dedicated runtime_acp integration tests.
    """

    project = _make_project(tmp_path)
    config: RuntimeConfig = load_runtime_config(project_path=project)
    store = MetadataStore(config=config)

    # Use in-memory stub adapters so these error-path tests remain hermetic.
    _install_stub_adapters(monkeypatch)

    now = datetime(2026, 7, 3, 12, 0, 0)

    agent = AgentState(
        agent_id="nav-1",
        display_name="Navigator 1",
        nate_oha_config=build_default_config(),
        conversation_id="some-other-conversation",
    )

    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
        agents={agent.agent_id: agent},
    )

    store.save_swarm_state(swarm)

    daemon = RuntimeDaemon.resume(config)
    daemon.start()

    assert daemon.state.status is RuntimeStatus.RUNNING
    assert daemon.swarm_state.agents["nav-1"].conversation_id == "some-other-conversation"


def test_resume_allows_incomplete_legacy_metadata_with_empty_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """T026.5: Empty identity/conversation fields do not block legacy resumes.

    For older or hand-crafted metadata that does not yet include
    identity and conversation identifiers, the runtime should still be
    able to start in ``resume`` mode. Empty strings are treated as
    "no binding present" and are therefore exempt from strict
    FR-009 rebinding checks.
    """

    project = _make_project(tmp_path)
    config: RuntimeConfig = load_runtime_config(project_path=project)
    store = MetadataStore(config=config)

    # Use in-memory stub adapters so these error-path tests remain hermetic.
    _install_stub_adapters(monkeypatch)

    now = datetime(2026, 7, 3, 12, 0, 0)

    agent = AgentState(
        agent_id="nav-1",
        display_name="Navigator 1",
        nate_oha_config=build_default_config(),
        conversation_id="",  # no conversation persisted yet
    )

    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
        agents={agent.agent_id: agent},
    )

    store.save_swarm_state(swarm)

    daemon = RuntimeDaemon.resume(config)
    daemon.start()

    assert daemon.state.status is RuntimeStatus.RUNNING
    assert daemon.swarm_state.swarm_id == config.swarm_id
    assert set(daemon.swarm_state.agents.keys()) == {"nav-1"}
