from __future__ import annotations

"""Shared runtime session abstraction for the Textual console.

This module defines :class:`RuntimeSession`, a client-side session object
that owns the cached view of a single running nate_ntm runtime instance.

Layering
========

The session is intentionally decoupled from Textual and UI concerns. The
stack for a connected console looks like this::

    Runtime (daemon)
       ^
       |  JSON-RPC + /events
       |
    RuntimeClient  (protocol/transport)
       ^
       |
    RuntimeSession (cached runtime model + lifecycle)
       ^
       |
     Textual (App + Screens + Widgets)

Key responsibilities of :class:`RuntimeSession`:

* Own a :class:`nate_ntm.api.runtime_client.RuntimeClient` instance.
* Maintain cached, latest-known views of:
  - runtime status
  - swarm overview
  - per-agent detail responses
  - a bounded buffer of recent runtime/agent events
* Expose an async ``connect``/``disconnect`` lifecycle that starts and
  stops background tasks for:
  - periodic snapshot refresh via the control API; and
  - consumption of the runtime's live event stream.
* Provide a simple, transport-agnostic update notification mechanism that
  screens/widgets (or other consumers) can observe.
* Track **degraded state** separately for control API failures vs.
  event-stream failures, so the UI can communicate partial outages.

The session never performs raw HTTP or WebSocket operations; all such
concerns are delegated to :class:`RuntimeClient`.
"""

import asyncio
from collections import deque
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Deque, Dict, List, Mapping, Optional

from nate_ntm.api.models import (
    AgentDetailEvent,
    AgentDetailResult,
    RuntimeStatusResult,
    SwarmOverviewResult,
)
from nate_ntm.api.runtime_client import EventsNotify, RuntimeClient


__all__ = ["RuntimeSession"]


@dataclass(slots=True)
class RuntimeSession:
    """Client-side runtime session with cached model and lifecycle.

    The session is constructed around a :class:`RuntimeClient` instance
    and provides higher-level, cached views of runtime state suitable for
    consumption by a Textual UI or other clients.

    Parameters
    ----------
    client:
        The :class:`RuntimeClient` used to talk to the runtime control
        API and event stream.

    poll_interval:
        Interval in seconds between periodic snapshot refreshes via the
        control API (``runtime.get_status`` + ``swarm.get_overview``).

    event_buffer_size:
        Maximum number of recent events retained in the session-wide
        event buffer. Older events are dropped as new ones arrive.
    """

    client: RuntimeClient
    poll_interval: float = 2.0
    event_buffer_size: int = 200

    # Cached state -----------------------------------------------------------------
    runtime_status: Optional[RuntimeStatusResult] = field(default=None, init=False)
    swarm_overview: Optional[SwarmOverviewResult] = field(default=None, init=False)
    agent_details: Dict[str, AgentDetailResult] = field(default_factory=dict, init=False)
    event_buffer: Deque[AgentDetailEvent] = field(init=False, repr=False)
    # Currently selected agent (if any), shared across UI components.
    selected_agent_id: Optional[str] = field(default=None, init=False)

    # Degraded state flags ---------------------------------------------------------
    control_degraded: bool = field(default=False, init=False)
    control_error: Optional[str] = field(default=None, init=False)

    events_degraded: bool = field(default=False, init=False)
    events_error: Optional[str] = field(default=None, init=False)

    # Lifecycle bookkeeping --------------------------------------------------------
    _connected: bool = field(default=False, init=False, repr=False)
    _poll_task: Optional[asyncio.Task[None]] = field(default=None, init=False, repr=False)
    _events_task: Optional[asyncio.Task[None]] = field(default=None, init=False, repr=False)
    _events_iter: Optional[AsyncIterator[EventsNotify]] = field(default=None, init=False, repr=False)

    # Update notification ----------------------------------------------------------
    _update_seq: int = field(default=0, init=False, repr=False)
    _update_event: Optional[asyncio.Event] = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:  # pragma: no cover - trivial wiring
        self.event_buffer = deque(maxlen=self.event_buffer_size)

    # ------------------------------------------------------------------
    # Public properties and basic helpers
    # ------------------------------------------------------------------

    @property
    def is_connected(self) -> bool:
        """Return ``True`` if the session is currently connected.

        "Connected" here means that :meth:`connect` has completed
        successfully and background tasks are running. It does *not*
        guarantee that the control API and event stream are both
        healthy; consult :attr:`control_degraded` and
        :attr:`events_degraded` for that.
        """

        return self._connected

    @property
    def update_seq(self) -> int:
        """Monotonically increasing sequence number for state updates.

        The sequence is incremented whenever cached state or degraded
        flags change. Consumers can store a value and later call
        :meth:`wait_for_update` to await a newer update.
        """

        return self._update_seq

    def select_agent(self, agent_id: Optional[str]) -> None:
        """Record the currently selected agent identifier.

        The selection is shared across all UI components that consume this
        session so that, for example, overview and detail screens can agree on
        which agent is "current".

        Passing ``None`` clears the selection. When the selection changes, the
        session's update sequence is incremented so that observers waiting via
        :meth:`wait_for_update` are notified.
        """

        if agent_id == self.selected_agent_id:
            return

        self.selected_agent_id = agent_id
        self._notify_updated()

    def _notify_updated(self) -> None:
        """Increment the update sequence and wake any waiters."""

        self._update_seq += 1
        if self._update_event is not None:
            self._update_event.set()

    async def wait_for_update(
        self,
        *,
        last_seen: Optional[int] = None,
        timeout: Optional[float] = None,
    ) -> int:
        """Wait until cached state changes.

        Parameters
        ----------
        last_seen:
            Previously observed update sequence number. If ``None`` or
            less than the current :attr:`update_seq`, the method returns
            immediately with the current value.

        timeout:
            Optional timeout in seconds. When provided and reached
            without a new update, :class:`asyncio.TimeoutError` is
            raised.

        Returns
        -------
        int
            The new :attr:`update_seq` value at the time an update was
            observed.
        """

        if self._update_event is None:
            # Lazily create the event on first use so that a session can
            # be inspected synchronously before connect() is called.
            self._update_event = asyncio.Event()

        while True:
            current = self._update_seq
            if last_seen is None or current > last_seen:
                return current

            event = self._update_event
            assert event is not None

            if timeout is not None:
                await asyncio.wait_for(event.wait(), timeout=timeout)
            else:
                await event.wait()

            event.clear()

    # ------------------------------------------------------------------
    # Lifecycle: connect / disconnect
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        """Connect the session and start background tasks.

        This method:

        * Clears cached state and degraded flags.
        * Starts a periodic snapshot refresh task that calls
          :meth:`RuntimeClient.get_runtime_status` and
          :meth:`RuntimeClient.get_swarm_overview`.
        * Starts a single event-consumer task that subscribes to the
          runtime event stream via :meth:`RuntimeClient.iter_events`.

        It is an error to call :meth:`connect` more than once without
        an intervening :meth:`disconnect`.
        """

        if self._connected:
            raise RuntimeError("RuntimeSession is already connected")

        # Reset cached state and flags for a fresh connection.
        self.runtime_status = None
        self.swarm_overview = None
        self.agent_details.clear()
        self.event_buffer.clear()
        self.selected_agent_id = None

        self.control_degraded = False
        self.control_error = None
        self.events_degraded = False
        self.events_error = None

        self._update_seq = 0
        if self._update_event is None:
            self._update_event = asyncio.Event()
        else:
            self._update_event.clear()

        # Spawn background tasks.
        loop = asyncio.get_running_loop()
        self._poll_task = loop.create_task(self._poll_loop(), name="RuntimeSession.poll_loop")

        # The event iterator is stored so that we can close it explicitly
        # on disconnect(), which in turn lets RuntimeClient perform a
        # best-effort unsubscribe.
        self._events_iter = self.client.iter_events(agent_ids=None, include_runtime=True, reconnect=True)
        self._events_task = loop.create_task(self._events_loop(), name="RuntimeSession.events_loop")

        self._connected = True

    async def disconnect(self) -> None:
        """Disconnect the session and stop background tasks.

        This method cancels the periodic polling and event-consumer
        tasks and performs best-effort cleanup of the runtime event
        subscription by explicitly closing the underlying async
        iterator, which allows :class:`RuntimeClient` to issue
        ``events.unsubscribe`` where appropriate.
        """

        if not self._connected:
            return

        self._connected = False

        # Cancel background tasks first so they stop consuming from the
        # event iterator and control API.
        tasks = [t for t in (self._poll_task, self._events_task) if t is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:  # pragma: no cover - expected
                pass

        self._poll_task = None
        self._events_task = None

        # Close the event iterator so that RuntimeClient can perform a
        # best-effort unsubscribe.
        if self._events_iter is not None:
            try:
                aclose = getattr(self._events_iter, "aclose", None)
                if aclose is not None:
                    await aclose()
            finally:
                self._events_iter = None

    # ------------------------------------------------------------------
    # Public query helpers
    # ------------------------------------------------------------------

    def get_cached_runtime_status(self) -> Optional[RuntimeStatusResult]:
        """Return the latest-known runtime status snapshot (if any)."""

        return self.runtime_status

    def get_cached_swarm_overview(self) -> Optional[SwarmOverviewResult]:
        """Return the latest-known swarm overview snapshot (if any)."""

        return self.swarm_overview

    def get_cached_agent_detail(self, agent_id: str) -> Optional[AgentDetailResult]:
        """Return the cached agent detail for ``agent_id`` (if any)."""

        return self.agent_details.get(agent_id)

    def get_recent_events(self, limit: Optional[int] = None) -> List[AgentDetailEvent]:
        """Return a copy of the most recent events from the buffer."""

        if limit is None or limit >= len(self.event_buffer):
            return list(self.event_buffer)
        if limit <= 0:
            return []
        # ``deque`` supports slicing only via ``itertools.islice``; cast
        # to ``list`` first for simplicity at this small scale.
        return list(self.event_buffer)[-limit:]


    async def shutdown_runtime(self, timeout_seconds: int = 30) -> Mapping[str, Any]:
        """Request a graceful shutdown of the connected runtime.

        This forwards to :meth:`RuntimeClient.shutdown_runtime` and records a
        control-plane degradation so that UI components can reflect that the
        runtime is in the process of shutting down.

        The method does not implicitly disconnect the session; callers are
        expected to invoke :meth:`disconnect` when appropriate.
        """

        result = await self.client.shutdown_runtime(timeout_seconds=timeout_seconds)
        # Mark the control plane as degraded but keep the last-known
        # snapshots so that the UI can display a final view while the
        # runtime exits.
        self.control_degraded = True
        if not self.control_error:
            self.control_error = "runtime shutdown requested"
        self._notify_updated()
        return result

    async def get_agent_detail(self, agent_id: str, max_events: int = 100, *, force_refresh: bool = False) -> AgentDetailResult:
        """Return detailed information for a single agent.

        When ``force_refresh`` is ``False`` (the default) and the agent
        is present in :attr:`agent_details`, the cached value is
        returned without performing a control API call. Otherwise, the
        detail is fetched from the runtime via :class:`RuntimeClient`,
        cached, and returned.
        """

        if not force_refresh and agent_id in self.agent_details:
            return self.agent_details[agent_id]

        try:
            detail = await self.client.get_agent_detail(agent_id, max_events=max_events)
        except Exception as exc:  # pragma: no cover - error shaping tested via RuntimeClient
            # Treat this as a control-plane degradation but re-raise the
            # underlying error so the caller can handle it if needed.
            self.control_degraded = True
            self.control_error = str(exc)
            self._notify_updated()
            raise

        self.agent_details[agent_id] = detail
        self.control_degraded = False
        self.control_error = None
        self._notify_updated()
        return detail

    # ------------------------------------------------------------------
    # Internal background loops
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Background task: periodically refresh cached snapshots.

        This task polls the control API for ``runtime.get_status`` and
        ``swarm.get_overview`` on a fixed interval. Failures set the
        :attr:`control_degraded` flag but do not clear the last-known
        snapshots so the UI can continue to display stale-but-useful
        information.
        """

        try:
            while True:
                try:
                    status = await self.client.get_runtime_status()
                    overview = await self.client.get_swarm_overview()
                except Exception as exc:  # pragma: no cover - error shaping tested via RuntimeClient
                    # Mark control plane as degraded while preserving
                    # the last-known snapshots.
                    self.control_degraded = True
                    self.control_error = str(exc)
                    self._notify_updated()
                else:
                    self.runtime_status = status
                    self.swarm_overview = overview
                    self.control_degraded = False
                    self.control_error = None
                    self._notify_updated()

                await asyncio.sleep(self.poll_interval)
        except asyncio.CancelledError:  # pragma: no cover - cooperative shutdown
            return

    async def _events_loop(self) -> None:
        """Background task: consume the runtime's live event stream.

        This task attaches to :meth:`RuntimeClient.iter_events` with
        ``reconnect=True`` so that transient network issues are handled
        by the client layer. Any unexpected errors are treated as a
        degraded event-stream state and recorded on
        :attr:`events_degraded` / :attr:`events_error`.
        """

        if self._events_iter is None:
            # Should not happen, but avoid failing the entire session if
            # connect() wiring is incomplete.
            self.events_degraded = True
            self.events_error = "events iterator not initialised"
            self._notify_updated()
            return

        agen = self._events_iter

        try:
            async for note in agen:
                # Append new events to the bounded buffer and notify
                # observers that state has changed.
                self.event_buffer.append(note.event)
                self.events_degraded = False
                self.events_error = None
                self._notify_updated()
        except asyncio.CancelledError:  # pragma: no cover - cooperative shutdown
            return
        except Exception as exc:  # pragma: no cover - defensive
            self.events_degraded = True
            self.events_error = str(exc)
            self._notify_updated()

