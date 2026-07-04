"""Integration test: runtime agent events over WebSocket JSON-RPC (US3).

This test builds on the US1 quickstart wiring for
``RuntimeDaemon`` + WebSocket JSON-RPC control API and exercises the
agent event streaming path defined for US3:

* Agent runtime events are appended to per-agent ``AgentEventStream``
  buffers owned by :class:`RuntimeState`.
* The :class:`AgentSupervisor` exposes an ``on_agent_event`` callback
  that is invoked whenever a new event is appended.
* :func:`nate_ntm.runtime.runner.create_runtime_control_context` wires
  this callback into
  :meth:`nate_ntm.api.jsonrpc_ws.JsonRpcWebSocketServer.publish_event`,
  which in turn emits ``events.notify`` JSON-RPC notifications to
  subscribed WebSocket clients.

The goal of this test is to validate that a runtime started via the
runner helpers will publish a concrete runtime-originated agent event
("AgentFailed") as an ``events.notify`` message to a subscribed
WebSocket client.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from pathlib import Path

import websockets

from nate_ntm.api.jsonrpc import JSONRPC_VERSION
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

    This mirrors the helper used in the US1 WebSocket control API
    integration test, ensuring that the runtime can be started in
    ``StartupMode.RESUME`` with a single configured agent.
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


def test_runtime_ws_events_us3_agent_failure_publishes_events_notify(tmp_path: Path) -> None:
    """US3: runtime-originated agent failure events reach WebSocket clients.

    The scenario exercised here is intentionally small but end-to-end:

    * Start a runtime in ``resume`` mode via ``serve_runtime_control_api``.
    * Connect a WebSocket client and subscribe to events for the single
      configured agent ``nav-1``.
    * Trigger a runtime-originated failure event for that agent via the
      scheduler/AgentSupervisor pipeline.
    * Assert that the client receives an ``events.notify`` message whose
      payload matches the runtime event.
    """

    async def main() -> None:
        config = _make_resume_config_and_metadata(tmp_path)

        ctx: RuntimeControlContext = create_runtime_control_context(
            config,
            StartupMode.RESUME,
            host="127.0.0.1",
            port=0,
        )

        # Start serving the control API (WebSocket + RuntimeDaemon lifecycle)
        # in the background.
        serve_task = asyncio.create_task(serve_runtime_control_api(ctx))

        async def _wait_for_server_port() -> int:
            """Wait until the WebSocket server has bound to a TCP port."""

            for _ in range(50):
                port = ctx.ws_server.bound_port
                if port != 0:
                    return port
                await asyncio.sleep(0.05)
            raise AssertionError("WebSocket server did not bind to a port in time")

        port = await _wait_for_server_port()

        uri = f"ws://127.0.0.1:{port}"

        async with websockets.connect(uri) as client:
            # 1. Subscribe to events for the single configured agent.
            subscribe_request = {
                "jsonrpc": JSONRPC_VERSION,
                "method": "events.subscribe",
                "params": {"agent_ids": ["nav-1"], "include_runtime": True},
                "id": 1,
            }
            await client.send(json.dumps(subscribe_request))

            raw_sub = await client.recv()
            sub_response = json.loads(raw_sub)

            assert sub_response["jsonrpc"] == JSONRPC_VERSION
            assert sub_response["id"] == 1
            sub_id = sub_response["result"]["subscription_id"]
            assert isinstance(sub_id, str)

            # 2. Wait until the daemon has fully started and the scheduler has
            # registered runtime state for the configured agent. This mirrors
            # the expectations validated in the US1 quickstart test.
            async def _wait_for_agent_runtime_state() -> None:
                for _ in range(50):
                    if ctx.daemon.state.status is RuntimeStatus.RUNNING and "nav-1" in ctx.daemon.state.agents:
                        return
                    await asyncio.sleep(0.05)
                raise AssertionError("Agent runtime state was not initialized in time")

            await _wait_for_agent_runtime_state()

            agent_state = ctx.daemon.state.agents["nav-1"]
            assert agent_state.status is AgentStatus.IDLE

            # 3. Trigger a runtime-originated failure event via the scheduler.
            scheduler = ctx.daemon.scheduler
            assert scheduler is not None

            scheduler.mark_agent_failed("nav-1", error="boom")

            # 4. The WebSocket client should receive an ``events.notify``
            # message for the subscribed agent. Use a small timeout to avoid
            # hanging the test if the wiring is broken.
            raw_notify = await asyncio.wait_for(client.recv(), timeout=5.0)
            notify = json.loads(raw_notify)

            assert notify["jsonrpc"] == JSONRPC_VERSION
            assert notify["method"] == "events.notify"
            params = notify["params"]
            assert params["subscription_id"] == sub_id

            event = params["event"]
            assert event["agent_id"] == "nav-1"
            assert event["type"] == "AgentFailed"
            # Basic sanity check on the event identifier format produced by
            # ``AgentSupervisor._append_runtime_event``.
            assert event["event_id"].startswith("nav-1:")

            # 5. Request a graceful shutdown so the serve loop can exit
            # cleanly before the test completes.
            shutdown_request = {
                "jsonrpc": JSONRPC_VERSION,
                "method": "runtime.shutdown",
                "params": {"timeout_seconds": 5},
                "id": 2,
            }
            await client.send(json.dumps(shutdown_request))
            raw_shutdown = await client.recv()
            shutdown_response = json.loads(raw_shutdown)

            assert shutdown_response["jsonrpc"] == JSONRPC_VERSION
            assert shutdown_response["id"] == 2
            result = shutdown_response["result"]
            assert result["accepted"] is True
            assert result["status"] == RuntimeStatus.SHUTTING_DOWN.value

        # Once shutdown has been requested, the serve loop should exit.
        await asyncio.wait_for(serve_task, timeout=5.0)

        # After the loop exits, the daemon should be marked as fully stopped.
        assert ctx.daemon.state.status is RuntimeStatus.STOPPED

    asyncio.run(main())
