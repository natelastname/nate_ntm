"""Unit tests for the RuntimeDaemon entrypoint and startup semantics.

Covers Phase 2 tasks T008 and T037 at the Python API level:

* Explicit `create` vs `resume` precondition checks.
* Construction of `RuntimeDaemon` in `resume` mode from existing
  metadata.
* Basic lifecycle transitions for `start`, `request_shutdown`, and
  `mark_stopped`.
"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Dict

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import (
    AcpAgentStatus,
    AcpClientError,
    BaseAcpClient,
    NateOhaAcpClient,
)
from nate_ntm.runtime.adapters import RuntimeAdapters
from nate_ntm.runtime.agent_mail_client import BaseAgentMailClient
from nate_ntm.runtime.daemon import (
    MetadataAlreadyExistsError,
    MetadataMissingError,
    RuntimeDaemon,
    RuntimeStartupError,
    StartupMode,
    _map_acp_state_to_last_known_status,
    check_startup_preconditions,
)
from nate_ntm.runtime.events import AgentEventSource
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.nate_oha_launch import build_effective_nate_oha_config
from nate_ntm.runtime.state import AgentRuntimeState, AgentStatus, RuntimeStatus
from nate_ntm.runtime.swarm_state import AgentState, SwarmState


def _make_config(project_root: Path) -> RuntimeConfig:
    project_root.mkdir(parents=True, exist_ok=True)
    return load_runtime_config(project_path=project_root)


def _write_minimal_swarm_state(config: RuntimeConfig) -> None:
    store = MetadataStore(config=config)
    now = datetime(2026, 7, 3, 12, 0, 0)
    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        # Agent Mail project ID is left empty for these minimal-metadata
        # tests; they do not exercise resume-time project-id validation.
        agent_mail_project_id="",
        created_at=now,
        last_updated_at=now,
    )
    store.save_swarm_state(swarm)


def _get_agent_mail_identity_from_config(agent: AgentState) -> str:
    """Extract the Agent Mail identity from an AgentState's NateOhaConfig.

    Helper for tests that need to assert FR-009-style identity stability
    without relying on legacy AgentState.agent_mail_identity fields.
    """

    cfg = getattr(agent, "nate_oha_config", None)
    if cfg is None:
        return ""
    features = getattr(cfg, "features", None)
    agent_mail_cfg = getattr(features, "agent_mail", None) if features is not None else None
    if agent_mail_cfg is None:
        return ""
    identity = getattr(agent_mail_cfg, "agent_identity", "") or ""
    return identity.strip()


@dataclass(slots=True)
class _StubAgentMailClient(BaseAgentMailClient):
    """Minimal in-memory Agent Mail client used by RuntimeDaemon tests.

    The daemon tests exercise Agent Mail wiring and metadata persistence
    semantics but do not require real Agent Mail network I/O. This stub
    provides a stable, side-effect free implementation of the
    :class:`BaseAgentMailClient` interface.
    """

    config: RuntimeConfig
    _project_id: str = "stub-mail-project"
    _identities: Dict[str, str] = field(default_factory=dict)

    def ensure_project(self) -> str:  # type: ignore[override]
        return self._project_id

    def ensure_agent_identity(self, agent_id: str) -> str:  # type: ignore[override]
        identity = self._identities.get(agent_id)
        if identity is None:
            identity = f"stub-mail-identity:{agent_id}"
            self._identities[agent_id] = identity
        return identity

    def ensure_agent_identity_with_credentials(  # type: ignore[override]
        self, agent_id: str, credentials_hint: str | None = None
    ) -> tuple[str, str | None]:
        # Preserve the default BaseAgentMailClient semantics of passing the
        # credential hint through unchanged.
        identity = self.ensure_agent_identity(agent_id)
        return identity, credentials_hint

    def get_unread_mail_flags(self, agent_ids):  # type: ignore[override]
        # Tests that use this stub either do not depend on unread-mail flags
        # or explicitly seed event streams; treat all agents as having no
        # unread mail by default.
        return {agent_id: False for agent_id in agent_ids}



def test_check_startup_preconditions_create_fails_if_metadata_exists(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    _write_minimal_swarm_state(config)

    with pytest.raises(MetadataAlreadyExistsError) as excinfo:
        check_startup_preconditions(config, StartupMode.CREATE)

    msg = str(excinfo.value)
    assert "Swarm state already exists" in msg


def test_check_startup_preconditions_resume_fails_if_metadata_missing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    with pytest.raises(MetadataMissingError) as excinfo:
        check_startup_preconditions(config, StartupMode.RESUME)

    msg = str(excinfo.value)
    assert "Swarm state not found" in msg


def test_runtime_daemon_resume_constructs_state_from_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_state(config)

    daemon = RuntimeDaemon.resume(config)

    assert daemon.config is config
    assert daemon.metadata_store.metadata_dir == config.metadata_dir
    assert daemon.swarm_state.swarm_id == config.swarm_id
    assert daemon.swarm_state.project_path == config.project_path

    assert daemon.state.config is config
    assert daemon.state.status is RuntimeStatus.STARTING
    assert daemon.startup_mode is StartupMode.RESUME
    assert daemon.started_at is None



def test_runtime_daemon_create_with_real_acp_persists_nate_oha_metadata(tmp_path: Path) -> None:
    """create() with REAL-style adapters persists nate-oha metadata (T217).

    This exercises the happy-path RuntimeDaemon.create flow using REAL
    adapters for both ACP and Agent Mail. The nate-oha binary and the
    mcp_agent_mail server on 127.0.0.1:8765 are expected to be available.
    """

    project = tmp_path / "project"
    # These REAL-adapter tests require a running Agent Mail MCP server.
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=1.0):
            pass
    except OSError:
        pytest.skip("Agent Mail server not available on 127.0.0.1:8765")


    project.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[3]
    base_config = repo_root / "nate-oha-profiles" / "profile1.json"

    env = {
        "NATE_NTM_PROJECT_DIR": str(project),
        "NATE_NTM_AGENT_MAIL_ADAPTER": "real",
        "NATE_NTM_NATE_OHA_CONFIG": str(base_config),
        "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
        "NATE_NTM_AGENT_MAIL_ENABLED": "true",
        "NATE_NTM_AGENT_MAIL_PROJECT": "mail-project-1",
        "NATE_NTM_AGENT_MAIL_URL": "http://127.0.0.1:8765/api",
    }
    config = load_runtime_config(project_path=project, env=env)

    daemon = RuntimeDaemon.create(config, agent_count=1)

    # Durable state should be persisted for the configured agent.
    store = MetadataStore(config=config)
    agent_state = store.load_agent_state("agent-1")

    assert agent_state.agent_id == "agent-1"

    # REAL Agent Mail should assign a stable, non-empty identity at the
    # NateOhaConfig level. The runtime no longer persists a separate
    # AgentState.agent_mail_identity field.
    identity = _get_agent_mail_identity_from_config(agent_state)
    assert identity
    assert identity == identity.strip()

    # At create-time no ACP conversation/session has been established yet,
    # so the conversation_id field should still be empty/None. It will be
    # filled in by async ACP lifecycle helpers (for example,
    # ``start_agent_async``) once a real ACP session is created.
    assert agent_state.conversation_id is None



def test_runtime_daemon_create_and_resume_with_real_acp_and_agent_mail(tmp_path: Path) -> None:
    """create() 	 resume() round-trip with REAL adapters (T217/T221).

    This verifies that:

    * RuntimeDaemon.create, when using REAL adapters, persists Agent Mail
      identities into metadata, and
    * RuntimeDaemon.resume, when invoked with the same REAL configuration,
      reuses those identifiers rather than allocating new ones.

    ACP conversation/session identifiers are established lazily by the async
    ACP lifecycle helpers (for example, ``start_agent_async``) and are not
    exercised by this unit test.
    """

    # These REAL-adapter tests require a running Agent Mail MCP server.
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=1.0):
            pass
    except OSError:
        pytest.skip("Agent Mail server not available on 127.0.0.1:8765")


    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[3]
    base_config = repo_root / "nate-oha-profiles" / "profile1.json"

    env = {
        "NATE_NTM_PROJECT_DIR": str(project),
        "NATE_NTM_AGENT_MAIL_ADAPTER": "real",
        "NATE_NTM_NATE_OHA_CONFIG": str(base_config),
        "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
        "NATE_NTM_AGENT_MAIL_ENABLED": "true",
        "NATE_NTM_AGENT_MAIL_PROJECT": "mail-project-1",
        "NATE_NTM_AGENT_MAIL_URL": "http://127.0.0.1:8765/api",
    }
    config = load_runtime_config(project_path=project, env=env)

    # Initial create with REAL adapters.
    _ = RuntimeDaemon.create(config, agent_count=1)

    store = MetadataStore(config=config)
    meta_before = store.load_agent_state("agent-1")

    identity_before = _get_agent_mail_identity_from_config(meta_before)
    assert identity_before

    # No ACP conversation/session has been created yet, so the
    # conversation_id field is expected to be empty/None at this stage.
    assert meta_before.conversation_id is None

    # Fresh resume with the same REAL configuration should not change
    # identifiers.
    daemon_resume = RuntimeDaemon.resume(config)
    meta_after = daemon_resume.metadata_store.load_agent_state("agent-1")

    identity_after = _get_agent_mail_identity_from_config(meta_after)
    assert identity_after == identity_before
    assert meta_after.conversation_id == meta_before.conversation_id



def test_runtime_daemon_create_populates_nate_oha_config_for_initial_agents(
    tmp_path: Path,
) -> None:
    """create() eagerly embeds NateOhaConfig into initial agent metadata (T217/T228).

    This exercises the create-mode path using stubbed adapters and a
    concrete nate-oha base configuration, verifying that the derived
    NateOhaConfig is persisted via SwarmState/AgentState and visible
    through both the swarm- and per-agent metadata views.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    # Create a minimal, schema-valid nate-oha JSON config on disk by
    # serializing the upstream default configuration. This avoids relying on
    # any particular sample profile layout while still exercising
    # load_nate_oha_config/build_effective_nate_oha_config end-to-end.
    from nate_oha.config import build_default_config

    base_config_path = project / "nate-oha-config.json"
    base_config = build_default_config()
    base_config_path.write_text(base_config.model_dump_json(indent=2), encoding="utf-8")

    env = {
        "NATE_NTM_PROJECT_DIR": str(project),
        "NATE_NTM_NATE_OHA_CONFIG": str(base_config_path),
        "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
    }
    config = load_runtime_config(project_path=project, env=env)

    # Use a stub Agent Mail adapter and a dummy ACP adapter that does not
    # interact with the real nate-oha binary. RuntimeDaemon.create only
    # requires that the ACP client exposes an on_event callback attribute.
    agent_mail = _StubAgentMailClient(config=config)

    class DummyAcpClient(BaseAcpClient):
        def start_agent(self, agent_id: str, *, metadata: AgentState) -> None:
            pass

        async def prompt(self, agent_id: str, prompt: str | None = None) -> str | None:
            return None

        async def interrupt(self, agent_id: str) -> None:
            return None

        def stop_agent(self, agent_id: str, *, timeout: float) -> None:
            pass

        def get_status(self, agent_id: str) -> AcpAgentStatus:
            return AcpAgentStatus(agent_id=agent_id, state="idle")

    acp = DummyAcpClient()
    adapters = RuntimeAdapters(agent_mail=agent_mail, acp=acp)

    daemon = RuntimeDaemon.create(config, agent_count=1, adapters=adapters)

    # Swarm and per-agent state should both expose a NateOhaConfig snapshot
    # for the newly created agent.
    store = MetadataStore(config=config)
    swarm = store.load_swarm_state()
    agent_state = store.load_agent_state("agent-1")

    assert "agent-1" in swarm.agents
    swarm_agent = swarm.agents["agent-1"]

    assert agent_state.nate_oha_config is not None
    assert swarm_agent.nate_oha_config is not None

    # Swarm- and per-agent views should agree on the serialized configuration.
    assert agent_state.nate_oha_config.model_dump() == swarm_agent.nate_oha_config.model_dump()






def test_runtime_daemon_create_raises_if_metadata_already_exists(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_state(config)

    with pytest.raises(MetadataAlreadyExistsError):
        _ = RuntimeDaemon.create(config)


def test_runtime_daemon_start_and_shutdown_transitions(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_state(config)
    daemon = RuntimeDaemon.resume(config)

    # Initially in STARTING state
    assert daemon.state.status is RuntimeStatus.STARTING
    assert daemon.state.shutdown_requested is False

    # After start(), runtime should be RUNNING and started_at set.
    daemon.start()
    assert daemon.state.status is RuntimeStatus.RUNNING
    assert isinstance(daemon.started_at, datetime)

    # Idempotent start() when already running should not fail.
    daemon.start()
    assert daemon.state.status is RuntimeStatus.RUNNING

    # Request shutdown moves to SHUTTING_DOWN from RUNNING.
    daemon.request_shutdown()
    assert daemon.state.shutdown_requested is True
    assert daemon.state.status is RuntimeStatus.SHUTTING_DOWN

    # Mark fully stopped.
    daemon.mark_stopped()
    assert daemon.state.status is RuntimeStatus.STOPPED


def test_runtime_daemon_start_rejects_invalid_transition(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_state(config)
    daemon = RuntimeDaemon.resume(config)

    # Move the state to STOPPED manually to simulate prior lifecycle.
    daemon.state.status = RuntimeStatus.STOPPED

    with pytest.raises(RuntimeStartupError):
        daemon.start()


def test_runtime_daemon_get_runtime_status_aggregates_agent_counts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_state(config)

    daemon = RuntimeDaemon.resume(config)

    # Seed runtime state with a mix of agent statuses.
    daemon.state.agents = {
        "a-start": AgentRuntimeState(agent_id="a-start", status=AgentStatus.STARTING),
        "a-idle": AgentRuntimeState(agent_id="a-idle", status=AgentStatus.IDLE),
        "a-run": AgentRuntimeState(agent_id="a-run", status=AgentStatus.RUNNING),
        "a-fail": AgentRuntimeState(agent_id="a-fail", status=AgentStatus.FAILED),
    }
    daemon.state.status = RuntimeStatus.RUNNING

    payload = daemon.get_runtime_status()

    assert payload["status"] == RuntimeStatus.RUNNING.value
    assert payload["project_path"] == str(config.project_path)
    assert payload["swarm_id"] == config.swarm_id

    counts = payload["agent_counts"]
    assert counts == {
        "total": 4,
        "starting": 1,
        "idle": 1,
        "running": 1,
        "waiting": 0,
        "failed": 1,
    }




def test_runtime_daemon_get_swarm_overview_joins_metadata_and_runtime_state(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_state(config)

    daemon = RuntimeDaemon.resume(config)

    # Attach durable agent state for two agents.
    base_swarm = daemon.swarm_state

    from nate_oha.config import build_default_config

    a1_state = AgentState(
        agent_id="a1",
        display_name="Agent One",
        nate_oha_config=build_default_config(),
    )
    a2_state = AgentState(
        agent_id="a2",
        display_name="Agent Two",
        nate_oha_config=build_default_config(),
    )
    daemon.swarm_state = base_swarm.model_copy(
        update={"agents": {"a1": a1_state, "a2": a2_state}}
    )

    # Runtime state includes two configured agents plus one extra.
    daemon.state.agents = {
        "a1": AgentRuntimeState(agent_id="a1", status=AgentStatus.RUNNING),
        "a2": AgentRuntimeState(
            agent_id="a2", status=AgentStatus.FAILED, last_error="boom"
        ),
        "orphan": AgentRuntimeState(agent_id="orphan", status=AgentStatus.IDLE),
    }
    daemon.state.status = RuntimeStatus.RUNNING

    overview = daemon.get_swarm_status()

    assert overview["swarm_id"] == config.swarm_id
    assert overview["project_path"] == str(config.project_path)
    assert overview["runtime_status"] == RuntimeStatus.RUNNING.value

    counts = overview["agent_counts"]
    assert counts["total"] == 3
    assert counts["running"] == 1
    assert counts["idle"] == 1
    assert counts["failed"] == 1

    agents_by_id = {a["agent_id"]: a for a in overview["agents"]}

    a1 = agents_by_id["a1"]
    assert a1["display_name"] == "Agent One"
    assert a1["status"] == AgentStatus.RUNNING.value
    assert a1["has_unread_mail"] is False
    assert a1["last_error"] is None

    a2 = agents_by_id["a2"]
    assert a2["display_name"] == "Agent Two"
    assert a2["status"] == AgentStatus.FAILED.value
    assert a2["has_unread_mail"] is False
    assert a2["last_error"] == "boom"

    orphan = agents_by_id["orphan"]
    # No metadata, so the display name should fall back to agent_id.
    assert orphan["display_name"] == "orphan"
    assert orphan["status"] == AgentStatus.IDLE.value
    assert orphan["has_unread_mail"] is False
    assert orphan["last_error"] is None












def test_runtime_daemon_acp_events_flow_into_supervisor_stream_with_nate_oha(
    tmp_path: Path,
) -> None:
    """Events from NateOhaAcpClient land in the AgentSupervisor stream (T230).

    This focuses on the wiring between :class:`BaseAcpClient.on_event` and
    :meth:`AgentSupervisor.append_agent_event`. We rely on a real
    :class:`NateOhaAcpClient` instance, which will perform its normal
    interaction with the ``nate-oha`` binary when constructed.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    # Take an explicit environment snapshot so the config loader does not
    # consult any repository-level .env files. Point Nate OHA at the same
    # sample profile used by the other NateOhaAcpClient tests so that
    # RuntimeDaemon.create can derive a concrete NateOHAConfig for the
    # initial agent.
    env_snapshot = dict(os.environ)
    repo_root = Path(__file__).resolve().parents[3]
    base_config = repo_root / "nate-oha-profiles" / "profile1.json"
    env_snapshot["NATE_NTM_NATE_OHA_CONFIG"] = str(base_config)
    env_snapshot["NATE_NTM_NATE_OHA_RUNTIME_MODE"] = "echo"
    config = load_runtime_config(project_path=project, env=env_snapshot)

    from nate_ntm.runtime.acp_client import NateOhaAcpClient

    client = NateOhaAcpClient(config=config)

    # Use a stub Agent Mail client so RuntimeDaemon.create can construct the
    # initial swarm metadata without external Agent Mail I/O. The ACP client
    # remains the real NateOhaAcpClient.
    agent_mail = _StubAgentMailClient(config=config)
    adapters = RuntimeAdapters(agent_mail=agent_mail, acp=client)

    daemon = RuntimeDaemon.create(config, agent_count=1, adapters=adapters)

    # Sanity: the daemon should be using our NateOhaAcpClient instance.
    assert daemon.acp_client is client

    # Synthesize a nate_OHA process-lifecycle event and deliver it via the
    # adapter's on_event callback. RuntimeDaemon.create should have wired this
    # callback to AgentSupervisor.append_agent_event.
    event = client._make_process_event(  # type: ignore[attr-defined]
        agent_id="agent-1",
        event_type="nate_oha_process_started",
        payload={"pid": 12345},
    )
    assert client.on_event is not None
    client.on_event(event)

    # The AgentSupervisor should now have a RuntimeState entry and event
    # stream for the agent with our synthesized event recorded.
    runtime_state = daemon.state.agents.get("agent-1")
    assert runtime_state is not None
    stream = runtime_state.event_stream
    assert stream is not None

    events = stream.get_events()
    assert len(events) == 1

    recorded = events[0]
    assert recorded.agent_id == "agent-1"
    assert recorded.source is AgentEventSource.ACP
    assert recorded.type == "nate_oha_process_started"
    assert recorded.payload["pid"] == 12345




def test_runtime_daemon_agent_detail_persists_running_status_from_nate_oha_acp(
    tmp_path: Path,
) -> None:
    """get_agent_detail persists a Running status from NateOhaAcpClient (T223).

    This test uses a real :class:`NateOhaAcpClient` instance and launches a
    nate_OHA subprocess for ``nav-1``. The adapter's ``get_status`` method
    reports a ``"running"`` state based on its internal
    :class:`NateOhaProcessRecord`, and :meth:`RuntimeDaemon.get_agent_detail`
    must persist that state into :attr:`AgentState.last_known_status`.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    # Take an explicit environment snapshot so the config loader does not
    # consult repository-level .env files. Point Nate OHA at the same
    # sample profile used by the real-path integration tests so that we
    # exercise a concrete launch spec rather than relying on
    # NateOhaAcpClient defaults.
    env_snapshot = dict(os.environ)
    repo_root = Path(__file__).resolve().parents[3]
    base_config = repo_root / "nate-oha-profiles" / "profile1.json"
    env_snapshot["NATE_NTM_NATE_OHA_CONFIG"] = str(base_config)
    env_snapshot["NATE_NTM_NATE_OHA_RUNTIME_MODE"] = "echo"

    config = load_runtime_config(project_path=project, env=env_snapshot)

    store = MetadataStore(config=config)
    now = datetime(2026, 7, 3, 12, 0, 0)

    # Build a NateOhaConfig for the agent using the same helper used by
    # RuntimeDaemon.create, but without any Agent Mail overrides. This
    # reflects the Milestone 2 design where the effective NateOhaConfig
    # is resolved once and then embedded into AgentState for persistence.
    nate_oha_cfg = build_effective_nate_oha_config(config=config)

    agent_state = AgentState(
        agent_id="nav-1",
        display_name="Navigator 1",
        last_known_status="Idle",
        nate_oha_config=nate_oha_cfg,
    )
    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
        agents={"nav-1": agent_state},
    )
    store.save_swarm_state(swarm)

    # Launch a real nate_OHA subprocess for ``nav-1`` so that
    # :meth:`NateOhaAcpClient.get_status` reports a ``"running"`` state via
    # its internal :class:`NateOhaProcessRecord`. We explicitly point the
    # adapter at the ``nate-oha`` binary used in this repository.
    client = NateOhaAcpClient(config=config, executable="nate-oha")
    client.start_agent("nav-1", metadata=agent_state)

    agent_mail = _StubAgentMailClient(config=config)
    adapters = RuntimeAdapters(agent_mail=agent_mail, acp=client)

    daemon = RuntimeDaemon.resume(config, adapters=adapters)

    # Sanity: the scheduler has not yet registered a runtime state entry.
    assert daemon.state.agents == {}

    detail = daemon.get_agent_detail(agent_id="nav-1", max_events=10)
    agent_payload = detail["agent"]
    assert agent_payload["status"] == AgentStatus.RUNNING.value

    reloaded_state = store.load_agent_state("nav-1")
    assert reloaded_state.last_known_status == AgentStatus.RUNNING.value

    # Best-effort cleanup of the nate_OHA process for this agent.
    client.stop_agent("nav-1", timeout=5.0)



def test_map_acp_state_to_last_known_status_core_mappings() -> None:
    """_map_acp_state_to_last_known_status handles core ACP states (T223).

    This unit-level test focuses on the pure mapping helper used by
    :meth:`RuntimeDaemon._refresh_last_known_status_from_acp` instead of
    stubbing :meth:`NateOhaAcpClient.get_status`. It verifies that the
    adapter-level states we expect from nate_OHA are translated into the
    persisted :class:`AgentStatus` values.
    """

    # Running maps directly to the AgentStatus.RUNNING string.
    assert _map_acp_state_to_last_known_status("running") == AgentStatus.RUNNING.value
    # Terminated/idle map to the simpler "Idle" snapshot.
    assert _map_acp_state_to_last_known_status("terminated") == AgentStatus.IDLE.value
    assert _map_acp_state_to_last_known_status("idle") == AgentStatus.IDLE.value
    # Failed propagates as "Failed".
    assert _map_acp_state_to_last_known_status("failed") == AgentStatus.FAILED.value

    # Transitional or unknown values do not overwrite an existing snapshot.
    assert _map_acp_state_to_last_known_status("starting") is None
    assert _map_acp_state_to_last_known_status("stopping") is None
    assert _map_acp_state_to_last_known_status("unknown") is None
    assert _map_acp_state_to_last_known_status("") is None

    # The helper is tolerant of case and surrounding whitespace.
    assert _map_acp_state_to_last_known_status("  RUNNING ") == AgentStatus.RUNNING.value



def test_runtime_daemon_agent_detail_falls_back_to_last_known_status_when_acp_absent(
    tmp_path: Path,
) -> None:
    """get_agent_detail uses persisted last_known_status when ACP is absent.

    This exercises the defensive path in
    :meth:`RuntimeDaemon._refresh_last_known_status_from_acp` by constructing a
    daemon with no ACP client configured. In that case, calls to
    :meth:`get_agent_detail` must rely solely on the persisted
    :attr:`AgentState.last_known_status` snapshot.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    env_snapshot = dict(os.environ)
    config = load_runtime_config(project_path=project, env=env_snapshot)

    store = MetadataStore(config=config)
    now = datetime(2026, 7, 3, 12, 0, 0)

    from nate_oha.config import build_default_config

    nate_oha_cfg = build_default_config()
    agent_state = AgentState(
        agent_id="nav-1",
        display_name="Navigator 1",
        last_known_status=AgentStatus.FAILED.value,
        nate_oha_config=nate_oha_cfg,
    )
    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        # Agent Mail project ID is intentionally left empty here; this test
        # focuses solely on last_known_status fallback behavior when ACP is
        # unavailable, not Agent Mail project validation.
        agent_mail_project_id="",
        created_at=now,
        last_updated_at=now,
        agents={"nav-1": agent_state},
    )
    store.save_swarm_state(swarm)

    # Construct a normal resume-mode daemon, then explicitly drop the ACP
    # client to simulate an environment where ACP status is unavailable.
    daemon = RuntimeDaemon.resume(config)
    daemon.acp_client = None

    assert daemon.state.agents == {}

    detail = daemon.get_agent_detail(agent_id="nav-1", max_events=10)
    agent_payload = detail["agent"]
    # When ACP status is unavailable, the daemon should fall back to the
    # last persisted snapshot rather than failing.
    assert agent_payload["status"] == AgentStatus.FAILED.value

    reloaded_state = store.load_agent_state("nav-1")
    assert reloaded_state.last_known_status == AgentStatus.FAILED.value

