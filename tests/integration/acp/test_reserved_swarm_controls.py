from __future__ import annotations

"""Integration tests: reserved swarm controls via the production adapter (US2).

These tests exercise the reserved-control operations exposed by
:class:`SwarmACPServerSession` using the real Epic 008 typed session
streaming layer and the production :class:`NateOhaAcpClient` adapter.

The goal, per T021 in ``specs/009-swarm-acp-mux/tasks.md``, is to verify
through the *production* dispatch path that:

* `_swarm_status` and `_agent_detail` return the contract-defined
  payloads using the mux-level views;
* `_detach` is idempotent from the external caller's perspective;
* repeated `_attach` to the same agent preserves a healthy attachment; and
* mux/domain failures reached via reserved controls map to the logical
  ``MUX_*`` error codes defined in the session contract.

Wire-level ACP encoding is deliberately out of scope here; these tests
operate at the logical level using :class:`SwarmACPServerSession` as the
outer adapter boundary.
"""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import AsyncIterator, Iterable, Mapping
import asyncio

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import AcpAgentSession, NateOhaAcpClient
from nate_ntm.runtime.acp_types import SessionUpdate
from nate_ntm.runtime.acp_update_stream import AcpSessionUpdateStream, ReceivedSessionUpdate
from nate_ntm.runtime.swarm_acp_mux import ExternalACPConnection, UnknownAgentError
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
    """Minimal daemon stub exposing durable swarm membership and views.

    SwarmACPMux validates durable membership via ``daemon.swarm_state``
    and, for US2, reuses ``get_swarm_status`` and ``get_agent_detail`` to
    implement mux-level views without depending on the real
    :class:`RuntimeDaemon` implementation.
    """

    def __init__(
        self,
        agent_ids: Iterable[str] = (),
        *,
        swarm_status: Mapping[str, object] | None = None,
        agent_details: Mapping[str, Mapping[str, object]] | None = None,
    ) -> None:
        self.swarm_state = _FakeSwarmState(agents={aid: object() for aid in agent_ids})
        self._swarm_status = dict(swarm_status or {})
        self._agent_details = {k: dict(v) for k, v in (agent_details or {}).items()}
        self.max_events_calls: list[tuple[str, int]] = []

    def get_swarm_status(self) -> dict[str, object]:
        return dict(self._swarm_status)

    def get_agent_detail(self, *, agent_id: str, max_events: int) -> dict[str, object]:
        self.max_events_calls.append((agent_id, max_events))
        detail = self._agent_details[agent_id]
        events = list(detail.get("events", []))
        return {
            "agent": detail["agent"],
            "events": events[:max_events],
        }


class _RecordingExternalConnection(ExternalACPConnection):  # type: ignore[misc]
    """Recording :class:`ExternalACPConnection` used by the tests.

    The mux forwards each typed :class:`SessionUpdate` to
    :meth:`session_update`. The integration tests assert on the sequence
    and identity of these calls.
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


def _make_server_session(
    *,
    config: RuntimeConfig,
    agent_id: str,
    swarm_status: Mapping[str, object] | None = None,
    agent_detail: Mapping[str, object] | None = None,
) -> tuple[SwarmACPServerSession, NateOhaAcpClient, AcpSessionUpdateStream, _FakeDaemon, _RecordingExternalConnection]:
    """Construct a SwarmACPServerSession wired to real Epic 008 plumbing.

    This helper mirrors the setup in ``test_swarm_acp_mux_real_path.py``
    but adds daemon-owned views for US2 reserved-control operations.
    """

    acp_client = NateOhaAcpClient(config=config)

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

    daemon = _FakeDaemon(
        agent_ids=[agent_id],
        swarm_status=swarm_status or {},
        agent_details={agent_id: (agent_detail or {"agent": {"agent_id": agent_id}, "events": []})},
    )
    external = _RecordingExternalConnection()

    server_session = SwarmACPServerSession(
        daemon=daemon,  # type: ignore[arg-type]
        agent_client=acp_client,  # type: ignore[arg-type]
        external_connection=external,  # type: ignore[arg-type]
        external_session_id="external-1",
    )

    return server_session, acp_client, stream, daemon, external


# ---------------------------------------------------------------------------
# T021 [US2] Reserved-control integration behaviour
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_reserved_swarm_status_and_agent_detail_payloads_real_path(tmp_path: Path) -> None:
    """Reserved `_swarm_status` and `_agent_detail` use mux views (US2).

    Through the production adapter surface, these operations must return
    the mux-level views defined in the session contract, combining
    daemon-owned data with connection-local attachment state.
    """

    agent_id = "agent-1"
    swarm_status = {"status": "ok"}
    agent_detail = {
        "agent": {"agent_id": agent_id},
        "events": ["e1", "e2", "e3"],
    }

    config = _make_config(tmp_path)
    session, acp_client, stream, daemon, _external = _make_server_session(
        config=config,
        agent_id=agent_id,
        swarm_status=swarm_status,
        agent_detail=agent_detail,
    )

    # Patch prompt/interrupt on the real NateOhaAcpClient instance so we
    # can assert that reserved controls never reach the agent.
    prompt_calls: list[tuple[str, str | None]] = []
    interrupt_calls: list[str] = []

    async def fake_prompt(agent: str, prompt: str | None = None) -> str | None:
        prompt_calls.append((agent, prompt))
        return f"reply:{agent}:{prompt}"

    async def fake_interrupt(agent: str) -> None:
        interrupt_calls.append(agent)

    acp_client.prompt = fake_prompt  # type: ignore[assignment]
    acp_client.interrupt = fake_interrupt  # type: ignore[assignment]

    # Before any attachment, `_swarm_status` must report no attached
    # agent and reuse the daemon-owned swarm view.
    status = await session.handle_reserved_control({"op": "_swarm_status", "payload": {}})
    assert status == {"attached_agent_id": None, "swarm": swarm_status}

    # `_agent_detail` must reuse the daemon view and add `attached`.
    detail = await session.handle_reserved_control(
        {"op": "_agent_detail", "payload": {"agent_id": agent_id, "max_events": 2}}
    )
    assert detail["attached"] is False
    assert detail["agent"] == agent_detail["agent"]
    assert detail["events"] == agent_detail["events"][:2]
    assert daemon.max_events_calls == [(agent_id, 2)]

    # Reserved controls must not be forwarded to the agent.
    assert prompt_calls == []
    assert interrupt_calls == []

    await session.close()


@pytest.mark.asyncio
async def test_reserved_detach_idempotent_and_same_agent_attach_preserves_attachment(tmp_path: Path) -> None:
    """`_detach` is idempotent and same-agent `_attach` preserves attachment.

    This test mirrors the US1 real-path scenario but drives detach
    through the reserved `_detach` control and attachment via the
    production :class:`SwarmACPServerSession.attach` helper, modelling
    how a real adapter would implement the `_attach` reserved control.
    """

    agent_id = "agent-1"
    config = _make_config(tmp_path)
    session, acp_client, stream, _daemon, external = _make_server_session(
        config=config,
        agent_id=agent_id,
    )

    # Independent subscriber that must remain across attach/detach.
    async with stream.subscribe() as independent_updates:
        # Retained update before any attachment.
        pre = _publish(stream, "pre")
        received_pre = await _anext_with_timeout(independent_updates)
        assert received_pre.update is pre

        # First `_detach` should be a no-op and return `{"detached": True}`.
        result1 = await session.handle_reserved_control({"op": "_detach", "payload": {}})
        assert result1 == {"detached": True}
        assert session.mux.attached_agent_id is None

        # Attach via the production `attach` helper, modelling an `_attach`
        # reserved control handled by a real adapter.
        ack_payloads: list[dict[str, str]] = []

        async def acknowledge(attached_id: str) -> None:
            ack_payloads.append({"attached_agent_id": attached_id})

        await session.attach(agent_id, acknowledge=acknowledge)
        assert ack_payloads == [{"attached_agent_id": agent_id}]
        assert session.mux.attached_agent_id == agent_id

        # Publish an update and ensure the retained history and the new
        # update are forwarded via the mux, with retained output first.
        u1 = _publish(stream, "u1")
        await external.wait_for_calls(2)
        forwarded = [u for (_sid, u) in external.calls]
        assert forwarded[0] is pre
        assert forwarded[1] is u1

        # Re-attach to the same agent via the helper. This must preserve
        # the existing healthy attachment and emit a second acknowledgment
        # payload.
        await session.attach(agent_id, acknowledge=acknowledge)
        assert ack_payloads == [
            {"attached_agent_id": agent_id},
            {"attached_agent_id": agent_id},
        ]
        assert session.mux.attached_agent_id == agent_id

        # A second update must still be forwarded; the independent
        # subscriber remains active throughout.
        u2 = _publish(stream, "u2")
        await external.wait_for_calls(3)
        forwarded = [u for (_sid, u) in external.calls]
        assert forwarded == [pre, u1, u2]

        received_u1 = await _anext_with_timeout(independent_updates)
        received_u2 = await _anext_with_timeout(independent_updates)
        assert received_u1.update is u1
        assert received_u2.update is u2

        # Detach twice; both calls must succeed and leave the mux
        # unattached while the independent subscriber continues to
        # receive updates.
        result2 = await session.handle_reserved_control({"op": "_detach", "payload": {}})
        assert result2 == {"detached": True}
        assert session.mux.attached_agent_id is None

        result3 = await session.handle_reserved_control({"op": "_detach", "payload": {}})
        assert result3 == {"detached": True}
        assert session.mux.attached_agent_id is None

        # Further updates are visible to the independent subscriber but
        # not forwarded via the mux.
        u3 = _publish(stream, "u3")
        await asyncio.sleep(0.05)
        assert len(external.calls) == 3

        received_u3 = await _anext_with_timeout(independent_updates)
        assert received_u3.update is u3

    await session.close()


@pytest.mark.asyncio
async def test_reserved_controls_logical_error_codes_real_path(tmp_path: Path) -> None:
    """Reserved controls map failures to logical `MUX_*` codes (US2).

    This test drives failures through :meth:`handle_reserved_control`
    and verifies that :meth:`SwarmACPServerSession.map_mux_error`
    produces the contract-defined logical error codes.
    """

    agent_id = "agent-1"
    config = _make_config(tmp_path)

    # Base setup with a single durable agent and real stream.
    session, acp_client, stream, daemon, _external = _make_server_session(
        config=config,
        agent_id=agent_id,
    )

    # Unknown agent for `_agent_detail` -> UnknownAgentError -> MUX_UNKNOWN_AGENT.
    with pytest.raises(Exception) as exc_info_unknown:
        await session.handle_reserved_control(
            {"op": "_agent_detail", "payload": {"agent_id": "missing-agent"}}
        )

    code_unknown = SwarmACPServerSession.map_mux_error(exc_info_unknown.value)
    assert code_unknown == "MUX_UNKNOWN_AGENT"

    # Malformed reserved request (missing `op`) -> ValueError -> MUX_INVALID_REQUEST.
    with pytest.raises(Exception) as exc_info_bad:
        await session.handle_reserved_control({"payload": {}})

    code_bad = SwarmACPServerSession.map_mux_error(exc_info_bad.value)
    assert code_bad == "MUX_INVALID_REQUEST"

    # Close the session, then attempt `_swarm_status` -> SwarmACPMuxClosedError -> MUX_CLOSED.
    await session.close()

    with pytest.raises(Exception) as exc_info_closed:
        await session.handle_reserved_control({"op": "_swarm_status", "payload": {}})

    code_closed = SwarmACPServerSession.map_mux_error(exc_info_closed.value)
    assert code_closed == "MUX_CLOSED"

    # Ensure that closing the session did not disturb the underlying
    # Epic 008 stream itself; publishing still succeeds.
    _publish(stream, "after-close")
    # No explicit assertion on forwarding here; the focus is on logical
    # error codes. The real-path forwarding contract is covered by
    # ``test_swarm_acp_mux_real_path.py``.

    # Silence unused-variable warnings.
    assert acp_client is not None
    assert daemon is not None
