"""Unit tests for :mod:`nate_ntm.runtime.runner` (US3 event streaming).

These tests focus on the thin wiring that connects the runtime's
:class:`~nate_ntm.runtime.events.AgentEvent` pipeline to the WebSocket
JSON-RPC control API via :func:`create_runtime_control_context`.

They complement the lower-level tests for :mod:`nate_ntm.api.jsonrpc`
(``build_events_notify_messages``) and :mod:`nate_ntm.api.server`
(``RuntimeApiServer.build_agent_event_notifications``) by ensuring that
runtime-originated agent events are actually forwarded to the
``JsonRpcWebSocketServer`` when a control API context is constructed.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from nate_ntm.api.jsonrpc_ws import JsonRpcWebSocketServer
from nate_ntm.config.runtime_config import load_runtime_config
from nate_ntm.runtime.daemon import StartupMode
from nate_ntm.runtime.events import AgentEvent
from nate_ntm.runtime.runner import create_runtime_control_context
from nate_ntm.runtime.state import AgentRuntimeState, AgentStatus


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    return project


def test_create_runtime_control_context_wires_agent_events_to_ws_server(tmp_path: Path) -> None:
    """AgentSupervisor events are bridged to JsonRpcWebSocketServer.publish_event.

    The ``create_runtime_control_context`` helper is responsible for wiring
    the runtime daemon, in-process API server, and WebSocket JSON-RPC server
    together. As part of US3, it also installs a small bridge from the
    :class:`~nate_ntm.runtime.agents.AgentSupervisor`'s
    ``on_agent_event`` callback to
    :meth:`~nate_ntm.api.jsonrpc_ws.JsonRpcWebSocketServer.publish_event`.

    This test verifies that when a runtime-originated agent event is
    appended via :meth:`AgentSupervisor.mark_agent_failed`, the bridged
    ``publish_event`` coroutine is scheduled on the active event loop and
    receives the corresponding :class:`AgentEvent` instance.
    """

    async def main() -> None:
        project = _make_project(tmp_path)
        config = load_runtime_config(project_path=project)

        # Monkeypatch ``publish_event`` on the WebSocket server *class* to
        # capture events instead of attempting real network IO. We patch at
        # the class level because :class:`JsonRpcWebSocketServer` uses
        # ``slots=True`` and does not allow rebinding instance attributes.
        seen: list[AgentEvent] = []

        original_publish = JsonRpcWebSocketServer.publish_event

        async def fake_publish(self: JsonRpcWebSocketServer, event: AgentEvent) -> None:  # type: ignore[override]
            seen.append(event)

        JsonRpcWebSocketServer.publish_event = fake_publish  # type: ignore[assignment]

        try:
            # Construct a runtime control context in ``create`` mode. This builds
            # a fresh RuntimeDaemon, RuntimeApiServer, and JsonRpcWebSocketServer,
            # and installs the AgentEvent bridge.
            ctx = create_runtime_control_context(
                config,
                StartupMode.CREATE,
                host="127.0.0.1",
                port=0,
            )

            scheduler = ctx.daemon.scheduler
            assert scheduler is not None
            supervisor = scheduler.agent_supervisor

            # The bridge should have installed an ``on_agent_event`` callback on
            # the supervisor.
            assert supervisor.on_agent_event is not None

            # Attach minimal runtime state for a single agent. We bypass
            # metadata-backed registration here because the goal is to exercise
            # the event pipeline from AgentSupervisor -> bridge -> ws_server.
            runtime_state = AgentRuntimeState(
                agent_id="agent-1",
                status=AgentStatus.RUNNING,
                last_error=None,
                event_stream=None,
            )
            supervisor.state.agents["agent-1"] = runtime_state

            # Trigger a runtime-originated failure event, which should cause
            # ``_append_runtime_event`` to append to the in-memory stream and
            # invoke the ``on_agent_event`` callback.
            supervisor.mark_agent_failed("agent-1", error="boom")

            # Allow the ``loop.create_task`` used by the bridge to schedule and
            # run the patched ``publish_event`` coroutine.
            await asyncio.sleep(0)

            assert len(seen) == 1
            event = seen[0]
            assert isinstance(event, AgentEvent)
            assert event.agent_id == "agent-1"
            assert event.type == "AgentFailed"
        finally:
            # Restore the original ``publish_event`` implementation so this
            # test does not affect other tests.
            JsonRpcWebSocketServer.publish_event = original_publish  # type: ignore[assignment]

    asyncio.run(main())
