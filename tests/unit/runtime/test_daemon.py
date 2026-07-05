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
from nate_ntm.runtime.acp_client import NateOhaAcpClient
from nate_ntm.runtime.events import AgentEventSource
from nate_ntm.runtime.daemon import (
    MetadataAlreadyExistsError,
    MetadataMissingError,
    RuntimeDaemon,
    RuntimeStartupError,
    StartupMode,
    check_startup_preconditions,
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



def test_runtime_daemon_create_initializes_and_persists_swarm_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    store = MetadataStore(config=config)
    swarm_path = store.metadata_dir / "swarm.json"
    assert not swarm_path.exists()

    daemon = RuntimeDaemon.create(config)

    assert daemon.config is config
    assert daemon.metadata_store.metadata_dir == config.metadata_dir
    assert daemon.swarm_metadata.swarm_id == config.swarm_id
    assert daemon.swarm_metadata.project_path == config.project_path

    # Agent Mail project ID should be initialized via FakeAgentMailClient
    # using the deterministic format from FakeAgentMailClient.ensure_project.
    expected_project_id = f"fake-mail-project:{config.swarm_id}:{config.project_path}"
    assert daemon.swarm_metadata.agent_mail_project_id == expected_project_id

    assert daemon.state.config is config


def test_runtime_daemon_create_with_real_acp_persists_nate_oha_metadata(tmp_path: Path) -> None:
    """create() + REAL-style ACP persist nate_OHA-compatible metadata (T217).

    This exercises RuntimeDaemon.create when supplied with a NateOhaAcpClient
    and FakeAgentMailClient via RuntimeAdapters. The resulting swarm and
    per-agent metadata must be suitable for launching nate_OHA-backed agents:

    * swarm_metadata.agent_mail_project_id is initialized via Agent Mail.
    * per-agent AgentMetadata records contain Agent Mail identities.
    * per-agent conversation_id values are stable and match
      NateOhaAcpClient.ensure_conversation for each agent.
    """

    project = tmp_path / "project"
    config = _make_config(project)

    # Use explicit adapters so that ACP is backed by NateOhaAcpClient while
    # Agent Mail remains the in-memory fake implementation.
    agent_mail = FakeAgentMailClient(config=config)
    acp = NateOhaAcpClient(config=config)
    adapters = RuntimeAdapters(agent_mail=agent_mail, acp=acp)

    daemon = RuntimeDaemon.create(config, agent_count=2, adapters=adapters)

    # The daemon should retain the supplied adapters.
    assert daemon.agent_mail_client is agent_mail
    assert daemon.acp_client is acp

    # Swarm-level Agent Mail project identifier should be derived from the
    # fake client and persisted into swarm metadata.
    expected_project_id = agent_mail.ensure_project()
    assert daemon.swarm_metadata.agent_mail_project_id == expected_project_id

    store = MetadataStore(config=config)
    swarm = store.load_swarm_metadata()
    assert set(swarm.agents.keys()) == {"agent-1", "agent-2"}

    for agent_id in ["agent-1", "agent-2"]:
        meta = store.load_agent_metadata(agent_id)

        # Agent Mail identity fields persisted for nate_OHA launches.
        assert meta.agent_mail_identity == f"fake-mail-identity:{agent_id}"

        # Conversation identifiers must be non-empty and match the ACP
        # adapter's deterministic ensure_conversation implementation.
        assert meta.conversation_id
        conv_from_adapter = acp.ensure_conversation(agent_id)
        assert meta.conversation_id == conv_from_adapter
        assert swarm.agents[agent_id].conversation_id == meta.conversation_id



def test_runtime_daemon_create_and_resume_with_nate_oha_acp_and_fake_agent_mail(
    tmp_path: Path,
) -> None:
    """create() → resume() round-trip with NateOhaAcpClient (T217/T221).

    This test verifies that:

    * RuntimeDaemon.create, when supplied with NateOhaAcpClient and
      FakeAgentMailClient, initializes AgentMetadata with persistent
      conversation IDs and Agent Mail identities, and
    * RuntimeDaemon.resume, when given fresh adapter instances with the same
      configuration, successfully revalidates those identifiers via the
      existing FR-009 resume logic (including conversation-id checks).
    """

    project = tmp_path / "project"
    config = _make_config(project)

    # First create a swarm with REAL-style ACP wiring.
    create_mail = FakeAgentMailClient(config=config)
    create_acp = NateOhaAcpClient(config=config)
    create_adapters = RuntimeAdapters(agent_mail=create_mail, acp=create_acp)

    daemon_create = RuntimeDaemon.create(config, agent_count=1, adapters=create_adapters)

    # Capture the persisted conversation ID and Agent Mail identity.
    store = MetadataStore(config=config)
    meta_before = store.load_agent_metadata("agent-1")
    assert meta_before.conversation_id
    assert meta_before.agent_mail_identity == "fake-mail-identity:agent-1"

    # Now resume with fresh adapter instances bound to the same config. The
    # resume path should revalidate Agent Mail and ACP identifiers without
    # creating new ones.
    resume_mail = FakeAgentMailClient(config=config)
    resume_acp = NateOhaAcpClient(config=config)
    resume_adapters = RuntimeAdapters(agent_mail=resume_mail, acp=resume_acp)

    daemon_resume = RuntimeDaemon.resume(config, adapters=resume_adapters)

    meta_after = daemon_resume.metadata_store.load_agent_metadata("agent-1")
    assert meta_after.conversation_id == meta_before.conversation_id
    assert meta_after.agent_mail_identity == meta_before.agent_mail_identity

    # The ACP adapter used during resume must agree with the persisted
    # conversation identifier.
    conv_from_resume_adapter = resume_acp.ensure_conversation("agent-1")
    assert conv_from_resume_adapter == meta_after.conversation_id



def test_runtime_daemon_resume_fails_when_acp_conversation_mismatch(tmp_path: Path) -> None:
    """Resume must fail clearly when ACP conversation IDs diverge (T221).

    This uses a deliberately misbehaving NateOhaAcpClient whose
    ensure_conversation implementation returns an identifier that does not
    match the conversation_id recorded in metadata. RuntimeDaemon.resume
    must detect the mismatch and raise RuntimeStartupError rather than
    silently accepting the inconsistency.
    """

    project = tmp_path / "project"
    config = _make_config(project)

    # Create initial metadata using a well-behaved adapter.
    create_mail = FakeAgentMailClient(config=config)
    create_acp = NateOhaAcpClient(config=config)
    create_adapters = RuntimeAdapters(agent_mail=create_mail, acp=create_acp)
    _ = RuntimeDaemon.create(config, agent_count=1, adapters=create_adapters)

    # Sanity: metadata contains a non-empty, adapter-consistent conversation ID.
    store = MetadataStore(config=config)
    meta = store.load_agent_metadata("agent-1")
    assert meta.conversation_id
    good_conv = meta.conversation_id
    assert good_conv == create_acp.ensure_conversation("agent-1")

    # Construct an ACP adapter that violates the contract by returning a
    # different conversation ID on resume.
    class BadConversationAcp(NateOhaAcpClient):
        def ensure_conversation(self, agent_id: str) -> str:  # type: ignore[override]
            return "conv-mismatch-123"

    resume_mail = FakeAgentMailClient(config=config)
    resume_acp = BadConversationAcp(config=config)
    resume_adapters = RuntimeAdapters(agent_mail=resume_mail, acp=resume_acp)

    with pytest.raises(RuntimeStartupError) as excinfo:
        _ = RuntimeDaemon.resume(config, adapters=resume_adapters)

    msg = str(excinfo.value)
    assert "ACP conversation ID mismatch on resume" in msg




def test_runtime_daemon_create_with_agents_initializes_agent_metadata(tmp_path: Path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)

    store = MetadataStore(config=config)

    daemon = RuntimeDaemon.create(config, agent_count=3)

    # In-memory swarm metadata should include three agents with deterministic
    # identifiers.
    assert set(daemon.swarm_metadata.agents.keys()) == {"agent-1", "agent-2", "agent-3"}

    # Swarm metadata and per-agent metadata should be persisted via
    # MetadataStore so that a later resume can reuse the same identities.
    swarm = store.load_swarm_metadata()
    assert set(swarm.agents.keys()) == {"agent-1", "agent-2", "agent-3"}

    for agent_id in ["agent-1", "agent-2", "agent-3"]:
        meta = store.load_agent_metadata(agent_id)
        assert meta.agent_id == agent_id
        assert meta.agent_mail_identity == f"fake-mail-identity:{agent_id}"
        assert meta.conversation_id == f"fake-conversation:{agent_id}"


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


def test_runtime_daemon_swarm_overview_includes_unread_mail_flags(tmp_path: Path) -> None:
    """swarm.get_overview should surface unread-mail flags via Agent Mail.

    This exercises the integration between RuntimeDaemon and
    FakeAgentMailClient for the ``has_unread_mail`` field.
    """

    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_metadata(config)

    daemon = RuntimeDaemon.resume(config)

    # Seed metadata and runtime state for two agents.
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

    daemon.state.agents = {
        "a1": AgentRuntimeState(agent_id="a1", status=AgentStatus.RUNNING),
        "a2": AgentRuntimeState(agent_id="a2", status=AgentStatus.IDLE),
    }
    daemon.state.status = RuntimeStatus.RUNNING

    # The RuntimeDaemon.resume path should have constructed a
    # FakeAgentMailClient by default.
    assert isinstance(daemon.agent_mail_client, FakeAgentMailClient)

    # Simulate unread mail for one of the agents.
    daemon.agent_mail_client.set_unread_count_for_test("a2", 3)  # type: ignore[union-attr]

    overview = daemon.get_swarm_overview()
    agents_by_id = {a["agent_id"]: a for a in overview["agents"]}

    assert agents_by_id["a1"]["has_unread_mail"] is False
    assert agents_by_id["a2"]["has_unread_mail"] is True



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



def test_runtime_daemon_create_owns_in_memory_integration_clients(tmp_path: Path) -> None:
    """RuntimeDaemon.create should construct in-memory Agent Mail and ACP clients.

    This reinforces the architectural rule that the runtime owns core
    integrations (Agent Mail and ACP) for the lifetime of the process.
    """

    project = tmp_path / "project"
    config = _make_config(project)

    daemon = RuntimeDaemon.create(config)

    # Agent Mail and ACP adapters should both be present and use the
    # in-memory fake implementations for US1.
    assert isinstance(daemon.agent_mail_client, FakeAgentMailClient)
    from nate_ntm.runtime.acp_client import FakeAcpClient

    assert isinstance(daemon.acp_client, FakeAcpClient)



def test_runtime_daemon_resume_owns_in_memory_integration_clients(tmp_path: Path) -> None:
    """RuntimeDaemon.resume should also own in-memory integration clients.

    Even though full rebinding semantics (FR-009) are deferred to US2,
    the daemon should still allocate runtime-owned Agent Mail and ACP
    adapters in resume mode so that the scheduler and future control API
    handlers have a stable surface.
    """

    project = tmp_path / "project"
    config = _make_config(project)
    _write_minimal_swarm_metadata(config)

    daemon = RuntimeDaemon.resume(config)

    assert isinstance(daemon.agent_mail_client, FakeAgentMailClient)
    from nate_ntm.runtime.acp_client import FakeAcpClient

    assert isinstance(daemon.acp_client, FakeAcpClient)




def test_runtime_daemon_acp_events_flow_into_supervisor_stream_with_fake_client(
    tmp_path: Path,
) -> None:
    """Events from FakeAcpClient land in the AgentSupervisor stream (T230).

    This verifies that :class:`RuntimeDaemon.create` wires the ACP adapter's
    ``on_event`` callback into :class:`AgentSupervisor.append_agent_event` so
    that events emitted by :class:`FakeAcpClient` are recorded in the
    per-agent :class:`AgentEventStream`.
    """

    project = tmp_path / "project"
    config = _make_config(project)

    # Construct the daemon in create mode with a single agent using the
    # default (fake) adapters.
    daemon = RuntimeDaemon.create(config, agent_count=1)

    assert daemon.acp_client is not None
    from nate_ntm.runtime.acp_client import FakeAcpClient

    assert isinstance(daemon.acp_client, FakeAcpClient)

    acp = daemon.acp_client

    # Sanity: the swarm metadata should contain the configured agent.
    assert set(daemon.swarm_metadata.agents.keys()) == {"agent-1"}

    # Trigger a fake ACP turn, which should emit an AgentEvent via the
    # adapter's ``on_event`` callback. The daemon's wiring should route this
    # into the AgentSupervisor's in-memory event stream for the agent.
    turn_id = acp.start_turn("agent-1", prompt="hello from test")
    assert turn_id

    # A RuntimeState entry and event stream should now exist for the agent,
    # with exactly one ACP-originated event reflecting the completed turn.
    runtime_state = daemon.state.agents.get("agent-1")
    assert runtime_state is not None
    stream = runtime_state.event_stream
    assert stream is not None

    events = stream.get_events()
    assert len(events) == 1

    event = events[0]
    assert event.agent_id == "agent-1"
    assert event.source is AgentEventSource.ACP
    assert event.type == "TurnCompleted"
    assert event.payload["turn_id"] == turn_id

    # The conversation ID in the payload should match the adapter's view.
    conv = acp.ensure_conversation("agent-1")
    assert event.payload["conversation_id"] == conv



def test_runtime_daemon_acp_events_flow_into_supervisor_stream_with_nate_oha(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Events from NateOhaAcpClient land in the AgentSupervisor stream (T230).

    This focuses on the wiring between :class:`BaseAcpClient.on_event` and
    :meth:`AgentSupervisor.append_agent_event` rather than on the concrete
    subprocess behavior. Events are synthesized via
    :meth:`NateOhaAcpClient._make_process_event` and delivered through the
    adapter's callback to mirror the real process-lifecycle events.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    # Take an explicit environment snapshot so the config loader does not
    # consult any repository-level .env files.
    env_snapshot = dict(os.environ)
    config = load_runtime_config(project_path=project, env=env_snapshot)

    # Use a real NateOhaAcpClient instance with external dependencies stubbed
    # in the same way as the dedicated adapter tests.
    from nate_ntm.runtime import acp_client as acp_mod
    from nate_ntm.runtime.acp_client import NateOhaAcpClient

    client = NateOhaAcpClient(config=config)

    # Avoid invoking the real nate_OHA binary during this test.
    monkeypatch.setattr(client, "_check_version", lambda: None)

    # Stub out ``subprocess.Popen`` so no real processes are spawned if
    # methods like ``start_agent`` are exercised in future tests.
    class DummyPopen:
        def __init__(self, *args: object, **kwargs: object) -> None:  # pragma: no cover - safety
            self.pid = 12345
            self._returncode: int | None = None

        def poll(self) -> int | None:
            return self._returncode

        def wait(self, timeout: float | None = None) -> int:
            if self._returncode is None:
                self._returncode = 0
            return self._returncode

        def terminate(self) -> None:
            if self._returncode is None:
                self._returncode = 0

        def kill(self) -> None:
            self._returncode = -9

        @property
        def returncode(self) -> int | None:
            return self._returncode

    def fake_popen(*args: object, **kwargs: object) -> DummyPopen:  # pragma: no cover - safety
        return DummyPopen(*args, **kwargs)

    monkeypatch.setattr(acp_mod.subprocess, "Popen", fake_popen)

    # Use a fake Agent Mail client so RuntimeDaemon.create can construct the
    # initial swarm metadata without external I/O.
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

