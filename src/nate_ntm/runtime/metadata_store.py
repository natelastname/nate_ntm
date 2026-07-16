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
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping

from ..config.runtime_config import RuntimeConfig
from .swarm_state import AgentState as PersistedAgentState, SwarmState as PersistedSwarmState

__all__ = [
    "MetadataStore",
]

# Durable state is represented exclusively by the Pydantic models
# :class:`SwarmState` and :class:`AgentState` from
# :mod:`nate_ntm.runtime.swarm_state`. This module provides a thin,
# file-based adapter around those models.

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

    def save_swarm_state(self, state: PersistedSwarmState) -> None:
        """Persist :class:`SwarmState` to ``swarm.json`` atomically."""

        # Use Pydantic's JSON-oriented dump mode so that datetimes, paths,
        # and nested models (including NateOhaConfig) are converted to plain
        # JSON-serializable values before writing.
        _atomic_write_json(_swarm_path(self.config), state.model_dump(mode="json"))

