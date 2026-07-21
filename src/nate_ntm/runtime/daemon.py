"""Runtime daemon entrypoint and startup semantics.

This module defines a small :class:`RuntimeDaemon` entrypoint class that
wires together:

* :class:`~nate_ntm.config.runtime_config.RuntimeConfig`
* :class:`~nate_ntm.runtime.metadata_store.MetadataStore`
* :class:`~nate_ntm.runtime.swarm_state.SwarmState`
* :class:`~nate_ntm.runtime.state.RuntimeState`

It also codifies explicit ``create`` vs ``resume`` startup semantics in
a way that the CLI can build on (see tasks T008 and T037):

* In **``create``** mode, starting the runtime MUST fail if swarm state
  already exists for the project unless a higher-level caller explicitly
  opts into overwrite or reuse behavior.
* In **``resume``** mode, starting the runtime MUST fail if required
  swarm state is missing.

Higher-level tasks (for example, T013 and later user stories) are
responsible for actually creating or populating new
:class:`SwarmState`/:class:`AgentState` records in ``create`` mode and
for wiring in the scheduler, ACP, and Agent Mail integrations. This
module focuses on safe, testable orchestration and lifecycle state
transitions.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from ..config.runtime_config import RuntimeConfig
from .acp_client import BaseAcpClient
from .adapters import RuntimeAdapters, create_runtime_adapters
from .agent_mail_client import BaseAgentMailClient, McpAgentMailClient
from .agents import AgentSupervisor
from .metadata_store import MetadataStore
from .nate_oha_launch import build_effective_nate_oha_config
from .scheduler import RuntimeScheduler
from .state import AgentStatus, RuntimeState, RuntimeStatus
from .swarm_state import AgentState, SwarmState

__all__ = [
    "StartupMode",
    "RuntimeStartupError",
    "MetadataAlreadyExistsError",
    "MetadataMissingError",
    "RuntimeDaemon",
    "check_startup_preconditions",
]

logger = logging.getLogger(__name__)


def _map_acp_state_to_last_known_status(acp_state: str) -> str | None:
    """Map adapter-level ACP state to ``AgentState.last_known_status``.

    This keeps the persisted string representation aligned with
    :class:`AgentStatus` while remaining tolerant of adapter-specific
    vocabularies. Only stable, high-level states are mapped; transitional
    or unknown values are ignored so they do not overwrite a more useful
    snapshot.
    """

    state = (acp_state or "").strip().lower()
    if not state:
        return None

    if state == "running":
        return AgentStatus.RUNNING.value

    # Treat terminated/idle adapter-level states as "Idle" from the
    # runtime's point of view so that callers see a simple snapshot.
    if state in {"terminated", "idle"}:
        return AgentStatus.IDLE.value

    if state == "failed":
        return AgentStatus.FAILED.value

    # For other values (for example, "starting", "stopping", "unknown"),
    # fall back to any previously persisted status.
    return None



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


def _swarm_state_path(config: RuntimeConfig) -> Path:
    """Return the expected path to ``swarm.json`` for ``config``.

    This mirrors the layout used by :class:`MetadataStore` without
    importing its private helpers.
    """

    return config.metadata_dir / "swarm.json"


def check_startup_preconditions(config: RuntimeConfig, mode: StartupMode) -> None:
    """Validate ``create`` vs ``resume`` semantics for the given ``config``.

    * For :data:`StartupMode.CREATE`, this raises
      :class:`MetadataAlreadyExistsError` if swarm state already exists
      under the project's metadata directory.
    * For :data:`StartupMode.RESUME`, this raises
      :class:`MetadataMissingError` if swarm state does not exist.

    This function is deliberately small and side-effect free so it can
    be exercised directly in unit tests and reused by the CLI layer.
    """

    swarm_path = _swarm_state_path(config)

    logger.debug(
        "check_startup_preconditions",
        extra={
            "mode": mode.value,
            "swarm_state_path": str(swarm_path),
        },
    )

    if mode is StartupMode.CREATE:
        if swarm_path.exists():
            logger.error(
                "startup_precondition_metadata_already_exists",
                extra={
                    "mode": mode.value,
                    "swarm_state_path": str(swarm_path),
                },
            )
            raise MetadataAlreadyExistsError(
                f"Swarm state already exists at {swarm_path}; refusing to "
                "start in create mode without an explicit override."
            )
    elif mode is StartupMode.RESUME:
        if not swarm_path.exists():
            logger.error(
                "startup_precondition_metadata_missing",
                extra={
                    "mode": mode.value,
                    "swarm_state_path": str(swarm_path),
                },
            )
            raise MetadataMissingError(
                f"Swarm state not found at {swarm_path}; cannot resume a "
                "swarm that has not been created."
            )
    else:  # pragma: no cover - defensive against future Enum variants
        logger.error(
            "startup_precondition_unsupported_mode",
            extra={
                "mode": str(mode),
                "swarm_state_path": str(swarm_path),
            },
        )
        raise RuntimeStartupError(f"Unsupported startup mode: {mode!r}")


@dataclass(slots=True)
class RuntimeDaemon:
    """Core runtime daemon entrypoint.

    At this stage (Phase 2), the daemon focuses on owning the resolved
    configuration, loaded swarm state, and top-level runtime state, plus
    explicit lifecycle transitions (``start`` and ``shutdown``).

    Scheduler wiring, ACP connections, Agent Mail polling, and control
    API integration are introduced in later tasks.
    """

    config: RuntimeConfig
    metadata_store: MetadataStore
    swarm_state: SwarmState
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

    # Runtime-owned integration clients. These are typically constructed
    # once per process (or logical runtime instance) and reused for the
    # lifetime of the daemon. In current phases these are production
    # adapters wired through :mod:`nate_ntm.runtime.adapters`.
    agent_mail_client: BaseAgentMailClient | None = None
    acp_client: BaseAcpClient | None = None

    @classmethod
    def create(
        cls,
        config: RuntimeConfig,
        *,
        agent_count: int | None = None,
        adapters: RuntimeAdapters | None = None,
    ) -> "RuntimeDaemon":
        """Construct a :class:`RuntimeDaemon` in ``create`` mode.

        This helper validates that swarm state does *not* already exist
        for the project, initializes a fresh :class:`SwarmState` record
        (optionally with a small set of initial agents), persists it via
        :class:`MetadataStore`, and returns a new :class:`RuntimeDaemon`
        with :class:`RuntimeState` in the ``Starting`` status.

        When ``agent_count`` is provided and greater than zero, a simple
        set of agents is created using the runtime-owned Agent Mail and
        ACP adapters supplied via ``adapters`` (for the MVP, the
        in-memory fake implementations). Their Agent Mail identities and
        ACP conversation IDs are persisted in the resulting
        :class:`SwarmState`/:class:`AgentState` records so that later
        resume flows can reuse them.
        """

        check_startup_preconditions(config, StartupMode.CREATE)
        logger.info(
            "runtime_create",
            extra={
                "swarm_id": config.swarm_id,
                "project_path": str(config.project_path),
                "adapter_mode": getattr(getattr(config, "adapter_mode", None), "value", None),
                "agent_count": agent_count,
            },
        )

        store = MetadataStore(config=config)

        if adapters is None:
            adapters = create_runtime_adapters(config)

        agent_mail_client: BaseAgentMailClient = adapters.agent_mail
        agent_mail_project_id = agent_mail_client.ensure_project()

        # Use the configured ACP adapter so that conversation identifiers
        # are derived consistently with later resume flows.
        acp_client: BaseAcpClient = adapters.acp

        # Optionally create a small set of initial agents with Agent Mail
        # identities allocated through the selected adapters. ACP
        # conversations/sessions are established lazily when an ACP
        # lifecycle operation (for example, ``start_agent_async``) is
        # invoked for the agent.
        agents: dict[str, AgentState] = {}
        if agent_count is not None and agent_count > 0:
            # Milestone 2 requires that every persisted AgentState carries a
            # fully resolved NateOhaConfig. When initial agents are requested
            # we therefore treat the nate-oha base configuration and runtime
            # mode as mandatory inputs rather than optional hints.
            if config.nate_oha_config_path is None or not config.nate_oha_runtime_mode:
                raise RuntimeStartupError(
                    "RuntimeConfig.nate_oha_config_path and RuntimeConfig.nate_oha_runtime_mode "
                    "must be set when creating initial agents; they are required to "
                    "derive a persisted NateOhaConfig for each agent."
                )

            for index in range(1, agent_count + 1):
                agent_id = f"agent-{index}"
                display_name = f"Agent {index}"

                # Allocate a stable Agent Mail identity + optional credentials
                # for this agent via the runtime-owned adapter. These values
                # are then embedded into the derived NateOhaConfig instead of
                # being stored as separate AgentState fields.
                agent_mail_identity, agent_mail_credentials_ref = (
                    agent_mail_client.ensure_agent_identity_with_credentials(agent_id)
                )

                try:
                    nate_oha_config = build_effective_nate_oha_config(
                        config=config,
                        agent_mail_identity=agent_mail_identity,
                        agent_mail_credentials_ref=agent_mail_credentials_ref,
                    )
                except ValueError as exc:
                    raise RuntimeStartupError(
                        f"Failed to build NateOhaConfig for agent {agent_id!r}: {exc}"
                    ) from exc

                agents[agent_id] = AgentState(
                    agent_id=agent_id,
                    display_name=display_name,
                    nate_oha_config=nate_oha_config,
                )


        now = datetime.utcnow()
        swarm = SwarmState(
            swarm_id=config.swarm_id,
            project_path=config.project_path,
            agent_mail_project_id=agent_mail_project_id,
            created_at=now,
            last_updated_at=now,
            agents=agents,
        )

        # Persist the newly created swarm state using atomic write
        # semantics provided by the MetadataStore.
        store.save_swarm_state(swarm)

        state = RuntimeState(config=config)

        # Wire a minimal scheduler and agent supervisor so that future
        # work (T016/T017) can build on this structure without changing
        # the public ``RuntimeDaemon`` API.
        agent_supervisor = AgentSupervisor(
            config=config,
            state=state,
            swarm_state=swarm,
        )
        scheduler = RuntimeScheduler(
            config=config,
            state=state,
            swarm_state=swarm,
            agent_supervisor=agent_supervisor,
            agent_mail_client=agent_mail_client,
        )

        # Wire the ACP adapter's event callback into the AgentSupervisor so
        # that adapter-emitted AgentEvent instances are appended to the
        # in-memory per-agent event streams and forwarded to any configured
        # listeners (for example, the WebSocket control API bridge).
        acp_client.on_event = agent_supervisor.append_agent_event

        return cls(
            config=config,
            metadata_store=store,
            swarm_state=swarm,
            state=state,
            startup_mode=StartupMode.CREATE,
            scheduler=scheduler,
            agent_mail_client=agent_mail_client,
            acp_client=acp_client,
        )

    @classmethod
    def resume(
        cls,
        config: RuntimeConfig,
        *,
        adapters: RuntimeAdapters | None = None,
    ) -> "RuntimeDaemon":
        """Construct a :class:`RuntimeDaemon` in ``resume`` mode.

        This helper validates that swarm state exists and is consistent
        with the provided configuration, then initializes a fresh
        :class:`RuntimeState` in the ``Starting`` status.

        The same adapter instances (or compatible equivalents) that were
        used during :meth:`create` should be supplied via ``adapters`` so
        that identifiers derived from the integration layer (for example,
        Agent Mail project IDs and ACP conversation IDs) can be
        revalidated against the persisted state.
        """

        check_startup_preconditions(config, StartupMode.RESUME)
        logger.info(
            "runtime_resume",
            extra={
                "swarm_id": config.swarm_id,
                "project_path": str(config.project_path),
                "adapter_mode": getattr(getattr(config, "adapter_mode", None), "value", None),
            },
        )

        store = MetadataStore(config=config)
        swarm = store.load_swarm_state()

        state = RuntimeState(config=config)

        # Mirror the `create` path and reuse the runtime-owned adapters so
        # that the daemon owns a consistent set of integration adapters
        # regardless of startup mode. For US2 we also enforce FR-009 by
        # rebinding these adapters against the persisted swarm/agent
        # state and validating that identifiers are reused on resume.
        if adapters is None:
            adapters = create_runtime_adapters(config)

        agent_mail_client: BaseAgentMailClient = adapters.agent_mail
        acp_client: BaseAcpClient = adapters.acp

        agent_supervisor = AgentSupervisor(
            config=config,
            state=state,
            swarm_state=swarm,
        )
        scheduler = RuntimeScheduler(
            config=config,
            state=state,
            swarm_state=swarm,
            agent_supervisor=agent_supervisor,
            agent_mail_client=agent_mail_client,
        )

        # Wire the ACP adapter's event callback into the AgentSupervisor so
        # that adapter-emitted AgentEvent instances are appended to the
        # in-memory per-agent event streams and forwarded to any configured
        # listeners (for example, the WebSocket control API bridge).
        acp_client.on_event = agent_supervisor.append_agent_event

        # Rebind the Agent Mail project identifier and per-agent identities.
        # These helpers are required to be idempotent: for a given
        # configuration and agent_id they must always return the same
        # identifier. On resume we treat any divergence between the
        # adapter-derived values and the persisted metadata as a hard
        # startup error, since it indicates an FR-009 violation.
        #
        # For the production MCP-backed Agent Mail client we always enforce
        # a strict project-id check on resume. The configured
        # :attr:`RuntimeConfig.agent_mail_project` (or its default) is treated
        # as the canonical project key; :class:`McpAgentMailClient.ensure_project`
        # must therefore resolve to the same identifier that was recorded in
        # :class:`SwarmState.agent_mail_project_id` at create time. Any
        # divergence indicates that the runtime is now pointed at a different
        # Agent Mail project for this swarm and is treated as a hard startup
        # error to protect FR-009.
        if swarm.agent_mail_project_id and isinstance(agent_mail_client, McpAgentMailClient):
            project_id = agent_mail_client.ensure_project()
            if project_id != swarm.agent_mail_project_id:
                logger.error(
                    "runtime_resume_agent_mail_project_mismatch_real",
                    extra={
                        "swarm_id": swarm.swarm_id,
                        "project_path": str(swarm.project_path),
                        "expected_project_id": swarm.agent_mail_project_id,
                        "actual_project_id": project_id,
                    },
                )
                raise RuntimeStartupError(
                    "Agent Mail project ID mismatch on resume for REAL adapter: "
                    f"adapter returned {project_id!r}, "
                    f"metadata has {swarm.agent_mail_project_id!r}"
                )


        for agent_id, meta in swarm.agents.items():
            # Prefer config-driven Agent Mail invariants when a persisted
            # NateOhaConfig with an enabled Agent Mail feature is available for
            # this agent. This keeps NateOhaConfig as the single source of
            # truth for launch-time behaviour (see ConfigOverhaul.md) while
            # still preserving backwards compatibility with older metadata
            # that relied on separate per-agent Agent Mail fields.
            cfg = getattr(meta, "nate_oha_config", None)
            features = getattr(cfg, "features", None) if cfg is not None else None
            agent_mail_cfg = getattr(features, "agent_mail", None) if features is not None else None

            if agent_mail_cfg is not None:
                # Config-driven Agent Mail. When the feature is disabled we do
                # not impose any FR-009 identity invariant for this agent.
                if not getattr(agent_mail_cfg, "enabled", False):
                    continue

                expected_identity = (getattr(agent_mail_cfg, "agent_identity", "") or "").strip()
                # Under the ConfigOverhaul MS2 model an enabled Agent Mail
                # feature *must* carry a non-empty agent identity. Treating an
                # empty string as "no binding present" would allow partially
                # configured agents to slip through resume checks, so we surface
                # this as an explicit startup error.
                if not expected_identity:
                    logger.error(
                        "runtime_resume_agent_mail_identity_missing",
                        extra={
                            "swarm_id": swarm.swarm_id,
                            "project_path": str(swarm.project_path),
                            "agent_id": agent_id,
                            "source": "nate_oha_config",
                        },
                    )
                    raise RuntimeStartupError(
                        "Agent Mail identity is missing or empty in NateOhaConfig for "
                        f"agent {agent_id!r} while Agent Mail is enabled; "
                        "please ensure features.agent_mail.agent_identity is configured."
                    )

                credentials_hint_raw = getattr(agent_mail_cfg, "credentials_ref", "")
                credentials_hint = (credentials_hint_raw or "").strip() or None

                identity, _credentials = agent_mail_client.ensure_agent_identity_with_credentials(
                    agent_id, credentials_hint
                )
                if identity != expected_identity:
                    logger.error(
                        "runtime_resume_agent_mail_identity_mismatch",
                        extra={
                            "swarm_id": swarm.swarm_id,
                            "project_path": str(swarm.project_path),
                            "agent_id": agent_id,
                            "expected_identity": expected_identity,
                            "actual_identity": identity,
                            "source": "nate_oha_config",
                        },
                    )
                    raise RuntimeStartupError(
                        "Agent Mail identity mismatch on resume for "
                        f"agent {agent_id!r}: adapter returned {identity!r}, "
                        "NateOhaConfig.features.agent_mail.agent_identity has "
                        f"{expected_identity!r}"
                    )


        return cls(
            config=config,
            metadata_store=store,
            swarm_state=swarm,
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
                logger.debug(
                    "runtime_start_idempotent",
                    extra={
                        "swarm_id": self.config.swarm_id,
                        "project_path": str(self.config.project_path),
                    },
                )
                return

            logger.error(
                "runtime_start_invalid_state",
                extra={
                    "swarm_id": self.config.swarm_id,
                    "project_path": str(self.config.project_path),
                    "status": self.state.status.value,
                },
            )
            raise RuntimeStartupError(
                f"Cannot start runtime from status {self.state.status!r}"
            )

        # Allow the scheduler to perform any startup-time registration or
        # initialization before we mark the runtime as fully running.
        if self.scheduler is not None:
            self.scheduler.start()

        self.state.status = RuntimeStatus.RUNNING
        self.started_at = datetime.utcnow()

        logger.info(
            "runtime_started",
            extra={
                "swarm_id": self.config.swarm_id,
                "project_path": str(self.config.project_path),
            },
        )

    def request_shutdown(self) -> None:
        """Request a graceful shutdown.

        This mirrors the semantics of `runtime.shutdown` in the control
        API contract at a high level: mark the runtime as shutting down
        and set a flag that can be observed by the event loop.
        """

        if self.state.status in {RuntimeStatus.STOPPED, RuntimeStatus.FAILED}:
            # Nothing to do; treat as idempotent.
            logger.debug(
                "runtime_shutdown_ignored",
                extra={
                    "swarm_id": self.config.swarm_id,
                    "project_path": str(self.config.project_path),
                    "status": self.state.status.value,
                },
            )
            return

        self.state.shutdown_requested = True

        if self.state.status is RuntimeStatus.RUNNING:
            self.state.status = RuntimeStatus.SHUTTING_DOWN

        logger.info(
            "runtime_shutdown_requested",
            extra={
                "swarm_id": self.config.swarm_id,
                "project_path": str(self.config.project_path),
                "status": self.state.status.value,
            },
        )

    def mark_stopped(self) -> None:
        """Mark the runtime as fully stopped.

        In a full implementation this would be called once all agents
        have terminated and the scheduler has completed cleanup.
        """

        self.state.status = RuntimeStatus.STOPPED
        logger.info(
            "runtime_stopped",
            extra={
                "swarm_id": self.config.swarm_id,
                "project_path": str(self.config.project_path),
            },
        )

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
            "swarm_id": self.config.swarm_id,
            "agent_counts": self._compute_agent_counts(),
        }

    def get_swarm_status(self) -> dict[str, object]:
        """Return a JSON-serializable snapshot for ``swarm.get_overview``.

        In addition to joining persisted metadata with live runtime
        status, this wires in unread mailbox summaries via the
        :class:`BaseAgentMailClient` when available, as required by the
        runtime API contract for User Story 1.
        """

        agent_counts = self._compute_agent_counts()

        # Union of configured agents (from durable state) and those currently
        # present in runtime state; this is tolerant of partial
        # initialization.
        all_agent_ids = set(self.swarm_state.agents.keys()) | set(
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
            metadata = self.swarm_state.agents.get(agent_id)
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
            "swarm_id": self.config.swarm_id,
            "project_path": str(self.config.project_path),
            "runtime_status": self.state.status.value,
            "agent_counts": agent_counts,
            "agents": agents,
        }


    def _refresh_last_known_status_from_acp(
        self, agent_id: str, metadata: AgentState | None
    ) -> AgentState | None:
        """Update :class:`AgentState.last_known_status` from the ACP adapter.

        This consults the runtime-owned ACP client (when present) for a
        lightweight adapter-level status, maps that to the persisted
        ``last_known_status`` representation, and persists the updated
        :class:`SwarmState` when a change is observed.

        The method is intentionally tolerant of adapter errors and unknown
        states so that callers (for example, :meth:`get_agent_detail`) can
        safely use it in fallback paths without affecting control flow.
        """

        acp_client = self.acp_client
        if acp_client is None:
            return metadata

        try:
            acp_status = acp_client.get_status(agent_id)
        except Exception:
            return metadata

        new_status = _map_acp_state_to_last_known_status(getattr(acp_status, "state", ""))
        if new_status is None:
            return metadata

        current_meta = metadata or self.swarm_state.agents.get(agent_id)
        if current_meta is None:
            # Unknown agent; nothing to refresh.
            return None

        if current_meta.last_known_status == new_status:
            return current_meta

        updated = current_meta.model_copy(update={"last_known_status": new_status})

        # Update the in-memory swarm state snapshot so subsequent calls in
        # this process see the refreshed status, and persist to disk.
        swarm = self.swarm_state
        agents = dict(swarm.agents)
        agents[agent_id] = updated
        self.swarm_state = swarm.model_copy(update={"agents": agents})
        self.metadata_store.save_swarm_state(self.swarm_state)

        return updated

    def get_agent_detail(self, agent_id: str, max_events: int = 100) -> dict[str, object]:
        """Return a JSON-serializable snapshot for ``agent.get_detail``.

        The result shape mirrors the contract in
        ``specs/001-swarm-runtime-orchestrator/contracts/runtime-api.md``:

        * ``agent``: joined view of persisted metadata and live runtime state.
        * ``events``: recent :class:`AgentEvent` records from the agent's
          in-memory :class:`~nate_ntm.runtime.events.AgentEventStream`.
        """

        runtime_state = self.state.agents.get(agent_id)

        # Prefer the latest persisted per-agent state from the metadata
        # store so that fields updated by background operations (for
        # example, ACP session identifiers written by ``start_agent_async``)
        # are reflected even when this daemon instance did not perform the
        # write itself. When no swarm/agent state exists yet, fall back to
        # the in-memory snapshot loaded at construction time.
        try:
            metadata = self.metadata_store.load_agent_state(agent_id)
        except FileNotFoundError:
            metadata = self.swarm_state.agents.get(agent_id)

        # When no live runtime state exists (for example, before the scheduler
        # has started or immediately after a crash), attempt to refresh the
        # persisted last-known status from the ACP adapter.
        if runtime_state is None:
            metadata = self._refresh_last_known_status_from_acp(agent_id, metadata)

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

        # Agent Mail identity is surfaced via the embedded NateOhaConfig
        # when present. This keeps the control API aligned with the
        # persisted configuration model instead of relying on separate
        # per-agent metadata fields.
        agent_mail_identity: str = ""
        if metadata is not None:
            cfg = getattr(metadata, "nate_oha_config", None)
            features = getattr(cfg, "features", None) if cfg is not None else None
            agent_mail_cfg = getattr(features, "agent_mail", None) if features is not None else None
            if agent_mail_cfg is not None:
                agent_mail_identity = (getattr(agent_mail_cfg, "agent_identity", "") or "").strip()

        # API-level schema expects ``conversation_id`` to be a string. Treat
        # ``None`` (or other false-y values) as "no persisted conversation"
        # and surface that as an empty string so callers do not need to
        # special-case ``null`` versus ``""``.
        conversation_id: str = ""
        if metadata is not None:
            value = metadata.conversation_id
            if isinstance(value, str):
                conversation_id = value
            elif value is not None:
                conversation_id = str(value)

        agent_payload: dict[str, object] = {
            "agent_id": agent_id,
            "display_name": display_name,
            "status": status_value,
            "agent_mail_identity": agent_mail_identity,
            "conversation_id": conversation_id,
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

