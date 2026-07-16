from __future__ import annotations

"""Durable swarm state models.

These Pydantic models represent the complete, persisted state of a swarm
as a single object graph, as described in ``ConfigOverhaul.md``.

They are intentionally focused on *durable* (on-disk) state rather than
in-memory runtime lifecycle data. In particular:

* :class:`AgentState` stores the persisted per-agent metadata, including
  the (future) fully-resolved Nate OHA configuration and ACP
  conversation identifier.
* :class:`SwarmState` aggregates all agents for a given swarm and
  carries a simple schema version for forward compatibility.

Persistence helpers (for example, filesystem layout and atomic writes)
are the responsibility of the metadata layer; callers are expected to
use :meth:`SwarmState.model_validate_json` and
:meth:`SwarmState.model_dump_json` when loading or saving instances.
"""

from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from pydantic import BaseModel, Field, field_validator

from .nate_oha_config_compat import NateOhaConfig

__all__ = [
    "AgentState",
    "SwarmState",
]


class AgentState(BaseModel):
    """Durable state for a single agent within a swarm.

    The durable representation intentionally keeps only the minimal set of
    per-agent fields that are not already captured inside
    :class:`NateOhaConfig`. Launch-time behavior is driven by
    :attr:`nate_oha_config`; identity and ACP conversation identifiers live
    here alongside a small amount of runtime-owned policy and status.
    """

    agent_id: str
    display_name: str

    role: Optional[str] = None

    agent_mail_identity: str = ""
    agent_mail_credentials_ref: str = ""

    # ACP-owned conversation identifier used for --resume. When empty,
    # the runtime should treat this agent as not yet bound to a
    # persistent ACP session.
    conversation_id: str = ""

    restart_policy: Dict[str, Any] = Field(default_factory=dict)

    # Snapshot of last persisted status (e.g. "Idle", "Running",
    # "Failed"). This mirrors ``AgentMetadata.last_known_status``.
    last_known_status: str = "Idle"

    # Fully resolved Nate OHA configuration for this agent. This is
    # optional during the migration period; future revisions may tighten
    # this to be required once all callers populate it.
    nate_oha_config: Optional[NateOhaConfig] = None

    @field_validator("agent_id", "display_name")
    @classmethod
    def _must_not_be_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value


class SwarmState(BaseModel):
    """Durable, single-object representation of an entire swarm.

    The intent is that a complete swarm can be recreated using only a
    single :class:`SwarmState` instance and the embedded
    :class:`AgentState` entries.
    """

    # Basic schema version to allow for future, non-breaking evolution
    # of the on-disk representation.
    schema_version: int = 1

    swarm_id: str

    # Resolved project directory for this swarm. This mirrors
    # :attr:`SwarmMetadata.project_path` and is validated against the
    # active :class:`RuntimeConfig` when loaded.
    project_path: Path

    # Adapter-owned Agent Mail project identifier, used to enforce
    # FR-009 style invariants across create→resume flows.
    agent_mail_project_id: str = ""

    created_at: datetime
    last_updated_at: datetime

    config_version: Optional[str] = None

    # Mapping of agent_id to AgentState.
    agents: Dict[str, AgentState] = Field(default_factory=dict)

    runtime_options: Dict[str, Any] = Field(default_factory=dict)

    # Convenience helpers around Pydantic's JSON (de-)serialization
    # methods. These keep call sites slightly more self-documenting
    # without taking on responsibility for filesystem I/O.

    @field_validator("swarm_id")
    @classmethod
    def _swarm_id_must_not_be_empty(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("must not be empty")
        return value

    @field_validator("project_path")
    @classmethod
    def _normalize_project_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @classmethod
    def from_json(cls, data: str) -> "SwarmState":
        """Parse a :class:`SwarmState` from a JSON string."""

        return cls.model_validate_json(data)

    def to_json(self, *, indent: int = 2) -> str:
        """Render this :class:`SwarmState` as a JSON string."""

        return self.model_dump_json(indent=indent)

    # ------------------------------------------------------------------
    # Invariant checks
    # ------------------------------------------------------------------

    def validate(self, *, expected_project_path: Path, expected_swarm_id: str) -> None:
        """Validate this state against runtime-level invariants.

        The checks mirror the older :class:`SwarmMetadata.validate` helper
        so that callers (for example, :class:`MetadataStore` and
        :class:`RuntimeDaemon`) can rely on consistent behaviour when
        loading persisted state:

        * ``swarm_id`` must match ``expected_swarm_id``.
        * ``project_path`` (after normalization) must match
          ``expected_project_path``.

        :raises ValueError: if any invariant is violated.
        """

        normalized_expected = expected_project_path.expanduser().resolve()

        if self.swarm_id != expected_swarm_id:
            raise ValueError(
                f"SwarmState.swarm_id {self.swarm_id!r} does not match expected "
                f"swarm_id {expected_swarm_id!r}"
            )

        if self.project_path != normalized_expected:
            raise ValueError(
                "SwarmState.project_path "
                f"{str(self.project_path)!r} does not match expected project_path "
                f"{str(normalized_expected)!r}"
            )

