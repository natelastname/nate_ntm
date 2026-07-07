from __future__ import annotations

"""Integration-style tests for the `nate-ntm console` entrypoint.

These tests exercise the Typer CLI wiring for the console command without
starting a full Textual event loop. They ensure that the console command is
registered and that invoking ``--help`` works without importing runtime
internals directly.
"""

from typer.testing import CliRunner

from nate_ntm.cli import app as cli_app


runner = CliRunner()


def test_console_command_help_available() -> None:
    """The `console` command is registered and exposes help output."""

    result = runner.invoke(cli_app, ["console", "--help"])

    assert result.exit_code == 0
    assert "Launch the Textual runtime console" in result.stdout
