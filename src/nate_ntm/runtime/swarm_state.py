from __future__ import annotations

"""Durable swarm state models.

These Pydantic models represent the complete, persisted state of a swarm
as a single object graph, as described in ``ConfigOverhaul.md``.

They are intentionally focused on *durable* (on-disk) state rather than
in-memory runtime lifecycle data. In particular:

  * :class:`AgentState` stores the persisted per-agent metadata, including
  the (future) fully-resolved nate-oha configuration and ACP
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

from pydantic import BaseModel, Field, field_validator, ConfigDict

from nate_oha.config import NateOHAConfig

__all__ = [
    "AgentState",
    "SwarmState",
]


class AgentState(BaseModel):
    """Durable state for a single agent within a swarm.

    The durable representation intentionally keeps only the minimal set of
    per-agent fields that are not already captured inside
    :class:`NateOHAConfig`.

    Launch-time behaviour is driven entirely by :attr:`nate_oha_config`.
    This model records only the agent's durable identity plus a small
    amount of runtime-owned policy and status that must survive process
    restarts but does **not** belong in :class:`NateOhaConfig`:

    * ``role`` – optional descriptive label used by UIs and tooling to
      distinguish agents within a swarm. This is swarm-local presentation
      metadata rather than nate-oha configuration.
    * ``restart_policy`` – swarm-owned policy for how the scheduler
      should treat failing agents. This is runtime behaviour, not part of
      the nate-oha process configuration.
    * ``last_known_status`` – snapshot of the last observed high-level
      status for this agent (for example, "Idle", "Running", "Failed").
      This is used by :class:`RuntimeDaemon` to provide meaningful status
      even when no live runtime state exists.
    * ``conversation_id`` – opaque ACP-owned session identifier used
      when resuming nate-oha processes via ``--resume``.

    All nate-oha configuration, including Agent Mail settings, lives
    inside :attr:`nate_oha_config`. Legacy per-agent Agent Mail fields
    such as ``agent_mail_identity`` and ``agent_mail_credentials_ref``
    have been removed; persisted state that still uses them is treated as
    invalid and will fail validation.
    """

    # Reject unknown/legacy fields so that on-disk swarm state either
    # conforms to the current schema or fails validation explicitly.
    model_config = ConfigDict(extra="forbid")

    agent_id: str
    display_name: str

    # Optional descriptive label for this agent's role or specialisation
    # within the swarm (for example, "navigator" or "implementer"). This
    # is presentation metadata that UIs can surface across restarts; it
    # does not influence nate-oha configuration and therefore does not
    # belong in :class:`NateOHAConfig`.
    role: Optional[str] = None

    # ACP-owned conversation identifier used for ``--resume``. ``None``
    # (or an empty string in older payloads) is treated as "no binding
    # present"; the runtime must not invent new identifiers locally.
    conversation_id: Optional[str] = None

    # Swarm-owned restart policy that future scheduler implementations
    # may consult when deciding how to handle failing agents. This lives
    # in durable state so policy survives restarts but remains separate
    # from nate-oha's own configuration.
    restart_policy: Dict[str, Any] = Field(default_factory=dict)

    # Snapshot of last persisted status (e.g. "Idle", "Running",
    # "Failed"). This mirrors ``AgentMetadata.last_known_status`` and is
    # used by :class:`RuntimeDaemon` when computing agent detail in the
    # absence of live runtime state.
    last_known_status: str = "Idle"

    # Fully resolved nate-oha configuration for this agent. Milestone 2
    # requires this to be present for all persisted agents; callers are
    # expected to derive and attach an effective configuration before
    # saving swarm state.
    nate_oha_config: NateOHAConfig

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

    # Reject unknown/legacy fields so that swarm.json either matches the
    # current schema or fails validation during load.
    model_config = ConfigDict(extra="forbid")

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

