"""Project-local swarm metadata persistence layer.

This module implements a small, file-based metadata store used by the
runtime daemon to persist swarm and per-agent metadata under the
project's metadata directory (for example, ``.nate_ntm/``).

It is intentionally conservative:

* JSON files only, with explicit dataclasses for :class:`SwarmMetadata`
  and :class:`AgentMetadata`.
* Atomic write semantics for all persistence operations (write to a
  temporary file in the same directory, flush/fsync, then rename into
  place) to avoid partially written metadata files (T038 / FR-014).
* Basic validation to ensure that loaded metadata is consistent with the
  current :class:`~nate_ntm.config.runtime_config.RuntimeConfig`.

Layout (see ``data-model.md`` §2.3):

.. code-block:: text

    .nate_ntm/
    ├── swarm.json             # SwarmMetadata (top-level)
    └── agents/
        ├── <agent_id>.json    # Individual AgentMetadata records
        └── ...

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

__all__ = [
    "AgentMetadata",
    "SwarmMetadata",
    "MetadataStore",
]

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class AgentMetadata:
    """Persisted configuration and identity for a single agent.

    Field names and semantics follow ``data-model.md`` §2.2. Some
    nested configuration objects are represented as opaque mappings to
    keep this layer independent of higher-level policy.
    """

    agent_id: str
    display_name: str

    role: str | None = None

    agent_mail_identity: str = ""
    agent_mail_credentials_ref: str = ""

    conversation_id: str = ""

    launch_config: Mapping[str, Any] = field(default_factory=dict)
    model: str | None = None
    task_description: str | None = None

    restart_policy: Mapping[str, Any] = field(default_factory=dict)

    # Snapshot of last persisted status (e.g. "Idle", "Running", "Failed").
    last_known_status: str = "Idle"

    def to_dict(self) -> Dict[str, Any]:
        """Render as a JSON-serializable mapping."""

        return {
            "agent_id": self.agent_id,
            "display_name": self.display_name,
            "role": self.role,
            "agent_mail_identity": self.agent_mail_identity,
            "agent_mail_credentials_ref": self.agent_mail_credentials_ref,
            "conversation_id": self.conversation_id,
            "launch_config": dict(self.launch_config),
            "model": self.model,
            "task_description": self.task_description,
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
            launch_config=dict(data.get("launch_config", {}) or {}),
            model=data.get("model"),
            task_description=data.get("task_description"),
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

    def validate(self, *, expected_project_path: Path | None = None, expected_swarm_id: str | None = None) -> None:
        """Validate basic invariants.

        * ``project_path`` MUST match ``expected_project_path`` (if given).
        * ``swarm_id`` MUST match ``expected_swarm_id`` (if given).
        * ``agents`` MUST have unique identifiers (enforced during parsing).
        """

        if expected_swarm_id is not None and self.swarm_id != expected_swarm_id:
            raise ValueError(
                f"SwarmMetadata.swarm_id {self.swarm_id!r} does not match expected {expected_swarm_id!r}"
            )

        if expected_project_path is not None:
            expected_resolved = expected_project_path.expanduser().resolve()
            if self.project_path != expected_resolved:
                raise ValueError(
                    "SwarmMetadata.project_path does not match the current project "
                    f"directory: {self.project_path!r} != {expected_resolved!r}"
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


def _agents_dir(config: RuntimeConfig) -> Path:
    return config.metadata_dir / "agents"


def _agent_path(config: RuntimeConfig, agent_id: str) -> Path:
    return _agents_dir(config) / f"{agent_id}.json"


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

    # -- SwarmMetadata -----------------------------------------------------

    def load_swarm_metadata(self) -> SwarmMetadata:
        """Load and validate :class:`SwarmMetadata` from ``swarm.json``.

        :raises FileNotFoundError: if the swarm metadata file does not
          exist.
        :raises ValueError: if the file is malformed or violates
          invariants.
        """

        path = _swarm_path(self.config)
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        swarm = SwarmMetadata.from_dict(raw)
        swarm.validate(
            expected_project_path=self.config.project_path,
            expected_swarm_id=self.config.swarm_id,
        )
        return swarm

    def save_swarm_metadata(self, swarm: SwarmMetadata) -> None:
        """Persist :class:`SwarmMetadata` to ``swarm.json`` atomically.

        Basic validation is performed before writing.
        """

        swarm.validate(
            expected_project_path=self.config.project_path,
            expected_swarm_id=self.config.swarm_id,
        )
        _atomic_write_json(_swarm_path(self.config), swarm.to_dict())

    # -- AgentMetadata -----------------------------------------------------

    def load_agent_metadata(self, agent_id: str) -> AgentMetadata:
        """Load metadata for a single agent.

        :raises FileNotFoundError: if the agent metadata file does not
          exist.
        :raises ValueError: if the file is malformed or violates
          invariants.
        """

        path = _agent_path(self.config, agent_id)
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)

        meta = AgentMetadata.from_dict(raw)
        if meta.agent_id != agent_id:
            raise ValueError(
                f"Agent metadata file {path} contains agent_id {meta.agent_id!r}, "
                f"expected {agent_id!r}"
            )
        return meta

    def save_agent_metadata(self, metadata: AgentMetadata) -> None:
        """Persist metadata for a single agent atomically."""

        if not metadata.agent_id:
            raise ValueError("AgentMetadata.agent_id must not be empty")
        _atomic_write_json(_agent_path(self.config, metadata.agent_id), metadata.to_dict())

    def load_all_agent_metadata(self) -> Dict[str, AgentMetadata]:
        """Load metadata for all agents under ``agents/``.

        Missing directories are treated as empty (no agents).
        """

        agents_dir = _agents_dir(self.config)
        if not agents_dir.exists():
            return {}

        result: Dict[str, AgentMetadata] = {}
        for path in sorted(agents_dir.glob("*.json")):
            meta = self.load_agent_metadata(path.stem)
            result[meta.agent_id] = meta
        return result

    def save_all_agent_metadata(self, agents: Iterable[AgentMetadata]) -> None:
        """Persist metadata for all provided agents.

        Each agent is written independently using the same atomic
        semantics as :meth:`save_agent_metadata`.
        """

        for meta in agents:
            self.save_agent_metadata(meta)
