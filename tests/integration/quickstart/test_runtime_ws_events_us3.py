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


class _JsonRpcWsTestClient:
    """Minimal long-lived JSON-RPC client for WebSocket tests.

    This helper is intentionally small and tailored for integration tests:

    * It keeps a single WebSocket connection open.
    * It can issue multiple JSON-RPC requests over that connection.
    * It can receive out-of-band notifications such as ``events.notify``.
    """

    def __init__(self, websocket: websockets.WebSocketClientProtocol) -> None:  # type: ignore[name-defined]
        self._ws = websocket
        self._next_id = 1

    async def call(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        request_id = self._next_id
        self._next_id += 1

        request = {
            "jsonrpc": JSONRPC_VERSION,
            "method": method,
            "params": params or {},
            "id": request_id,
        }

        await self._ws.send(json.dumps(request))
        raw = await self._ws.recv()
        response = json.loads(raw)

        assert response["jsonrpc"] == JSONRPC_VERSION
        assert response["id"] == request_id
        return response

    async def call_for_result(self, method: str, params: dict[str, object] | None = None) -> dict[str, object]:
        response = await self.call(method, params)
        assert "result" in response
        return response["result"]

    async def recv_notification(self, *, timeout: float = 5.0) -> dict[str, object]:
        raw = await asyncio.wait_for(self._ws.recv(), timeout=timeout)
        message = json.loads(raw)
        assert message["jsonrpc"] == JSONRPC_VERSION
        assert "method" in message
        return message



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

        async with websockets.connect(uri) as websocket:
            client = _JsonRpcWsTestClient(websocket)

            # 1. Subscribe to events for the single configured agent.
            sub_result = await client.call_for_result(
                "events.subscribe",
                {"agent_ids": ["nav-1"], "include_runtime": True},
            )
            sub_id = sub_result["subscription_id"]
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
            notify = await client.recv_notification(timeout=5.0)

            assert notify["method"] == "events.notify"
            params = notify["params"]
            assert params["subscription_id"] == sub_id

            notified_event = params["event"]
            assert notified_event["agent_id"] == "nav-1"
            assert notified_event["type"] == "AgentFailed"
            # Basic sanity check on the event identifier format produced by
            # ``AgentSupervisor._append_runtime_event``.
            assert notified_event["event_id"].startswith("nav-1:")

            # 5. Fetch the agent detail snapshot and verify that the
            # in-memory ``AgentEventStream`` replays the same event.
            detail = await client.call_for_result(
                "agent.get_detail",
                {"agent_id": "nav-1", "max_events": 10},
            )

            events = detail["events"]
            assert isinstance(events, list)
            assert len(events) == 1
            detail_event = events[0]
            assert detail_event["event_id"] == notified_event["event_id"]
            assert detail_event["agent_id"] == notified_event["agent_id"]
            assert detail_event["type"] == notified_event["type"]

            # 6. Request a graceful shutdown so the serve loop can exit
            # cleanly before the test completes.
            shutdown_result = await client.call_for_result(
                "runtime.shutdown",
                {"timeout_seconds": 5},
            )

            assert shutdown_result["accepted"] is True
            assert shutdown_result["status"] == RuntimeStatus.SHUTTING_DOWN.value

        # Once shutdown has been requested, the serve loop should exit.
        await asyncio.wait_for(serve_task, timeout=5.0)

        # After the loop exits, the daemon should be marked as fully stopped.
        assert ctx.daemon.state.status is RuntimeStatus.STOPPED

    asyncio.run(main())
