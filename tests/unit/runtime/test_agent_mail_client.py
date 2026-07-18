"""Unit tests for the Agent Mail MCP-backed client.

This module focuses on the production :class:`McpAgentMailClient` behavior
that we still rely on in the nate-oha / ACP-centric design. Tests that
exercise the in-memory ``FakeAgentMailClient`` (including deterministic
project IDs, identities, and unread-mail flags) have been removed as part
of the migration away from fake adapters.
"""

from __future__ import annotations

from pathlib import Path
import socket

import pytest

from nate_ntm.config.runtime_config import load_runtime_config
from nate_ntm.runtime.agent_mail_client import McpAgentMailClient


def _make_mcp_client(tmp_path: Path, project_key: str | None = None) -> McpAgentMailClient:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    # ``load_runtime_config`` will resolve ``agent_mail_project`` from the
    # explicit argument when provided, falling back to the project path
    # otherwise. We rely on that behavior here to match the runtime's
    # production configuration logic.
    config = load_runtime_config(
        project_path=project,
        agent_mail_project=project_key,
    )
    return McpAgentMailClient(config=config)



def test_mcp_agent_mail_client_ensure_project_uses_configured_project_key(tmp_path: Path) -> None:
    """McpAgentMailClient.ensure_project returns the configured project key.

    For REAL adapters the Agent Mail *project key* comes from
    :class:`RuntimeConfig.agent_mail_project` (or, by default, the
    absolute project path). The client must return that same key from
    :meth:`ensure_project` so that it can be stored in
    ``SwarmMetadata.agent_mail_project_id`` and propagated into
    ``AGENT_MAIL_PROJECT`` for nate-oha launches.

    """

    # These tests require a running Agent Mail MCP server.
    try:
        with socket.create_connection(("127.0.0.1", 8765), timeout=1.0):
            pass
    except OSError:
        pytest.skip("Agent Mail server not available on 127.0.0.1:8765")


    # Use an explicit project key that does not look like a path to make
    # the expectation clear and robust. This will be passed through to the
    # real ``mcp_agent_mail`` server running on 127.0.0.1:8765.
    project_key = "proj-explicit-key-123"
    client = _make_mcp_client(tmp_path, project_key=project_key)

    # Call the real MCP-backed implementation twice. The client is expected
    # to return a stable project identifier derived from the configured
    # ``agent_mail_project`` value and to cache it across calls.
    project_id_1 = client.ensure_project()
    project_id_2 = client.ensure_project()

    # ``ensure_project`` must always return the configured project key (or an
    # equivalent canonical identifier) and must be stable across calls.
    # If the underlying MCP service changes this behavior, these assertions
    # will fail and we will update the implementation accordingly.
    assert project_id_1 == project_id_2
    assert project_id_1 == client.config.agent_mail_project
