"""Agent subprocess launch and lifecycle supervision primitives (skeleton).

This module provides the *internal* interfaces for managing agents within a
single-swarm runtime process. It is intentionally conservative for US1:

* It wires :class:`AgentMetadata` from the persisted swarm description
  into :class:`AgentRuntimeState` entries in :class:`RuntimeState`.
* It creates per-agent :class:`~nate_ntm.runtime.events.AgentEventStream`
  instances so that later user stories can attach event streaming without
  changing the basic wiring.
* It does **not** yet launch real subprocesses or ACP connections; those
  behaviors are added in follow-up work for FR-004/FR-005.

This keeps the core runtime data structures and responsibilities aligned
with the spec (see ``data-model.md`` §3) without over-committing to a
particular scheduler or process model.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Iterable, Mapping

from ..config.runtime_config import RuntimeConfig
from .events import AgentEvent, AgentEventSource, AgentEventStream
from .metadata_store import AgentMetadata, SwarmMetadata
from .state import AgentRuntimeState, AgentStatus, RuntimeState

__all__ = ["AgentSupervisor"]


@dataclass(slots=True)
class AgentSupervisor:
    """Manage in-memory runtime state for agents.

    For US1 this focuses on establishing and maintaining the mapping
    between persisted :class:`AgentMetadata` records and
    :class:`AgentRuntimeState` entries in :class:`RuntimeState`.

    Later phases will extend this class to:

    * Launch and supervise agent subprocesses.
    * Establish and refresh ACP connections.
    * Surface subprocess/ACP events into the scheduler.
    * Apply restart policies based on :class:`AgentMetadata.restart_policy`.
    """

    config: RuntimeConfig
    state: RuntimeState
    swarm_metadata: SwarmMetadata

    # ------------------------------------------------------------------
    # Registration helpers
    # ------------------------------------------------------------------

    def _get_or_create_event_stream(self, runtime_state: AgentRuntimeState) -> AgentEventStream:
        """Return the event stream for ``runtime_state``, creating one if missing.

        This keeps :class:`AgentSupervisor` robust when tests or future
        scheduler logic seed :class:`AgentRuntimeState` entries without an
        attached :class:`AgentEventStream`.
        """

        stream = runtime_state.event_stream
        if stream is None:
            stream = AgentEventStream(agent_id=runtime_state.agent_id)
            runtime_state.event_stream = stream
        return stream

    def _append_runtime_event(
        self,
        runtime_state: AgentRuntimeState,
        *,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        source: AgentEventSource = AgentEventSource.RUNTIME,
    ) -> None:
        """Append a runtime-originated event to the agent's event stream.

        Event identifiers are generated locally in a simple, per-agent
        monotonic fashion suitable for in-memory inspection and tests.
        """

        stream = self._get_or_create_event_stream(runtime_state)
        event_id = f"{runtime_state.agent_id}:{len(stream) + 1}"
        event = AgentEvent(
            event_id=event_id,
            timestamp=datetime.utcnow(),
            agent_id=runtime_state.agent_id,
            source=source,
            type=event_type,
            payload=payload or {},
        )
        stream.append(event)

    def iter_configured_agents(self) -> Iterable[AgentMetadata]:
        """Iterate over :class:`AgentMetadata` records from the swarm.

        This is a thin wrapper over ``swarm_metadata.agents.values()``
        that exists primarily to keep the call-sites within this module
        clear and testable.
        """

        return self.swarm_metadata.agents.values()

    def ensure_agent_runtime_state(self, metadata: AgentMetadata) -> AgentRuntimeState:
        """Ensure that ``RuntimeState.agents`` has an entry for ``metadata``.

        If a runtime state for this agent already exists, it is returned
        unchanged. Otherwise a new :class:`AgentRuntimeState` instance is
        created with a default ``Starting`` status and attached
        :class:`AgentEventStream`.
        """

        agent_id = metadata.agent_id

        existing = self.state.agents.get(agent_id)
        if existing is not None:
            return existing

        runtime_state = AgentRuntimeState(
            agent_id=agent_id,
            status=AgentStatus.STARTING,
        )
        # Ensure an event stream is attached for new agents.
        self._get_or_create_event_stream(runtime_state)
        self.state.agents[agent_id] = runtime_state
        return runtime_state

    def ensure_agents_registered(self) -> None:
        """Ensure all configured agents have a runtime state entry.

        This is the primary entry point used by the scheduler/daemon
        during startup. It walks the agents defined in
        :class:`SwarmMetadata` and ensures that each has a corresponding
        :class:`AgentRuntimeState` in :class:`RuntimeState`.

        Existing runtime entries are left untouched so that tests (and
        later, the real scheduler) can seed richer state before
        registration occurs.
        """

        for metadata in self.iter_configured_agents():
            self.ensure_agent_runtime_state(metadata)

    # ------------------------------------------------------------------
    # Placeholders for future lifecycle management
    # ------------------------------------------------------------------

    # The following methods capture *intended* responsibilities but are
    # deliberately left as no-ops or minimal stubs for US1. They are
    # included to clarify ownership and to keep call sites stable as we
    # iterate on the scheduler and adapter implementations.

    def mark_agent_failed(self, agent_id: str, *, error: str | None = None) -> AgentRuntimeState:
        """Transition an agent to ``Failed`` and record an error summary.

        This helper is a small, synchronous building block for the
        scheduler and tests. In later phases it will be invoked from
        subprocess and ACP error handlers.
        """

        runtime_state = self.state.agents.get(agent_id)
        if runtime_state is None:
            raise KeyError(f"Unknown agent_id: {agent_id!r}")

        runtime_state.status = AgentStatus.FAILED
        runtime_state.last_error = error

        self._append_runtime_event(
            runtime_state,
            event_type="AgentFailed",
            payload={"last_error": error} if error is not None else {},
        )

        return runtime_state

    def restart_agent(self, agent_id: str) -> AgentRuntimeState:
        """Apply a simple restart for a failed agent (dev-mode).

        For US1 this does **not** spawn a real subprocess. Instead it
        models a restart by:

        * Re-initializing the agent's ``subprocess_handle`` placeholder.
        * Clearing ``last_error``.
        * Transitioning status from ``Failed`` back to ``Starting`` and then
          to ``Idle`` in one step.

        Restart limits and backoff policies from
        :class:`AgentMetadata.restart_policy` are intentionally deferred.
        """

        runtime_state = self.state.agents.get(agent_id)
        if runtime_state is None:
            raise KeyError(f"Unknown agent_id: {agent_id!r}")

        # Even if the agent is not currently in Failed state, we allow a
        # restart request as a way to "refresh" its placeholder subprocess
        # and mark it Idle.
        runtime_state.status = AgentStatus.STARTING
        runtime_state.last_error = None
        runtime_state.subprocess_handle = object()
        runtime_state.status = AgentStatus.IDLE

        self._append_runtime_event(runtime_state, event_type="AgentRestarted")

        return runtime_state


    def launch_all_agents(self) -> None:
        """Launch all configured agents (dev-mode implementation for US1).

        This helper does **not** start real subprocesses yet. Instead it
        provides a deterministic, in-memory approximation of a launched
        agent suitable for unit and integration tests:

        * Ensures that :class:`RuntimeState.agents` contains entries for
          all configured agents (via :meth:`ensure_agents_registered`).
        * For each agent currently in ``Starting`` state without a
          ``subprocess_handle``, attaches a lightweight placeholder
          object and transitions the status to ``Idle``.

        Existing runtime entries with a non-``Starting`` status are left
        unchanged so that tests (and future scheduler logic) can seed
        richer lifecycle behavior before calling this method.
        """

        # Capture the set of agents that already had runtime state prior
        # to this call so that we can distinguish newly registered agents
        # from entries that were pre-seeded by tests or the scheduler.
        existing_ids = set(self.state.agents.keys())

        # Ensure that every configured agent has a runtime state entry; this
        # is idempotent and preserves any pre-seeded entries.
        self.ensure_agents_registered()

        # Simulate successful subprocess launch for any *newly* registered
        # agents that are still in the initial ``Starting`` state by:
        #
        # * Attaching a simple opaque object as ``subprocess_handle``.
        # * Marking the agent as ``Idle`` to represent a ready-but-not-
        #   currently-running subprocess.
        for agent_id, runtime_state in self.state.agents.items():
            if (
                agent_id not in existing_ids
                and runtime_state.status is AgentStatus.STARTING
                and runtime_state.subprocess_handle is None
            ):
                runtime_state.subprocess_handle = object()
                runtime_state.status = AgentStatus.IDLE

    # Additional lifecycle hooks (e.g. ``handle_subprocess_exit``) will
    # be introduced alongside concrete scheduler and ACP integrations.
