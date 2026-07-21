from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest
from acp.schema import BaseModel

from nate_ntm.runtime.acp_update_stream import AcpSessionUpdateStream


class _Update(BaseModel):
    text: str


def _publish(stream: AcpSessionUpdateStream, text: str) -> None:
    stream.publish(_Update(text=text), received_at=datetime.now(timezone.utc))


@pytest.mark.asyncio
async def test_subscription_replays_history_then_live_updates() -> None:
    stream = AcpSessionUpdateStream(max_events=2)
    _publish(stream, "discarded")
    _publish(stream, "history-1")
    _publish(stream, "history-2")

    async with stream.subscribe() as updates:
        assert (await anext(updates)).update.text == "history-1"
        assert (await anext(updates)).update.text == "history-2"
        _publish(stream, "live")
        assert (await anext(updates)).update.text == "live"


@pytest.mark.asyncio
async def test_close_wakes_waiter_and_removes_subscription() -> None:
    stream = AcpSessionUpdateStream()

    async with stream.subscribe() as updates:
        waiter = asyncio.create_task(anext(updates))
        await asyncio.sleep(0)
        stream.close()
        with pytest.raises(StopAsyncIteration):
            await waiter

    assert not stream._subscribers


@pytest.mark.asyncio
async def test_subscribers_receive_the_same_ordered_updates() -> None:
    stream = AcpSessionUpdateStream()

    async with stream.subscribe() as first, stream.subscribe() as second:
        for text in ("one", "two", "three"):
            _publish(stream, text)

        assert [(await anext(first)).update.text for _ in range(3)] == [
            "one",
            "two",
            "three",
        ]
        assert [(await anext(second)).update.text for _ in range(3)] == [
            "one",
            "two",
            "three",
        ]
