"""Integration test: RuntimeDaemon + FastAPI JSON-RPC control API (US1).

This test exercises a thin end-to-end path from a project directory on
 disk through:

* ``RuntimeConfig`` resolution.
* ``RuntimeDaemon.resume`` startup semantics.
* ``RuntimeApiServer`` exposed via the unified FastAPI control API.
* A real HTTP JSON-RPC client talking to the running runtime.

The goal is to complement existing quickstart-style tests by validating
that a runtime started in-process with the control API can be
queried via ``runtime.get_status`` and shut down via ``runtime.shutdown``
using the JSON-RPC shape defined in ``contracts/runtime-api.md``.
"""

from __future__ import annotations

import asyncio
from datetime import datetime
from pathlib import Path

from nate_ntm.api.client import JsonRpcHttpClient
from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.daemon import StartupMode
from nate_ntm.runtime.metadata_store import AgentMetadata, MetadataStore, SwarmMetadata
from nate_ntm.runtime.runner import (
    RuntimeControlContext,
    create_runtime_control_context,
    serve_runtime_control_api,
)
from nate_ntm.runtime.state import AgentStatus, RuntimeStatus


def _make_resume_config_and_metadata(tmp_path: Path) -> RuntimeConfig:
    """Create a project with minimal swarm/agent metadata for resume mode.

    This mirrors the "create once, then resume" flow used in other US1
    integration tests: swarm and agent metadata are written under
    ``.nate_ntm/``, and the runtime later resumes from that state.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    config: RuntimeConfig = load_runtime_config(project_path=project)
    store = MetadataStore(config=config)

    now = datetime(2026, 7, 3, 12, 0, 0)

    agent = AgentMetadata(agent_id="nav-1", display_name="Navigator 1")
    swarm = SwarmMetadata(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
        agents={agent.agent_id: agent},
    )

    store.save_swarm_metadata(swarm)
    store.save_agent_metadata(agent)

    return config


def test_runtime_ws_control_api_us1_status_and_shutdown(tmp_path: Path) -> None:
    """US1: runtime.get_status and runtime.shutdown over HTTP JSON-RPC.

    This test runs a :class:`RuntimeDaemon` plus
    the FastAPI-based control API in-process via the
    :mod:`nate_ntm.runtime.runner` helpers and verifies that:

    * ``runtime.get_status`` reports ``Running`` with correct identifiers.
    * ``runtime.shutdown`` triggers a graceful shutdown of the runner.
    """

    async def main() -> None:
        # Arrange: create project, metadata, and a runtime control context
        # using resume semantics.
        config = _make_resume_config_and_metadata(tmp_path)

        # Use an ephemeral port for the control API server to avoid clashes
        # with other tests or local processes. The actual bound port is
        # discovered via ``ctx.bound_port`` after startup.
        ctx: RuntimeControlContext = create_runtime_control_context(
            config,
            StartupMode.RESUME,
            host="127.0.0.1",
            port=0,
        )

        # Start serving the control API in the background.
        serve_task = asyncio.create_task(serve_runtime_control_api(ctx))

        async def _wait_for_server_port() -> int:
            """Wait until the control API server has bound to a TCP port."""

            for _ in range(50):
                port = ctx.bound_port
                if port != 0:
                    return port
                await asyncio.sleep(0.05)
            raise AssertionError("Control API server did not bind to a port in time")

        port = await _wait_for_server_port()

        client = JsonRpcHttpClient(host="127.0.0.1", port=port, timeout=5.0)

        async def _wait_for_running_status() -> dict[str, object]:
            last_exc: Exception | None = None
            for _ in range(50):
                try:
                    result = await client.call_for_result("runtime.get_status", {})
                except OSError as exc:
                    last_exc = exc
                    await asyncio.sleep(0.1)
                    continue
                return result
            raise AssertionError(f"runtime.get_status did not succeed: {last_exc!r}")

        status = await _wait_for_running_status()

        assert status["status"] == RuntimeStatus.RUNNING.value
        assert status["project_path"] == str(config.project_path)
        assert status["swarm_id"] == config.swarm_id

        # For this minimal test we only expect a single configured agent.
        counts = status["agent_counts"]
        assert counts["total"] == 1

        # Request a graceful shutdown via the control API.
        shutdown_result = await client.call_for_result(
            "runtime.shutdown", {"timeout_seconds": 5}
        )
        assert shutdown_result["accepted"] is True
        assert shutdown_result["status"] == RuntimeStatus.SHUTTING_DOWN.value

        # Once shutdown has been requested, the serve loop should exit.
        await asyncio.wait_for(serve_task, timeout=5.0)

        # After the loop exits, the daemon should be marked as fully
        # stopped.
        assert ctx.daemon.state.status is RuntimeStatus.STOPPED

        # The single agent should have been launched in dev-mode via the
        # scheduler when the daemon started.
        assert set(ctx.daemon.state.agents.keys()) == {"nav-1"}
        agent_state = ctx.daemon.state.agents["nav-1"]
        assert agent_state.status is AgentStatus.IDLE

    asyncio.run(main())



def test_runtime_ws_control_api_us1_create_with_agents_status_and_overview(tmp_path: Path) -> None:
    """US1.5: create-mode startup with a non-empty fake swarm.

    This test mirrors a CLI-driven flow of starting the runtime in
    ``create`` mode with a configured number of agents, but drives the
    underlying runner helpers directly. It verifies that:

    * ``runtime.get_status`` reports the expected agent counts for the
      newly created swarm.
    * ``swarm.get_overview`` lists the fake agents with ``Idle`` status.
    * ``runtime.shutdown`` cleanly tears down the control API server.
    """

    async def main() -> None:
        project = tmp_path / "project"
        project.mkdir(parents=True, exist_ok=True)

        config: RuntimeConfig = load_runtime_config(project_path=project)

        ctx: RuntimeControlContext = create_runtime_control_context(
            config,
            StartupMode.CREATE,
            host="127.0.0.1",
            port=0,
            agent_count=2,
        )

        serve_task = asyncio.create_task(serve_runtime_control_api(ctx))

        async def _wait_for_server_port() -> int:
            for _ in range(50):
                port = ctx.bound_port
                if port != 0:
                    return port
                await asyncio.sleep(0.05)
            raise AssertionError("Control API server did not bind to a port in time")

        port = await _wait_for_server_port()

        client = JsonRpcHttpClient(host="127.0.0.1", port=port, timeout=5.0)

        async def _wait_for_running_status() -> dict[str, object]:
            last_exc: Exception | None = None
            for _ in range(50):
                try:
                    result = await client.call_for_result("runtime.get_status", {})
                except OSError as exc:
                    last_exc = exc
                    await asyncio.sleep(0.1)
                    continue
                return result
            raise AssertionError(f"runtime.get_status did not succeed: {last_exc!r}")

        status = await _wait_for_running_status()

        assert status["status"] == RuntimeStatus.RUNNING.value
        assert status["project_path"] == str(config.project_path)
        assert status["swarm_id"] == config.swarm_id

        counts = status["agent_counts"]
        assert counts == {
            "total": 2,
            "starting": 0,
            "idle": 2,
            "running": 0,
            "waiting": 0,
            "failed": 0,
        }

        overview = await client.call_for_result("swarm.get_overview", {})
        agents = overview["agents"]
        assert len(agents) == 2

        agent_ids = sorted(a["agent_id"] for a in agents)
        assert agent_ids == ["agent-1", "agent-2"]

        for agent in agents:
            assert agent["status"] == AgentStatus.IDLE.value
            assert agent["last_error"] is None

        # Request a graceful shutdown via the control API.
        shutdown_result = await client.call_for_result(
            "runtime.shutdown", {"timeout_seconds": 5}
        )
        assert shutdown_result["accepted"] is True
        assert shutdown_result["status"] == RuntimeStatus.SHUTTING_DOWN.value

        await asyncio.wait_for(serve_task, timeout=5.0)
        assert ctx.daemon.state.status is RuntimeStatus.STOPPED

        # The two fake agents should have been launched in dev-mode via the
        # scheduler when the daemon started.
        assert set(ctx.daemon.state.agents.keys()) == {"agent-1", "agent-2"}
        for agent_state in ctx.daemon.state.agents.values():
            assert agent_state.status is AgentStatus.IDLE

    asyncio.run(main())

