from __future__ import annotations

"""Tests for the agent inspection screen and detail view.

These tests focus on two aspects:

* The detail view renders information based solely on cached
  :class:`RuntimeSession` state (selected agent + cached detail result).
* The inspection screen uses :meth:`RuntimeSession.get_agent_detail` to
  populate the cache for the currently selected agent and asks the view to
  refresh once additional information is available.

The tests intentionally avoid running a full Textual event loop and instead
invoke private helpers directly while stubbing out ``refresh`` and ``query_one``
where needed.
"""

from typing import Any, List, Tuple

import pytest

from nate_ntm.api.models import AgentDetailAgent, AgentDetailEvent, AgentDetailResult
from nate_ntm.tui.runtime_session import RuntimeSession
from nate_ntm.tui.screens.agent_inspect import AgentInspectScreen, AgentInspectView


class _DummyClient:
    """Minimal stand-in for :class:`RuntimeClient`.

    The inspection tests never invoke any of the client's async methods; it
    exists only so that we can construct a :class:`RuntimeSession` instance and
    populate its cached state directly.
    """

    # No behavior required for this slice.
    pass


def _make_session_with_agent_detail() -> Tuple[RuntimeSession, AgentDetailResult]:
    """Return a ``RuntimeSession`` with a single cached agent detail entry."""

    session = RuntimeSession(client=_DummyClient())  # type: ignore[arg-type]

    detail = AgentDetailResult(
        agent=AgentDetailAgent(
            agent_id="agent-1",
            display_name="Agent One",
            status="idle",
            agent_mail_identity="agent-1@example.test",
            conversation_id="conv-1",
            last_error=None,
        ),
        events=[
            AgentDetailEvent(
                event_id="evt-1",
                timestamp="2026-07-07T12:00:00Z",
                agent_id="agent-1",
                source="runtime",
                type="started",
                payload={"info": "started"},
            )
        ],
    )

    session.select_agent("agent-1")
    session.agent_details["agent-1"] = detail

    return session, detail


def test_agent_inspect_view_renders_message_when_no_agent_selected() -> None:
    """AgentInspectView shows a friendly message when nothing is selected."""

    session = RuntimeSession(client=_DummyClient())  # type: ignore[arg-type]
    view = AgentInspectView(session)

    text = view.render()

    assert "Agent inspection" in text
    assert "(no agent selected)" in text


def test_agent_inspect_view_renders_cached_detail_for_selected_agent() -> None:
    """AgentInspectView uses RuntimeSession.selected_agent_id and cached detail."""

    session, detail = _make_session_with_agent_detail()
    view = AgentInspectView(session)

    text = view.render()

    assert f"id: {detail.agent.agent_id}" in text
    assert f"name: {detail.agent.display_name}" in text
    assert f"status: {detail.agent.status}" in text
    assert detail.agent.agent_mail_identity in text
    assert detail.agent.conversation_id in text

    # The recent-events section should mention at least one event for the agent.
    assert "Recent events" in text
    assert "type=started" in text


@pytest.mark.asyncio
async def test_agent_inspect_screen_loads_detail_via_runtime_session() -> None:
    """AgentInspectScreen fetches detail for the current selection via session.

    This test drives the private ``_load_initial_detail`` helper directly using
    a small fake session that records calls to ``get_agent_detail``. The
    Textual ``query_one`` method is stubbed so that we can assert that the view
    is asked to refresh after detail has been loaded.
    """

    class _FakeSession:
        def __init__(self) -> None:
            self.selected_agent_id = "agent-1"
            self.calls: List[tuple[str, Any]] = []

        async def get_agent_detail(
            self, agent_id: str, max_events: int = 100, *, force_refresh: bool = False
        ) -> object:  # pragma: no cover - behavior verified via call recording
            self.calls.append(
                (
                    "get_agent_detail",
                    {
                        "agent_id": agent_id,
                        "max_events": max_events,
                        "force_refresh": force_refresh,
                    },
                )
            )
            return object()

    session = _FakeSession()
    screen = AgentInspectScreen(session)  # type: ignore[arg-type]

    # Stub out ``query_one`` so that we can observe a refresh request without
    # requiring a full Textual DOM.
    query_calls: list[Any] = []

    class _FakeView:
        def __init__(self) -> None:
            self.refresh_calls: int = 0

        def refresh(self) -> None:  # pragma: no cover - trivial
            self.refresh_calls += 1

    fake_view = _FakeView()

    def _fake_query_one(selector: str, widget_type: Any) -> Any:
        query_calls.append((selector, widget_type))
        return fake_view

    screen.query_one = _fake_query_one  # type: ignore[assignment]

    await screen._load_initial_detail()

    # The screen should have asked the session for detail for the selected id.
    assert session.calls == [
        (
            "get_agent_detail",
            {"agent_id": "agent-1", "max_events": 100, "force_refresh": False},
        )
    ]

    # And the view should have been asked to refresh.
    assert query_calls
    assert fake_view.refresh_calls >= 1
