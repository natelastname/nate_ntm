"""Unit tests for the OpenHands ACP client adapters (T015/T102).

These tests exercise the in-memory / dev-mode implementation used by the
runtime and integration tests and a small amount of behavior from the
production :class:`OpenHandsAcpClient`. The bulk of the real client's
HTTP behavior is covered by gated integration tests.
"""

from __future__ import annotations

from pathlib import Path
import os

import pytest

from nate_ntm.config.runtime_config import load_runtime_config
from nate_ntm.runtime import acp_client as acp_mod
from nate_ntm.runtime.acp_client import AcpClientError, AcpAgentStatus, FakeAcpClient, OpenHandsAcpClient, NateOhaAcpClient
from nate_ntm.runtime.adapters import RuntimeAdapters
from nate_ntm.runtime.agent_mail_client import FakeAgentMailClient
from nate_ntm.runtime.daemon import RuntimeDaemon
from nate_ntm.runtime.events import AgentEventSource
from nate_ntm.runtime.metadata_store import AgentMetadata, MetadataStore


def _make_fake_client(tmp_path: Path) -> FakeAcpClient:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    config = load_runtime_config(project_path=project)
    return FakeAcpClient(config=config)


def _make_openhands_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> OpenHandsAcpClient:
    """Construct an OpenHandsAcpClient with network I/O stubbed out.

    The client's low-level ``_request`` method is monkeypatched to avoid any
    real HTTP calls and to return predictable payloads for the operations
    under test.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    config = load_runtime_config(project_path=project)

    calls: list[tuple[str, str, dict, str]] = []

    def _fake_request(self, method: str, path: str, *, body: dict | None = None, request_name: str):  # type: ignore[override]
        calls.append((method, path, body or {}, request_name))

        if path == "/threads":
            # Echo back the requested thread_id to simulate a successful
            # ThreadCreate response.
            assert body is not None
            return {"thread_id": body.get("thread_id")}

        # Runs are returned with a simple fixed run_id.
        assert path.startswith("/threads/") and path.endswith("/runs")
        return {"run_id": "run-123"}

    monkeypatch.setattr(OpenHandsAcpClient, "_request", _fake_request, raising=True)

    client = OpenHandsAcpClient(config=config, base_url="http://example.invalid")
    # Attach the call log for assertions in tests.
    client._test_calls = calls  # type: ignore[attr-defined]

    return client



def _make_nate_oha_client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> NateOhaAcpClient:
    """Construct a NateOhaAcpClient with external dependencies stubbed.

    These tests focus on adapter-level lifecycle semantics (start/stop/status)
    and do not exercise the real ``nate_OHA`` binary. The version
    compatibility check is bypassed here and covered explicitly in later
    tests.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    # For NateOhaAcpClient tests we take an explicit snapshot of the
    # current process environment so that ``load_runtime_config`` does not
    # consult the repository-level ``.env`` file. Individual tests control
    # Agent Mail-related variables via ``monkeypatch``.
    env_snapshot = dict(os.environ)
    config = load_runtime_config(project_path=project, env=env_snapshot)

    client = NateOhaAcpClient(config=config)

    # Avoid invoking the real nate_OHA CLI during unit tests that focus on
    # adapter behavior.
    monkeypatch.setattr(client, "_check_version", lambda: None)

    # Stub out ``subprocess.Popen`` so that no real processes are spawned.
    popen_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    class DummyPopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.args = args
            self.kwargs = kwargs
            self.pid = 12345
            self._returncode: int | None = None

        def poll(self) -> int | None:
            return self._returncode

        def wait(self, timeout: float | None = None) -> int:
            if self._returncode is None:
                # Simulate a clean exit when waited on.
                self._returncode = 0
            return self._returncode

        def terminate(self) -> None:
            # Simulate graceful termination.
            if self._returncode is None:
                self._returncode = 0

        def kill(self) -> None:
            # Simulate forced termination.
            self._returncode = -9

        @property
        def returncode(self) -> int | None:
            return self._returncode

    def fake_popen(*args: object, **kwargs: object) -> DummyPopen:
        popen_calls.append((args, kwargs))
        return DummyPopen(*args, **kwargs)

    monkeypatch.setattr(acp_mod.subprocess, "Popen", fake_popen)

    # Attach the call log so later tests can make assertions about the
    # constructed command and environment.
    client._test_popen_calls = popen_calls  # type: ignore[attr-defined]

    return client


def test_fake_acp_client_ensures_stable_conversation_ids(tmp_path: Path) -> None:
    """``ensure_conversation`` returns stable, per-agent conversation IDs."""

    client = _make_fake_client(tmp_path)

    a1_conv_first = client.ensure_conversation("agent-1")
    a1_conv_second = client.ensure_conversation("agent-1")
    a2_conv = client.ensure_conversation("agent-2")

    assert a1_conv_first
    assert a1_conv_first == a1_conv_second
    assert a2_conv
    assert a1_conv_first != a2_conv


def test_fake_acp_client_allocates_unique_turn_ids(tmp_path: Path) -> None:
    """``start_turn`` allocates monotonically increasing, per-agent turn IDs."""

    client = _make_fake_client(tmp_path)

    conv = client.ensure_conversation("agent-1")
    assert conv  # sanity

    turn_1 = client.start_turn("agent-1")
    turn_2 = client.start_turn("agent-1")

    assert turn_1 != turn_2
    assert turn_1.startswith("fake-turn:agent-1:")
    assert turn_2.startswith("fake-turn:agent-1:")

    # Different agents receive their own turn sequences.
    client.ensure_conversation("agent-2")
    other_turn = client.start_turn("agent-2")
    assert other_turn.startswith("fake-turn:agent-2:")


def test_fake_acp_client_start_and_stop_agent_update_status(tmp_path: Path) -> None:
    """``start_agent``/``stop_agent`` update adapter-level status for agents."""

    client = _make_fake_client(tmp_path)

    # Before an agent is started, status should default to ``idle``.
    status_before = client.get_status("agent-1")
    assert isinstance(status_before, AcpAgentStatus)
    assert status_before.agent_id == "agent-1"
    assert status_before.state == "idle"

    # After starting the agent, status should report it as running.
    meta = AgentMetadata(agent_id="agent-1", display_name="Agent One")
    client.start_agent("agent-1", metadata=meta)

    status_running = client.get_status("agent-1")
    assert status_running.state == "running"

    # Stopping the agent should transition it to a terminated state.
    client.stop_agent("agent-1", timeout=1.0)
    status_stopped = client.get_status("agent-1")
    assert status_stopped.state == "terminated"


def test_fake_acp_client_start_turn_emits_event_when_callback_configured(tmp_path: Path) -> None:
    """``start_turn`` emits an AgentEvent via the optional callback."""

    events: list[dict] = []

    def _on_event(event) -> None:
        events.append(event)

    client = _make_fake_client(tmp_path)
    client.on_event = _on_event

    # Calling ``start_turn`` should allocate a turn ID and emit an event.
    turn_id = client.start_turn("agent-1", prompt="hello world")
    assert turn_id

    assert len(events) == 1
    event = events[0]

    assert event.agent_id == "agent-1"
    assert event.source is AgentEventSource.ACP
    assert event.type == "TurnCompleted"
    assert event.payload["adapter"] == "fake"
    assert event.payload["turn_id"] == turn_id

    # The conversation ID in the payload should match ensure_conversation.
    conv = client.ensure_conversation("agent-1")
    assert event.payload["conversation_id"] == conv

    # When a prompt is provided it should be echoed into the payload.
    assert event.payload["prompt"] == "hello world"


def test_openhands_acp_client_ensures_stable_conversation_ids(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """OpenHands client returns stable conversation IDs per agent.

    This test uses a stubbed HTTP layer to avoid network access and
    verifies that:

    * repeated calls for the same ``agent_id`` within a process reuse the
      cached thread ID and do not issue extra HTTP calls, and
    * a fresh client with the same configuration derives the same
      thread ID for that agent.
    """

    client = _make_openhands_client(tmp_path, monkeypatch)

    conv1 = client.ensure_conversation("agent-1")
    conv2 = client.ensure_conversation("agent-1")

    assert conv1
    assert conv1 == conv2

    calls = getattr(client, "_test_calls")
    assert len(calls) == 1
    method, path, body, _ = calls[0]
    assert method == "POST"
    assert path == "/threads"
    assert body["metadata"]["nate_ntm_agent_id"] == "agent-1"

    # A new client with the same config should derive the same thread ID.
    client2 = _make_openhands_client(tmp_path, monkeypatch)
    conv3 = client2.ensure_conversation("agent-1")
    assert conv3 == conv1



def test_nate_oha_acp_client_start_and_stop_update_status(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``start_agent``/``stop_agent`` should update adapter-level status for agents.

    This test describes the expected lifecycle semantics for
    :class:`NateOhaAcpClient` without depending on the concrete subprocess
    implementation. It is written before T212–T214 and will initially fail
    until the implementation is complete.
    """

    client = _make_nate_oha_client(tmp_path, monkeypatch)

    # Before an agent is started, status should default to ``idle``.
    status_before = client.get_status("agent-1")
    assert isinstance(status_before, AcpAgentStatus)
    assert status_before.agent_id == "agent-1"
    assert status_before.state == "idle"
    assert status_before.last_exit_code is None
    assert status_before.last_error is None
    assert status_before.restart_count == 0

    # After starting the agent, status should report it as running.
    meta = AgentMetadata(agent_id="agent-1", display_name="Agent One")
    client.start_agent("agent-1", metadata=meta)

    status_running = client.get_status("agent-1")
    assert isinstance(status_running, AcpAgentStatus)
    assert status_running.agent_id == "agent-1"
    assert status_running.state == "running"
    assert status_running.last_exit_code is None
    assert status_running.last_error is None

    # Stopping the agent should transition it to a terminated state and record
    # the last exit code.
    client.stop_agent("agent-1", timeout=5.0)
    status_stopped = client.get_status("agent-1")
    assert isinstance(status_stopped, AcpAgentStatus)
    assert status_stopped.agent_id == "agent-1"
    assert status_stopped.state == "terminated"
    # Exit code may vary by implementation but should be an int once the
    # process has terminated.
    assert isinstance(status_stopped.last_exit_code, (int, type(None)))




def test_nate_oha_acp_client_start_agent_emits_started_and_ready_events(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """start_agent emits process-started and ready events via on_event.

    This covers the happy-path lifecycle where nate_OHA starts successfully
    and remains running after the initial readiness check.
    """

    events: list[object] = []

    def _on_event(event: object) -> None:
        events.append(event)

    client = _make_nate_oha_client(tmp_path, monkeypatch)
    client.on_event = _on_event

    meta = AgentMetadata(agent_id="agent-1", display_name="Agent One")

    client.start_agent("agent-1", metadata=meta)

    # The adapter should emit a started event followed by a ready event.
    assert len(events) == 2

    from nate_ntm.runtime.events import AgentEvent

    started, ready = events
    assert isinstance(started, AgentEvent)
    assert isinstance(ready, AgentEvent)

    assert started.agent_id == "agent-1"
    assert started.source is AgentEventSource.ACP
    assert started.type == "nate_oha_process_started"
    assert started.payload["pid"] == 12345

    assert ready.agent_id == "agent-1"
    assert ready.source is AgentEventSource.ACP
    assert ready.type == "nate_oha_process_ready"
    assert ready.payload["pid"] == 12345



def test_nate_oha_acp_client_start_agent_emits_start_failed_event_on_immediate_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """start_agent emits a start_failed event when nate_OHA exits early.

    This simulates a process that exits during the initial readiness check,
    causing ``start_agent`` to mark the record as failed and raise
    :class:`AcpClientError`.
    """

    events: list[object] = []

    def _on_event(event: object) -> None:
        events.append(event)

    client = _make_nate_oha_client(tmp_path, monkeypatch)
    client.on_event = _on_event

    # Override the subprocess stub so that the spawned process appears to
    # exit immediately with a non-zero code.
    class FailingPopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.pid = 4242
            self._returncode = 17

        def poll(self) -> int:
            return self._returncode

        def wait(self, timeout: float | None = None) -> int:  # pragma: no cover - safety
            return self._returncode

        def terminate(self) -> None:  # pragma: no cover - safety
            pass

        def kill(self) -> None:  # pragma: no cover - safety
            pass

        @property
        def returncode(self) -> int:
            return self._returncode

    monkeypatch.setattr(acp_mod.subprocess, "Popen", FailingPopen)

    meta = AgentMetadata(agent_id="agent-1", display_name="Agent One")

    with pytest.raises(AcpClientError) as excinfo:
        client.start_agent("agent-1", metadata=meta)

    msg = str(excinfo.value)
    assert "exited during startup" in msg

    # Two events should have been emitted: started and start_failed.
    assert [e.type for e in events] == [
        "nate_oha_process_started",
        "nate_oha_process_start_failed",
    ]

    started, failed = events
    assert started.payload["pid"] == 4242
    assert failed.payload["exit_code"] == 17



def test_nate_oha_acp_client_stop_agent_emits_exited_event_on_clean_shutdown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """stop_agent emits nate_oha_process_exited for a clean termination."""

    events: list[object] = []

    def _on_event(event: object) -> None:
        events.append(event)

    client = _make_nate_oha_client(tmp_path, monkeypatch)
    client.on_event = _on_event

    # Stub Popen so that the process appears to be running when started and
    # exits cleanly (exit code 0) when terminated.
    class ExitingPopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.pid = 1111
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

        def kill(self) -> None:  # pragma: no cover - safety
            self._returncode = -9

        @property
        def returncode(self) -> int | None:
            return self._returncode

    monkeypatch.setattr(acp_mod.subprocess, "Popen", ExitingPopen)

    meta = AgentMetadata(agent_id="agent-1", display_name="Agent One")

    client.start_agent("agent-1", metadata=meta)
    client.stop_agent("agent-1", timeout=5.0)

    # We expect three events: started, ready, exited.
    types = [e.type for e in events]
    assert types == [
        "nate_oha_process_started",
        "nate_oha_process_ready",
        "nate_oha_process_exited",
    ]

    exited = events[-1]
    assert exited.payload["exit_code"] == 0



def test_nate_oha_acp_client_stop_agent_emits_crashed_event_on_non_zero_exit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """stop_agent emits nate_oha_process_crashed when exit code is non-zero."""

    events: list[object] = []

    def _on_event(event: object) -> None:
        events.append(event)

    client = _make_nate_oha_client(tmp_path, monkeypatch)
    client.on_event = _on_event

    # Stub Popen so that the process appears to be running and then exits
    # with a non-zero code when terminated.
    class CrashingPopen:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.pid = 2222
            self._returncode: int | None = None

        def poll(self) -> int | None:
            return self._returncode

        def wait(self, timeout: float | None = None) -> int:
            if self._returncode is None:
                self._returncode = 1
            return self._returncode

        def terminate(self) -> None:
            if self._returncode is None:
                self._returncode = 1

        def kill(self) -> None:  # pragma: no cover - safety
            self._returncode = -9

        @property
        def returncode(self) -> int | None:
            return self._returncode

    monkeypatch.setattr(acp_mod.subprocess, "Popen", CrashingPopen)

    meta = AgentMetadata(agent_id="agent-1", display_name="Agent One")

    client.start_agent("agent-1", metadata=meta)
    client.stop_agent("agent-1", timeout=5.0)

    # We expect three events: started, ready, crashed.
    types = [e.type for e in events]
    assert types == [
        "nate_oha_process_started",
        "nate_oha_process_ready",
        "nate_oha_process_crashed",
    ]

    crashed = events[-1]
    assert crashed.payload["exit_code"] == 1



def test_nate_oha_acp_client_ensure_conversation_is_idempotent_and_deterministic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``ensure_conversation`` is stable per agent and across clients.

    This covers the core T220 requirement that the nate_OHA-backed ACP
    adapter derive deterministic, idempotent conversation identifiers for
    agents, suitable for reuse on resume.
    """

    client1 = _make_nate_oha_client(tmp_path, monkeypatch)

    a1_first = client1.ensure_conversation("agent-1")
    a1_second = client1.ensure_conversation("agent-1")
    a2_conv = client1.ensure_conversation("agent-2")

    assert a1_first
    assert a1_first == a1_second
    assert a2_conv
    assert a1_first != a2_conv

    # A fresh client with the same configuration should derive the same
    # identifier for a given agent.
    client2 = _make_nate_oha_client(tmp_path, monkeypatch)
    a1_third = client2.ensure_conversation("agent-1")
    assert a1_third == a1_first



def test_nate_oha_acp_client_ensure_conversation_reuses_existing_metadata_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Existing ``AgentMetadata.conversation_id`` values are reused.

    When per-agent metadata already has a non-empty ``conversation_id``,
    ``ensure_conversation`` must return that value instead of deriving a
    new one so that conversation continuity is preserved.
    """

    client = _make_nate_oha_client(tmp_path, monkeypatch)
    store = MetadataStore(config=client.config)

    preexisting_id = "conv-preexisting-123"
    meta = AgentMetadata(agent_id="nav-1", display_name="Navigator 1", conversation_id=preexisting_id)
    store.save_agent_metadata(meta)

    conv = client.ensure_conversation("nav-1")
    assert conv == preexisting_id

    # Metadata should still report the same ID.
    reloaded = store.load_agent_metadata("nav-1")
    assert reloaded.conversation_id == preexisting_id



def test_nate_oha_acp_client_ensure_conversation_persists_allocated_id_when_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Missing ``conversation_id`` in metadata is filled and persisted.

    When ``AgentMetadata`` exists but ``conversation_id`` is empty,
    ``ensure_conversation`` must allocate a stable identifier and write it
    back to the metadata store so later runs can reuse it.
    """

    client = _make_nate_oha_client(tmp_path, monkeypatch)
    store = MetadataStore(config=client.config)

    meta = AgentMetadata(agent_id="nav-1", display_name="Navigator 1")
    store.save_agent_metadata(meta)

    conv1 = client.ensure_conversation("nav-1")
    assert conv1

    reloaded1 = store.load_agent_metadata("nav-1")
    assert reloaded1.conversation_id == conv1

    # Idempotent: subsequent calls return the same ID and keep metadata in sync.
    conv2 = client.ensure_conversation("nav-1")
    assert conv2 == conv1

    reloaded2 = store.load_agent_metadata("nav-1")
    assert reloaded2.conversation_id == conv1



def test_nate_oha_acp_client_start_agent_includes_conversation_id_env_when_present(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``start_agent`` propagates ``conversation_id`` into the child env.

    When metadata has a non-empty ``conversation_id``, the launch
    environment must include ``NATE_NTM_AGENT_CONVERSATION_ID`` so that
    nate_OHA/OpenHands can reconnect to the correct ACP conversation.
    """

    # Minimal Agent Mail configuration so environment construction succeeds.
    monkeypatch.delenv("NATE_NTM_AGENT_MAIL_URL", raising=False)
    monkeypatch.setenv("AGENT_MAIL_PROJECT", "test-project")
    monkeypatch.setenv("AGENT_MAIL_UPSTREAM_URL", "https://agent-mail.invalid/mcp")

    client = _make_nate_oha_client(tmp_path, monkeypatch)

    meta = AgentMetadata(
        agent_id="agent-1",
        display_name="Agent One",
        agent_mail_identity="agent-mail-identity",
        agent_mail_credentials_ref="secret-token-ref",
        conversation_id="conv-123",
    )

    client.start_agent("agent-1", metadata=meta)

    popen_calls = getattr(client, "_test_popen_calls")
    assert len(popen_calls) == 1
    (args, kwargs) = popen_calls[0]

    env = kwargs.get("env")
    assert isinstance(env, dict)
    assert env["NATE_NTM_AGENT_CONVERSATION_ID"] == "conv-123"



def test_runtime_create_with_nate_oha_acp_persists_conversation_metadata(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """REAL-style runtime creation works with NateOhaAcpClient (T220).

    This regression test exercises the create-path wiring used in US1/US2:

    * ``RuntimeDaemon.create`` uses the ACP adapter's
      ``ensure_conversation`` to allocate per-agent conversation IDs.
    * With :class:`NateOhaAcpClient` as the ACP adapter, this should no
      longer raise ``NotImplementedError`` and must result in persisted
      metadata with a non-empty, stable ``conversation_id`` for each
      created agent.
    """

    client = _make_nate_oha_client(tmp_path, monkeypatch)
    config = client.config

    # Use the in-memory FakeAgentMailClient to avoid any external I/O
    # while still exercising the RuntimeDaemon.create path.
    agent_mail = FakeAgentMailClient(config=config)
    adapters = RuntimeAdapters(agent_mail=agent_mail, acp=client)

    daemon = RuntimeDaemon.create(config, agent_count=1, adapters=adapters)

    # Metadata should contain a single agent with a non-empty
    # conversation identifier that matches the adapter's view.
    store = daemon.metadata_store
    all_meta = store.load_all_agent_metadata()
    assert set(all_meta.keys()) == {"agent-1"}

    meta = all_meta["agent-1"]
    assert meta.conversation_id
    assert daemon.swarm_metadata.agents["agent-1"].conversation_id == meta.conversation_id

    conv_from_adapter = client.ensure_conversation("agent-1")
    assert conv_from_adapter == meta.conversation_id



def test_nate_oha_acp_client_builds_command_and_env_for_agent_mail(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """start_agent constructs the expected command and Agent Mail env.

    This test focuses on the process-launch contract for nate_OHA when
    Agent Mail is enabled. It verifies that the adapter:

    * builds the ``nate_OHA acp --enable-agent-mail`` command line
    * derives Agent Mail launch settings from :class:`RuntimeConfig` and
      :class:`AgentMetadata`
    * populates the required ``AGENT_MAIL_*`` variables and the runtime
      correlation ``NATE_NTM_*`` variables in the child environment.
    """

    # Ensure NATE_NTM-specific Agent Mail URL variables do not interfere
    # with this test's expectations about how ``load_runtime_config``
    # resolves ``agent_mail_upstream_url``.
    monkeypatch.delenv("NATE_NTM_AGENT_MAIL_URL", raising=False)

    # Configure minimal Agent Mail settings via environment so that
    # :func:`load_runtime_config` populates ``RuntimeConfig.agent_mail_project``
    # and ``RuntimeConfig.agent_mail_upstream_url``. ``NateOhaAcpClient``
    # itself no longer reads these variables directly from :mod:`os.environ`;
    # it relies solely on the resolved runtime config.
    monkeypatch.setenv("AGENT_MAIL_PROJECT", "test-project")
    monkeypatch.setenv("AGENT_MAIL_UPSTREAM_URL", "https://agent-mail.invalid/mcp")

    client = _make_nate_oha_client(tmp_path, monkeypatch)

    # Sanity-check that the runtime config captured the Agent Mail launch
    # settings; the ACP client uses these, not the ambient environment,
    # when constructing the child process environment.
    assert client.config.agent_mail_project == "test-project"
    assert client.config.agent_mail_upstream_url == "https://agent-mail.invalid/mcp"

    meta = AgentMetadata(
        agent_id="agent-1",
        display_name="Agent One",
        agent_mail_identity="agent-mail-identity",
        agent_mail_credentials_ref="secret-token-ref",
    )

    client.start_agent("agent-1", metadata=meta)

    popen_calls = getattr(client, "_test_popen_calls")
    assert len(popen_calls) == 1
    (args, kwargs) = popen_calls[0]

    # The first positional argument is the command list.
    cmd = args[0]
    assert cmd == ["nate_OHA", "acp", "--enable-agent-mail"]

    # The environment must include the Agent Mail and nate_ntm correlation
    # variables required by the process-launch contract.
    env = kwargs.get("env")
    assert isinstance(env, dict)

    assert env["NATE_NTM_PROJECT_PATH"] == str(client.config.project_path)
    assert env["NATE_NTM_SWARM_ID"] == client.config.swarm_id
    assert env["NATE_NTM_AGENT_ID"] == "agent-1"

    # Agent Mail variables: project, identity, token, and upstream URL
    assert env["AGENT_MAIL_PROJECT"] == client.config.agent_mail_project
    assert env["AGENT_MAIL_AGENT"] == "agent-mail-identity"
    assert env["AGENT_MAIL_TOKEN"] == "secret-token-ref"
    assert env["AGENT_MAIL_UPSTREAM_URL"] == client.config.agent_mail_upstream_url


def test_nate_oha_acp_client_fails_fast_when_agent_mail_project_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Agent Mail identity set but project missing → fail before Popen.

    When ``AgentMetadata.agent_mail_identity`` is non-empty, the adapter
    # Ensure NATE_NTM-specific Agent Mail URL variables do not interfere
    # with this test's expectations about how ``load_runtime_config``
    # resolves ``agent_mail_upstream_url``.
    monkeypatch.delenv("NATE_NTM_AGENT_MAIL_URL", raising=False)


    must require a configured Agent Mail project. If neither
    ``AGENT_MAIL_PROJECT`` nor ``NATE_NTM_AGENT_MAIL_PROJECT`` is set,
    ``start_agent`` should raise :class:`AcpClientError` and avoid
    spawning a nate_OHA subprocess.
    """

    # Ensure NATE_NTM-specific Agent Mail URL variables do not interfere
    # with this test's expectations about how ``load_runtime_config``
    # resolves ``agent_mail_upstream_url``.
    monkeypatch.delenv("NATE_NTM_AGENT_MAIL_URL", raising=False)


    # Ensure no Agent Mail project is visible in the environment.
    monkeypatch.delenv("AGENT_MAIL_PROJECT", raising=False)
    monkeypatch.delenv("NATE_NTM_AGENT_MAIL_PROJECT", raising=False)

    # Provide other required Agent Mail settings so that the missing
    # project is the only failure reason.
    monkeypatch.setenv("AGENT_MAIL_UPSTREAM_URL", "https://agent-mail.invalid/mcp")

    client = _make_nate_oha_client(tmp_path, monkeypatch)

    # Sanity-check that the runtime config reflects the intended
    # misconfiguration: project missing but upstream URL present.
    assert client.config.agent_mail_project is None
    assert client.config.agent_mail_upstream_url == "https://agent-mail.invalid/mcp"


    meta = AgentMetadata(
        agent_id="agent-1",
        display_name="Agent One",
        agent_mail_identity="agent-mail-identity",
        agent_mail_credentials_ref="secret-token-ref",
    )

    with pytest.raises(AcpClientError) as excinfo:
        client.start_agent("agent-1", metadata=meta)

    msg = str(excinfo.value)
    assert "Agent Mail project is not configured" in msg

    # The subprocess layer should not be invoked on configuration errors.
    popen_calls = getattr(client, "_test_popen_calls")
    assert popen_calls == []



def test_nate_oha_acp_client_fails_fast_when_agent_mail_upstream_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Agent Mail identity set but upstream URL missing → fail before Popen.

    This is the symmetric case to the missing-project test: with a
    configured project and identity but no upstream URL, ``start_agent``
    must raise :class:`AcpClientError` and never call ``subprocess.Popen``.
    """

    # Configure a project but deliberately omit all recognized upstream
    # URL variables.
    monkeypatch.setenv("AGENT_MAIL_PROJECT", "test-project")
    monkeypatch.delenv("AGENT_MAIL_UPSTREAM_URL", raising=False)
    monkeypatch.delenv("NATE_NTM_AGENT_MAIL_URL", raising=False)
    monkeypatch.delenv("AGENT_MAIL_URL", raising=False)

    client = _make_nate_oha_client(tmp_path, monkeypatch)

    # Sanity-check that the runtime config reflects the intended
    # misconfiguration: project present but upstream URL missing.
    assert client.config.agent_mail_project == "test-project"
    assert client.config.agent_mail_upstream_url is None


    meta = AgentMetadata(
        agent_id="agent-1",
        display_name="Agent One",
        agent_mail_identity="agent-mail-identity",
        agent_mail_credentials_ref="secret-token-ref",
    )

    with pytest.raises(AcpClientError) as excinfo:
        client.start_agent("agent-1", metadata=meta)

    msg = str(excinfo.value)
    assert "Agent Mail upstream URL is not configured" in msg

    popen_calls = getattr(client, "_test_popen_calls")
    assert popen_calls == []



def test_openhands_acp_client_start_turn_uses_thread_and_returns_run_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """``start_turn`` delegates to the runs endpoint and returns ``run_id``."""

    client = _make_openhands_client(tmp_path, monkeypatch)

    run_id = client.start_turn("agent-42")
    assert run_id == "run-123"

    calls = getattr(client, "_test_calls")
    # One call to create the thread, one to create the run.
    assert len(calls) == 2
    _, path_thread, _, _ = calls[0]
    method_run, path_run, body_run, _ = calls[1]

    assert path_thread == "/threads"
    assert method_run == "POST"
    assert path_run.startswith("/threads/") and path_run.endswith("/runs")
    assert body_run["metadata"]["nate_ntm_agent_id"] == "agent-42"

