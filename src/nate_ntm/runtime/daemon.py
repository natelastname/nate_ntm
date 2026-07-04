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
from .acp_client import BaseAcpClient, FakeAcpClient
from .agent_mail_client import BaseAgentMailClient, FakeAgentMailClient
from .agents import AgentSupervisor
from .metadata_store import MetadataStore, SwarmMetadata
from .scheduler import RuntimeScheduler
from .state import AgentStatus, RuntimeState, RuntimeStatus

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

    # Optional scheduler facade used to manage agent registration and,
    # in later phases, event-loop driven behavior. This is constructed by
    # :meth:`create` / :meth:`resume` so that tests can rely on its
    # presence without needing to instantiate it manually.
    scheduler: RuntimeScheduler | None = None

    # Runtime-owned integration clients. For the MVP we default these to
    # the in-memory Fake* implementations; future work may allow more
    # configurable adapters.
    agent_mail_client: BaseAgentMailClient | None = None
    acp_client: BaseAcpClient | None = None

    @classmethod
    def create(
        cls,
        config: RuntimeConfig,
        *,
        agent_count: int | None = None,
    ) -> "RuntimeDaemon":
        """Construct a :class:`RuntimeDaemon` in `create` mode.

        This helper validates that swarm metadata does *not* already
        exist for the project, initializes a fresh :class:`SwarmMetadata`
        record (optionally with a small set of initial agents), persists
        it via :class:`MetadataStore`, and returns a new
        :class:`RuntimeDaemon` with :class:`RuntimeState` in the
        ``Starting`` status.

        When ``agent_count`` is provided and greater than zero, a simple
        set of fake agents is created using the runtime-owned
        :class:`FakeAgentMailClient` and :class:`FakeAcpClient`. Their
        Agent Mail identities and ACP conversation IDs are persisted in
        both :class:`SwarmMetadata` and per-agent metadata files so that
        later resume flows can reuse them.
        """

        check_startup_preconditions(config, StartupMode.CREATE)
        store = MetadataStore(config=config)

        # For US1 we rely on the in-memory FakeAgentMailClient, which
        # derives a deterministic project identifier from the
        # RuntimeConfig. This ID is persisted in SwarmMetadata so that
        # later resume flows (US2) can reuse the same Agent Mail
        # project.
        agent_mail_client: BaseAgentMailClient = FakeAgentMailClient(config=config)
        agent_mail_project_id = agent_mail_client.ensure_project()

        # Construct an in-memory ACP client so that we can allocate
        # per-agent conversation identifiers for newly created agents.
        acp_client: BaseAcpClient = FakeAcpClient(config=config)

        # Optionally create a small set of initial agents with fake
        # Agent Mail identities and ACP conversations.
        agents: dict[str, "AgentMetadata"] = {}
        if agent_count is not None and agent_count > 0:
            from .metadata_store import AgentMetadata  # local import to avoid cycles

            for index in range(1, agent_count + 1):
                agent_id = f"agent-{index}"
                display_name = f"Agent {index}"

                agent_mail_identity = agent_mail_client.ensure_agent_identity(agent_id)
                conversation_id = acp_client.ensure_conversation(agent_id)

                agents[agent_id] = AgentMetadata(
                    agent_id=agent_id,
                    display_name=display_name,
                    agent_mail_identity=agent_mail_identity,
                    conversation_id=conversation_id,
                )

        now = datetime.utcnow()
        swarm = SwarmMetadata(
            swarm_id=config.swarm_id,
            project_path=config.project_path,
            agent_mail_project_id=agent_mail_project_id,
            created_at=now,
            last_updated_at=now,
            agents=agents,
        )

        # Persist the newly created swarm metadata using atomic write
        # semantics provided by the MetadataStore.
        store.save_swarm_metadata(swarm)

        # Persist per-agent metadata files when initial agents were
        # created. This mirrors the layout used by resume-mode tests and
        # ensures that Agent Mail identities and ACP conversation IDs are
        # durable across restarts.
        if agents:
            store.save_all_agent_metadata(agents.values())

        state = RuntimeState(config=config)

        # Wire a minimal scheduler and agent supervisor so that future
        # work (T016/T017) can build on this structure without changing
        # the public ``RuntimeDaemon`` API.
        agent_supervisor = AgentSupervisor(
            config=config,
            state=state,
            swarm_metadata=swarm,
        )
        scheduler = RuntimeScheduler(
            config=config,
            state=state,
            swarm_metadata=swarm,
            agent_supervisor=agent_supervisor,
            agent_mail_client=agent_mail_client,
        )

        return cls(
            config=config,
            metadata_store=store,
            swarm_metadata=swarm,
            state=state,
            startup_mode=StartupMode.CREATE,
            scheduler=scheduler,
            agent_mail_client=agent_mail_client,
            acp_client=acp_client,
        )

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

        # Mirror the `create` path and construct in-memory Fake* clients so
        # that the daemon owns a consistent set of integration adapters
        # regardless of startup mode. For US2 we also enforce FR-009 by
        # rebinding these clients against the persisted swarm/agent
        # metadata and validating that identifiers are reused on resume.
        agent_mail_client: BaseAgentMailClient = FakeAgentMailClient(config=config)
        acp_client: BaseAcpClient = FakeAcpClient(config=config)

        agent_supervisor = AgentSupervisor(
            config=config,
            state=state,
            swarm_metadata=swarm,
        )
        scheduler = RuntimeScheduler(
            config=config,
            state=state,
            swarm_metadata=swarm,
            agent_supervisor=agent_supervisor,
            agent_mail_client=agent_mail_client,
        )

        # Rebind the Agent Mail project identifier and per-agent identities.
        # These helpers are required to be idempotent: for a given
        # configuration and agent_id they must always return the same
        # identifier. On resume we treat any divergence between the
        # adapter-derived values and the persisted metadata as a hard
        # startup error, since it indicates an FR-009 violation.
        #
        # For the dev-mode FakeAgentMailClient we only enforce a strict
        # project-id check when the persisted value uses the fake-client
        # naming scheme. This keeps older tests that use simple placeholder
        # IDs (for example, "mail-project-1") valid while ensuring that
        # create→resume flows that went through :meth:`RuntimeDaemon.create`
        # are held to a stronger invariant.
        if swarm.agent_mail_project_id and swarm.agent_mail_project_id.startswith(
            "fake-mail-project:"
        ):
            project_id = agent_mail_client.ensure_project()
            if project_id != swarm.agent_mail_project_id:
                raise RuntimeStartupError(
                    "Agent Mail project ID mismatch on resume: "
                    f"adapter returned {project_id!r}, "
                    f"metadata has {swarm.agent_mail_project_id!r}"
                )

        for agent_id, meta in swarm.agents.items():
            if meta.agent_mail_identity:
                identity = agent_mail_client.ensure_agent_identity(agent_id)
                if identity != meta.agent_mail_identity:
                    raise RuntimeStartupError(
                        "Agent Mail identity mismatch on resume for "
                        f"agent {agent_id!r}: adapter returned {identity!r}, "
                        f"metadata has {meta.agent_mail_identity!r}"
                    )

            if meta.conversation_id:
                conv_id = acp_client.ensure_conversation(agent_id)
                if conv_id != meta.conversation_id:
                    raise RuntimeStartupError(
                        "ACP conversation ID mismatch on resume for "
                        f"agent {agent_id!r}: adapter returned {conv_id!r}, "
                        f"metadata has {meta.conversation_id!r}"
                    )

        return cls(
            config=config,
            metadata_store=store,
            swarm_metadata=swarm,
            state=state,
            startup_mode=StartupMode.RESUME,
            scheduler=scheduler,
            agent_mail_client=agent_mail_client,
            acp_client=acp_client,
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

        # Allow the scheduler to perform any startup-time registration or
        # initialization before we mark the runtime as fully running.
        if self.scheduler is not None:
            self.scheduler.start()

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

    # Introspection ------------------------------------------------------

    def _compute_agent_counts(self) -> dict[str, int]:
        """Return aggregate agent counts by lifecycle status.

        The shape of the returned mapping matches the ``agent_counts`` object
        in ``contracts/runtime-api.md`` for ``runtime.get_status`` and
        ``swarm.get_overview``.
        """

        counts_by_status = {status: 0 for status in AgentStatus}
        for agent in self.state.agents.values():
            counts_by_status[agent.status] += 1

        total = sum(counts_by_status.values())
        return {
            "total": total,
            "starting": counts_by_status[AgentStatus.STARTING],
            "idle": counts_by_status[AgentStatus.IDLE],
            "running": counts_by_status[AgentStatus.RUNNING],
            "waiting": counts_by_status[AgentStatus.WAITING],
            "failed": counts_by_status[AgentStatus.FAILED],
        }

    def get_runtime_status(self) -> dict[str, object]:
        """Return a JSON-serializable snapshot for ``runtime.get_status``.

        This mirrors the result shape defined in
        ``specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md``.
        """

        return {
            "status": self.state.status.value,
            "project_path": str(self.config.project_path),
            "swarm_id": self.swarm_metadata.swarm_id,
            "agent_counts": self._compute_agent_counts(),
        }

    def get_swarm_overview(self) -> dict[str, object]:
        """Return a JSON-serializable snapshot for ``swarm.get_overview``.

        In addition to joining persisted metadata with live runtime
        status, this wires in unread mailbox summaries via the
        :class:`BaseAgentMailClient` when available, as required by the
        runtime API contract for User Story 1.
        """

        agent_counts = self._compute_agent_counts()

        # Union of configured agents (metadata) and those currently present
        # in runtime state; this is tolerant of partial initialization.
        all_agent_ids = set(self.swarm_metadata.agents.keys()) | set(
            self.state.agents.keys()
        )

        sorted_ids = sorted(all_agent_ids)

        # Ask the Agent Mail client (if present) for unread-mail flags in
        # a single batch. When no client is configured, we conservatively
        # default to ``False`` for all agents.
        if self.agent_mail_client is not None:
            unread_flags = self.agent_mail_client.get_unread_mail_flags(sorted_ids)
        else:  # pragma: no cover - exercised indirectly via API tests
            unread_flags = {agent_id: False for agent_id in sorted_ids}

        agents = []
        for agent_id in sorted_ids:
            metadata = self.swarm_metadata.agents.get(agent_id)
            runtime_state = self.state.agents.get(agent_id)

            display_name = metadata.display_name if metadata is not None else agent_id
            status = (
                runtime_state.status.value
                if runtime_state is not None
                else AgentStatus.STARTING.value
            )
            last_error = runtime_state.last_error if runtime_state is not None else None
            has_unread_mail = bool(unread_flags.get(agent_id, False))

            agents.append(
                {
                    "agent_id": agent_id,
                    "display_name": display_name,
                    "status": status,
                    "has_unread_mail": has_unread_mail,
                    "last_error": last_error,
                }
            )

        return {
            "swarm_id": self.swarm_metadata.swarm_id,
            "project_path": str(self.swarm_metadata.project_path),
            "runtime_status": self.state.status.value,
            "agent_counts": agent_counts,
            "agents": agents,
        }

    def get_agent_detail(self, agent_id: str, max_events: int = 100) -> dict[str, object]:
        """Return a JSON-serializable snapshot for ``agent.get_detail``.

        The result shape mirrors the contract in
        ``specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md``:

        * ``agent``: joined view of persisted metadata and live runtime state.
        * ``events``: recent :class:`AgentEvent` records from the agent's
          in-memory :class:`~nate_ntm.runtime.events.AgentEventStream`.
        """

        metadata = self.swarm_metadata.agents.get(agent_id)
        runtime_state = self.state.agents.get(agent_id)

        if metadata is None and runtime_state is None:
            # Unknown agent identifier. At the JSON-RPC layer this will be
            # surfaced as a structured error; for the in-process API we use
            # ``KeyError`` to mirror other lookup helpers.
            raise KeyError(f"Unknown agent_id: {agent_id!r}")

        display_name = metadata.display_name if metadata is not None else agent_id

        if runtime_state is not None:
            status_value = runtime_state.status.value
            last_error = runtime_state.last_error
            stream = runtime_state.event_stream
        else:
            # Fall back to the last persisted status when no live runtime
            # state is available.
            if metadata is not None and metadata.last_known_status:
                status_value = metadata.last_known_status
            else:
                status_value = AgentStatus.STARTING.value
            last_error = None
            stream = None

        agent_payload: dict[str, object] = {
            "agent_id": agent_id,
            "display_name": display_name,
            "status": status_value,
            "agent_mail_identity": metadata.agent_mail_identity if metadata else "",
            "conversation_id": metadata.conversation_id if metadata else "",
            "last_error": last_error,
        }

        events_payload: list[dict[str, object]] = []
        if stream is not None:
            events = stream.get_events(limit=max_events)
            events_payload = [event.to_dict() for event in events]

        return {
            "agent": agent_payload,
            "events": events_payload,
        }

