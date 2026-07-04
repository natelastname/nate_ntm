"""Runtime daemon entrypoint and startup semantics.

This module defines a small `RuntimeDaemon` entrypoint class that wires
`together:

* :class:`~nate_ntm.config.runtime_config.RuntimeConfig`
* :class:`~nate_ntm.runtime.metadata_store.MetadataStore`
* :class:`~nate_ntm.runtime.metadata_store.SwarmMetadata`
* :class:`~nate_ntm.runtime.state.RuntimeState`

It also codifies explicit `create` vs `resume` startup semantics in a
way that the CLI can build on (see tasks T008 and T037):

* In **`create`** mode, starting the runtime MUST fail if swarm
  metadata already exists for the project unless a higher-level caller
  explicitly opts into overwrite or reuse behavior.
* In **`resume`** mode, starting the runtime MUST fail if required
  swarm metadata is missing.

Higher-level tasks (for example, T013 and later user stories) are
responsible for actually creating new `SwarmMetadata`/`AgentMetadata`
records in `create` mode and for wiring in the scheduler, ACP, and Agent
Mail integrations. This module focuses on safe, testable orchestration
and lifecycle state transitions.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from ..config.runtime_config import RuntimeConfig
from .metadata_store import MetadataStore, SwarmMetadata
from .state import RuntimeState, RuntimeStatus

__all__ = [
    "StartupMode",
    "RuntimeStartupError",
    "MetadataAlreadyExistsError",
    "MetadataMissingError",
    "RuntimeDaemon",
    "check_startup_preconditions",
]


class StartupMode(str, Enum):
    """Explicit startup modes for the runtime daemon.

    These correspond to the CLI `--mode` values described in tasks T008
    and T037.
    """

    CREATE = "create"
    RESUME = "resume"


class RuntimeStartupError(RuntimeError):
    """Base class for startup/precondition failures."""


class MetadataAlreadyExistsError(RuntimeStartupError):
    """Raised when `mode=create` is requested but metadata already exists."""


class MetadataMissingError(RuntimeStartupError):
    """Raised when `mode=resume` is requested but required metadata is missing."""


def _swarm_metadata_path(config: RuntimeConfig) -> Path:
    """Return the expected path to `swarm.json` for `config`.

    This mirrors the layout used by :class:`MetadataStore` without
    importing its private helpers.
    """

    return config.metadata_dir / "swarm.json"


def check_startup_preconditions(config: RuntimeConfig, mode: StartupMode) -> None:
    """Validate `create` vs `resume` semantics for the given `config`.

    * For :data:`StartupMode.CREATE`, this raises
      :class:`MetadataAlreadyExistsError` if swarm metadata already
      exists under the project's metadata directory.
    * For :data:`StartupMode.RESUME`, this raises
      :class:`MetadataMissingError` if swarm metadata does not exist.

    This function is deliberately small and side-effect free so it can
    be exercised directly in unit tests and reused by the CLI layer.
    """

    swarm_path = _swarm_metadata_path(config)

    if mode is StartupMode.CREATE:
        if swarm_path.exists():
            raise MetadataAlreadyExistsError(
                f"Swarm metadata already exists at {swarm_path}; refusing to "
                "start in create mode without an explicit override."
            )
    elif mode is StartupMode.RESUME:
        if not swarm_path.exists():
            raise MetadataMissingError(
                f"Swarm metadata not found at {swarm_path}; cannot resume a "
                "swarm that has not been created."
            )
    else:  # pragma: no cover - defensive against future Enum variants
        raise RuntimeStartupError(f"Unsupported startup mode: {mode!r}")


@dataclass(slots=True)
class RuntimeDaemon:
    """Core runtime daemon entrypoint.

    At this stage (Phase 2), the daemon focuses on owning the resolved
    configuration, loaded swarm metadata, and top-level runtime state,
    plus explicit lifecycle transitions (`start` and `shutdown`).

    Scheduler wiring, ACP connections, Agent Mail polling, and control
    API integration are introduced in later tasks.
    """

    config: RuntimeConfig
    metadata_store: MetadataStore
    swarm_metadata: SwarmMetadata
    state: RuntimeState

    startup_mode: StartupMode
    """Startup mode used to construct this daemon (create or resume)."""

    started_at: Optional[datetime] = None
    """Timestamp when :meth:`start` was last called, if ever."""

    @classmethod
    def resume(cls, config: RuntimeConfig) -> "RuntimeDaemon":
        """Construct a :class:`RuntimeDaemon` in `resume` mode.

        This helper validates that swarm metadata exists and is
        consistent with the provided configuration, then initializes a
        fresh :class:`RuntimeState` in the `Starting` status.
        """

        check_startup_preconditions(config, StartupMode.RESUME)
        store = MetadataStore(config=config)
        swarm = store.load_swarm_metadata()

        state = RuntimeState(config=config)
        # The scheduler and agent runtime states will be wired in by
        # later tasks; for now we only track high-level runtime status.

        return cls(
            config=config,
            metadata_store=store,
            swarm_metadata=swarm,
            state=state,
            startup_mode=StartupMode.RESUME,
        )

    # Lifecycle ---------------------------------------------------------

    def start(self) -> None:
        """Mark the runtime as running.

        In later phases this will also start the scheduler/event loop and
        initialize agents. For Phase 2 we restrict ourselves to state
        transitions that are easy to unit test.
        """

        if self.state.status is not RuntimeStatus.STARTING:
            # We allow idempotent `start()` when already running but
            # reject obviously invalid transitions.
            if self.state.status is RuntimeStatus.RUNNING:
                return
            raise RuntimeStartupError(
                f"Cannot start runtime from status {self.state.status!r}"
            )

        self.state.status = RuntimeStatus.RUNNING
        self.started_at = datetime.utcnow()

    def request_shutdown(self) -> None:
        """Request a graceful shutdown.

        This mirrors the semantics of `runtime.shutdown` in the control
        API contract at a high level: mark the runtime as shutting down
        and set a flag that can be observed by the event loop.
        """

        if self.state.status in {RuntimeStatus.STOPPED, RuntimeStatus.FAILED}:
            # Nothing to do; treat as idempotent.
            return

        self.state.shutdown_requested = True

        if self.state.status is RuntimeStatus.RUNNING:
            self.state.status = RuntimeStatus.SHUTTING_DOWN

    def mark_stopped(self) -> None:
        """Mark the runtime as fully stopped.

        In a full implementation this would be called once all agents
        have terminated and the scheduler has completed cleanup.
        """

        self.state.status = RuntimeStatus.STOPPED
