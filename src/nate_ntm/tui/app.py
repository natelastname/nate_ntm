from __future__ import annotations

"""Textual application shell for the nate_ntm runtime console.

This module defines :class:`ConsoleApp`, a thin Textual :class:`~textual.app.App`
subclass that uses a single shared
:class:`~nate_ntm.tui.runtime_session.RuntimeSession` instance provided by the
caller.

Layering
========

The app is responsible for:

* Accepting a single shared :class:`RuntimeSession` instance supplied by the
  caller.
* Pushing the default :class:`OverviewScreen` as the initial screen.

The lifecycle of :class:`RuntimeSession` (``connect`` / ``disconnect``)
is owned by the CLI/entrypoint layer, not by the Textual application.

Textual screens and widgets **do not** talk to the runtime or transports
directly; they observe a shared :class:`RuntimeSession` owned by this app.
The construction of :class:`RuntimeClient` and :class:`RuntimeSession` lives
outside the Textual layer (for example, in the Typer CLI entrypoint).
"""

from typing import Any

from textual.app import App, ComposeResult

from nate_ntm.tui.runtime_session import RuntimeSession
from nate_ntm.tui.screens.overview import OverviewScreen


class ConsoleApp(App[None]):
    """Textual runtime console application.

    The app is constructed around a single shared :class:`RuntimeSession`
    instance provided by the caller. All screens obtain runtime state via that
    shared session and must not create additional protocol clients.
    """

    TITLE = "nate_ntm Runtime Console"

    def __init__(self, session: RuntimeSession, **kwargs: Any) -> None:
        """Construct a new console app.

        Parameters
        ----------
        session:
            The shared :class:`RuntimeSession` used by all screens and widgets.
            The session should be created, configured, and connected by the
            caller (typically the Typer CLI entrypoint); the app does not
            manage the session lifecycle.
        """

        super().__init__(**kwargs)
        self.session = session

    async def on_mount(self) -> None:  # pragma: no cover - exercised via Textual runtime
        """Push the overview screen once the app is mounted.

        The :class:`RuntimeSession` is expected to be already connected by the
        caller (for example, the Typer CLI entrypoint).
        """

        await self.push_screen(OverviewScreen(self.session))

    def compose(self) -> ComposeResult:  # pragma: no cover - UI composition
        """Compose the root view.

        The app itself does not render substantial UI; it immediately pushes
        :class:`OverviewScreen` from :meth:`on_mount`. This method is provided
        for completeness and potential future extensions.
        """

        # Textual requires a compose method, but since we push the overview
        # screen explicitly in :meth:`on_mount`, there is nothing to compose at
        # the root level for now.
        yield from ()
