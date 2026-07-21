from __future__ import annotations

"""Unit tests for :mod:`nate_ntm.runtime.swarm_acp_mux` (US1).

These tests exercise the connection-local mux behaviour described in
``specs/009-swarm-acp-mux/spec.md`` and the Epic 009 tasks document,
with a focus on User Story 1:

* attachment preparation and activation;
* forwarding of typed :class:`SessionUpdate` instances using
  :class:`AcpSessionUpdateStream` from Epic 008;
* prompt/interrupt delegation; and
* detach semantics relative to independent subscribers.

The tests use small fake implementations of:

* the durable swarm state / daemon surface required by the mux; and
* the :class:`SwarmAgentClient` and :class:`ExternalACPConnection`
  protocols.

This keeps them narrowly scoped to mux behaviour without depending on
concrete ACP transport or the NateOha adapter.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import AsyncIterator, Iterable
import asyncio

import pytest

from nate_ntm.runtime.acp_types import SessionUpdate
from nate_ntm.runtime.acp_update_stream import (
    AcpSessionUpdateStream,
    AgentSessionNotActive,
    ReceivedSessionUpdate,
)
from nate_ntm.runtime.swarm_acp_mux import (
    SwarmACPMux,
    UnknownAgentError,
    NoAttachedAgentError,
    StaleAttachmentError,
)


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

    The mux validates durable membership via ``daemon.swarm_state.agents``
    but does not otherwise depend on :class:`RuntimeDaemon` for US1
    behaviour.
    """

    def __init__(self, agent_ids: Iterable[str] = ()) -> None:
        self.swarm_state = _FakeSwarmState(agents={aid: object() for aid in agent_ids})


class _FakeAgentClient:
    """In-memory stand-in for :class:`SwarmAgentClient`.

    It owns per-agent :class:`AcpSessionUpdateStream` instances and
    exposes them via ``subscribe_acp_updates``. Prompt and interrupt
    calls are recorded for assertions.
    """

    def __init__(self) -> None:
        self.streams: dict[str, AcpSessionUpdateStream] = {}
        self.prompts: list[tuple[str, str]] = []
        self.interrupts: list[str] = []

    def add_agent(self, agent_id: str, *, stream: AcpSessionUpdateStream | None = None) -> None:
        self.streams[agent_id] = stream or AcpSessionUpdateStream()

    def subscribe_acp_updates(
        self, agent_id: str
    ) -> AsyncIterator[ReceivedSessionUpdate]:  # type: ignore[override]
        stream = self.streams.get(agent_id)
        if stream is None:
            # Mirror the behaviour of NateOhaAcpClient when no concrete
            # ACP session exists for a durable agent.
            raise AgentSessionNotActive(f"No active ACP session for {agent_id!r}")
        return stream.subscribe()

    async def prompt(self, agent_id: str, prompt: str) -> str | None:
        self.prompts.append((agent_id, prompt))
        return f"reply:{agent_id}:{prompt}"

    async def interrupt(self, agent_id: str) -> None:
        self.interrupts.append(agent_id)


class _FakeExternalConnection:
    """Fake :class:`ExternalACPConnection` that records forwarded updates."""

    def __init__(self) -> None:
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


def _make_mux(
    *,
    durable_agents: Iterable[str],
    agent_client: _FakeAgentClient,
    external: _FakeExternalConnection,
) -> SwarmACPMux:
    daemon = _FakeDaemon(agent_ids=durable_agents)
    return SwarmACPMux(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=agent_client,
        external_connection=external,
        external_session_id="session-1",
    )


def _publish(stream: AcpSessionUpdateStream, label: str) -> _DummyUpdate:
    update = _DummyUpdate(label=label)
    t0 = datetime(2024, 1, 1, 12, 0, 0)
    stream.publish(update, received_at=t0)
    return update


async def _anext_with_timeout(it: AsyncIterator[ReceivedSessionUpdate], timeout: float = 1.0) -> ReceivedSessionUpdate:
    return await asyncio.wait_for(it.__anext__(), timeout=timeout)


# ---------------------------------------------------------------------------
# T004 [US1] Attachment transaction tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prepare_attach_successful_first_attachment() -> None:
    agent_client = _FakeAgentClient()
    agent_client.add_agent("agent-1")
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    prepared = await mux.prepare_attach("agent-1")

    assert prepared.agent_id == "agent-1"
    assert prepared.newly_prepared is True
    assert prepared.token is not None
    assert mux.attached_agent_id == "agent-1"

    await mux.close()


@pytest.mark.asyncio
async def test_prepare_attach_propagates_agent_session_not_active_and_leaves_unattached() -> None:
    agent_client = _FakeAgentClient()
    # Durable membership knows about the agent, but there is no active
    # ACP session in the client.
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    with pytest.raises(AgentSessionNotActive):
        await mux.prepare_attach("agent-1")

    # The mux must remain unattached after a failed subscription.
    assert mux.attached_agent_id is None

    await mux.close()


@pytest.mark.asyncio
async def test_prepare_attach_unknown_agent_rejected_via_unknown_agent_error() -> None:
    agent_client = _FakeAgentClient()
    agent_client.add_agent("agent-1")
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    with pytest.raises(UnknownAgentError):
        await mux.prepare_attach("missing-agent")

    assert mux.attached_agent_id is None
    await mux.close()


@pytest.mark.asyncio
async def test_prepare_attach_failure_clears_previous_attachment() -> None:
    agent_client = _FakeAgentClient()
    stream1 = AcpSessionUpdateStream()
    agent_client.add_agent("agent-1", stream=stream1)

    # Durable swarm membership also includes ``agent-2`` but there is no
    # live session for it.
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1", "agent-2"], agent_client=agent_client, external=external)

    # First, attach successfully to agent-1.
    prepared1 = await mux.prepare_attach("agent-1")
    await mux.activate_attachment(prepared1)
    assert mux.attached_agent_id == "agent-1"

    # Now attempt to reattach to agent-2 which has no active session.
    with pytest.raises(AgentSessionNotActive):
        await mux.prepare_attach("agent-2")

    # The mux should no longer be attached to the previous agent.
    assert mux.attached_agent_id is None

    await mux.close()


@pytest.mark.asyncio
async def test_prepare_attach_same_agent_reuse_sets_newly_prepared_false() -> None:
    agent_client = _FakeAgentClient()
    agent_client.add_agent("agent-1", stream=AcpSessionUpdateStream())
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    prepared1 = await mux.prepare_attach("agent-1")
    await mux.activate_attachment(prepared1)

    prepared2 = await mux.prepare_attach("agent-1")

    assert prepared2.agent_id == "agent-1"
    assert prepared2.newly_prepared is False
    # Implementation uses the underlying _Attachment instance as the token.
    assert prepared2.token is prepared1.token
    assert mux.attached_agent_id == "agent-1"

    await mux.close()


@pytest.mark.asyncio
async def test_stale_prepared_handles_cannot_activate_or_abort_newer_attachment() -> None:
    agent_client = _FakeAgentClient()
    agent_client.add_agent("agent-1", stream=AcpSessionUpdateStream())
    agent_client.add_agent("agent-2", stream=AcpSessionUpdateStream())
    external = _FakeExternalConnection()
    mux = _make_mux(
        durable_agents=["agent-1", "agent-2"],
        agent_client=agent_client,
        external=external,
    )

    stale = await mux.prepare_attach("agent-1")

    # Switch to a new attachment for agent-2, making the previous handle stale.
    prepared2 = await mux.prepare_attach("agent-2")
    assert mux.attached_agent_id == "agent-2"

    # Activation with a stale handle must fail.
    with pytest.raises(StaleAttachmentError):
        await mux.activate_attachment(stale)

    # Aborting with a stale handle must be a no-op.
    await mux.abort_attachment(stale)

    # The current attachment must remain intact.
    assert mux.attached_agent_id == "agent-2"

    await mux.close()


# ---------------------------------------------------------------------------
# T005 [US1] Acknowledgment and rollback tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_no_forwarding_before_activation() -> None:
    agent_client = _FakeAgentClient()
    stream = AcpSessionUpdateStream()
    agent_client.add_agent("agent-1", stream=stream)
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    # Publish a retained update before attachment to ensure it is present
    # in the snapshot.
    pre_update = _publish(stream, "pre")

    prepared = await mux.prepare_attach("agent-1")

    # Additional live updates published during preparation.
    mid_update = _publish(stream, "mid")

    # No forwarded updates are allowed before activation.
    assert external.calls == []

    # Activation begins replay and live forwarding.
    await mux.activate_attachment(prepared)
    await external.wait_for_calls(2)

    forwarded_updates = [u for (_sid, u) in external.calls]
    assert forwarded_updates[0] is pre_update
    assert forwarded_updates[1] is mid_update

    await mux.close()


@pytest.mark.asyncio
async def test_activation_replays_retained_before_live_updates() -> None:
    agent_client = _FakeAgentClient()
    stream = AcpSessionUpdateStream()
    agent_client.add_agent("agent-1", stream=stream)
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    # Two retained updates prior to attachment.
    pre1 = _publish(stream, "pre1")
    pre2 = _publish(stream, "pre2")

    prepared = await mux.prepare_attach("agent-1")

    # One live update published after preparation but before activation.
    live = _publish(stream, "live1")

    await mux.activate_attachment(prepared)
    await external.wait_for_calls(3)

    forwarded = [u for (_sid, u) in external.calls]
    assert forwarded[0] is pre1
    assert forwarded[1] is pre2
    assert forwarded[2] is live

    await mux.close()


@pytest.mark.asyncio
async def test_attach_ack_failure_rolls_back_new_attachment() -> None:
    agent_client = _FakeAgentClient()
    stream = AcpSessionUpdateStream()
    agent_client.add_agent("agent-1", stream=stream)
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    async def failing_ack(agent_id: str) -> None:  # pragma: no cover - simple helper
        raise RuntimeError("synthetic ack failure")

    # Pre-publish an update; it must never be forwarded when ack fails.
    _publish(stream, "pre")

    with pytest.raises(RuntimeError):
        await mux.attach("agent-1", acknowledge=failing_ack)

    # No attachment remains and no updates were forwarded.
    assert mux.attached_agent_id is None
    assert external.calls == []

    # The per-agent stream must not have any active subscribers.
    assert len(stream._subscribers) == 0  # type: ignore[attr-defined]

    await mux.close()


@pytest.mark.asyncio
async def test_attach_ack_failure_preserves_reused_same_agent_attachment() -> None:
    agent_client = _FakeAgentClient()
    stream = AcpSessionUpdateStream()
    agent_client.add_agent("agent-1", stream=stream)
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    async def ok_ack(agent_id: str) -> None:
        return None

    # First attachment succeeds.
    await mux.attach("agent-1", acknowledge=ok_ack)
    first_attachment = mux._attachment  # type: ignore[attr-defined]
    assert mux.attached_agent_id == "agent-1"

    # Publish an update and ensure it is forwarded via the first attachment.
    u1 = _publish(stream, "u1")
    await external.wait_for_calls(1)
    assert external.calls[0][1] is u1

    async def failing_ack(agent_id: str) -> None:  # pragma: no cover - simple helper
        raise RuntimeError("synthetic ack failure")

    # Second attach to the same agent reuses the existing attachment. Ack
    # failure must *not* tear it down.
    with pytest.raises(RuntimeError):
        await mux.attach("agent-1", acknowledge=failing_ack)

    assert mux.attached_agent_id == "agent-1"
    assert mux._attachment is first_attachment  # type: ignore[attr-defined]

    # Further updates must still be forwarded through the intact attachment.
    u2 = _publish(stream, "u2")
    await external.wait_for_calls(2)
    assert external.calls[1][1] is u2

    await mux.close()


@pytest.mark.asyncio
async def test_abort_attachment_with_stale_handle_is_noop() -> None:
    agent_client = _FakeAgentClient()
    agent_client.add_agent("agent-1", stream=AcpSessionUpdateStream())
    agent_client.add_agent("agent-2", stream=AcpSessionUpdateStream())
    external = _FakeExternalConnection()
    mux = _make_mux(
        durable_agents=["agent-1", "agent-2"],
        agent_client=agent_client,
        external=external,
    )

    stale = await mux.prepare_attach("agent-1")

    prepared2 = await mux.prepare_attach("agent-2")
    assert mux.attached_agent_id == "agent-2"

    await mux.abort_attachment(stale)

    # Attachment to agent-2 must remain intact.
    assert mux.attached_agent_id == "agent-2"
    assert mux._attachment is prepared2.token  # type: ignore[attr-defined]

    await mux.close()


# ---------------------------------------------------------------------------
# T006 [US1] Forwarding and switching tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_forwarding_preserves_session_update_identity_and_order() -> None:
    agent_client = _FakeAgentClient()
    stream = AcpSessionUpdateStream()
    agent_client.add_agent("agent-1", stream=stream)
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    prepared = await mux.prepare_attach("agent-1")
    await mux.activate_attachment(prepared)

    u1 = _publish(stream, "u1")
    u2 = _publish(stream, "u2")
    u3 = _publish(stream, "u3")

    await external.wait_for_calls(3)

    forwarded = [u for (_sid, u) in external.calls]
    assert forwarded == [u1, u2, u3]

    await mux.close()


@pytest.mark.asyncio
async def test_update_published_during_preparation_delivered_exactly_once_after_activation() -> None:
    agent_client = _FakeAgentClient()
    stream = AcpSessionUpdateStream()
    agent_client.add_agent("agent-1", stream=stream)
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    pre = _publish(stream, "pre")

    prepared = await mux.prepare_attach("agent-1")

    mid = _publish(stream, "mid")

    await mux.activate_attachment(prepared)
    await external.wait_for_calls(2)

    forwarded = [u for (_sid, u) in external.calls]
    assert forwarded == [pre, mid]

    await mux.close()


@pytest.mark.asyncio
async def test_switching_stops_old_forwarding_before_new_attachment() -> None:
    agent_client = _FakeAgentClient()
    stream1 = AcpSessionUpdateStream()
    stream2 = AcpSessionUpdateStream()
    agent_client.add_agent("agent-1", stream=stream1)
    agent_client.add_agent("agent-2", stream=stream2)
    external = _FakeExternalConnection()
    mux = _make_mux(
        durable_agents=["agent-1", "agent-2"],
        agent_client=agent_client,
        external=external,
    )

    # First attachment to agent-1.
    prepared1 = await mux.prepare_attach("agent-1")
    await mux.activate_attachment(prepared1)

    # Ensure the forwarding task is running by publishing one update.
    _publish(stream1, "a1")
    await external.wait_for_calls(1)
    first_attachment = mux._attachment  # type: ignore[attr-defined]

    # Switch to agent-2. The old forwarding task must be cancelled and
    # awaited, and its subscription exited, before the new attachment is
    # established.
    prepared2 = await mux.prepare_attach("agent-2")

    assert mux.attached_agent_id == "agent-2"
    assert first_attachment is not mux._attachment  # type: ignore[attr-defined]

    # Old forwarding task must have completed and the old stream must have
    # no remaining subscribers.
    assert first_attachment.task is not None and first_attachment.task.done()  # type: ignore[attr-defined]
    assert len(stream1._subscribers) == 0  # type: ignore[attr-defined]

    # New attachment is not yet active; no forwarding occurs until
    # activation.
    _publish(stream2, "b1-pre-activate")
    await asyncio.sleep(0.01)
    assert len(external.calls) == 1

    await mux.activate_attachment(prepared2)

    # Updates published during preparation are retained and delivered
    # after activation, just like the single-agent case covered by
    # ``test_update_published_during_preparation_delivered_exactly_once_after_activation``.
    _publish(stream2, "b1")
    await external.wait_for_calls(3)

    labels = [update.label for (_sid, update) in external.calls]  # type: ignore[attr-defined]
    assert labels == ["a1", "b1-pre-activate", "b1"]

    await mux.close()


@pytest.mark.asyncio
async def test_obsolete_attachment_finished_does_not_clear_newer_attachment() -> None:
    agent_client = _FakeAgentClient()
    stream1 = AcpSessionUpdateStream()
    stream2 = AcpSessionUpdateStream()
    agent_client.add_agent("agent-1", stream=stream1)
    agent_client.add_agent("agent-2", stream=stream2)
    external = _FakeExternalConnection()
    mux = _make_mux(
        durable_agents=["agent-1", "agent-2"],
        agent_client=agent_client,
        external=external,
    )

    # Create an initial attachment that will become obsolete.
    prepared1 = await mux.prepare_attach("agent-1")
    obsolete_attachment = mux._attachment  # type: ignore[attr-defined]

    # Switch to agent-2, making the first attachment obsolete.
    await mux.prepare_attach("agent-2")
    assert mux.attached_agent_id == "agent-2"

    # Simulate completion of the obsolete forwarding task. The helper
    # must not clear the newer attachment.
    await mux._attachment_finished(obsolete_attachment)  # type: ignore[attr-defined]

    assert mux.attached_agent_id == "agent-2"

    await mux.close()


# ---------------------------------------------------------------------------
# T007 [US1] Agent operations and detach behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_prompt_and_interrupt_delegate_to_attached_agent() -> None:
    agent_client = _FakeAgentClient()
    agent_client.add_agent("agent-1", stream=AcpSessionUpdateStream())
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    prepared = await mux.prepare_attach("agent-1")
    await mux.activate_attachment(prepared)

    reply = await mux.prompt("hello")
    await mux.interrupt()

    assert reply == "reply:agent-1:hello"
    assert agent_client.prompts == [("agent-1", "hello")]
    assert agent_client.interrupts == ["agent-1"]

    await mux.close()


@pytest.mark.asyncio
async def test_prompt_and_interrupt_raise_when_unattached() -> None:
    agent_client = _FakeAgentClient()
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=[], agent_client=agent_client, external=external)

    with pytest.raises(NoAttachedAgentError):
        await mux.prompt("hello")

    with pytest.raises(NoAttachedAgentError):
        await mux.interrupt()

    await mux.close()


@pytest.mark.asyncio
async def test_detach_is_idempotent_and_clears_attachment() -> None:
    agent_client = _FakeAgentClient()
    agent_client.add_agent("agent-1", stream=AcpSessionUpdateStream())
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    prepared = await mux.prepare_attach("agent-1")
    await mux.activate_attachment(prepared)

    assert mux.attached_agent_id == "agent-1"

    await mux.detach()
    assert mux.attached_agent_id is None

    # Second detach is a no-op.
    await mux.detach()
    assert mux.attached_agent_id is None

    await mux.close()


@pytest.mark.asyncio
async def test_detach_exits_only_mux_subscription_and_preserves_independent_subscriber() -> None:
    agent_client = _FakeAgentClient()
    stream = AcpSessionUpdateStream()
    agent_client.add_agent("agent-1", stream=stream)
    external = _FakeExternalConnection()
    mux = _make_mux(durable_agents=["agent-1"], agent_client=agent_client, external=external)

    async with stream.subscribe() as independent_updates:
        # Attach the mux, creating a second subscriber.
        prepared = await mux.prepare_attach("agent-1")
        await mux.activate_attachment(prepared)

        # There should now be two live subscribers: the independent one
        # plus the mux.
        assert len(stream._subscribers) == 2  # type: ignore[attr-defined]

        # Publish an update and confirm the independent subscriber sees it.
        u1 = _publish(stream, "u1")
        received1 = await _anext_with_timeout(independent_updates)
        assert received1.update is u1

        # Detach the mux. The independent subscriber must remain
        # subscribed and continue receiving updates.
        await mux.detach()
        assert mux.attached_agent_id is None
        assert len(stream._subscribers) == 1  # type: ignore[attr-defined]

        u2 = _publish(stream, "u2")
        received2 = await _anext_with_timeout(independent_updates)
        assert received2.update is u2

    await mux.close()
