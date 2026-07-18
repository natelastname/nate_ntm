"""Unit tests for REAL ACP adapter wiring (T102).

These tests ensure that requesting ``AdapterKind.REAL`` for ACP selects
:class:`NateOhaAcpClient` without performing any external network I/O.
Process-launch and version-check behavior is exercised by dedicated
unit tests for :class:`NateOhaAcpClient`.
"""

from __future__ import annotations

from pathlib import Path

from nate_ntm.config.runtime_config import AdapterKind, load_runtime_config
from nate_ntm.runtime.adapters import create_runtime_adapters
from nate_ntm.runtime.agent_mail_client import McpAgentMailClient
from nate_ntm.runtime.acp_client import NateOhaAcpClient


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    return project


def test_real_adapter_mode_uses_nate_oha_acp_client(tmp_path: Path) -> None:
    """AdapterKind.REAL selects :class:`NateOhaAcpClient` for ACP.

    This test only exercises adapter construction; it does not perform any
    process or network I/O and therefore remains safe to run in offline CI.
    """

    project = _make_project(tmp_path)

    # Build a config that enables REAL only for ACP via the specific
    # adapter override. Agent Mail continues to use the canonical
    # MCP-backed client.
    config = load_runtime_config(project_path=project, acp_adapter=AdapterKind.REAL)

    adapters = create_runtime_adapters(config)

    assert isinstance(adapters.acp, NateOhaAcpClient)
    assert isinstance(adapters.agent_mail, McpAgentMailClient)


def test_global_real_adapter_mode_uses_real_for_both(tmp_path: Path) -> None:
    """Global REAL mode selects real adapters for both integrations."""

    project = _make_project(tmp_path)

    # Global REAL mode continues to construct the same concrete adapters,
    # keeping the selection logic simple and forward-only.
    config = load_runtime_config(project_path=project, adapter_mode=AdapterKind.REAL)

    adapters = create_runtime_adapters(config)

    assert isinstance(adapters.agent_mail, McpAgentMailClient)
    assert isinstance(adapters.acp, NateOhaAcpClient)
