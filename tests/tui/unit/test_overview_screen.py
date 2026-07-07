from __future__ import annotations

"""Tests for the default overview screen and its widgets.

These tests exercise the rendering behavior of the overview screen's
constituent widgets using a pre-populated :class:`RuntimeSession` instance and
verify that the screen reacts to session update notifications.

The tests intentionally avoid running a full Textual event loop; where
necessary we call private helpers directly and stub out ``refresh``.
"""

import asyncio
import contextlib
from typing import Any, List

import pytest

from nate_ntm.api.models import AgentCounts, AgentDetailEvent, AgentOverview, RuntimeStatusResult, SwarmOverviewResult
from nate_ntm.tui.runtime_session import RuntimeSession
from nate_ntm.tui.screens import OverviewScreen
from nate_ntm.tui.widgets import AgentTable, EventView, SwarmSummary


class _DummyClient:
    """Minimal stand-in for :class:`RuntimeClient` used by RuntimeSession.

    The overview-related tests never invoke any of the client's async methods;
    it exists only so that we can construct a ``RuntimeSession`` instance and
    populate its cached state directly.
    """

    # No behavior required for this slice.
    pass


def _make_session_with_cached_state() -> RuntimeSession:
    """Return a ``RuntimeSession`` with cached status, overview, events, and flags.

    The returned session has never been ``connect()``-ed; instead we populate
    its caches directly to simulate the state that would normally be produced
    by the background polling and event loops.
    """

    session = RuntimeSession(client=_DummyClient())  # type: ignore[arg-type]

    counts = AgentCounts(
        total=3,
        starting=1,
        idle=1,
        running=1,
        waiting=0,
        failed=0,
    )

    session.runtime_status = RuntimeStatusResult(
        status="running",
        project_path="/tmp/project",
        swarm_id="swarm-001",
        agent_counts=counts,
    )

    agents: List[AgentOverview] = [
        AgentOverview(
            agent_id="nav-1",
            display_name="Navigator",
            status="running",
            has_unread_mail=False,
        ),
        AgentOverview(
            agent_id="plan-1",
            display_name="Planner",
            status="idle",
            has_unread_mail=True,
        ),
    ]

    session.swarm_overview = SwarmOverviewResult(
        swarm_id="swarm-001",
        project_path="/tmp/project",
        runtime_status="running",
        agent_counts=counts,
        agents=agents,
    )

    # Simulate a single recent event in the bounded buffer.
    session.event_buffer.append(
        AgentDetailEvent(
            event_id="evt-1",
            timestamp="2025-01-01T00:00:00Z",
            agent_id="nav-1",
            source="runtime",
            type="STATE_CHANGE",
            payload={"state": "running"},
        )
    )

    # Mark both control plane and events as degraded so that SwarmSummary can
    # surface those flags in its output.
    session.control_degraded = True
    session.control_error = "control failure"
    session.events_degraded = True
    session.events_error = "events failure"

    return session


def test_swarm_summary_renders_runtime_and_swarm_info() -> None:
    """SwarmSummary includes high-level runtime, swarm, and degraded info."""

    session = _make_session_with_cached_state()
    widget = SwarmSummary(session)

    text = widget.render()

    # Runtime and swarm identifiers.
    assert "Runtime status: running" in text
    assert "Project: /tmp/project" in text
    assert "Swarm: swarm-001" in text

    # Agent count metrics.
    assert "Agents: total=3" in text
    assert "starting=1" in text
    assert "idle=1" in text
    assert "running=1" in text
    assert "waiting=0" in text
    assert "failed=0" in text

    # Degraded flags should be surfaced inline.
    assert "[control degraded: control failure]" in text
    assert "[events degraded: events failure]" in text


def test_agent_table_renders_agents_from_swarm_overview() -> None:
    """AgentTable lists agents and their latest-known states."""

    session = _make_session_with_cached_state()
    widget = AgentTable(session)

    text = widget.render()

    assert "Agents:" in text
    # Both agents from the overview should be present.
    assert "nav-1" in text
    assert "Navigator" in text
    assert "status=running" in text

    assert "plan-1" in text
    assert "Planner" in text
    assert "status=idle" in text



def test_agent_table_marks_selected_agent_in_output() -> None:
    """AgentTable indicates the selected agent via a marker in its text.

    The selection is driven by ``RuntimeSession.selected_agent_id`` and should
    be reflected when rendering the table.
    """

    session = _make_session_with_cached_state()
    widget = AgentTable(session)

    # With no selection set, all rows use the default "-" marker.
    text = widget.render()
    assert "  - nav-1" in text
    assert "  - plan-1" in text
    assert "> nav-1" not in text

    # Selecting an agent on the session should be reflected in the output.
    session.select_agent("nav-1")
    text_with_selection = widget.render()
    assert "  > nav-1" in text_with_selection
    # The other agent should remain unselected.
    assert "  - plan-1" in text_with_selection



def test_agent_table_keyboard_navigation_updates_session_selection() -> None:
    """AgentTable's keyboard actions update the shared session selection.

    This test calls the Textual ``action_`` handlers directly without running
    a full event loop and stubs out ``refresh`` so that the widget can request
    re-renders without needing an active App.
    """

    session = _make_session_with_cached_state()
    widget = AgentTable(session)

    # Stub out refresh so the test does not depend on Textual internals.
    refresh_calls: list[Any] = []

    def _fake_refresh() -> None:
        refresh_calls.append(object())

    widget.refresh = _fake_refresh  # type: ignore[assignment]

    assert session.selected_agent_id is None

    # First "down" selects the first agent in the overview.
    widget.action_cursor_down()
    assert session.selected_agent_id == "nav-1"

    # Moving down again selects the next (and last) agent.
    widget.action_cursor_down()
    assert session.selected_agent_id == "plan-1"

    # Additional "down" presses should clamp at the last agent.
    widget.action_cursor_down()
    assert session.selected_agent_id == "plan-1"

    # Moving up should move back to the previous agent.
    widget.action_cursor_up()
    assert session.selected_agent_id == "nav-1"

    # The widget should have requested at least one refresh.
    assert len(refresh_calls) >= 1


def test_event_view_renders_recent_events() -> None:
    """EventView renders a simple list of recent events from the session."""

    session = _make_session_with_cached_state()
    widget = EventView(session, limit=10)

    text = widget.render()

    assert "Events (most recent first):" in text
    # Our test event is for nav-1; the exact timestamp and event-type fields
    # are intentionally loosely formatted in the widget.
    assert "agent=nav-1" in text


@pytest.mark.asyncio
async def test_overview_screen_reacts_to_session_updates() -> None:
    """OverviewScreen waits on the session and refreshes when updated.

    This test drives the private ``_watch_session_updates`` helper directly
    with a small fake session that records calls to ``wait_for_update``. The
    Textual screen's ``refresh`` method is stubbed so that we can assert that
    it is called when the session signals an update.
    """

    class _FakeSession:
        def __init__(self) -> None:
            self.calls: int = 0

        async def wait_for_update(self, *, last_seen: int | None = None, timeout: float | None = None) -> int:  # noqa: D401
            """Fake ``wait_for_update`` that immediately reports a new seq value."""

            self.calls += 1
            # Yield control back to the event loop so the watcher task can
            # interleave with this coroutine in tests.
            await asyncio.sleep(0)
            return self.calls

    session = _FakeSession()
    screen = OverviewScreen(session)  # type: ignore[arg-type]

    refresh_calls: list[Any] = []

    def _fake_refresh() -> None:
        refresh_calls.append(object())

    # Replace Textual's ``refresh`` method with our recording stub.
    screen.refresh = _fake_refresh  # type: ignore[assignment]

    task = asyncio.create_task(screen._watch_session_updates())

    # Allow the watcher to run at least one iteration.
    await asyncio.sleep(0.05)

    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task

    assert session.calls >= 1
    assert len(refresh_calls) >= 1
