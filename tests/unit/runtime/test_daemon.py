"""Unit tests for the RuntimeDaemon entrypoint and startup semantics.

Covers Phase 2 tasks T008 and T037 at the Python API level:

* Explicit `create` vs `resume` precondition checks.
* Construction of `RuntimeDaemon` in `resume` mode from existing
  metadata.
* Basic lifecycle transitions for `start`, `request_shutdown`, and
  `mark_stopped`.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
import os

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.agent_mail_client import FakeAgentMailClient
from nate_ntm.runtime.adapters import RuntimeAdapters
from nate_ntm.runtime.acp_client import AcpAgentStatus, AcpClientError, NateOhaAcpClient
from nate_ntm.runtime.events import AgentEventSource
from nate_ntm.runtime.daemon import (
    MetadataAlreadyExistsError,
    MetadataMissingError,
    RuntimeDaemon,
    RuntimeStartupError,
    StartupMode,
    check_startup_preconditions,
    _map_acp_state_to_last_known_status,
)
from nate_ntm.runtime.metadata_store import AgentMetadata, MetadataStore, SwarmMetadata
from nate_ntm.runtime.state import AgentRuntimeState, AgentStatus, RuntimeStatus


def _make_config(project_root: Path) -> RuntimeConfig:
    project_root.mkdir(parents=True, exist_ok=True)
    return load_runtime_config(project_path=project_root)


def _write_minimal_swarm_metadata(config: RuntimeConfig) -> None:
    store = MetadataStore(config=config)
    now = datetime(2026, 7, 3, 12, 0, 0)
    swarm = SwarmMetadata(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
    )
    store.save_swarm_metadata(swarm)


def test_check_startup_preconditions_create_fails_if_metadata_exists(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    _write_minimal_swarm_metadata(config)

    with pytest.raises(MetadataAlreadyExistsError) as excinfo:
        check_startup_preconditions(config, StartupMode.CREATE)

    msg = str(excinfo.value)
    assert "Swarm metadata already exists" in msg


def test_check_startup_preconditions_resume_fails_if_metadata_missing(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    with pytest.raises(MetadataMissingError) as excinfo:
        check_startup_preconditions(config, StartupMode.RESUME)

    msg = str(excinfo.value)
    assert "Swarm metadata not found" in msg


def test_runtime_daemon_resume_constructs_state_from_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_metadata(config)

    daemon = RuntimeDaemon.resume(config)

    assert daemon.config is config
    assert daemon.metadata_store.metadata_dir == config.metadata_dir
    assert daemon.swarm_metadata.swarm_id == config.swarm_id
    assert daemon.swarm_metadata.project_path == config.project_path

    assert daemon.state.config is config
    assert daemon.state.status is RuntimeStatus.STARTING
    assert daemon.startup_mode is StartupMode.RESUME
    assert daemon.started_at is None



def test_runtime_daemon_create_with_real_acp_persists_nate_oha_metadata(tmp_path: Path) -> None:
    """create() with REAL-style adapters persists Nate OHA metadata (T217).

    This exercises the happy-path RuntimeDaemon.create flow using REAL
    adapters for both ACP and Agent Mail. The nate-oha binary and the
    mcp_agent_mail server on 127.0.0.1:8765 are expected to be available.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    env = {
        "NATE_NTM_PROJECT_DIR": str(project),
        "NATE_NTM_ADAPTER_MODE": "real",
    }
    config = load_runtime_config(project_path=project, env=env)

    daemon = RuntimeDaemon.create(config, agent_count=1)

    # Metadata should be persisted for the configured agent.
    store = MetadataStore(config=config)
    meta = store.load_agent_metadata("agent-1")

    assert meta.agent_id == "agent-1"
    # REAL Agent Mail should assign a stable, non-empty identity.
    assert meta.agent_mail_identity
    assert meta.agent_mail_identity == meta.agent_mail_identity.strip()

    # The conversation ID must come from nate-oha via the ACP client and be
    # recorded into AgentMetadata.conversation_id.
    assert meta.conversation_id
    conv_from_acp = daemon.acp_client.ensure_conversation("agent-1")
    assert conv_from_acp == meta.conversation_id



def test_runtime_daemon_create_and_resume_with_real_acp_and_agent_mail(tmp_path: Path) -> None:
    """create() 	 resume() round-trip with REAL adapters (T217/T221).

    This verifies that:

    * RuntimeDaemon.create, when using REAL adapters, persists Nate OHA
      conversation IDs and Agent Mail identities into metadata, and
    * RuntimeDaemon.resume, when invoked with the same REAL configuration,
      reuses those identifiers rather than allocating new ones.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    env = {
        "NATE_NTM_PROJECT_DIR": str(project),
        "NATE_NTM_ADAPTER_MODE": "real",
    }
    config = load_runtime_config(project_path=project, env=env)

    # Initial create with REAL adapters.
    _ = RuntimeDaemon.create(config, agent_count=1)

    store = MetadataStore(config=config)
    meta_before = store.load_agent_metadata("agent-1")

    assert meta_before.agent_mail_identity
    assert meta_before.conversation_id

    # Fresh resume with the same REAL configuration should not change
    # identifiers.
    daemon_resume = RuntimeDaemon.resume(config)
    meta_after = daemon_resume.metadata_store.load_agent_metadata("agent-1")

    assert meta_after.agent_mail_identity == meta_before.agent_mail_identity
    assert meta_after.conversation_id == meta_before.conversation_id

    # The resumed ACP client must agree with the persisted conversation ID.
    conv_from_acp = daemon_resume.acp_client.ensure_conversation("agent-1")
    assert conv_from_acp == meta_after.conversation_id






def test_runtime_daemon_create_raises_if_metadata_already_exists(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_metadata(config)

    with pytest.raises(MetadataAlreadyExistsError):
        _ = RuntimeDaemon.create(config)


def test_runtime_daemon_start_and_shutdown_transitions(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_metadata(config)
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
    _write_minimal_swarm_metadata(config)
    daemon = RuntimeDaemon.resume(config)

    # Move the state to STOPPED manually to simulate prior lifecycle.
    daemon.state.status = RuntimeStatus.STOPPED

    with pytest.raises(RuntimeStartupError):
        daemon.start()


def test_runtime_daemon_get_runtime_status_aggregates_agent_counts(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_metadata(config)

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
    _write_minimal_swarm_metadata(config)

    daemon = RuntimeDaemon.resume(config)

    # Attach agent metadata for two agents.
    base_swarm = daemon.swarm_metadata
    a1_meta = AgentMetadata(agent_id="a1", display_name="Agent One")
    a2_meta = AgentMetadata(agent_id="a2", display_name="Agent Two")
    daemon.swarm_metadata = SwarmMetadata(
        swarm_id=base_swarm.swarm_id,
        project_path=base_swarm.project_path,
        agent_mail_project_id=base_swarm.agent_mail_project_id,
        created_at=base_swarm.created_at,
        last_updated_at=base_swarm.last_updated_at,
        config_version=base_swarm.config_version,
        agents={"a1": a1_meta, "a2": a2_meta},
        runtime_options=base_swarm.runtime_options,
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

    overview = daemon.get_swarm_overview()

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
    # consult any repository-level .env files.
    env_snapshot = dict(os.environ)
    config = load_runtime_config(project_path=project, env=env_snapshot)

    from nate_ntm.runtime.acp_client import NateOhaAcpClient

    client = NateOhaAcpClient(config=config)

    # Use a fake Agent Mail client so RuntimeDaemon.create can construct the
    # initial swarm metadata without external Agent Mail I/O. The ACP client
    # remains the real NateOhaAcpClient.
    agent_mail = FakeAgentMailClient(config=config)
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
    must persist that state into :attr:`AgentMetadata.last_known_status`.
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

    meta = AgentMetadata(
        agent_id="nav-1",
        display_name="Navigator 1",
        last_known_status="Idle",
    )
    swarm = SwarmMetadata(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
        agents={"nav-1": meta},
    )
    store.save_swarm_metadata(swarm)
    store.save_agent_metadata(meta)

    # Launch a real nate_OHA subprocess for ``nav-1`` so that
    # :meth:`NateOhaAcpClient.get_status` reports a ``"running"`` state via
    # its internal :class:`NateOhaProcessRecord`. We explicitly point the
    # adapter at the ``nate-oha`` binary used in this repository.
    client = NateOhaAcpClient(config=config, executable="nate-oha")
    client.start_agent("nav-1", metadata=meta)

    agent_mail = FakeAgentMailClient(config=config)
    adapters = RuntimeAdapters(agent_mail=agent_mail, acp=client)

    daemon = RuntimeDaemon.resume(config, adapters=adapters)

    # Sanity: the scheduler has not yet registered a runtime state entry.
    assert daemon.state.agents == {}

    detail = daemon.get_agent_detail(agent_id="nav-1", max_events=10)
    agent_payload = detail["agent"]
    assert agent_payload["status"] == AgentStatus.RUNNING.value

    reloaded_meta = store.load_agent_metadata("nav-1")
    assert reloaded_meta.last_known_status == AgentStatus.RUNNING.value

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
    :attr:`AgentMetadata.last_known_status` snapshot.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    env_snapshot = dict(os.environ)
    config = load_runtime_config(project_path=project, env=env_snapshot)

    store = MetadataStore(config=config)
    now = datetime(2026, 7, 3, 12, 0, 0)

    meta = AgentMetadata(
        agent_id="nav-1",
        display_name="Navigator 1",
        last_known_status=AgentStatus.FAILED.value,
    )
    swarm = SwarmMetadata(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
        agents={"nav-1": meta},
    )
    store.save_swarm_metadata(swarm)
    store.save_agent_metadata(meta)

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

    reloaded_meta = store.load_agent_metadata("nav-1")
    assert reloaded_meta.last_known_status == AgentStatus.FAILED.value

