"""In-memory runtime and per-agent lifecycle state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from ..config.runtime_config import RuntimeConfig


class RuntimeStatus(str, Enum):
    STARTING = "Starting"
    RUNNING = "Running"
    SHUTTING_DOWN = "ShuttingDown"
    STOPPED = "Stopped"
    FAILED = "Failed"


class AgentStatus(str, Enum):
    STARTING = "Starting"
    IDLE = "Idle"
    RUNNING = "Running"
    WAITING = "Waiting"
    FAILED = "Failed"


@dataclass(slots=True)
class AgentRuntimeState:
    agent_id: str
    status: AgentStatus = AgentStatus.STARTING
    last_error: str | None = None


@dataclass(slots=True)
class RuntimeState:
    config: RuntimeConfig
    agents: dict[str, AgentRuntimeState] = field(default_factory=dict)
    status: RuntimeStatus = RuntimeStatus.STARTING
    shutdown_requested: bool = False

    def get_agent(self, agent_id: str) -> AgentRuntimeState | None:
        return self.agents.get(agent_id)

    def set_agent_status(self, agent_id: str, status: AgentStatus) -> None:
        if agent_id not in self.agents:
            raise KeyError(f"Unknown agent_id: {agent_id}")
        self.agents[agent_id].status = status
