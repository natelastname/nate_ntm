"""Unit tests for RuntimeScheduler (runtime/scheduler.py).

These tests exercise the initial T017 scaffolding: a scheduler that
coordinates agent registration via AgentSupervisor but does not yet
implement a full event loop.
"""

from __future__ import annotations

from datetime import datetime

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.agents import AgentSupervisor
from nate_ntm.runtime.scheduler import RuntimeScheduler
from nate_ntm.runtime.state import AgentRuntimeState, AgentStatus, RuntimeState
from nate_ntm.runtime.swarm_state import AgentState, SwarmState


def _make_runtime_state(config: RuntimeConfig) -> RuntimeState:
    return RuntimeState(config=config)



def _make_config(project_root) -> RuntimeConfig:
    project_root.mkdir(parents=True, exist_ok=True)
    return load_runtime_config(project_path=project_root)


def _make_swarm_state(
    config: RuntimeConfig, *, agents: dict[str, AgentState] | None = None
) -> SwarmState:
    now = datetime(2026, 7, 3, 12, 0, 0)
    return SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="",
        created_at=now,
        last_updated_at=now,
        agents=agents or {},
    )


def test_scheduler_start_registers_and_launches_agents_via_supervisor(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    a1 = AgentState(agent_id="a1", display_name="Agent One")
    swarm = _make_swarm_state(config, agents={"a1": a1})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)
    scheduler = RuntimeScheduler(
        config=config,
        state=state,
        swarm_state=swarm,
        agent_supervisor=supervisor,
    )

    assert scheduler.running is False
    assert state.agents == {}

    scheduler.start()

    # The scheduler should now be marked as running.
    assert scheduler.running is True

    # And the runtime state should contain entries for the configured agents.
    assert set(state.agents.keys()) == {"a1"}
    assert state.agents["a1"].status is AgentStatus.IDLE
    assert state.agents["a1"].subprocess_handle is not None


def test_scheduler_start_is_idempotent(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    a1 = AgentState(agent_id="a1", display_name="Agent One")
    swarm = _make_swarm_state(config, agents={"a1": a1})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)
    scheduler = RuntimeScheduler(
        config=config,
        state=state,
        swarm_state=swarm,
        agent_supervisor=supervisor,
    )

    scheduler.start()
    first_state = dict(state.agents)

    # Second call should be a no-op because `running` is already True.
    scheduler.start()

    assert scheduler.running is True
    assert state.agents == first_state


def test_scheduler_stop_clears_running_flag(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    a1 = AgentState(agent_id="a1", display_name="Agent One")
    swarm = _make_swarm_state(config, agents={"a1": a1})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)
    scheduler = RuntimeScheduler(
        config=config,
        state=state,
        swarm_state=swarm,
        agent_supervisor=supervisor,
    )

    scheduler.start()
    assert scheduler.running is True

    scheduler.stop()
    assert scheduler.running is False


def test_scheduler_respects_preseeded_runtime_state(tmp_path) -> None:
    """Scheduler should not overwrite pre-seeded AgentRuntimeState entries.

    This mirrors how the RuntimeDaemon and tests seed state before
    calling start(); scheduler.start() must only *add* missing entries.
    """

    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    # Two agents are configured in the swarm state, but one already has runtime state.
    a1 = AgentState(agent_id="a1", display_name="Agent One")
    a2 = AgentState(agent_id="a2", display_name="Agent Two")
    swarm = _make_swarm_state(config, agents={"a1": a1, "a2": a2})

    preexisting = AgentRuntimeState(
        agent_id="a1",
        status=AgentStatus.RUNNING,
    )
    state.agents["a1"] = preexisting

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)
    scheduler = RuntimeScheduler(
        config=config,
        state=state,
        swarm_state=swarm,
        agent_supervisor=supervisor,
    )

    scheduler.start()

    # Pre-existing entry should be preserved.
    assert state.agents["a1"] is preexisting
    assert state.agents["a1"].status is AgentStatus.RUNNING

    # And the second agent should have been added and launched.
    assert "a2" in state.agents
    assert state.agents["a2"].status is AgentStatus.IDLE
    assert state.agents["a2"].subprocess_handle is not None


def test_scheduler_mark_agent_failed_delegates_and_updates_state(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    a1 = AgentState(agent_id="a1", display_name="Agent One")
    swarm = _make_swarm_state(config, agents={"a1": a1})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)
    scheduler = RuntimeScheduler(
        config=config,
        state=state,
        swarm_state=swarm,
        agent_supervisor=supervisor,
    )

    # Seed runtime state as if the scheduler had already started.
    supervisor.ensure_agents_registered()
    runtime_state = state.agents["a1"]
    assert runtime_state.status is AgentStatus.STARTING

    scheduler.mark_agent_failed("a1", error="boom")

    assert runtime_state.status is AgentStatus.FAILED
    assert runtime_state.last_error == "boom"


def test_scheduler_restart_agent_delegates_and_marks_idle(tmp_path) -> None:
    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    a1 = AgentState(agent_id="a1", display_name="Agent One")
    swarm = _make_swarm_state(config, agents={"a1": a1})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)
    scheduler = RuntimeScheduler(
        config=config,
        state=state,
        swarm_state=swarm,
        agent_supervisor=supervisor,
    )

    supervisor.ensure_agents_registered()
    runtime_state = state.agents["a1"]
    runtime_state.status = AgentStatus.FAILED
    runtime_state.last_error = "boom"

    scheduler.restart_agent("a1")

    assert runtime_state.status is AgentStatus.IDLE
    assert runtime_state.last_error is None
    assert runtime_state.subprocess_handle is not None



def test_scheduler_enqueues_mail_received_events_from_unread_flags(tmp_path) -> None:
    """Scheduler polls Agent Mail once at startup and enqueues events.

    This exercises the US2 behavior added in T024: when a
    :class:`BaseAgentMailClient` is provided, ``RuntimeScheduler.start``
    consults ``get_unread_mail_flags`` and records a ``MailReceived``
    event for each agent that currently has unread mail.
    """

    from nate_ntm.runtime.agent_mail_client import FakeAgentMailClient
    from nate_ntm.runtime.events import AgentEventSource

    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    a1 = AgentState(agent_id="a1", display_name="Agent One")
    a2 = AgentState(agent_id="a2", display_name="Agent Two")
    swarm = _make_swarm_state(config, agents={"a1": a1, "a2": a2})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)

    # Seed the fake Agent Mail client with an unread message for ``a1``
    # only. ``a2`` remains without unread mail.
    mail_client = FakeAgentMailClient(config=config)
    mail_client.ensure_project()
    mail_client.set_unread_count_for_test("a1", 3)

    scheduler = RuntimeScheduler(
        config=config,
        state=state,
        swarm_state=swarm,
        agent_supervisor=supervisor,
        agent_mail_client=mail_client,
    )

    scheduler.start()

    # Both agents should have runtime state entries.
    assert set(state.agents.keys()) == {"a1", "a2"}

    # ``a1`` should have a MailReceived event from Agent Mail.
    a1_state = state.agents["a1"]
    assert a1_state.event_stream is not None
    a1_events = list(a1_state.event_stream)
    assert any(
        e.source is AgentEventSource.AGENT_MAIL and e.type == "MailReceived" for e in a1_events
    )

    # ``a2`` should have no MailReceived events.
    a2_state = state.agents["a2"]
    assert a2_state.event_stream is not None
    a2_events = list(a2_state.event_stream)
    assert all(e.type != "MailReceived" for e in a2_events)


def test_scheduler_unread_mail_poll_is_idempotent(tmp_path) -> None:
    """Second call to start() does not duplicate MailReceived events.

    ``RuntimeScheduler.start`` short-circuits when ``running`` is True,
    so the unread-mail poll and event enqueue path must run exactly once
    per scheduler instance.
    """

    from nate_ntm.runtime.agent_mail_client import FakeAgentMailClient

    project = tmp_path / "project"
    config = _make_config(project)
    state = _make_runtime_state(config)

    a1 = AgentState(agent_id="a1", display_name="Agent One")
    swarm = _make_swarm_state(config, agents={"a1": a1})

    supervisor = AgentSupervisor(config=config, state=state, swarm_state=swarm)

    mail_client = FakeAgentMailClient(config=config)
    mail_client.ensure_project()
    mail_client.set_unread_count_for_test("a1", 1)

    scheduler = RuntimeScheduler(
        config=config,
        state=state,
        swarm_state=swarm,
        agent_supervisor=supervisor,
        agent_mail_client=mail_client,
    )

    scheduler.start()
    a1_state = state.agents["a1"]
    assert a1_state.event_stream is not None
    first_events = list(a1_state.event_stream)

    # Second call should be a no-op with respect to events.
    scheduler.start()
    second_events = list(a1_state.event_stream)

    assert second_events == first_events

