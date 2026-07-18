"""Quickstart-style integration tests for US2 swarm resume semantics.

These tests correspond to T025 in ``tasks.md`` and exercise a thin
end-to-end path from a project directory on disk through:

* ``RuntimeConfig`` resolution for that project.
* ``RuntimeDaemon.create`` in ``create`` mode with a small agent set.
* Clean shutdown of the initial runtime instance.
* ``RuntimeDaemon.resume`` startup semantics against the same metadata.
* ``RuntimeApiServer`` handlers for ``runtime.get_status`` and
  ``swarm.get_overview``.

The goal for this US2 slice is to validate FR-009 and SC-002 at a
basic level under the ConfigOverhaul MS2 model: when a swarm is created
and later resumed, all agents must reuse their persisted Agent Mail
identities and ACP conversation identifiers, and the runtime API must
continue to expose consistent swarm/agent views for the resumed
instance.

Unlike earlier iterations that relied on legacy per-agent Agent Mail
fields, these tests treat the embedded :class:`nate_oha.config.NateOHAConfig`
as the single source of truth for Agent Mail configuration. Agent Mail
identities are read **only** from
``AgentState.nate_oha_config.features.agent_mail.agent_identity``; if an
agent lacks a valid effective configuration, the tests fail.

To keep the tests self-contained and runnable without a real Agent Mail
service or ACP backend, a small in-memory stub adapter is injected via
:func:`nate_ntm.runtime.adapters.create_runtime_adapters`. This stub
mimics the idempotent project/identity behaviour of the production
MCP-backed client without performing any network I/O.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Iterable, Tuple

import pytest
from nate_oha.config import AgentMailFeatureConfig, FeaturesConfig, build_default_config

from nate_ntm.api.server import RuntimeApiServer
from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import AcpAgentStatus, BaseAcpClient
from nate_ntm.runtime.adapters import RuntimeAdapters
from nate_ntm.runtime.agent_mail_client import BaseAgentMailClient
from nate_ntm.runtime.daemon import RuntimeDaemon
from nate_ntm.runtime.nate_oha_launch import materialize_nate_oha_config
from nate_ntm.runtime.state import RuntimeStatus
from nate_ntm.runtime.swarm_state import AgentState


class _StubAgentMailClient(BaseAgentMailClient):
    """In-memory Agent Mail stub for tests.

    This stub mirrors the shape of :class:`McpAgentMailClient` sufficiently
    for US2 tests while avoiding any external network calls. Identities and
    tokens are derived deterministically from ``agent_id`` so that
    create/resume flows can assert on stability without persisting any
    additional state.
    """

    def __init__(self, config: RuntimeConfig) -> None:  # pragma: no cover - trivial
        self.config = config

    def ensure_project(self) -> str:
        # Mirror the production semantics: prefer an explicit project key,
        # otherwise fall back to the absolute project path.
        return (self.config.agent_mail_project or str(self.config.project_path)).strip()

    def ensure_agent_identity_with_credentials(
        self, agent_id: str, credentials_hint: str | None = None
    ) -> Tuple[str, str | None]:
        # Deterministic identity/token mapping so tests can rely on stable
        # values across create→resume flows.
        identity = f"identity-{agent_id}"
        token = credentials_hint or f"token-{agent_id}"
        return identity, token

    def get_unread_mail_flags(self, agent_ids: Iterable[str]) -> Dict[str, bool]:
        # For these tests we treat all agents as having no unread mail.
        return {agent_id: False for agent_id in agent_ids}


class _StubAcpClient(BaseAcpClient):
    """Minimal ACP stub used to satisfy RuntimeDaemon wiring.

    The US2 resume tests do not exercise ACP behaviour directly; this stub
    simply provides no-op implementations of the abstract methods so that
    :class:`RuntimeDaemon` and :class:`RuntimeScheduler` can be constructed
    without talking to a real ACP backend.
    """

    def __init__(self, config: RuntimeConfig) -> None:  # pragma: no cover - trivial
        self.config = config

    def start_agent(self, agent_id: str, *, metadata: AgentState) -> None:  # pragma: no cover - unused stub
        return None

    async def start_agent_async(self, agent_id: str, *, metadata: AgentState) -> None:  # pragma: no cover - unused
        return None

    def stop_agent(self, agent_id: str, *, timeout: float) -> None:  # pragma: no cover - unused stub
        return None

    async def stop_agent_async(self, agent_id: str, *, timeout: float) -> None:  # pragma: no cover - unused
        return None

    async def prompt(self, agent_id: str, prompt: str | None = None) -> str | None:  # pragma: no cover - unused
        return None

    async def interrupt(self, agent_id: str) -> None:  # pragma: no cover - unused
        return None

    def get_status(self, agent_id: str) -> AcpAgentStatus:  # pragma: no cover - unused
        return AcpAgentStatus(agent_id=agent_id, state="idle")


def _install_stub_adapters(monkeypatch: pytest.MonkeyPatch) -> None:
    """Patch ``create_runtime_adapters`` to use in-memory stubs.

    This keeps the US2 quickstart tests hermetic: no real Agent Mail or ACP
    services are contacted, but the runtime still observes the same
    high-level adapter contract it would with production implementations.
    """

    # Patch the symbol imported into ``nate_ntm.runtime.daemon`` so that
    # both ``RuntimeDaemon.create`` and ``RuntimeDaemon.resume`` see the
    # stubbed adapters. Also patch the shared factory in
    # :mod:`nate_ntm.runtime.adapters` and the helper used by
    # :mod:`nate_ntm.runtime.runner` so that higher-level orchestration
    # helpers remain hermetic as well.
    from nate_ntm.runtime import adapters as runtime_adapters_module
    from nate_ntm.runtime import daemon as runtime_daemon
    from nate_ntm.runtime import runner as runtime_runner

    def _make_adapters(config: RuntimeConfig) -> RuntimeAdapters:
        return RuntimeAdapters(
            agent_mail=_StubAgentMailClient(config=config),
            acp=_StubAcpClient(config=config),
        )

    monkeypatch.setattr(runtime_daemon, "create_runtime_adapters", _make_adapters)
    monkeypatch.setattr(runtime_runner, "create_runtime_adapters", _make_adapters)
    monkeypatch.setattr(runtime_adapters_module, "create_runtime_adapters", _make_adapters)


def _get_identity_and_conversation_from_config(meta: AgentState) -> Tuple[str, str]:
    """Return the Agent Mail identity and conversation ID for ``meta``.

    The authoritative source of Agent Mail configuration is the effective
    :class:`NateOHAConfig` embedded in :class:`AgentState`. These tests read
    the identity **only** from
    ``meta.nate_oha_config.features.agent_mail.agent_identity`` and treat
    missing or empty values as test failures.
    """

    cfg = meta.nate_oha_config
    features = getattr(cfg, "features", None)
    if features is None:
        raise AssertionError("Agent nate_oha_config is missing features configuration")

    agent_mail_cfg = getattr(features, "agent_mail", None)
    if agent_mail_cfg is None:
        raise AssertionError("Agent NateOhaConfig.features.agent_mail is not configured")

    if not getattr(agent_mail_cfg, "enabled", False):
        raise AssertionError("Agent Mail feature is disabled in NateOhaConfig")

    identity = (getattr(agent_mail_cfg, "agent_identity", "") or "").strip()
    if not identity:
        raise AssertionError("Agent Mail identity is empty in embedded NateOhaConfig")

    conversation_id = meta.conversation_id or ""
    return identity, conversation_id


def _make_config(tmp_path: Path) -> RuntimeConfig:
    """Construct a RuntimeConfig suitable for US2 create/resume tests.

    This helper creates a temporary project directory, materializes a
    minimal nate-oha JSON configuration, and enables Agent Mail so that
    :class:`RuntimeDaemon.create` can derive an effective
    :class:`NateOHAConfig` for each agent.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    base_cfg = build_default_config()
    base_config_path = materialize_nate_oha_config(config=base_cfg)

    return load_runtime_config(
        project_path=project,
        nate_oha_config_path=base_config_path,
        nate_oha_runtime_mode="echo",
        agent_mail_enabled=True,
        agent_mail_project="mail-project-1",
        agent_mail_upstream_url="https://agent-mail.invalid/mcp",
    )


def _create_swarm_with_agents(config: RuntimeConfig, agent_count: int) -> Dict[str, Tuple[str, str]]:
    """Create a new swarm with ``agent_count`` agents via RuntimeDaemon.create.

    The returned mapping records the embedded Agent Mail identity and
    conversation ID for each agent so that the US2 test can assert that
    these values are reused on resume.
    """

    daemon = RuntimeDaemon.create(config, agent_count=agent_count)
    daemon.start()

    swarm = daemon.swarm_state
    identities: Dict[str, Tuple[str, str]] = {}
    for agent_id, meta in swarm.agents.items():
        identities[agent_id] = _get_identity_and_conversation_from_config(meta)

    # Drive a clean, in-process shutdown to mirror the quickstart flow.
    daemon.request_shutdown()
    daemon.mark_stopped()

    return identities


def test_resume_swarm_us2_reuses_agent_identities_and_conversations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """US2: resume reuses Agent Mail identities and ACP conversations.

    This test exercises a simple create → shutdown → resume cycle for a
    small stubbed swarm and asserts that the resumed runtime observes the
    same Agent Mail identities and ACP conversation identifiers for each
    agent as were persisted at creation time. Identities are read directly
    from the embedded :class:`NateOHAConfig` for each agent.
    """

    # Arrange: build a runtime config with nate-oha and Agent Mail enabled
    # and install stub integration adapters.
    config = _make_config(tmp_path)
    _install_stub_adapters(monkeypatch)

    # Create a swarm with two agents and capture their identities.
    identities_before = _create_swarm_with_agents(config, agent_count=2)

    # Act: resume the swarm from the same project metadata.
    daemon = RuntimeDaemon.resume(config)
    daemon.start()

    # The resumed daemon should report ``Running`` at the runtime level.
    assert daemon.state.status is RuntimeStatus.RUNNING

    # The swarm state loaded on resume must contain the same agents and the
    # same identity/conversation tuples as at creation time.
    swarm_after = daemon.swarm_state
    identities_after: Dict[str, Tuple[str, str]] = {}
    for agent_id, meta in swarm_after.agents.items():
        identities_after[agent_id] = _get_identity_and_conversation_from_config(meta)

    assert identities_after == identities_before

    # Sanity-check: the runtime API still exposes consistent status and
    # overview information for the resumed swarm.
    server = RuntimeApiServer(daemon=daemon)

    status = server.get_runtime_status()
    assert status["status"] == RuntimeStatus.RUNNING.value
    assert status["project_path"] == str(config.project_path)
    assert status["swarm_id"] == config.swarm_id

    counts = status["agent_counts"]
    assert counts["total"] == len(identities_before)

    overview = server.get_swarm_overview()
    assert overview["swarm_id"] == config.swarm_id
    assert overview["project_path"] == str(config.project_path)
    assert overview["runtime_status"] == RuntimeStatus.RUNNING.value
    assert len(overview["agents"]) == len(identities_before)
