"""Gated E2E tests for REAL runtime + nate_OHA + Agent Mail.

These tests are intended to exercise as much of the *real* runtime
stack as is currently implemented when running with:

* A live ``mcp_agent_mail`` server reachable at
  ``http://127.0.0.1:8765/api`` (or equivalent, configured via
  ``NATE_NTM_AGENT_MAIL_URL`` / ``AGENT_MAIL_URL``).
* A working ``nate_OHA`` installation on ``PATH``.
* REAL adapters for both Agent Mail (``McpAgentMailClient``) and ACP
  (``NateOhaAcpClient``).

The goal is to complement the lighter T242 smoke tests under
``tests/integration/quickstart/`` with a single, opt-in E2E test that
covers:

1. Creating a one-agent swarm with REAL adapters.
2. Verifying that Agent Mail project/identity/credentials and ACP
   conversation identifiers are allocated and persisted.
3. Starting a real ``nate_OHA`` subprocess via the runtime-owned
   ``NateOhaAcpClient`` and observing process-lifecycle events through
   the runtime's event pipeline.
4. Shutting the agent down cleanly and confirming that no subprocess
   handles are left behind.
5. Resuming the same swarm with fresh adapters and verifying that the
   Agent Mail identity, credentials, and ACP conversation identifier are
   reused.
6. Starting and stopping ``nate_OHA`` again under the resumed daemon.

These tests deliberately *do not* assert on model outputs or detailed
Agent Mail message contents. They focus on lifecycle and integration
facts that should remain stable across model and server upgrades.

Limitations / TODOs
-------------------

The current runtime scheduler (see ``runtime/scheduler.py``) is still a
skeleton: it does not yet trigger ACP turns based on Agent Mail inbox
contents. As a result, this E2E test does **not** attempt to:

* Deliver a real Agent Mail message to the agent and
* Observe an ACP turn or tool invocation driven by that message.

Once T016/T017 (or equivalent) introduce scheduler-driven mail→turn
behavior, this module should be extended with a follow-up E2E test that:

* Sends a small Agent Mail message for the configured agent, and
* Asserts (via ``agent.get_detail`` or the WebSocket events stream) that
  the runtime observes the unread mail and starts at least one ACP
  turn.

For now, the focus is on validating the REAL adapter wiring,
create→start→shutdown→resume flows, and basic event propagation from the
nate_OHA ACP adapter into the runtime's event streams.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from nate_ntm.config.runtime_config import AdapterKind, RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import NateOhaAcpClient
from nate_ntm.runtime.adapters import create_runtime_adapters
from nate_ntm.runtime.agent_mail_client import McpAgentMailClient
from nate_ntm.runtime.daemon import RuntimeDaemon, StartupMode
from nate_ntm.runtime.state import RuntimeStatus


RUN_REAL_E2E = bool(os.environ.get("NATE_OHA_E2E"))

pytestmark = pytest.mark.skipif(
    not RUN_REAL_E2E,
    reason=(
        "Set NATE_OHA_E2E=1 to run REAL runtime + nate_OHA + Agent Mail E2E test. "
        "Requires a live mcp_agent_mail server and nate_OHA on PATH."
    ),
)


def _make_real_runtime_config(project_path: Path) -> RuntimeConfig:
    """Construct a ``RuntimeConfig`` for REAL adapters for E2E tests.

    This mirrors the helper used in the T242 quickstart integration test
    but is kept local to avoid cross-test imports. It explicitly sets
    ``adapter_mode=AdapterKind.REAL`` and uses a project-local metadata
    directory under ``project_path`` so that each test run is isolated.
    """

    return load_runtime_config(
        project_path=project_path,
        metadata_dir=project_path / ".nate_ntm",
        adapter_mode=AdapterKind.REAL,
    )


def test_real_runtime_nate_oha_agent_mail_create_start_resume(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """End-to-end launch + resume test for REAL runtime with nate_OHA.

    High-level scenario:

    1. Create a temp project directory and configure REAL adapters for
       both Agent Mail and ACP.
    2. Call :meth:`RuntimeDaemon.create` with ``agent_count=1`` so that
       the runtime allocates an Agent Mail project, per-agent identity,
       credentials, and an ACP conversation ID via the REAL adapters.
    3. Start the runtime (scheduler) and then launch a real ``nate_OHA``
       subprocess for the agent via ``daemon.acp_client.start_agent``.
    4. Use the runtime API to fetch agent detail and verify that
       process-lifecycle events from :class:`NateOhaAcpClient` have been
       recorded.
    5. Stop the nate_OHA process and request a graceful runtime
       shutdown, ensuring that no subprocess handles are left behind.
    6. Construct fresh adapters and call :meth:`RuntimeDaemon.resume`
       against the same project metadata, verifying that the Agent Mail
       project, per-agent identity/credentials, and ACP conversation ID
       are reused.
    7. Start the resumed runtime, launch nate_OHA for the agent again,
       and confirm that agent metadata (identity + conversation) is
       unchanged.
    8. Stop the agent and cleanly shut the resumed runtime down.

    This test intentionally avoids asserting on model outputs or Agent
    Mail message contents. It focuses on lifecycle and metadata
    continuity across create→start→shutdown→resume cycles.
    """

    # Align runtime and nate_OHA Agent Mail configuration so that both
    # use the same project key and upstream URL. We use the absolute
    # project path as the Agent Mail project key to keep the mapping
    # simple and deterministic.
    project_key = str(tmp_path)
    base_url = "http://127.0.0.1:8765/api"

    # RuntimeConfig will pick these up via its Agent Mail resolution
    # helpers, and NateOhaAcpClient will in turn propagate them into
    # AGENT_MAIL_* for the child nate_OHA process.
    monkeypatch.setenv("NATE_NTM_AGENT_MAIL_PROJECT", project_key)
    monkeypatch.setenv("NATE_NTM_AGENT_MAIL_URL", base_url)

    # ------------------------------------------------------------------
    # Phase 1: create a swarm, start the runtime, and launch nate_OHA.
    # ------------------------------------------------------------------

    config = _make_real_runtime_config(tmp_path)

    adapters = create_runtime_adapters(config)
    assert isinstance(adapters.agent_mail, McpAgentMailClient)
    assert isinstance(adapters.acp, NateOhaAcpClient)

    # Create a new swarm with a single agent. This allocates the Agent
    # Mail project + identity/credentials and the ACP conversation ID,
    # persisting them into SwarmMetadata and per-agent metadata files
    # under .nate_ntm/.
    daemon = RuntimeDaemon.create(config, agent_count=1, adapters=adapters)

    # Basic sanity checks on the created metadata.
    swarm = daemon.swarm_metadata
    assert swarm.agent_mail_project_id
    assert swarm.agent_mail_project_id == project_key
    assert set(swarm.agents.keys()) == {"agent-1"}

    agent_meta = daemon.metadata_store.load_agent_metadata("agent-1")
    assert agent_meta.agent_mail_identity
    assert agent_meta.agent_mail_credentials_ref
    assert agent_meta.conversation_id

    # Start the runtime/scheduler so that runtime state is initialized.
    assert daemon.state.status is RuntimeStatus.STARTING
    daemon.start()
    assert daemon.state.status is RuntimeStatus.RUNNING

    # Launch a real nate_OHA process for the agent using the metadata
    # produced above. Any configuration errors (for example, missing
    # Agent Mail settings or incompatible nate_OHA version) should
    # surface as an AcpClientError from start_agent.
    acp_client = daemon.acp_client
    assert isinstance(acp_client, NateOhaAcpClient)

    acp_client.start_agent("agent-1", metadata=agent_meta)

    status = acp_client.get_status("agent-1")
    assert status.agent_id == "agent-1"
    assert status.state == "running"

    # Use the in-process runtime API to fetch agent detail and verify
    # that at least one ACP-originated event was recorded (for example
    # ``nate_oha_process_started`` / ``nate_oha_process_ready``).
    detail = daemon.get_agent_detail("agent-1", max_events=10)
    events = detail["events"]
    assert isinstance(events, list)
    assert events, "Expected at least one runtime/ACP event for agent-1"

    # Stop the nate_OHA process and confirm that no live subprocess
    # handle remains for the agent.
    acp_client.stop_agent("agent-1", timeout=acp_client.shutdown_timeout)

    status_after = acp_client.get_status("agent-1")
    assert status_after.state in {"terminated", "failed"}
    assert "agent-1" not in acp_client._process_handles

    # Request a graceful runtime shutdown and mark it fully stopped.
    daemon.request_shutdown()
    assert daemon.state.shutdown_requested is True
    daemon.mark_stopped()
    assert daemon.state.status is RuntimeStatus.STOPPED

    # ------------------------------------------------------------------
    # Phase 2: resume the same swarm with fresh adapters and ensure that
    # Agent Mail + ACP identifiers are reused.
    # ------------------------------------------------------------------

    # Build a fresh set of adapters to mirror a new process. We rely on
    # the same project_path and Agent Mail environment so that
    # RuntimeConfig and McpAgentMailClient derive the same project key.
    config2 = _make_real_runtime_config(tmp_path)
    adapters2 = create_runtime_adapters(config2)
    assert isinstance(adapters2.agent_mail, McpAgentMailClient)
    assert isinstance(adapters2.acp, NateOhaAcpClient)

    # Resume the daemon against the existing metadata. The REAL Agent
    # Mail adapter and REAL ACP adapter will revalidate that the
    # project/identity/conversation identifiers match the persisted
    # values, raising RuntimeStartupError on mismatch.
    daemon2 = RuntimeDaemon.resume(config2, adapters=adapters2)
    assert daemon2.startup_mode is StartupMode.RESUME

    swarm2 = daemon2.swarm_metadata
    assert swarm2.agent_mail_project_id == swarm.agent_mail_project_id
    assert set(swarm2.agents.keys()) == {"agent-1"}

    agent_meta2 = daemon2.metadata_store.load_agent_metadata("agent-1")

    # Agent Mail and ACP identifiers must be reused on resume.
    assert agent_meta2.agent_mail_identity == agent_meta.agent_mail_identity
    assert agent_meta2.agent_mail_credentials_ref == agent_meta.agent_mail_credentials_ref
    assert agent_meta2.conversation_id == agent_meta.conversation_id

    # Start the resumed runtime and launch nate_OHA for the agent again.
    assert daemon2.state.status is RuntimeStatus.STARTING
    daemon2.start()
    assert daemon2.state.status is RuntimeStatus.RUNNING

    acp_client2 = daemon2.acp_client
    assert isinstance(acp_client2, NateOhaAcpClient)

    acp_client2.start_agent("agent-1", metadata=agent_meta2)

    status2 = acp_client2.get_status("agent-1")
    assert status2.agent_id == "agent-1"
    assert status2.state == "running"

    # Fetch agent detail again and confirm that the persisted identity
    # and conversation identifiers are unchanged across the resume.
    detail2 = daemon2.get_agent_detail("agent-1", max_events=20)
    agent_payload2 = detail2["agent"]
    assert agent_payload2["agent_mail_identity"] == agent_meta.agent_mail_identity
    assert agent_payload2["conversation_id"] == agent_meta.conversation_id

    # Stop the nate_OHA process under the resumed daemon and ensure that
    # no subprocess handle remains.
    acp_client2.stop_agent("agent-1", timeout=acp_client2.shutdown_timeout)

    status2_after = acp_client2.get_status("agent-1")
    assert status2_after.state in {"terminated", "failed"}
    assert "agent-1" not in acp_client2._process_handles

    # Request shutdown for the resumed runtime and mark it fully
    # stopped.
    daemon2.request_shutdown()
    assert daemon2.state.shutdown_requested is True
    daemon2.mark_stopped()
    assert daemon2.state.status is RuntimeStatus.STOPPED
