from __future__ import annotations

"""Integration test: SwarmACPMux real-path behaviour via production adapter (US1).

This test exercises the connection-scoped :class:`SwarmACPMux` through the
*production* Swarm ACP server adapter primitives in
:mod:`nate_ntm.runtime.swarm_acp_server` using the real Epic 008 typed
session streaming layer.

The goal is to validate the minimal real-path scenario described by
T015 in ``specs/009-swarm-acp-mux/tasks.md`` without introducing any
alternate telemetry paths or test-only adapters:

* A real :class:`AcpSessionUpdateStream` owned by
  :class:`AcpAgentSession` is used.
* The mux consumes updates exclusively via
  :meth:`NateOhaAcpClient.subscribe_acp_updates`.
* Typed :class:`SessionUpdate` values are forwarded to an
  :class:`ExternalACPConnection` implementation provided by the test.
* The per-session attachment transaction and detach semantics are
  routed through :class:`SwarmACPServerSession`.

No legacy :class:`AgentEvent` or :class:`AgentEventStream` surfaces are
used in this test; the only telemetry path is
``AcpSessionUpdateStream`` 
→ :meth:`NateOhaAcpClient.subscribe_acp_updates` 
→ :class:`SwarmACPMux` 
→ :class:`ExternalACPConnection`.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Iterable
import asyncio

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import AcpAgentSession, NateOhaAcpClient
from nate_ntm.runtime.acp_types import SessionUpdate
from nate_ntm.runtime.acp_update_stream import AcpSessionUpdateStream, ReceivedSessionUpdate
from nate_ntm.runtime.swarm_acp_mux import ExternalACPConnection
from nate_ntm.runtime.swarm_acp_server import SwarmACPServerSession


# ---------------------------------------------------------------------------
# Test scaffolding
# ---------------------------------------------------------------------------


class _DummyUpdate(SessionUpdate):  # type: ignore[misc]
    """Minimal concrete ``SessionUpdate`` for testing.

    The real ACP SDK exposes concrete subclasses of the
    :class:`SessionUpdate` protocol type. For tests we provide a
    lightweight stand-in so that :class:`AcpSessionUpdateStream` and the
    mux can operate on strongly-typed values without depending on
    specific ACP schemas.

    ``SessionUpdate`` is an alias of ``acp.schema.BaseModel``; declaring
    ``label`` as a field here lets us construct instances via
    ``_DummyUpdate(label="...")`` without relying on dynamic
    attributes, which may be forbidden by the SDK's Pydantic
    configuration.
    """

    label: str


@dataclass
class _FakeSwarmState:
    agents: dict[str, object]


class _FakeDaemon:
    """Minimal daemon stub exposing durable swarm membership.

    For this real-path test we need only the durable swarm-membership
    surface that :class:`SwarmACPMux` consults via
    ``daemon.swarm_state.agents``. Runtime views such as
    ``get_swarm_status`` and ``get_agent_detail`` are exercised by
    separate tests and are not required here.
    """

    def __init__(self, agent_ids: Iterable[str] = ()) -> None:
        self.swarm_state = _FakeSwarmState(agents={aid: object() for aid in agent_ids})


class _RecordingExternalConnection(ExternalACPConnection):  # type: ignore[misc]
    """Recording :class:`ExternalACPConnection` used by the test.

    The mux forwards each typed :class:`SessionUpdate` to
    :meth:`session_update`. This test asserts on the sequence and
    identity of these calls.
    """

    def __init__(self) -> None:  # pragma: no cover - trivial initialiser
        self.calls: list[tuple[str, SessionUpdate]] = []
        self._cond = asyncio.Condition()

    async def session_update(self, *, session_id: str, update: SessionUpdate) -> None:
        async with self._cond:
            self.calls.append((session_id, update))
            self._cond.notify_all()

    async def wait_for_calls(self, count: int, timeout: float = 1.0) -> None:
        """Wait until at least ``count`` calls have been recorded."""

        async with self._cond:
            if len(self.calls) >= count:
                return

            async def _wait() -> None:
                while len(self.calls) < count:
                    await self._cond.wait()

            await asyncio.wait_for(_wait(), timeout=timeout)




class _FailingRecordingExternal(ExternalACPConnection):  # type: ignore[misc]
    """Recording connection that can be instructed to fail forwarding.

    Until ``fail_now`` is set, this behaves like
    :class:`_RecordingExternalConnection`, recording all forwarded updates.
    Once ``fail_now`` is set, any subsequent :meth:`session_update` call
    raises a synthetic forwarding failure. This allows integration tests to
    trigger a real-path forwarding failure without introducing alternative
    telemetry paths.
    """

    def __init__(self) -> None:  # pragma: no cover - trivial initialiser
        self.calls: list[tuple[str, SessionUpdate]] = []
        self._cond = asyncio.Condition()
        self.fail_now: asyncio.Event = asyncio.Event()
        self.failures: list[BaseException] = []

    async def session_update(self, *, session_id: str, update: SessionUpdate) -> None:
        async with self._cond:
            if self.fail_now.is_set():
                exc = RuntimeError("synthetic real-path forwarding failure")
                self.failures.append(exc)
                raise exc

            self.calls.append((session_id, update))
            self._cond.notify_all()

    async def wait_for_calls(self, count: int, timeout: float = 1.0) -> None:
        """Wait until at least ``count`` calls have been recorded."""

        async with self._cond:
            if len(self.calls) >= count:
                return

            async def _wait() -> None:
                while len(self.calls) < count:
                    await self._cond.wait()

            await asyncio.wait_for(_wait(), timeout=timeout)

def _make_config(tmp_path: Path) -> RuntimeConfig:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    return load_runtime_config(project_path=project)


def _publish(stream: AcpSessionUpdateStream, label: str) -> _DummyUpdate:
    update = _DummyUpdate(label=label)
    t0 = datetime(2024, 1, 1, 12, 0, 0)
    stream.publish(update, received_at=t0)
    return update


async def _anext_with_timeout(
    it: AsyncIterator[ReceivedSessionUpdate], timeout: float = 1.0
) -> ReceivedSessionUpdate:
    return await asyncio.wait_for(it.__anext__(), timeout=timeout)


# ---------------------------------------------------------------------------
# T015 [US1] Minimal real-path behaviour through the production adapter
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swarm_acp_mux_real_path_minimal(tmp_path: Path) -> None:
    """Real-path SwarmACPMux behaviour via SwarmACPServerSession (US1).

    This scenario verifies that:

    * a real Epic 008 subscription is established **before** the
      `_attach` acknowledgment is written;
    * no retained or live update is forwarded before acknowledgment;
    * retained output precedes live output on the external connection;
    * prompt and interrupt reach the attached agent via the mux;
    * detach stops mux delivery without stopping the agent; and
    * an independent subscriber on the agent's
      :class:`AcpSessionUpdateStream` continues receiving updates after
      detach.
    """

    config = _make_config(tmp_path)
    acp_client = NateOhaAcpClient(config=config)

    agent_id = "agent-1"

    # Create a synthetic live AcpAgentSession with a real
    # AcpSessionUpdateStream. This mirrors the setup in the Epic 008
    # unit tests but uses the production
    # :class:`NateOhaAcpClient.subscribe_acp_updates` API to establish
    # subscriptions.
    session = AcpAgentSession(
        agent_id=agent_id,
        conversation_id="conv-1",
        process=object(),
        connection=object(),
        protocol_client=object(),
        status="running",
        stderr_task=None,
        exit_monitor_task=None,
    )

    acp_client._sessions[agent_id] = session  # type: ignore[attr-defined]
    stream = session.update_stream

    # Independent subscriber that must remain active across mux attach
    # and detach. This uses the real Epic 008 subscription API.
    async with stream.subscribe() as independent_updates:
        # Durable swarm membership is represented by a minimal daemon
        # stub. SwarmACPMux validates `agent_id` against
        # `daemon.swarm_state.agents`.
        daemon = _FakeDaemon(agent_ids=[agent_id])
        external = _RecordingExternalConnection()

        server_session = SwarmACPServerSession(
            daemon=daemon,  # type: ignore[arg-type]
            agent_client=acp_client,  # type: ignore[arg-type]
            external_connection=external,  # type: ignore[arg-type]
            external_session_id="external-1",
        )

        # Patch prompt/interrupt on the real NateOhaAcpClient instance so
        # we can assert that agent-directed operations are routed through
        # the mux without performing any external ACP I/O.
        prompt_calls: list[tuple[str, str | None]] = []
        interrupt_calls: list[str] = []

        async def fake_prompt(agent: str, prompt: str | None = None) -> str | None:
            prompt_calls.append((agent, prompt))
            return f"reply:{agent}:{prompt}"

        async def fake_interrupt(agent: str) -> None:
            interrupt_calls.append(agent)

        acp_client.prompt = fake_prompt  # type: ignore[assignment]
        acp_client.interrupt = fake_interrupt  # type: ignore[assignment]

        # ------------------------------------------------------------------
        # Attach: subscription before acknowledgment, no forwarding pre-ack
        # ------------------------------------------------------------------

        # Retained updates published before attachment.
        pre1 = _publish(stream, "pre1")
        pre2 = _publish(stream, "pre2")
        # Drain the retained updates from the independent subscriber so that
        # later assertions can focus on post-attach and post-detach behaviour.
        received_pre1 = await _anext_with_timeout(independent_updates)
        received_pre2 = await _anext_with_timeout(independent_updates)
        assert received_pre1.update is pre1
        assert received_pre2.update is pre2



        # Sanity: only the independent subscriber is present before the
        # mux prepares its attachment.
        assert len(stream._subscribers) == 1  # type: ignore[attr-defined]

        ack_called = asyncio.Event()

        async def acknowledge(agent: str) -> None:
            # At acknowledgment time the mux must already have established
            # its Epic 008 subscription via subscribe_acp_updates, adding
            # a second subscriber to the underlying stream.
            assert agent == agent_id
            assert len(stream._subscribers) == 2  # type: ignore[attr-defined]

            # No retained or live update may have been forwarded before
            # the acknowledgment is written.
            assert external.calls == []

            # Publish a live update while forwarding is still gated. This
            # must be delivered after the retained history.
            mid = _publish(stream, "mid")
            # Keep a reference to silence unused-variable warnings and
            # to mirror the retained/live distinction in assertions.
            assert mid is not None

            ack_called.set()


        await server_session.attach(agent_id, acknowledge=acknowledge)
        assert ack_called.is_set()

        # Forwarded updates must now include the two retained events
        # followed by the live event published during acknowledgment.
        await external.wait_for_calls(3)
        forwarded_updates = [u for (_sid, u) in external.calls]

        assert [u.label for u in forwarded_updates[:2]] == ["pre1", "pre2"]  # type: ignore[attr-defined]
        assert forwarded_updates[0] is pre1
        assert forwarded_updates[1] is pre2
        assert forwarded_updates[2].label == "mid"  # type: ignore[attr-defined]

        # The independent subscriber should also observe the live update
        # published during acknowledgment.
        received_mid = await _anext_with_timeout(independent_updates)
        assert getattr(received_mid.update, "label", None) == "mid"

        # ------------------------------------------------------------------
        # Agent-directed operations: prompt and interrupt via the mux
        # ------------------------------------------------------------------

        reply = await server_session.prompt("hello")
        await server_session.interrupt()

        assert reply == "reply:agent-1:hello"
        assert prompt_calls == [(agent_id, "hello")]
        assert interrupt_calls == [agent_id]

        # ------------------------------------------------------------------
        # Detach: stop mux delivery, preserve independent subscriber
        # ------------------------------------------------------------------

        await server_session.detach()

        # Detach is idempotent and removes only the mux's subscription.
        # The independent subscriber remains active.
        assert len(stream._subscribers) == 1  # type: ignore[attr-defined]

        after_detach = _publish(stream, "after-detach")

        # Independent subscriber should observe the post-detach update.
        received = await _anext_with_timeout(independent_updates)
        assert received.update is after_detach

        # Give the mux a brief window; it must not forward the
        # post-detach update to the external connection.
        await asyncio.sleep(0.05)
        assert all(u is not after_detach for (_sid, u) in external.calls)

        # The agent itself must remain runtime-managed: the underlying
        # AcpAgentSession stays present in the NateOhaAcpClient and
        # remains in a running-like state. Detach and adapter shutdown
        # do not stop the agent.
        persisted_session = acp_client._sessions.get(agent_id)  # type: ignore[attr-defined]
        assert persisted_session is session
        assert persisted_session.status in {"starting", "running"}

        # Session shutdown for this external ACP session must go through
        # the production adapter surface so that `_attach`, `_detach`, and
        # `close()` are serialised by `_control_lock`. Exercise the
        # adapter's `close()` method rather than closing the mux directly.
        await server_session.close()


# ---------------------------------------------------------------------------
# T024 [US3] Macro real-path scenario: attach, switch, failure, and cleanup
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_swarm_acp_mux_real_path_switch_and_forwarding_failure(tmp_path: Path) -> None:
    """Macro real-path scenario across two agents with forwarding failure.

    This scenario exercises the production Swarm ACP server adapter and mux
    using the real Epic 008 streaming path for two agents, A and B. It
    validates that:

    * the connection attaches to agent A and receives A's retained and live
      updates via the mux;
    * prompt and interrupt are routed to the currently attached agent;
    * switching to agent B stops forwarding A's subsequent output;
    * B's retained history is replayed before B's live output;
    * an injected forwarding failure on the external connection causes the
      mux to report a failure; and
    * the outer connection handler terminates and cleans up while both
      agents remain runtime-managed by :class:`NateOhaAcpClient`.
    """

    config = _make_config(tmp_path)
    acp_client = NateOhaAcpClient(config=config)

    agent_a = "agent-A"
    agent_b = "agent-B"

    # Create two synthetic live AcpAgentSession instances with real
    # AcpSessionUpdateStream objects, mirroring the Epic 008 setup.
    session_a = AcpAgentSession(
        agent_id=agent_a,
        conversation_id="conv-A",
        process=object(),
        connection=object(),
        protocol_client=object(),
        status="running",
        stderr_task=None,
        exit_monitor_task=None,
    )
    session_b = AcpAgentSession(
        agent_id=agent_b,
        conversation_id="conv-B",
        process=object(),
        connection=object(),
        protocol_client=object(),
        status="running",
        stderr_task=None,
        exit_monitor_task=None,
    )

    acp_client._sessions[agent_a] = session_a  # type: ignore[attr-defined]
    acp_client._sessions[agent_b] = session_b  # type: ignore[attr-defined]

    stream_a = session_a.update_stream
    stream_b = session_b.update_stream

    daemon = _FakeDaemon(agent_ids=[agent_a, agent_b])
    external = _FailingRecordingExternal()

    server_session = SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=acp_client,  # type: ignore[arg-type]
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="external-1",
    )

    # Patch prompt/interrupt to record per-agent calls without performing
    # any real ACP I/O.
    prompt_calls: list[tuple[str, str | None]] = []
    interrupt_calls: list[str] = []

    async def fake_prompt(agent: str, prompt: str | None = None) -> str | None:
        prompt_calls.append((agent, prompt))
        return f"reply:{agent}:{prompt}"

    async def fake_interrupt(agent: str) -> None:
        interrupt_calls.append(agent)

    acp_client.prompt = fake_prompt  # type: ignore[assignment]
    acp_client.interrupt = fake_interrupt  # type: ignore[assignment]

    replies: list[str | None] = []

    async def serve_inbound(sess: SwarmACPServerSession) -> None:
        assert sess is server_session

        # ---------------------------
        # Attach to agent A
        # ---------------------------

        pre_a1 = _publish(stream_a, "A_pre1")
        pre_a2 = _publish(stream_a, "A_pre2")

        ack_a_called = asyncio.Event()

        async def acknowledge_a(attached_id: str) -> None:
            assert attached_id == agent_a
            # Publish a live update while forwarding is still gated; this
            # must appear after the retained history for A.
            mid_a = _publish(stream_a, "A_mid")
            assert mid_a is not None
            ack_a_called.set()

        await sess.attach(agent_a, acknowledge=acknowledge_a)
        assert ack_a_called.is_set()

        # After attachment completes, the external connection should have
        # seen A's retained and mid-attachment updates.
        await external.wait_for_calls(3)
        forwarded = [u for (_sid, u) in external.calls]
        assert forwarded[0] is pre_a1
        assert forwarded[1] is pre_a2
        assert getattr(forwarded[2], "label", None) == "A_mid"

        # A live update after attachment flows through normally.
        live_a1 = _publish(stream_a, "A_live1")
        await external.wait_for_calls(4)
        assert external.calls[3][1] is live_a1

        # Prompt and interrupt should target agent A.
        reply_a = await sess.prompt("hello-A")
        replies.append(reply_a)
        await sess.interrupt()

        # ---------------------------
        # Switch to agent B
        # ---------------------------

        pre_b1 = _publish(stream_b, "B_pre1")
        pre_b2 = _publish(stream_b, "B_pre2")
        assert pre_b1 is not None and pre_b2 is not None

        ack_b_called = asyncio.Event()

        async def acknowledge_b(attached_id: str) -> None:
            assert attached_id == agent_b
            # As with A, publish a mid-ack update that must be replayed
            # before B's post-ack live output.
            mid_b = _publish(stream_b, "B_mid")
            assert mid_b is not None
            ack_b_called.set()

        await sess.attach(agent_b, acknowledge=acknowledge_b)
        assert ack_b_called.is_set()

        # Immediately after switching to B, A's subsequent output must no
        # longer be forwarded.
        _publish(stream_a, "A_post_switch")

        # B live output after attachment should appear after B's retained
        # history and mid-ack update.
        live_b1 = _publish(stream_b, "B_live1")

        # Give the mux a moment to deliver the new B output.
        await external.wait_for_calls(7)
        labels = [getattr(u, "label", None) for (_sid, u) in external.calls]

        # Expected A sequence.
        assert labels[0:4] == ["A_pre1", "A_pre2", "A_mid", "A_live1"]

        # B's retained+mid sequence must precede B_live1.
        assert labels[4:7] == ["B_pre1", "B_pre2", "B_mid"]

        # No A output published after the switch may be forwarded.
        assert "A_post_switch" not in labels

        # Prompt and interrupt should now target agent B.
        reply_b = await sess.prompt("hello-B")
        replies.append(reply_b)
        await sess.interrupt()

        # ---------------------------
        # Inject a forwarding failure while attached to B
        # ---------------------------

        external.fail_now.set()
        _publish(stream_b, "B_fail")

        # Block until cancelled by the connection-lifetime race.
        try:
            await asyncio.Future()
        except asyncio.CancelledError:
            # The adapter's run_connection helper is expected to cancel
            # inbound processing when forwarding fails.
            raise

    close_called = asyncio.Event()

    async def close_transport() -> None:
        close_called.set()

    # Run the connection to first completion: the forwarding failure should
    # win the race and terminate the handler.
    with pytest.raises(RuntimeError) as exc_info:
        await server_session.run_connection(serve_inbound, close_transport=close_transport)

    assert "synthetic real-path forwarding failure" in str(exc_info.value)
    assert external.failures, "Expected at least one recorded forwarding failure"

    # Prompt/interrupt routing must have targeted the correct agents.
    assert prompt_calls == [(agent_a, "hello-A"), (agent_b, "hello-B")]
    assert interrupt_calls == [agent_a, agent_b]
    assert replies == [f"reply:{agent_a}:hello-A", f"reply:{agent_b}:hello-B"]

    # The adapter must have closed the per-session mux and invoked the
    # transport-close callback exactly once.
    assert close_called.is_set()
    assert server_session.mux._closed is True  # type: ignore[attr-defined]

    # Both agents remain runtime-managed by the ACP client.
    persisted_a = acp_client._sessions.get(agent_a)  # type: ignore[attr-defined]
    persisted_b = acp_client._sessions.get(agent_b)  # type: ignore[attr-defined]
    assert persisted_a is session_a
    assert persisted_b is session_b
    assert persisted_a.status in {"starting", "running"}
    assert persisted_b.status in {"starting", "running"}

