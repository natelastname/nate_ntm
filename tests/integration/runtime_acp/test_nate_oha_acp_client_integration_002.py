"""Gated integration smoke tests for NateOhaAcpClient with real nate_OHA.

These tests implement T219 from ``specs/002-nate-oha-acp-adapter/tasks.md``.
They exercise the production :class:`NateOhaAcpClient` adapter against a real
``nate_OHA`` installation in a minimal way:

* A simple ``start_agent`` → ``stop_agent`` roundtrip succeeds without
  leaking subprocess handles.

The module is **opt-in** and skipped by default so that normal CI does not
require nate_OHA (or any particular LLM backend) to be installed. To run
these tests locally, set the ``NATE_OHA_INTEGRATION`` environment variable
before invoking pytest, for example::

    NATE_OHA_INTEGRATION=1 uv run pytest -q \
      tests/integration/runtime_acp/test_nate_oha_acp_client_integration_002.py

It is the caller's responsibility to ensure that:

* The ``nate_OHA`` executable is on ``PATH``.
* Any required LLM/API credentials are configured so that nate_OHA can
  start successfully.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nate_ntm.config.runtime_config import AdapterKind, RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import NateOhaAcpClient
from nate_ntm.runtime.metadata_store import AgentMetadata

from nate_ntm.runtime.nate_oha_launch import build_effective_nate_oha_config


RUN_REAL_NATE_OHA = bool(os.environ.get("NATE_OHA_INTEGRATION"))

pytestmark = pytest.mark.skipif(
    not RUN_REAL_NATE_OHA,
    reason="Set NATE_OHA_INTEGRATION=1 to run nate_OHA integration smoke tests.",
)


def _make_runtime_config(project_path: Path) -> RuntimeConfig:
    """Return a ``RuntimeConfig`` suitable for NateOhaAcpClient integration.

    The config uses ``AdapterKind.REAL`` so that it matches the production
    code paths and points Nate OHA at the repository's sample profile.
    Metadata is written under ``project_path`` so that each test can run in
    isolation.
    """

    # Snapshot the current environment so ``load_runtime_config`` does not
    # consult any repository-level .env files and then overlay the minimal
    # Nate OHA launch settings required by NateOhaAcpClient.
    env_snapshot = dict(os.environ)
    repo_root = Path(__file__).resolve().parents[3]
    base_config = repo_root / "nate-oha-profiles" / "profile1.json"

    env_snapshot.update(
        {
            "NATE_NTM_PROJECT_DIR": str(project_path),
            "NATE_NTM_ADAPTER_MODE": AdapterKind.REAL.value,
            "NATE_NTM_NATE_OHA_CONFIG": str(base_config),
            "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
        }
    )

    return load_runtime_config(
        project_path=project_path,
        metadata_dir=project_path / ".nate_ntm",
        adapter_mode=AdapterKind.REAL,
        env=env_snapshot,
    )


def test_start_and_stop_agent_roundtrip(tmp_path: Path) -> None:
    """Launch and cleanly stop a real nate_OHA process for one agent.

    This is a minimal smoke test for ``start_agent`` / ``stop_agent`` using
    the real nate_OHA executable. It does not assert on nate_OHA's
    functional behavior beyond successful startup and shutdown as observed
    through :class:`NateOhaAcpClient`'s process records.
    """

    config = _make_runtime_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    # Use a minimal agent metadata record with no Agent Mail identity so that
    # nate_OHA is launched without Agent Mail integration. Attach a persisted
    # NateOhaConfig snapshot so that the adapter can launch from the unified
    # configuration model.
    base_meta = AgentMetadata(agent_id="agent-1", display_name="Agent One")
    nate_oha_cfg = build_effective_nate_oha_config(config=config, metadata=base_meta)
    meta = AgentMetadata(agent_id="agent-1", display_name="Agent One", nate_oha_config=nate_oha_cfg)

    # ``start_agent`` will perform the nate_OHA version check and then
    # spawn the subprocess. Any incompatibility or startup failure should
    # surface as ``AcpClientError``.
    client.start_agent("agent-1", metadata=meta)

    status = client.get_status("agent-1")
    assert status.agent_id == "agent-1"
    assert status.state == "running"

    # Request a graceful shutdown and ensure the adapter reports a
    # non-running state and drops its subprocess handle.
    client.stop_agent("agent-1", timeout=client.shutdown_timeout)

    status_after = client.get_status("agent-1")
    assert status_after.state in {"terminated", "failed"}

    # Internal bookkeeping should no longer hold a live process handle for
    # this agent.
    assert "agent-1" not in client._process_handles
