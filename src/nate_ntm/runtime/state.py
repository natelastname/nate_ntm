"""Runtime and agent state data structures.

This module defines the in-memory data structures that represent
high-level runtime lifecycle state and per-agent status, as described in
``specs/001-swarm-runtime-orchestrator/data-model.md``.

The goal is to keep these structures small, explicit, and easy to mock
so that other components (scheduler, daemon, API) can depend on them
without bringing in heavy runtime behavior.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Dict, Optional

from ..config.runtime_config import RuntimeConfig

if TYPE_CHECKING:  # pragma: no cover - import cycle avoidance
    from .events import AgentEventStream


class RuntimeStatus(str, Enum):
    """High-level lifecycle state for the Runtime daemon.

    Mirrors the conceptual states in the data model (Starting, Running,
    ShuttingDown, Stopped, Failed) while remaining extensible for
    future refinements.
    """

    STARTING = "Starting"
    RUNNING = "Running"
    SHUTTING_DOWN = "ShuttingDown"
    STOPPED = "Stopped"
    FAILED = "Failed"


class AgentStatus(str, Enum):
    """Lifecycle state of an individual agent instance.

    See data-model.md §4.2 for the informal semantics.
    """

    STARTING = "Starting"
    IDLE = "Idle"
    RUNNING = "Running"
    WAITING = "Waiting"
    FAILED = "Failed"


@dataclass(slots=True)
class AgentRuntimeState:
    """Ephemeral, in-memory state for a single agent instance.

    Note that this intentionally does *not* include concrete subprocess
    or ACP connection types so that this module can be imported without
    pulling in asyncio or external client dependencies.
    """

    agent_id: str
    """Stable identifier for the agent within the swarm."""

    status: AgentStatus = AgentStatus.STARTING
    """Current lifecycle status for the agent."""

    current_turn_id: Optional[str] = None
    """Identifier of the current ACP turn, if any."""

    last_error: Optional[str] = None
    """Summary of the most recent error for this agent, if any."""

    # Placeholders for richer types that will be wired in later phases.
    subprocess_handle: Optional[object] = None
    acp_connection: Optional[object] = None

    # Per-agent in-memory event stream; typically wired by the runtime
    # when agents are created. Optional to keep state structures usable
    # in isolation and in tests that do not yet depend on events.
    event_stream: Optional["AgentEventStream"] = None


@dataclass(slots=True)
class RuntimeState:
    """Top-level in-memory state for the running Runtime daemon.

    This structure is owned by the Runtime and may be exposed in
    read-only form via the control API. It intentionally focuses on
    high-level status and relationships rather than implementation
    details of the scheduler or event loop.
    """

    config: RuntimeConfig
    """Resolved runtime configuration in effect for this process."""

    agents: Dict[str, AgentRuntimeState] = field(default_factory=dict)
    """Mapping of agent_id to :class:`AgentRuntimeState`."""

    status: RuntimeStatus = RuntimeStatus.STARTING
    """Overall lifecycle status of the runtime."""

    shutdown_requested: bool = False
    """Indicates that a graceful shutdown has been requested."""

    def get_agent(self, agent_id: str) -> Optional[AgentRuntimeState]:
        """Return the runtime state for ``agent_id``, if present."""

        return self.agents.get(agent_id)

    def set_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        """Update the status of an existing agent.

        This helper intentionally does **not** create agents implicitly;
        callers must register agents explicitly in the ``agents``
        mapping to avoid accidental typos.
        """

        if agent_id not in self.agents:
            raise KeyError(f"Unknown agent_id: {agent_id}")
        self.agents[agent_id].status = status
