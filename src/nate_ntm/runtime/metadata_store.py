"""Project-local swarm state persistence layer.

This module implements a small, file-based store used by the runtime
daemon to persist swarm and per-agent state under the project's
metadata directory (for example, ``.nate_ntm/``).

It is intentionally conservative:

* JSON files only, containing a single Pydantic :class:`SwarmState`
  object graph (see :mod:`nate_ntm.runtime.swarm_state`).
* Atomic write semantics for all persistence operations (write to a
  temporary file in the same directory, flush/fsync, then rename into
  place) to avoid partially written state files (T038 / FR-014).
* Basic validation to ensure that loaded state is consistent with the
  current :class:`~nate_ntm.config.runtime_config.RuntimeConfig`.

Layout (see ``ConfigOverhaul.md`` and ``data-model.md`` §2.3):

.. code-block:: text

    .nate_ntm/
    └── swarm.json   # Single SwarmState object graph (authoritative)

This module does **not** perform higher-level lifecycle logic such as
"create vs resume" decisions; that is the responsibility of the
:mod:`nate_ntm.runtime.daemon` implementation.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, MutableMapping

from ..config.runtime_config import RuntimeConfig
from .nate_oha_config_compat import NateOhaConfig
from .swarm_state import AgentState as PersistedAgentState, SwarmState as PersistedSwarmState

__all__ = [
    "AgentMetadata",
    "SwarmMetadata",
    "MetadataStore",
]

# ---------------------------------------------------------------------------
# Canonical state models
# ---------------------------------------------------------------------------
#
# Durable state is represented exclusively by the Pydantic models
# :class:`SwarmState` and :class:`AgentState` from
# :mod:`nate_ntm.runtime.swarm_state`. This module provides a thin,
# file-based adapter around those models.
#
# Legacy :class:`SwarmMetadata` and :class:`AgentMetadata` dataclasses are
# retained as in-memory views for compatibility with existing call sites.
# They are *not* persisted directly; instead they are materialized from and
# written back into the canonical :class:`SwarmState` representation.


# ---------------------------------------------------------------------------
# Legacy metadata views (compatibility layer)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AgentMetadata:
    """Persisted configuration and identity for a single agent.

    This dataclass is a thin, in-memory view over the durable
    :class:`~nate_ntm.runtime.swarm_state.AgentState` model. Only fields
    that are not represented inside :class:`NateOhaConfig` are retained
    here; launch-time behaviour is owned by the embedded Nate OHA
    configuration.
    """

    agent_id: str
    display_name: str

    role: str | None = None

    agent_mail_identity: str = ""
    agent_mail_credentials_ref: str = ""

    conversation_id: str = ""

    restart_policy: Mapping[str, Any] = field(default_factory=dict)

    # Snapshot of last persisted status (e.g. "Idle", "Running", "Failed").
    last_known_status: str = "Idle"

    # Fully resolved Nate OHA configuration for this agent. This mirrors
    # :attr:`AgentState.nate_oha_config` and is persisted via
    # :class:`SwarmState`. It is optional during the migration period and
    # may be populated only for nate-oha-backed agents.
    nate_oha_config: NateOhaConfig | None = None

    def to_dict(self) -> Dict[str, Any]:
        """Render as a JSON-serializable mapping."""

        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "role": self.role,
            "agent_mail_identity": self.agent_mail_identity,
            "agent_mail_credentials_ref": self.agent_mail_credentials_ref,
            "conversation_id": self.conversation_id,
            "restart_policy": dict(self.restart_policy),
            "last_known_status": self.last_known_status,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "AgentMetadata":
        """Create :class:`AgentMetadata` from a JSON-derived mapping."""

        agent_id = str(data.get("agent_id", "")).strip()
        display_name = str(data.get("display_name", "")).strip()
        if not agent_id:
            raise ValueError("AgentMetadata.agent_id must not be empty")
        if not display_name:
            raise ValueError("AgentMetadata.display_name must not be empty")

        return cls(
            agent_id=agent_id,
            display_name=display_name,
            role=data.get("role"),
            agent_mail_identity=str(data.get("agent_mail_identity", "")),
            agent_mail_credentials_ref=str(
                data.get("agent_mail_credentials_ref", "")
            ),
            conversation_id=str(data.get("conversation_id", "")),
            restart_policy=dict(data.get("restart_policy", {}) or {}),
            last_known_status=str(data.get("last_known_status", "Idle")),
        )


@dataclass(frozen=True, slots=True)
class SwarmMetadata:
    """Persisted, project-local description of a swarm.

    See ``data-model.md`` §2.1 for field semantics and invariants.
    """

    swarm_id: str
    project_path: Path
    agent_mail_project_id: str

    created_at: datetime
    last_updated_at: datetime

    config_version: str | None = None

    # Mapping of agent_id to AgentMetadata.
    agents: Mapping[str, AgentMetadata] = field(default_factory=dict)

    runtime_options: Mapping[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Render as a JSON-serializable mapping."""

        return {
            "swarm_id": self.swarm_id,
            "project_path": str(self.project_path),
            "agent_mail_project_id": self.agent_mail_project_id,
            "created_at": self.created_at.isoformat(),
            "last_updated_at": self.last_updated_at.isoformat(),
            "config_version": self.config_version,
            "agents": [a.to_dict() for a in self.agents.values()],
            "runtime_options": dict(self.runtime_options),
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "SwarmMetadata":
        """Create :class:`SwarmMetadata` from a JSON-derived mapping."""

        swarm_id = str(data.get("swarm_id", "")).strip()
        if not swarm_id:
            raise ValueError("SwarmMetadata.swarm_id must not be empty")

        raw_project_path = data.get("project_path")
        if not raw_project_path:
            raise ValueError("SwarmMetadata.project_path must be set")
        project_path = Path(str(raw_project_path)).expanduser().resolve()

        def _parse_dt(key: str) -> datetime:
            raw = data.get(key)
            if not raw:
                raise ValueError(f"SwarmMetadata.{key} must be set")
            try:
                return datetime.fromisoformat(str(raw))
            except ValueError as exc:  # pragma: no cover - defensive
                raise ValueError(f"Invalid datetime value for {key!r}: {raw!r}") from exc

        created_at = _parse_dt("created_at")
        last_updated_at = _parse_dt("last_updated_at")

        agents_data = data.get("agents") or []
        agents: MutableMapping[str, AgentMetadata] = {}
        for raw_agent in agents_data:
            agent = AgentMetadata.from_dict(raw_agent)
            if agent.agent_id in agents:
                raise ValueError(f"Duplicate agent_id in SwarmMetadata: {agent.agent_id}")
            agents[agent.agent_id] = agent

        return cls(
            swarm_id=swarm_id,
            project_path=project_path,
            agent_mail_project_id=str(data.get("agent_mail_project_id", "")),
            created_at=created_at,
            last_updated_at=last_updated_at,
            config_version=data.get("config_version"),
            agents=agents,
            runtime_options=dict(data.get("runtime_options", {}) or {}),
        )

    def validate(
        self,
        *,
        expected_project_path: Path | None = None,
        expected_swarm_id: str | None = None,
    ) -> None:
        """Validate basic invariants.

        * ``project_path`` MUST match ``expected_project_path`` (if given).
        * ``swarm_id`` MUST match ``expected_swarm_id`` (if given).
        * ``agents`` MUST have unique identifiers (enforced during parsing).
        """

        if expected_swarm_id is not None and self.swarm_id != expected_swarm_id:
            raise ValueError(
                "SwarmMetadata.swarm_id "
                f"{self.swarm_id!r} does not match expected {expected_swarm_id!r}"
            )

        if expected_project_path is not None:
            expected_resolved = expected_project_path.expanduser().resolve()
            if self.project_path != expected_resolved:
                raise ValueError(
                    "SwarmMetadata.project_path does not match the current project "
                    f"directory: {self.project_path!r} != {expected_resolved!r}"
                )


# ---------------------------------------------------------------------------
# Conversion helpers: dataclasses <-> Pydantic models
# ---------------------------------------------------------------------------


def _agent_state_from_metadata(meta: AgentMetadata) -> PersistedAgentState:
    """Convert :class:`AgentMetadata` into its persisted :class:`AgentState` form.

    This preserves the existing field semantics while allowing a single
    Pydantic object graph (:class:`SwarmState`) to act as the on-disk
    source of truth.
    """

    return PersistedAgentState(
        agent_id=meta.agent_id,
        display_name=meta.display_name,
        role=meta.role,
        agent_mail_identity=meta.agent_mail_identity,
        agent_mail_credentials_ref=meta.agent_mail_credentials_ref,
        conversation_id=meta.conversation_id,
        restart_policy=dict(meta.restart_policy),
        last_known_status=meta.last_known_status,
        nate_oha_config=meta.nate_oha_config,
    )


def _agent_metadata_from_state(state: PersistedAgentState) -> AgentMetadata:
    """Convert a persisted :class:`AgentState` into :class:`AgentMetadata`."""

    return AgentMetadata(
        agent_id=state.agent_id,
        display_name=state.display_name,
        role=state.role,
        agent_mail_identity=state.agent_mail_identity,
        agent_mail_credentials_ref=state.agent_mail_credentials_ref,
        conversation_id=state.conversation_id or "",
        restart_policy=dict(state.restart_policy or {}),
        last_known_status=state.last_known_status,
        nate_oha_config=state.nate_oha_config,
    )


def _swarm_state_from_metadata(swarm: SwarmMetadata) -> PersistedSwarmState:
    """Convert :class:`SwarmMetadata` into its persisted :class:`SwarmState` form."""

    agents = {agent_id: _agent_state_from_metadata(meta) for agent_id, meta in swarm.agents.items()}

    return PersistedSwarmState(
        swarm_id=swarm.swarm_id,
        project_path=swarm.project_path,
        agent_mail_project_id=swarm.agent_mail_project_id,
        created_at=swarm.created_at,
        last_updated_at=swarm.last_updated_at,
        config_version=swarm.config_version,
        agents=agents,
        runtime_options=dict(swarm.runtime_options),
    )


def _swarm_metadata_from_state(state: PersistedSwarmState) -> SwarmMetadata:
    """Convert a persisted :class:`SwarmState` into :class:`SwarmMetadata`."""

    agents: MutableMapping[str, AgentMetadata] = {}
    for agent_id, agent_state in state.agents.items():
        meta = _agent_metadata_from_state(agent_state)
        if meta.agent_id in agents:
            raise ValueError(f"Duplicate agent_id in SwarmState: {meta.agent_id}")
        agents[meta.agent_id] = meta

    return SwarmMetadata(
        swarm_id=state.swarm_id,
        project_path=state.project_path,
        agent_mail_project_id=state.agent_mail_project_id,
        created_at=state.created_at,
        last_updated_at=state.last_updated_at,
        config_version=state.config_version,
        agents=agents,
        runtime_options=dict(state.runtime_options or {}),
    )


# ---------------------------------------------------------------------------
# Low-level helpers
# ---------------------------------------------------------------------------


def _atomic_write_json(path: Path, data: Mapping[str, Any]) -> None:
    """Atomically write JSON data to ``path``.

    Implementation follows the usual pattern:

    1. Create a temporary file in the target directory.
    2. Serialize JSON, ``flush`` and ``os.fsync``.
    3. ``os.replace`` the temporary file into place.
    """

    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    # Use mkstemp to control directory and ensure the file handle is valid
    # across platforms.
    fd, tmp_name = tempfile.mkstemp(
        dir=str(path.parent), prefix=path.name + ".", suffix=".tmp"
    )
    tmp_path = Path(tmp_name)

    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, sort_keys=True, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, path)
    finally:
        # If something went wrong before ``os.replace``, ensure the temp file
        # is not left behind.
        if tmp_path.exists() and tmp_path != path:
            try:
                tmp_path.unlink()
            except OSError:
                # Best-effort cleanup; failures are not fatal.
                pass


def _swarm_path(config: RuntimeConfig) -> Path:
    return config.metadata_dir / "swarm.json"




# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class MetadataStore:
    """File-based metadata store bound to a specific runtime configuration.

    This helper encapsulates common path and validation logic. It can be
    created from a :class:`RuntimeConfig` and then reused by runtime
    components that need to load or save swarm/agent metadata.
    """

    config: RuntimeConfig

    @property
    def metadata_dir(self) -> Path:
        return self.config.metadata_dir

    @property
    def swarm_id(self) -> str:
        return self.config.swarm_id

    @property
    def project_path(self) -> Path:
        return self.config.project_path

    # -- SwarmState -----------------------------------------------------

    def load_swarm_state(self) -> PersistedSwarmState:
        """Load the persisted :class:`SwarmState` from ``swarm.json``.

        This validates both the Pydantic schema and higher-level
        invariants such as ``project_path`` and ``swarm_id`` matching the
        current :class:`RuntimeConfig`.

        :raises FileNotFoundError: if the swarm state file does not exist.
        :raises ValueError: if the file is malformed or violates
          invariants.
        """

        path = _swarm_path(self.config)
        with path.open("r", encoding="utf-8") as f:
            raw_text = f.read()

        state = PersistedSwarmState.from_json(raw_text)
        state.validate(
            expected_project_path=self.config.project_path,
            expected_swarm_id=self.config.swarm_id,
        )
        return state



    def load_swarm_metadata(self) -> SwarmMetadata:
        """Load and validate :class:`SwarmMetadata` from the persisted state.

        This method remains for compatibility but delegates to
        :class:`SwarmState` as the on-disk source of truth.

        :raises FileNotFoundError: if the swarm metadata file does not
          exist.
        :raises ValueError: if the file is malformed or violates
          invariants.
        """

        state = self.load_swarm_state()
        swarm = _swarm_metadata_from_state(state)
        swarm.validate(
            expected_project_path=self.config.project_path,
            expected_swarm_id=self.config.swarm_id,
        )
        return swarm

    def save_swarm_metadata(self, swarm: SwarmMetadata) -> None:
        """Persist :class:`SwarmMetadata` via a :class:`SwarmState` wrapper.

        Basic validation is performed before writing.
        """

        swarm.validate(
            expected_project_path=self.config.project_path,
            expected_swarm_id=self.config.swarm_id,
        )
        state = _swarm_state_from_metadata(swarm)
        self.save_swarm_state(state)


    # -- AgentState helpers -----------------------------------------------

    def load_agent_state(self, agent_id: str) -> PersistedAgentState:
        """Load state for a single agent from the persisted swarm state.

        :raises FileNotFoundError: if the swarm or the requested agent does
          not exist.
        :raises ValueError: if the file is malformed or violates
          invariants.
        """

        state = self.load_swarm_state()
        try:
            agent_state = state.agents[agent_id]
        except KeyError as exc:
            # Mirror previous behaviour where a missing per-agent file
            # surfaced as ``FileNotFoundError``.
            raise FileNotFoundError(
                f"Agent state not found for {agent_id!r}"
            ) from exc

        # Basic consistency check: the embedded id should match the key.
        if agent_state.agent_id != agent_id:
            raise ValueError(
                f"Agent state for id {agent_id!r} contains agent_id "
                f"{agent_state.agent_id!r}"
            )

        return agent_state

    def save_agent_state(self, agent_state: PersistedAgentState) -> None:
        """Persist state for a single agent via :class:`SwarmState`.

        The caller is expected to have created swarm-level state first; if
        no swarm state exists, this raises :class:`FileNotFoundError`.
        """

        if not agent_state.agent_id:
            raise ValueError("AgentState.agent_id must not be empty")

        try:
            state = self.load_swarm_state()
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                "Swarm state not found; cannot save agent state before "
                "the swarm has been created."
            ) from exc

        # Update or insert the per-agent entry and bump the last-updated
        # timestamp to reflect the change.
        state.agents[agent_state.agent_id] = agent_state
        state.last_updated_at = datetime.utcnow()
        self.save_swarm_state(state)

    def load_all_agent_states(self) -> Dict[str, PersistedAgentState]:
        """Load state for all agents from the persisted swarm state.

        When no swarm state exists, this returns an empty mapping to
        mirror the previous behaviour where a missing ``agents/``
        directory was treated as "no agents".
        """

        try:
            state = self.load_swarm_state()
        except FileNotFoundError:
            return {}

        # Use a regular ``dict`` so callers can mutate independently of the
        # underlying :class:`SwarmState` if they wish.
        return dict(state.agents)

    def save_all_agent_states(
        self, agents: Iterable[PersistedAgentState]
    ) -> None:
        """Persist state for all provided agents in a single swarm update.

        This performs a read-modify-write of :class:`SwarmState`, updating
        or inserting the provided agents and bumping ``last_updated_at``
        once after all changes.
        """

        try:
            state = self.load_swarm_state()
        except FileNotFoundError as exc:
            raise FileNotFoundError(
                "Swarm state not found; cannot save agent state before "
                "the swarm has been created."
            ) from exc

        updated = False
        for agent_state in agents:
            if not agent_state.agent_id:
                raise ValueError("AgentState.agent_id must not be empty")
            state.agents[agent_state.agent_id] = agent_state
            updated = True

        if updated:
            state.last_updated_at = datetime.utcnow()
            self.save_swarm_state(state)

    # -- AgentMetadata compatibility helpers -------------------------------

    def load_agent_metadata(self, agent_id: str) -> AgentMetadata:
        """Load metadata for a single agent from the persisted swarm state.

        :raises FileNotFoundError: if the swarm or the requested agent
          does not exist.
        :raises ValueError: if the file is malformed or violates
          invariants.
        """

        agent_state = self.load_agent_state(agent_id)
        meta = _agent_metadata_from_state(agent_state)
        if meta.agent_id != agent_id:
            raise ValueError(
                f"Agent metadata for id {agent_id!r} contains agent_id {meta.agent_id!r}"
            )
        return meta

    def save_agent_metadata(self, metadata: AgentMetadata) -> None:
        """Persist metadata for a single agent via :class:`SwarmState`.

        The caller is expected to have created swarm-level state first; if
        no swarm state exists, this raises :class:`FileNotFoundError`.
        """

        if not metadata.agent_id:
            raise ValueError("AgentMetadata.agent_id must not be empty")

        # Delegate to the AgentState-based helper so that durable state
        # remains the single source of truth.
        agent_state = _agent_state_from_metadata(metadata)
        self.save_agent_state(agent_state)

    def load_all_agent_metadata(self) -> Dict[str, AgentMetadata]:
        """Load metadata for all agents from the persisted swarm state.

        When no swarm metadata exists, this returns an empty mapping to
        mirror the previous behaviour where a missing ``agents/``
        directory was treated as "no agents".
        """

        try:
            states = self.load_all_agent_states()
        except FileNotFoundError:
            return {}

        result: Dict[str, AgentMetadata] = {}
        for agent_id, agent_state in states.items():
            meta = _agent_metadata_from_state(agent_state)
            result[meta.agent_id] = meta
        return result

    def save_all_agent_metadata(self, agents: Iterable[AgentMetadata]) -> None:
        """Persist metadata for all provided agents.

        Each agent is written independently using the same atomic
        semantics as :meth:`save_agent_metadata`.
        """

        for meta in agents:
            self.save_agent_metadata(meta)

    def save_swarm_state(self, state: PersistedSwarmState) -> None:
        """Persist :class:`SwarmState` to ``swarm.json`` atomically."""

        # Use Pydantic's JSON-oriented dump mode so that datetimes, paths,
        # and nested models (including NateOhaConfig) are converted to plain
        # JSON-serializable values before writing.
        _atomic_write_json(_swarm_path(self.config), state.model_dump(mode="json"))

