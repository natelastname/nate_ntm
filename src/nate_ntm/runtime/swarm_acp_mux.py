"""Connection-scoped multiplexer for typed ACP session updates."""

from __future__ import annotations

import asyncio
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable, Protocol, TYPE_CHECKING

from .acp_types import SessionUpdate
from .acp_update_stream import ReceivedSessionUpdate

if TYPE_CHECKING:
    from .daemon import RuntimeDaemon


class SwarmAgentClient(Protocol):
    def subscribe_acp_updates(
        self, agent_id: str
    ) -> AbstractAsyncContextManager[AsyncIterator[ReceivedSessionUpdate]]: ...

    async def prompt(self, agent_id: str, prompt: str) -> str | None: ...

    async def interrupt(self, agent_id: str) -> None: ...


class ExternalACPConnection(Protocol):
    async def session_update(
        self, *, session_id: str, update: SessionUpdate
    ) -> None: ...


class SwarmACPMuxError(RuntimeError):
    pass


class SwarmACPMuxClosedError(SwarmACPMuxError):
    pass


class UnknownAgentError(SwarmACPMuxError):
    pass


class NoAttachedAgentError(SwarmACPMuxError):
    pass


class StaleAttachmentError(SwarmACPMuxError):
    pass


class UnsupportedReservedUpdateError(SwarmACPMuxError):
    pass


@dataclass(frozen=True, slots=True)
class PreparedAttachment:
    agent_id: str
    token: object
    newly_prepared: bool


@dataclass(slots=True)
class _Attachment:
    agent_id: str
    subscription: AbstractAsyncContextManager[AsyncIterator[ReceivedSessionUpdate]]
    updates: AsyncIterator[ReceivedSessionUpdate]
    enabled: asyncio.Event = field(default_factory=asyncio.Event)
    task: asyncio.Task[None] | None = None


@dataclass(slots=True)
class SwarmACPMux:
    """Expose at most one internal agent session to one external ACP client."""

    daemon: RuntimeDaemon
    agent_client: SwarmAgentClient
    external_connection: ExternalACPConnection
    external_session_id: str
    attached_agent_id: str | None = None

    _attachment: _Attachment | None = field(default=None, init=False, repr=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)
    _failure: asyncio.Future[None] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        self._failure = asyncio.get_running_loop().create_future()

    def _ensure_open(self) -> None:
        if self._closed:
            raise SwarmACPMuxClosedError("SwarmACPMux is closed")

    async def prepare_attach(self, agent_id: str) -> PreparedAttachment:
        self._ensure_open()
        if agent_id not in self.daemon.swarm_state.agents:
            raise UnknownAgentError(f"Unknown agent_id: {agent_id!r}")

        async with self._lock:
            self._ensure_open()
            current = self._attachment
            if (
                current is not None
                and current.agent_id == agent_id
                and current.task is not None
                and not current.task.done()
            ):
                return PreparedAttachment(agent_id, current, False)
            self._attachment = None
            self.attached_agent_id = None

        if current is not None:
            await self._cleanup(current)

        subscription = self.agent_client.subscribe_acp_updates(agent_id)
        updates = await subscription.__aenter__()
        attachment = _Attachment(agent_id, subscription, updates)

        async with self._lock:
            if self._closed:
                await subscription.__aexit__(None, None, None)
                raise SwarmACPMuxClosedError("SwarmACPMux closed during attach")
            self._attachment = attachment
            self.attached_agent_id = agent_id

        return PreparedAttachment(agent_id, attachment, True)

    async def activate_attachment(self, prepared: PreparedAttachment) -> None:
        self._ensure_open()
        async with self._lock:
            attachment = self._attachment
            if attachment is not prepared.token:
                raise StaleAttachmentError("Prepared attachment is no longer current")
            if attachment.task is None or attachment.task.done():
                attachment.task = asyncio.create_task(self._forward(attachment))
            attachment.enabled.set()

    async def abort_attachment(self, prepared: PreparedAttachment) -> None:
        if not prepared.newly_prepared:
            return
        async with self._lock:
            if self._attachment is not prepared.token:
                return
            attachment = self._attachment
            self._attachment = None
            self.attached_agent_id = None
        if attachment is not None:
            await self._cleanup(attachment)

    async def attach(
        self,
        agent_id: str,
        *,
        acknowledge: Callable[[str], Awaitable[None]],
    ) -> None:
        prepared = await self.prepare_attach(agent_id)
        try:
            await acknowledge(agent_id)
        except BaseException:
            await self.abort_attachment(prepared)
            raise
        await self.activate_attachment(prepared)

    async def detach(self) -> None:
        self._ensure_open()
        async with self._lock:
            attachment = self._attachment
            self._attachment = None
            self.attached_agent_id = None
        if attachment is not None:
            await self._cleanup(attachment)

    async def prompt(self, text: str) -> str | None:
        attachment = self._require_attachment()
        return await self.agent_client.prompt(attachment.agent_id, text)

    async def interrupt(self) -> None:
        attachment = self._require_attachment()
        await self.agent_client.interrupt(attachment.agent_id)

    def get_swarm_status(self) -> dict[str, object]:
        self._ensure_open()
        return {
            "attached_agent_id": self.attached_agent_id,
            "swarm": self.daemon.get_swarm_status(),
        }

    def get_agent_detail(
        self,
        agent_id: str,
        *,
        max_events: int | None = None,
    ) -> dict[str, object]:
        self._ensure_open()
        try:
            agent = self.daemon.get_agent_detail(agent_id)
        except KeyError as exc:
            raise UnknownAgentError(f"Unknown agent_id: {agent_id!r}") from exc
        return {
            "attached": agent_id == self.attached_agent_id,
            "agent": agent,
        }

    async def wait_failed(self) -> None:
        await self._failure

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            attachment = self._attachment
            self._attachment = None
            self.attached_agent_id = None
            if not self._failure.done():
                self._failure.cancel()
        if attachment is not None:
            await self._cleanup(attachment)

    async def __aenter__(self) -> SwarmACPMux:
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    def _require_attachment(self) -> _Attachment:
        self._ensure_open()
        attachment = self._attachment
        if attachment is None:
            raise NoAttachedAgentError("No agent attached to SwarmACPMux")
        return attachment

    async def _forward(self, attachment: _Attachment) -> None:
        try:
            await attachment.enabled.wait()
            async for received in attachment.updates:
                await self.external_connection.session_update(
                    session_id=self.external_session_id,
                    update=received.update,
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not self._failure.done():
                self._failure.set_exception(exc)
            raise
        finally:
            asyncio.create_task(self._finished(attachment))

    async def _finished(self, attachment: _Attachment) -> None:
        async with self._lock:
            if self._attachment is not attachment:
                return
            self._attachment = None
            self.attached_agent_id = None
        await self._cleanup(attachment, cancel=False)

    async def _cleanup(
        self,
        attachment: _Attachment,
        *,
        cancel: bool = True,
    ) -> None:
        task = attachment.task
        if cancel and task is not None and not task.done():
            task.cancel()
        if task is not None and task is not asyncio.current_task():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await attachment.subscription.__aexit__(None, None, None)
        except Exception:
            pass
