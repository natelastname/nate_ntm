from __future__ import annotations

"""Connection-scoped typed ACP session multiplexer (SwarmACPMux).

This module implements the connection-local mux described in
``specs/009-swarm-acp-mux/spec.md``. It routes one external swarm ACP
session to at most one concrete agent ACP session at a time, consuming
typed ``ReceivedSessionUpdate`` values from the Epic 008
``AcpSessionUpdateStream`` and forwarding the underlying
``SessionUpdate`` objects to an :class:`ExternalACPConnection`.

The implementation deliberately *does not* own ACP transport, replay
buffers, overflow policy, or generic :class:`AgentEvent` telemetry.
Those responsibilities belong to other runtime components (see specs
001, 002, and 008). SwarmACPMux is intentionally small and
connection-scoped.
"""

import asyncio
import logging
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import AsyncIterator, Protocol, Callable, Awaitable, TYPE_CHECKING

from .acp_types import SessionUpdate
from .acp_update_stream import ReceivedSessionUpdate, AgentSessionNotActive

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checking
    from .daemon import RuntimeDaemon


logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Narrow interfaces used by the mux
# ---------------------------------------------------------------------------


class SwarmAgentClient(Protocol):
    """Narrow protocol for agent operations consumed by the mux.

    This is implemented by :class:`NateOhaAcpClient` and intentionally
    exposes only the operations required by SwarmACPMux so that the mux
    can be tested against fakes.
    """

    def subscribe_acp_updates(
        self,
        agent_id: str,
    ) -> AbstractAsyncContextManager[AsyncIterator[ReceivedSessionUpdate]]:
        """Return a subscription to the agent's typed ACP update stream.

        The async context manager yields an :class:`AsyncIterator` that
        first replays retained history and then yields live
        :class:`ReceivedSessionUpdate` values as they arrive.
        """

    async def prompt(self, agent_id: str, prompt: str) -> str | None:  # pragma: no cover - protocol
        """Send a prompt to the given agent and return optional text output."""

    async def interrupt(self, agent_id: str) -> None:  # pragma: no cover - protocol
        """Request that the given agent interrupt its current work, if any."""


class ExternalACPConnection(Protocol):
    """Abstraction for writing typed updates to an external ACP session.

    The concrete Swarm ACP server adapter implements this interface.
    """

    async def session_update(
        self,
        *,
        session_id: str,
        update: SessionUpdate,
    ) -> None:  # pragma: no cover - protocol
        """Forward a typed :class:`SessionUpdate` to the external session."""


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PreparedAttachment:
    """Handle returned by :meth:`SwarmACPMux.prepare_attach`.

    ``token`` is an opaque identity marker for the underlying
    :class:`_Attachment`. Callers MUST treat it as an opaque value and
    use it only for subsequent calls to :meth:`activate_attachment` and
    :meth:`abort_attachment`.
    """

    agent_id: str
    token: object
    newly_prepared: bool


@dataclass(slots=True)
class _Attachment:
    """Concrete subscription and forwarding state for one attached agent."""

    agent_id: str
    subscription: AbstractAsyncContextManager[AsyncIterator[ReceivedSessionUpdate]]
    updates: AsyncIterator[ReceivedSessionUpdate]
    forwarding_enabled: asyncio.Event
    task: asyncio.Task[None] | None = None


# ---------------------------------------------------------------------------
# Error model
# ---------------------------------------------------------------------------


class SwarmACPMuxError(RuntimeError):
    """Base error type for mux-related failures."""


class SwarmACPMuxClosedError(SwarmACPMuxError):
    """Raised when a public operation is attempted on a closed mux."""


class UnknownAgentError(SwarmACPMuxError):
    """Raised when a requested agent is not in durable swarm membership."""


class NoAttachedAgentError(SwarmACPMuxError):
    """Raised when an agent-directed operation is attempted while unattached."""


class StaleAttachmentError(SwarmACPMuxError):
    """Raised when a :class:`PreparedAttachment` no longer matches the current attachment."""


class UnsupportedReservedUpdateError(SwarmACPMuxError):
    """Raised by the Swarm ACP server adapter for unknown reserved controls."""


# ---------------------------------------------------------------------------
# SwarmACPMux implementation
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class SwarmACPMux:
    """Connection-scoped typed ACP session multiplexer.

    One :class:`SwarmACPMux` instance exists per external ACP session
    and MUST NOT be shared between external sessions.
    """

    daemon: "RuntimeDaemon"
    agent_client: SwarmAgentClient
    external_connection: ExternalACPConnection
    external_session_id: str

    attached_agent_id: str | None = None

    _attachment: _Attachment | None = None
    _lifecycle_lock: asyncio.Lock = field(
        default_factory=asyncio.Lock,
        init=False,
        repr=False,
    )
    _failure: asyncio.Future[None] = field(
        init=False,
        repr=False,
    )
    _closed: bool = field(
        default=False,
        init=False,
        repr=False,
    )

    # ------------------------------------------------------------------
    # Initialization and basic helpers
    # ------------------------------------------------------------------

    def __post_init__(self) -> None:
        """Initialize the internal failure future.

        ``_failure`` represents the first *fatal* forwarding failure.
        It is completed exactly once by :meth:`_report_failure` or
        cancelled by :meth:`close`. Normal agent-stream exhaustion and
        lifecycle-driven cancellation (``detach`` / ``close``) MUST NOT
        complete this future.
        """

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - construction outside a running loop
            loop = asyncio.get_event_loop()
        self._failure = loop.create_future()

    # Small internal helpers -------------------------------------------------

    def _ensure_open(self) -> None:
        """Raise :class:`SwarmACPMuxClosedError` if the mux is closed."""

        if self._closed:
            raise SwarmACPMuxClosedError("SwarmACPMux is closed")

    def _current_attachment(self) -> _Attachment | None:
        """Return the current attachment (if any) without synchronization."""

        return self._attachment

    def _attachment_matches_token(self, token: object) -> bool:
        """Return ``True`` if ``token`` identifies the current attachment.

        ``PreparedAttachment.token`` is the identity marker; in this
        implementation it is the :class:`_Attachment` instance itself.
        """

        attachment = self._attachment
        return attachment is not None and attachment is token

    def _pop_attachment_for_detach(self) -> _Attachment | None:
        """Clear and return the current attachment under ``_lifecycle_lock``.

        This helper performs only synchronous state changes and MUST be
        called with ``_lifecycle_lock`` held. The caller is responsible
        for cancelling and awaiting the forwarding task and exiting the
        subscription context outside the lock to avoid deadlocks with
        :meth:`_attachment_finished`.
        """

        attachment = self._attachment
        if attachment is None:
            self.attached_agent_id = None
            return None

        self._attachment = None
        self.attached_agent_id = None
        return attachment

    # ------------------------------------------------------------------
    # Public API: attachment lifecycle
    # ------------------------------------------------------------------

    async def prepare_attach(self, agent_id: str) -> PreparedAttachment:
        """Prepare an attachment to ``agent_id`` without starting forwarding.

        This establishes the internal ACP subscription but does *not*
        begin forwarding. It implements the first stage of the
        three-stage transaction described in spec §8.1:

        1. establish the internal ACP subscription;
        2. send the external attachment acknowledgment;
        3. activate forwarding.
        """

        self._ensure_open()

        # Step 2 from spec §8.1 – validate against durable swarm membership.
        if agent_id not in self.daemon.swarm_state.agents:
            raise UnknownAgentError(f"Unknown agent_id: {agent_id!r}")

        async with self._lifecycle_lock:
            self._ensure_open()

            current = self._attachment
            if (
                current is not None
                and self.attached_agent_id == agent_id
                and current.task is not None
                and not current.task.done()
            ):
                # Idempotent same-agent attach for a healthy attachment.
                logger.info(
                    "mux_prepare",
                    extra={
                        "external_session_id": self.external_session_id,
                        "agent_id": agent_id,
                        "idempotent": True,
                        "previous_agent_id": agent_id,
                    },
                )
                return PreparedAttachment(
                    agent_id=agent_id,
                    token=current,
                    newly_prepared=False,
                )

            # New attachment (either first attach or switching agents).
            # Completely detach any previous attachment first to ensure that
            # old forwarding is stopped before the new attachment is
            # acknowledged (spec §§7, 8.1, 16.3).
            previous = self._pop_attachment_for_detach()
            previous_agent_id = previous.agent_id if previous is not None else None

        # Perform any required cleanup for the previous attachment outside
        # the lock to avoid deadlocks with the forwarding task.
        if previous is not None:
            logger.info(
                "mux_prepare",
                extra={
                    "external_session_id": self.external_session_id,
                    "agent_id": agent_id,
                    "idempotent": False,
                    "previous_agent_id": previous_agent_id,
                },
            )
            await self._cleanup_attachment(previous)
        else:
            logger.info(
                "mux_prepare",
                extra={
                    "external_session_id": self.external_session_id,
                    "agent_id": agent_id,
                    "idempotent": False,
                    "previous_agent_id": None,
                },
            )

        # Establish a new subscription for ``agent_id``.
        cm = self.agent_client.subscribe_acp_updates(agent_id)
        try:
            updates = await cm.__aenter__()
        except Exception:
            # Errors from ``subscribe_acp_updates`` (including
            # ``AgentSessionNotActive``) propagate and MUST NOT leave the
            # mux attached.
            #
            # ``cm.__aexit__`` is not invoked here because the context
            # manager failed to enter successfully; any required cleanup is
            # the responsibility of the provider.
            raise

        attachment = _Attachment(
            agent_id=agent_id,
            subscription=cm,
            updates=updates,
            forwarding_enabled=asyncio.Event(),
            task=None,
        )

        async with self._lifecycle_lock:
            # The mux might have been closed while we were establishing
            # the subscription; if so, immediately detach/cleanup.
            if self._closed:
                # Drop the new attachment without exposing it.
                # We must exit the subscription context we just entered.
                await self._cleanup_attachment(attachment)
                raise SwarmACPMuxClosedError("SwarmACPMux closed during prepare_attach")

            self._attachment = attachment
            self.attached_agent_id = agent_id

        return PreparedAttachment(agent_id=agent_id, token=attachment, newly_prepared=True)

    async def activate_attachment(self, prepared: PreparedAttachment) -> None:
        """Activate a previously prepared attachment.

        This is called *after* the external `_attach` acknowledgment has
        been successfully written. It starts (or idempotently confirms)
        the forwarding task.
        """

        self._ensure_open()

        async with self._lifecycle_lock:
            self._ensure_open()

            attachment = self._attachment
            if attachment is None or not self._attachment_matches_token(prepared.token):
                raise StaleAttachmentError("PreparedAttachment is no longer current")

            started_new_task = False

            # If a forwarding task already exists and is healthy, treat this
            # as an idempotent no-op.
            if attachment.task is not None and not attachment.task.done():
                # Ensure the forwarding gate is open in case it was not yet
                # released. ``Event.set()`` is idempotent.
                attachment.forwarding_enabled.set()
            else:
                # Start a new forwarding task for this attachment.
                loop = asyncio.get_running_loop()
                task = loop.create_task(self._run_forwarding(attachment))
                attachment.task = task
                started_new_task = True

            # Release the forwarding gate so that retained+live updates can
            # begin flowing only after acknowledgment (which has already
            # happened by the time this method is called).
            attachment.forwarding_enabled.set()
            agent_id = attachment.agent_id

        logger.info(
            "mux_activate",
            extra={
                "external_session_id": self.external_session_id,
                "agent_id": agent_id,
                "started_new_task": started_new_task,
            },
        )

    async def abort_attachment(self, prepared: PreparedAttachment) -> None:
        """Abort a prepared attachment after acknowledgment failure.

        The semantics follow spec §8.2–§8.3:

        * If ``prepared.newly_prepared`` is ``True`` *and* the token still
          refers to the current attachment, roll back that candidate
          attachment and leave the mux unattached.
        * If ``prepared.newly_prepared`` is ``False`` and the token still
          refers to the current healthy attachment, leave it intact (the
          `_attach` call was merely re-acknowledging an existing
          attachment).
        * If the token is stale (no longer refers to the current
          attachment), this is a no-op.
        """

        async with self._lifecycle_lock:
            attachment = self._attachment

            if attachment is None or not self._attachment_matches_token(prepared.token):
                # Stale handle; do not modify current attachment.
                return

            if not prepared.newly_prepared:
                # Idempotent same-agent case; keep the existing healthy
                # attachment intact.
                return

            # Roll back the freshly prepared attachment.
            candidate = self._pop_attachment_for_detach()

        if candidate is not None:
            await self._cleanup_attachment(candidate)

    async def attach(
        self,
        agent_id: str,
        *,
        acknowledge: Callable[[str], Awaitable[None]],
    ) -> None:
        """Convenience helper that performs prepare → ack → activate.

        This mirrors the pattern in spec §8.3 and MUST provide the same
        acknowledgment-failure semantics as the explicit three-step
        sequence.
        """

        prepared = await self.prepare_attach(agent_id)

        try:
            await acknowledge(agent_id)
        except BaseException:
            await self.abort_attachment(prepared)
            raise

        await self.activate_attachment(prepared)

    async def detach(self) -> None:
        """Detach the mux from its current agent.

        Detach is idempotent and removes only this mux's subscription.
        Other subscribers to the same :class:`AcpSessionUpdateStream`
        remain active (spec §§8.4, 16.5).
        """

        self._ensure_open()

        async with self._lifecycle_lock:
            attachment = self._pop_attachment_for_detach()

        if attachment is not None:
            logger.info(
                "mux_detach",
                extra={
                    "external_session_id": self.external_session_id,
                    "agent_id": attachment.agent_id,
                    "no_op": False,
                },
            )
            await self._cleanup_attachment(attachment)
        else:
            logger.info(
                "mux_detach",
                extra={
                    "external_session_id": self.external_session_id,
                    "agent_id": None,
                    "no_op": True,
                },
            )

    # ------------------------------------------------------------------
    # Public API: agent-directed operations and views
    # ------------------------------------------------------------------

    async def prompt(self, text: str) -> str | None:
        """Send a prompt to the currently attached agent.

        Raises :class:`NoAttachedAgentError` if there is no attachment.
        """

        self._ensure_open()

        async with self._lifecycle_lock:
            self._ensure_open()
            attachment = self._attachment
            if attachment is None:
                raise NoAttachedAgentError("No agent attached to SwarmACPMux")
            agent_id = attachment.agent_id

        return await self.agent_client.prompt(agent_id, text)

    async def interrupt(self) -> None:
        """Request an interrupt for the currently attached agent.

        Raises :class:`NoAttachedAgentError` if there is no attachment.
        """

        self._ensure_open()

        async with self._lifecycle_lock:
            self._ensure_open()
            attachment = self._attachment
            if attachment is None:
                raise NoAttachedAgentError("No agent attached to SwarmACPMux")
            agent_id = attachment.agent_id

        await self.agent_client.interrupt(agent_id)

    def get_swarm_status(self) -> dict[str, object]:
        """Return swarm overview plus connection-local attachment state."""

        self._ensure_open()

        return {
            "attached_agent_id": self.attached_agent_id,
            "swarm": self.daemon.get_swarm_status(),
        }

    def get_agent_detail(
        self,
        agent_id: str,
        *,
        max_events: int = 100,
    ) -> dict[str, object]:
        """Return agent detail plus whether this mux is attached to it.

        Unknown agents are reported as :class:`UnknownAgentError`.
        """

        self._ensure_open()

        try:
            detail = self.daemon.get_agent_detail(agent_id=agent_id, max_events=max_events)
        except KeyError as exc:  # mirrors RuntimeDaemon contract
            raise UnknownAgentError(f"Unknown agent_id: {agent_id!r}") from exc

        return {
            "attached": agent_id == self.attached_agent_id,
            "agent": detail["agent"],
            "events": detail["events"],
        }

    # ------------------------------------------------------------------
    # Public API: failure observation and closure
    # ------------------------------------------------------------------

    async def wait_failed(self) -> None:
        """Wait for the first fatal forwarding failure and re-raise it.

        Clean mux closure cancels the pending waiter; normal agent-stream
        exhaustion leaves the mux open and unattached and does not
        complete the failure future.
        """

        await self._failure

    async def close(self) -> None:
        """Close the mux and detach from any attached agent.

        This method is idempotent. After :meth:`close` has been called,
        subsequent public operations raise
        :class:`SwarmACPMuxClosedError`.
        """

        async with self._lifecycle_lock:
            if self._closed:
                logger.info(
                    "mux_close",
                    extra={
                        "external_session_id": self.external_session_id,
                        "already_closed": True,
                        "last_agent_id": self.attached_agent_id,
                    },
                )
                return

            self._closed = True
            attachment = self._pop_attachment_for_detach()

            # Cancel any pending failure waiter without treating closure
            # itself as a failure.
            if not self._failure.done():
                self._failure.cancel()

        logger.info(
            "mux_close",
            extra={
                "external_session_id": self.external_session_id,
                "already_closed": False,
                "last_agent_id": getattr(attachment, "agent_id", None) if attachment is not None else None,
            },
        )

        if attachment is not None:
            await self._cleanup_attachment(attachment)

    async def __aenter__(self) -> "SwarmACPMux":
        self._ensure_open()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:  # type: ignore[override]
        await self.close()

    # ------------------------------------------------------------------
    # Internal lifecycle methods
    # ------------------------------------------------------------------

    async def _run_forwarding(self, attachment: _Attachment) -> None:
        """Forward retained and live updates for ``attachment``.

        This method:

        * waits for activation via ``attachment.forwarding_enabled``;
        * consumes the already-established ``attachment.updates``
          iterator (it MUST NOT call ``subscribe_acp_updates`` itself);
        * forwards each ``SessionUpdate`` to the external ACP
          connection; and
        * records fatal failures via :meth:`_report_failure`.
        """

        try:
            # Wait until activation is requested. The event is set by
            # :meth:`activate_attachment` after the external `_attach`
            # acknowledgment has been written.
            await attachment.forwarding_enabled.wait()

            async for received in attachment.updates:
                try:
                    await self.external_connection.session_update(
                        session_id=self.external_session_id,
                        update=received.update,
                    )
                except Exception as exc:  # pragma: no cover - exercised via integration tests
                    self._report_failure(exc, attachment)
                    raise
        except asyncio.CancelledError:
            # Cancellation from ``detach`` / ``close`` is treated as
            # normal lifecycle behavior and MUST NOT be reported as a
            # failure.
            raise
        except Exception as exc:  # pragma: no cover - exercised via integration tests
            self._report_failure(exc, attachment)
            raise
        finally:
            # Natural exhaustion or fatal failure: perform identity-safe
            # cleanup. When an obsolete attachment completes after a
            # newer one has been installed, ``_attachment_finished`` is a
            # no-op.
            #
            # The cleanup runs in a separate task so that we never try to
            # ``await`` the forwarding task from within itself, which would
            # prevent its exception from being retrieved cleanly. The
            # ``_cleanup_attachment`` helper swallows any fatal forwarding
            # errors after they have been recorded via ``_report_failure``.
            loop = asyncio.get_running_loop()
            loop.create_task(self._attachment_finished(attachment))

    async def _attachment_finished(self, attachment: _Attachment) -> None:
        """Clear mux state if ``attachment`` is still current and exit its subscription.

        This helper is called when a forwarding task completes
        (successfully or with a failure). It ensures that completion of
        an obsolete attachment does not modify a newer attachment (spec
        §14 `_attachment_finished`).
        """

        async with self._lifecycle_lock:
            if self._attachment is not attachment:
                # Either we have already detached or switched to a new
                # attachment. In either case, detachment/cleanup is
                # handled elsewhere.
                return

            # Clear attachment state; the mux becomes unattached but
            # remains open.
            self._attachment = None
            self.attached_agent_id = None

        # Exit the retained subscription context outside the lock to
        # avoid deadlocks with other lifecycle operations.
        await self._cleanup_attachment(attachment, cancel_task=False)

    async def _cleanup_attachment(
        self,
        attachment: _Attachment,
        *,
        cancel_task: bool = True,
    ) -> None:
        """Cancel and await the forwarding task and exit the subscription.

        ``cancel_task`` is ``False`` when the caller knows the task has
        already completed (for example, from :meth:`_attachment_finished`).
        """

        task = attachment.task
        if cancel_task and task is not None and not task.done():
            task.cancel()

        if task is not None:
            try:
                await task
            except asyncio.CancelledError:
                # Expected when we cancelled the task as part of detach/
                # close; do not treat as a failure.
                pass
            except Exception:
                # Fatal failures should already have been recorded via
                # ``_report_failure``. We intentionally swallow them here
                # to avoid double-reporting.
                pass

        # Exit the subscription context manager exactly once.
        try:
            await attachment.subscription.__aexit__(None, None, None)
        except Exception:
            # Subscription-exit failures are unexpected but local to this
            # mux. They should not bring down the process; callers
            # observe them via logs rather than surfaced exceptions.
            pass

    def _report_failure(self, exc: BaseException, attachment: _Attachment | None = None) -> None:
        """Record the first fatal forwarding failure, if any.

        A fatal failure includes:

        * exceptions raised while consuming the ACP subscription; and
        * exceptions writing an update to the external ACP connection.

        Normal subscription exhaustion and lifecycle-driven cancellation
        (``detach`` / ``close``) MUST NOT call this method.
        """

        if self._failure.done():
            return

        self._failure.set_exception(exc)

        logger.error(
            "mux_forwarding_failed",
            extra={
                "external_session_id": self.external_session_id,
                "agent_id": attachment.agent_id if attachment is not None else self.attached_agent_id,
                "exception_type": type(exc).__name__,
            },
            exc_info=exc,
        )


__all__ = [
    "SwarmACPMux",
    "SwarmACPMuxError",
    "SwarmACPMuxClosedError",
    "UnknownAgentError",
    "NoAttachedAgentError",
    "StaleAttachmentError",
    "UnsupportedReservedUpdateError",
    "PreparedAttachment",
    "SwarmAgentClient",
    "ExternalACPConnection",
]
