"""Event-driven runtime scheduler skeleton.

This module defines a minimal :class:`RuntimeScheduler` abstraction for
US1. Its responsibilities in this slice are intentionally narrow:

* Bridge between :class:`SwarmMetadata` (configured agents) and
  :class:`RuntimeState` by asking :class:`AgentSupervisor` to ensure all
  agents are registered.
* Provide a place for future event loop and integration wiring without
  entangling the :class:`RuntimeDaemon` itself with asynchronous
  mechanics.

It does **not** yet implement a real event loop, Agent Mail polling, or
ACP turn management; those responsibilities are reserved for later
iterations of T016/T017 and follow-on stories.
"""

from __future__ import annotations

from dataclasses import dataclass

from ..config.runtime_config import RuntimeConfig
from .agent_mail_client import BaseAgentMailClient
from .agents import AgentSupervisor
from .metadata_store import SwarmMetadata
from .state import RuntimeState

__all__ = ["RuntimeScheduler"]


@dataclass(slots=True)
class RuntimeScheduler:
    """Minimal scheduler facade for the runtime.

    In this phase the scheduler is a thin facade owned by the
    :class:`~nate_ntm.runtime.daemon.RuntimeDaemon`. It delegates agent
    registration and (later) subprocess management to
    :class:`AgentSupervisor`.
    """

    config: RuntimeConfig
    state: RuntimeState
    swarm_metadata: SwarmMetadata
    agent_supervisor: AgentSupervisor

    # Optional Agent Mail adapter used to poll for unread messages at
    # startup. For US2 this is used in dev-mode to synthesize
    # "MailReceived" events for agents that have unread mail so that the
    # scheduler and event pipeline can treat them as eligible for
    # scheduling on resume.
    agent_mail_client: BaseAgentMailClient | None = None

    running: bool = False

    def start(self) -> None:
        """Initialize scheduler-managed state.

        For US1 this wires configured agents into runtime state **and**
        simulates an initial "launch" via :class:`AgentSupervisor`:

        * All agents described in :class:`SwarmMetadata` gain
          corresponding entries in :class:`RuntimeState.agents`.
        * Newly registered agents are transitioned from ``Starting`` to
          ``Idle`` with a lightweight placeholder subprocess handle.

        For US2, when an :class:`BaseAgentMailClient` is configured, the
        scheduler also performs a one-time poll for unread Agent Mail at
        startup and enqueues runtime events for agents that currently
        have unread messages. This provides a simple, testable hook for
        resume-time scheduling behavior without requiring a full event
        loop yet.
        """

        if self.running:
            return

        # Ensure that runtime state reflects configured agents and that
        # newly added ones are treated as "launched" in dev-mode.
        self.agent_supervisor.launch_all_agents()

        # After agents are registered/launched, consult Agent Mail (when
        # available) for unread messages and enqueue corresponding
        # runtime events. This allows higher layers (and future scheduler
        # logic) to treat those agents as having work available on
        # resume.
        if self.agent_mail_client is not None and self.swarm_metadata.agents:
            agent_ids = list(self.swarm_metadata.agents.keys())
            flags = self.agent_mail_client.get_unread_mail_flags(agent_ids)
            for agent_id, has_unread in flags.items():
                if not has_unread:
                    continue
                # Only enqueue events for agents that have runtime
                # state; in a well-formed startup flow launch_all_agents
                # above guarantees this.
                if agent_id not in self.state.agents:
                    continue
                self.agent_supervisor.record_unread_mail(agent_id)

        self.running = True

    def stop(self) -> None:
        """Stop the scheduler (stub).

        In a full implementation this would coordinate graceful
        termination of the event loop and any outstanding work. For now
        it is a simple flag used to mirror the eventual lifecycle.
        """

        self.running = False

    # ------------------------------------------------------------------
    # Simple lifecycle helpers
    # ------------------------------------------------------------------

    def mark_agent_failed(self, agent_id: str, *, error: str | None = None) -> None:
        """Record an agent failure via :class:`AgentSupervisor`.

        In future iterations this will be called from subprocess/ACP event
        handlers and may consult per-agent restart policies before deciding
        whether to restart the agent.
        """

        self.agent_supervisor.mark_agent_failed(agent_id, error=error)

    def restart_agent(self, agent_id: str) -> None:
        """Request a simple dev-mode restart for an agent.

        This delegates to :meth:`AgentSupervisor.restart_agent`, which
        currently models restarts by refreshing the placeholder subprocess
        handle and transitioning the agent back to ``Idle``.
        """

        self.agent_supervisor.restart_agent(agent_id)

