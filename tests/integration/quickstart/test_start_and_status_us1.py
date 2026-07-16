"""Quickstart-style integration tests for US1 swarm startup and status.

These tests correspond to T020 in ``tasks.md`` and exercise a thin
end-to-end path from a project directory on disk through:

* ``RuntimeConfig`` resolution for that project.
* ``MetadataStore`` / ``SwarmMetadata`` / ``AgentMetadata`` persistence
  under ``.nate_ntm/``.
* ``RuntimeDaemon.resume`` startup semantics.
* ``RuntimeApiServer`` handlers for ``runtime.get_status`` and
  ``swarm.get_overview``.

The goal is to cover the spirit of SC-001 and the US1 acceptance
scenarios without requiring the full scheduler, Agent Mail, or ACP
integrations yet. Agent lifecycle behavior is simulated by seeding
``RuntimeState.agents`` directly; later tasks (T016/T017) will make
these states the product of real subprocess and scheduler activity.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Tuple

from nate_ntm.api.server import RuntimeApiServer
from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.daemon import RuntimeDaemon
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.swarm_state import AgentState, SwarmState
from nate_ntm.runtime.state import AgentRuntimeState, AgentStatus, RuntimeStatus


def _make_started_daemon_with_agents(
    tmp_path: Path,
) -> Tuple[RuntimeDaemon, RuntimeApiServer, RuntimeConfig]:
    """Create a started RuntimeDaemon with persisted swarm/agent metadata.

    This helper mirrors the "Given a valid project directory and
    configured external services" setup from US1:

    * A project directory exists on disk.
    * Swarm and agent metadata are written under ``.nate_ntm/``.
    * The runtime is started in ``resume`` mode, loading that metadata.
    * In-memory agent runtime state reflects a mix of lifecycle states.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    config: RuntimeConfig = load_runtime_config(project_path=project)
    store = MetadataStore(config=config)

    now = datetime(2026, 7, 3, 12, 0, 0)

    # Define three agents with minimal persisted state. The runtime
    # will use ``RuntimeState.agents`` for live status; these records
    # ensure that swarm-level state is also present and consistent.
    agent_running = AgentState(
        agent_id="nav-1",
        display_name="Navigator 1",
        last_known_status="Running",
    )
    agent_idle = AgentState(
        agent_id="nav-2",
        display_name="Navigator 2",
        last_known_status="Idle",
    )
    agent_failed = AgentState(
        agent_id="nav-3",
        display_name="Navigator 3",
        last_known_status="Failed",
    )

    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
        agents={
            agent_running.agent_id: agent_running,
            agent_idle.agent_id: agent_idle,
            agent_failed.agent_id: agent_failed,
        },
    )

    # Persist swarm state to disk to exercise the
    # MetadataStore layout and SwarmState validation on resume.
    store.save_swarm_state(swarm)

    # Construct a RuntimeDaemon in resume mode, which will re-load and
    # validate the swarm metadata we just wrote.
    daemon = RuntimeDaemon.resume(config)

    # Seed in-memory runtime state to simulate scheduler/agent behavior.
    daemon.state.agents = {
        "nav-1": AgentRuntimeState(
            agent_id="nav-1", status=AgentStatus.RUNNING
        ),
        "nav-2": AgentRuntimeState(
            agent_id="nav-2", status=AgentStatus.IDLE
        ),
        "nav-3": AgentRuntimeState(
            agent_id="nav-3",
            status=AgentStatus.FAILED,
            last_error="boom",
        ),
    }

    # Drive the high-level runtime lifecycle into ``Running``.
    daemon.start()

    server = RuntimeApiServer(daemon=daemon)
    return daemon, server, config


def test_start_and_status_us1_runtime_get_status_reports_running_and_counts(
    tmp_path: Path,
) -> None:
    """SC-001: runtime.get_status reports Running with accurate counts.

    From a standing start with valid configuration, this exercises a
    create-once-then-resume path where the runtime loads swarm metadata
    for a project and, once started, exposes aggregate agent counts via
    ``runtime.get_status``.
    """

    daemon, server, config = _make_started_daemon_with_agents(tmp_path)

    # Sanity-check daemon lifecycle state.
    assert daemon.state.status is RuntimeStatus.RUNNING

    payload = server.get_runtime_status()

    assert payload["status"] == RuntimeStatus.RUNNING.value
    assert payload["project_path"] == str(config.project_path)
    assert payload["swarm_id"] == config.swarm_id

    counts = payload["agent_counts"]
    assert counts == {
        "total": 3,
        "starting": 0,
        "idle": 1,
        "running": 1,
        "waiting": 0,
        "failed": 1,
    }


def test_start_and_status_us1_swarm_overview_returns_agent_summaries(
    tmp_path: Path,
) -> None:
    """US1: swarm.get_overview returns per-agent summaries and counts.

    This complements the previous test by validating that
    ``swarm.get_overview`` exposes:

    * Swarm-level identifiers and runtime status.
    * Aggregate agent counts consistent with ``runtime.get_status``.
    * Per-agent summaries that join persisted metadata (ID/display
      name) with live runtime status and last error.
    """

    daemon, server, config = _make_started_daemon_with_agents(tmp_path)

    overview = server.get_swarm_overview()

    assert overview["swarm_id"] == config.swarm_id
    assert overview["project_path"] == str(config.project_path)
    assert overview["runtime_status"] == RuntimeStatus.RUNNING.value

    # Agent counts should mirror those from runtime.get_status.
    counts = overview["agent_counts"]
    assert counts == {
        "total": 3,
        "starting": 0,
        "idle": 1,
        "running": 1,
        "waiting": 0,
        "failed": 1,
    }

    agents = {a["agent_id"]: a for a in overview["agents"]}
    assert set(agents.keys()) == {"nav-1", "nav-2", "nav-3"}

    a1 = agents["nav-1"]
    assert a1["display_name"] == "Navigator 1"
    assert a1["status"] == AgentStatus.RUNNING.value
    assert a1["has_unread_mail"] is False
    assert a1["last_error"] is None

    a2 = agents["nav-2"]
    assert a2["display_name"] == "Navigator 2"
    assert a2["status"] == AgentStatus.IDLE.value
    assert a2["has_unread_mail"] is False
    assert a2["last_error"] is None

    a3 = agents["nav-3"]
    assert a3["display_name"] == "Navigator 3"
    assert a3["status"] == AgentStatus.FAILED.value
    assert a3["has_unread_mail"] is False
    assert a3["last_error"] == "boom"



def test_scheduler_launches_agents_from_swarm_metadata(tmp_path: Path) -> None:
    """US1 dev-mode: scheduler launches agents based on SwarmMetadata.

    This test exercises the path where agent runtime state is derived from
    persisted swarm metadata via RuntimeDaemon.resume() and the
    RuntimeScheduler/AgentSupervisor wiring, rather than being manually
    seeded in tests. It validates that, in dev-mode, configured agents are
    treated as launched (Idle) once the runtime has started.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    config: RuntimeConfig = load_runtime_config(project_path=project)
    store = MetadataStore(config=config)

    now = datetime(2026, 7, 3, 12, 0, 0)

    a1 = AgentMetadata(agent_id="nav-1", display_name="Navigator 1")
    a2 = AgentMetadata(agent_id="nav-2", display_name="Navigator 2")

    swarm = SwarmMetadata(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
        agents={
            a1.agent_id: a1,
            a2.agent_id: a2,
        },
    )

    # Persist swarm and agent metadata to mirror the create-then-resume
    # layout. RuntimeDaemon.resume() will load the swarm metadata and
    # construct RuntimeState/scheduler wiring.
    store.save_swarm_metadata(swarm)
    store.save_agent_metadata(a1)
    store.save_agent_metadata(a2)

    daemon = RuntimeDaemon.resume(config)

    # Do not manually seed daemon.state.agents; instead rely on
    # RuntimeScheduler.start() → AgentSupervisor.launch_all_agents() to
    # derive runtime state from metadata and dev-mode "launch" behavior.
    assert daemon.state.agents == {}

    daemon.start()

    assert daemon.state.status is RuntimeStatus.RUNNING

    # After startup, runtime state should contain entries for the
    # configured agents, marked as Idle to represent dev-mode launched
    # subprocesses.
    assert set(daemon.state.agents.keys()) == {"nav-1", "nav-2"}

    for runtime_state in daemon.state.agents.values():
        assert runtime_state.status is AgentStatus.IDLE
        assert runtime_state.subprocess_handle is not None

    # The public runtime.get_status API should reflect these counts.
    server = RuntimeApiServer(daemon=daemon)
    payload = server.get_runtime_status()

    assert payload["status"] == RuntimeStatus.RUNNING.value
    assert payload["project_path"] == str(config.project_path)
    assert payload["swarm_id"] == config.swarm_id

    counts = payload["agent_counts"]
    assert counts == {
        "total": 2,
        "starting": 0,
        "idle": 2,
        "running": 0,
        "waiting": 0,
        "failed": 0,
    }



def test_scheduler_failure_and_restart_are_reflected_in_runtime_api(tmp_path: Path) -> None:
    """US1 dev-mode: failure/restart transitions surface via the runtime API.

    This test builds on ``test_scheduler_launches_agents_from_swarm_metadata``
    by exercising the simple failure/restart helpers on
    :class:`RuntimeScheduler` and asserting that:

    * ``runtime.get_status`` agent counts are updated.
    * ``swarm.get_overview`` agent summaries reflect status/last_error.
    * ``agent.get_detail`` returns the corresponding events.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    config: RuntimeConfig = load_runtime_config(project_path=project)
    store = MetadataStore(config=config)

    now = datetime(2026, 7, 3, 12, 0, 0)

    agent = AgentState(agent_id="nav-1", display_name="Navigator 1")

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
    assert daemon.scheduler is not None

    # Start the runtime; this will cause the scheduler to register and
    # "launch" the configured agent in dev-mode.
    daemon.start()
    assert daemon.state.status is RuntimeStatus.RUNNING

    assert set(daemon.state.agents.keys()) == {"nav-1"}
    runtime_state = daemon.state.agents["nav-1"]
    assert runtime_state.status is AgentStatus.IDLE

    server = RuntimeApiServer(daemon=daemon)

    # Baseline: agent detail should show Idle with no events.
    detail_before = server.get_agent_detail(agent_id="nav-1", max_events=10)
    agent_before = detail_before["agent"]
    assert agent_before["status"] == AgentStatus.IDLE.value
    assert agent_before["last_error"] is None
    assert detail_before["events"] == []

    # Simulate a failure via the scheduler.
    daemon.scheduler.mark_agent_failed("nav-1", error="boom")

    assert runtime_state.status is AgentStatus.FAILED
    assert runtime_state.last_error == "boom"

    status_after_fail = server.get_runtime_status()
    counts_after_fail = status_after_fail["agent_counts"]
    assert counts_after_fail == {
        "total": 1,
        "starting": 0,
        "idle": 0,
        "running": 0,
        "waiting": 0,
        "failed": 1,
    }

    overview_after_fail = server.get_swarm_overview()
    agents_after_fail = {a["agent_id"]: a for a in overview_after_fail["agents"]}
    a = agents_after_fail["nav-1"]
    assert a["status"] == AgentStatus.FAILED.value
    assert a["last_error"] == "boom"

    detail_after_fail = server.get_agent_detail(agent_id="nav-1", max_events=10)
    agent_after_fail = detail_after_fail["agent"]
    assert agent_after_fail["status"] == AgentStatus.FAILED.value
    assert agent_after_fail["last_error"] == "boom"

    events_after_fail = detail_after_fail["events"]
    assert any(e["type"] == "AgentFailed" for e in events_after_fail)

    # Now request a restart via the scheduler.
    daemon.scheduler.restart_agent("nav-1")

    assert runtime_state.status is AgentStatus.IDLE
    assert runtime_state.last_error is None

    status_after_restart = server.get_runtime_status()
    counts_after_restart = status_after_restart["agent_counts"]
    assert counts_after_restart == {
        "total": 1,
        "starting": 0,
        "idle": 1,
        "running": 0,
        "waiting": 0,
        "failed": 0,
    }

    overview_after_restart = server.get_swarm_overview()
    agents_after_restart = {a["agent_id"]: a for a in overview_after_restart["agents"]}
    a2 = agents_after_restart["nav-1"]
    assert a2["status"] == AgentStatus.IDLE.value
    assert a2["last_error"] is None

    detail_after_restart = server.get_agent_detail(agent_id="nav-1", max_events=10)
    agent_after_restart = detail_after_restart["agent"]
    assert agent_after_restart["status"] == AgentStatus.IDLE.value
    assert agent_after_restart["last_error"] is None

    events_after_restart = detail_after_restart["events"]
    event_types = {e["type"] for e in events_after_restart}
    assert "AgentFailed" in event_types
    assert "AgentRestarted" in event_types

