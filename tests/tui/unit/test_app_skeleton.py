from __future__ import annotations

"""Basic smoke tests for the Textual console app shell.

These tests intentionally avoid running the full Textual event loop; they only
verify that the main application and default screen can be imported and
instantiated without pulling in runtime internals or transport details.
"""

from nate_ntm.tui.app import ConsoleApp
from nate_ntm.tui.screens import OverviewScreen
from nate_ntm.tui.runtime_session import RuntimeSession


class _DummyClient:
    """Minimal stand-in for RuntimeClient used by RuntimeSession.

    We don't exercise networking here; this dummy exists only so that we can
    construct a :class:`RuntimeSession` if needed.
    """

    # RuntimeSession only requires that the client object expose the async
    # methods it calls from its internal loops. For smoke tests we don't call
    # those methods, so the dummy can remain empty.
    pass


def test_console_app_instantiation() -> None:
    """ConsoleApp can be constructed around an existing RuntimeSession."""

    session = RuntimeSession(client=_DummyClient())  # type: ignore[arg-type]
    app = ConsoleApp(session=session)
    assert isinstance(app, ConsoleApp)


def test_overview_screen_instantiation_with_runtime_session() -> None:
    """OverviewScreen accepts a RuntimeSession and stores it for later use."""

    session = RuntimeSession(client=_DummyClient())  # type: ignore[arg-type]
    screen = OverviewScreen(session)

    assert screen.session is session
