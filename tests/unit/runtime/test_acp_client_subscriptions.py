from __future__ import annotations

from datetime import datetime
from pathlib import Path
import asyncio

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import AcpAgentSession, AcpClientError, NateOhaAcpClient
from nate_ntm.runtime.acp_types import SessionUpdate
from nate_ntm.runtime.acp_update_stream import AgentSessionNotActive, StreamClosedError


def _make_config(tmp_path: Path) -> RuntimeConfig:
    project_root = tmp_path / "project"
    project_root.mkdir(parents=True, exist_ok=True)
    return load_runtime_config(project_path=project_root)



class _DummyUpdate(SessionUpdate):  # type: ignore[misc]
    """Minimal concrete SessionUpdate type for testing.

    The real ACP SDK exposes concrete subclasses of the ``SessionUpdate``
    protocol type; for tests we provide a lightweight stand-in so that
    ``AcpSessionUpdateStream`` can assign sequence numbers and retain
    history without depending on specific ACP schemas.
    """

    pass


@pytest.mark.asyncio
async def test_stop_agent_async_closes_typed_update_stream(tmp_path: Path) -> None:
    """Stopping an async ACP session closes its typed update stream.

    This ensures that callers publishing into the per-session
    :class:`AcpSessionUpdateStream` after shutdown observe
    :class:`StreamClosedError` and can treat the stream as terminal.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-async-stop-1"

    class DummyContext:
        def __init__(self) -> None:
            self.exited = False

        async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
            self.exited = True

    ctx = DummyContext()

    # Create a synthetic AcpAgentSession with a live typed update stream.
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

    # Publish a single update to confirm the stream accepts events prior to
    # shutdown.
    t0 = datetime(2024, 1, 1, 12, 0, 0)
    session.update_stream.publish(_DummyUpdate(), received_at=t0)

    client._session_contexts[agent_id] = ctx  # type: ignore[assignment]
    client._sessions[agent_id] = session

    await client.stop_agent_async(agent_id, timeout=5.0)

    # The underlying context manager should have been exited and the
    # in-memory session marked as terminated.
    assert ctx.exited is True
    assert client._sessions[agent_id].status == "terminated"

    # Further publishes into the per-session stream must fail with
    # StreamClosedError.
    with pytest.raises(StreamClosedError):
        session.update_stream.publish(_DummyUpdate(), received_at=t0)


@pytest.mark.asyncio
async def test_stop_agent_async_closes_typed_update_stream_on_failure(tmp_path: Path) -> None:
    """Even when shutdown fails, the typed stream is closed.

    When the underlying async context manager raises during exit,
    :meth:`stop_agent_async` re-raises an :class:`AcpClientError` but still
    closes the per-session typed update stream so callers observe a
    consistent terminal state.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-async-stop-fail-1"

    class FailingContext:
        async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
            raise RuntimeError("synthetic shutdown failure")

    ctx = FailingContext()

    session = AcpAgentSession(
        agent_id=agent_id,
        conversation_id="conv-2",
        process=object(),
        connection=object(),
        protocol_client=object(),
        status="running",
        stderr_task=None,
        exit_monitor_task=None,
    )

    t0 = datetime(2024, 1, 1, 12, 0, 0)
    session.update_stream.publish(_DummyUpdate(), received_at=t0)

    client._session_contexts[agent_id] = ctx  # type: ignore[assignment]
    client._sessions[agent_id] = session

    # Shutdown failures propagate as AcpClientError.
    with pytest.raises(AcpClientError):
        await client.stop_agent_async(agent_id, timeout=5.0)

    # The synthetic session object remains available in this test and its
    # typed update stream must still be closed.
    with pytest.raises(StreamClosedError):
        session.update_stream.publish(_DummyUpdate(), received_at=t0)



@pytest.mark.asyncio
async def test_on_session_update_preserves_typed_update_and_timestamp(tmp_path: Path) -> None:
    """_on_session_update forwards typed updates intact into the stream.

    This verifies that a concrete ``SessionUpdate`` instance and its
    associated ``received_at`` timestamp are preserved end-to-end from the
    ACP callback into :class:`AcpSessionUpdateStream` and
    :meth:`subscribe_acp_updates`.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-update-pass-1"

    # Create a synthetic live session with an attached typed update stream.
    session = AcpAgentSession(
        agent_id=agent_id,
        conversation_id="conv-typed-1",
        process=object(),
        connection=object(),
        protocol_client=object(),
        status="running",
        stderr_task=None,
        exit_monitor_task=None,
    )

    client._sessions[agent_id] = session

    update = _DummyUpdate()
    received_at = datetime(2024, 1, 1, 12, 0, 0)

    # Simulate the ACP SDK invoking the protocol client's callback, which in
    # turn calls ``NateOhaAcpClient._on_session_update``.
    client._on_session_update(
        agent_id=agent_id,
        session_id="conv-typed-1",
        update=update,
        received_at=received_at,
    )

    # The first item observed via ``subscribe_acp_updates`` should reflect
    # the exact update object and timestamp that were passed into
    # ``_on_session_update``.
    async with client.subscribe_acp_updates(agent_id) as updates:
        received = await asyncio.wait_for(updates.__anext__(), timeout=1.0)

    assert received.sequence == 1
    assert received.update is update
    assert received.received_at == received_at



@pytest.mark.asyncio
async def test_subscribe_acp_updates_requires_active_session(tmp_path: Path) -> None:
    """subscribe_acp_updates rejects missing or inactive sessions.

    When no live AcpAgentSession exists for an agent, or when the
    recorded session is not in a "starting" / "running" state, the
    helper must raise AgentSessionNotActive instead of exposing a
    dangling subscription.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-acp-no-session-1"

    # No session has been recorded for this agent.
    with pytest.raises(AgentSessionNotActive):
        async with client.subscribe_acp_updates(agent_id):
            assert False, "unreachable"

    # A recorded but inactive session (for example, terminated) must
    # also be rejected.
    session = AcpAgentSession(
        agent_id=agent_id,
        conversation_id="conv-inactive-1",
        process=object(),
        connection=object(),
        protocol_client=object(),
        status="terminated",
        stderr_task=None,
        exit_monitor_task=None,
    )
    client._sessions[agent_id] = session

    with pytest.raises(AgentSessionNotActive):
        async with client.subscribe_acp_updates(agent_id):
            assert False, "unreachable"


@pytest.mark.asyncio
async def test_subscribe_acp_updates_binds_to_single_session_stream(tmp_path: Path) -> None:
    """Subscriptions attach to one concrete session-owned update stream.

    Once established, a subscription created via subscribe_acp_updates
    observes updates from the session's AcpSessionUpdateStream that was
    active at attachment time.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-acp-attach-1"

    session = AcpAgentSession(
        agent_id=agent_id,
        conversation_id="conv-attach-1",
        process=object(),
        connection=object(),
        protocol_client=object(),
        status="running",
        stderr_task=None,
        exit_monitor_task=None,
    )
    client._sessions[agent_id] = session

    t0 = datetime(2024, 1, 1, 12, 0, 0)

    async with client.subscribe_acp_updates(agent_id) as updates:
        ev = session.update_stream.publish(_DummyUpdate(), received_at=t0)
        received = await asyncio.wait_for(updates.__anext__(), timeout=1.0)

    # The subscription must observe exactly the event published into the
    # owning session's typed stream.
    assert received is ev



@pytest.mark.asyncio
async def test_subscribe_acp_updates_iterator_terminates_when_stream_closed(tmp_path: Path) -> None:
    """Iterators from subscribe_acp_updates terminate promptly after stream close.

    When the underlying :class:`AcpSessionUpdateStream` is closed while a
    subscriber is waiting for new items, the iterator should produce all
    already-published events and then terminate with ``StopAsyncIteration``
    instead of blocking indefinitely.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-acp-close-iter-1"

    session = AcpAgentSession(
        agent_id=agent_id,
        conversation_id="conv-close-iter-1",
        process=object(),
        connection=object(),
        protocol_client=object(),
        status="running",
        stderr_task=None,
        exit_monitor_task=None,
    )
    client._sessions[agent_id] = session

    t0 = datetime(2024, 1, 1, 12, 0, 0)

    async with client.subscribe_acp_updates(agent_id) as updates:
        # Publish a single update and observe it via the subscription.
        ev = session.update_stream.publish(_DummyUpdate(), received_at=t0)
        first = await asyncio.wait_for(updates.__anext__(), timeout=1.0)
        assert first is ev

        # Closing the stream should cause subsequent ``__anext__`` calls to
        # terminate quickly with StopAsyncIteration rather than hanging.
        session.update_stream.close()

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(updates.__anext__(), timeout=1.0)


@pytest.mark.asyncio
async def test_subscribe_acp_updates_on_closed_stream_replays_history(tmp_path: Path) -> None:
    """Subscribing after a hard stream error replays retained history.

    When a per-session update stream has been closed due to an internal
    error, :meth:`subscribe_acp_updates` still exposes a finite iterator
    that yields the retained history and then terminates without hanging.
    """

    config = _make_config(tmp_path)
    client = NateOhaAcpClient(config=config)

    agent_id = "agent-acp-closed-history-1"

    session = AcpAgentSession(
        agent_id=agent_id,
        conversation_id="conv-closed-history-1",
        process=object(),
        connection=object(),
        protocol_client=object(),
        status="running",
        stderr_task=None,
        exit_monitor_task=None,
    )
    client._sessions[agent_id] = session

    t0 = datetime(2024, 1, 1, 12, 0, 0)

    # Publish a couple of updates to build up retained history.
    ev1 = session.update_stream.publish(_DummyUpdate(), received_at=t0)
    ev2 = session.update_stream.publish(_DummyUpdate(), received_at=t0)

    # Simulate a terminal error in the stream.
    error = RuntimeError("synthetic stream failure")
    session.update_stream.close(error)

    # Sanity-check internal flags for diagnostics.
    assert session.update_stream._closed is True
    assert isinstance(session.update_stream._close_error, RuntimeError)

    # Subscribing after closure should yield the retained history and then
    # terminate without exposing a live tail.
    async with client.subscribe_acp_updates(agent_id) as updates:
        items = [item async for item in updates]

    assert items == [ev1, ev2]

