"""Epic 005: end-to-end async ACP session persistence with REAL adapters.

This integration test ties together several runtime components using their
production implementations:

* :class:`RuntimeDaemon` in ``resume`` mode.
* :func:`create_runtime_adapters` with ``AdapterKind.REAL`` so that
  :class:`McpAgentMailClient` and :class:`NateOhaAcpClient` are used.
* The async ACP lifecycle on :class:`NateOhaAcpClient` via
  :meth:`start_agent_async` / :meth:`stop_agent_async`.

The goal is to exercise the real ACP SDK wiring for Epic 005 and to
validate that the opaque ``session_id`` returned by ``session/new`` is
persisted into :class:`AgentMetadata.conversation_id` and reused via
:meth:`ensure_conversation` and
:meth:`RuntimeDaemon.get_agent_detail`.

Unlike the focused unit tests in
``tests/unit/runtime/test_nate_oha_acp_client_async.py``, this test does
*not* patch :func:`open_nate_oha_acp_client`. It launches a real
``nate-oha`` subprocess via the ACP SDK and relies on the configured
``mcp_agent_mail`` service for the Agent Mail project lookup in resume
mode.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from pathlib import Path

import pytest

from nate_ntm.config.runtime_config import AdapterKind, load_runtime_config
from nate_ntm.runtime.acp_client import NateOhaAcpClient
from nate_ntm.runtime.adapters import create_runtime_adapters
from nate_ntm.runtime.agent_mail_client import McpAgentMailClient
from nate_ntm.runtime.daemon import RuntimeDaemon
from nate_ntm.runtime.events import AgentEvent
from nate_ntm.runtime.metadata_store import AgentMetadata, MetadataStore, SwarmMetadata


@pytest.mark.asyncio
async def test_runtime_daemon_acp_async_persists_session_id_and_exposes_via_detail(
    tmp_path: Path,
) -> None:
    """REAL-path async ACP session persistence and reuse (Epic 005).

    This test exercises the async ACP lifecycle using the real
    :class:`NateOhaAcpClient` and ACP SDK:

    * Swarm and agent metadata are created with an empty
      ``conversation_id`` so that :meth:`start_agent_async` takes the
      ``session/new`` path.
    * :class:`RuntimeDaemon` is constructed in ``resume`` mode with REAL
      adapters provided by :func:`create_runtime_adapters`, wiring in a
      :class:`McpAgentMailClient` and :class:`NateOhaAcpClient`.
    * :meth:`NateOhaAcpClient.start_agent_async` establishes an ACP
      session and receives an opaque ``session_id`` from the server.
    * That ``session_id`` is persisted into
      :class:`AgentMetadata.conversation_id` on disk and cached in the
      adapter's in-memory session map.
    * A fresh :class:`NateOhaAcpClient` instance with the same
      :class:`RuntimeConfig` reuses the same identifier via
      :meth:`ensure_conversation`.
    * :meth:`RuntimeDaemon.get_agent_detail` surfaces the persisted
      conversation identifier even when no live runtime state entry
      exists for the agent yet.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    # Point Nate OHA at the sample profile used throughout this repository
    # for real-path tests. Using an existing config mirrors the quickstart
    # flow and ensures that ``--set runtime.mode=...`` overrides operate on
    # a valid base tree.
    repo_root = Path(__file__).resolve().parents[3]
    base_config = repo_root / "nate-oha-profiles" / "profile1.json"

    # Take an explicit snapshot of the current environment so that
    # ``load_runtime_config`` does not consult repository-level .env
    # files. We then overlay the adapter-mode selection to force REAL
    # adapters for both Agent Mail and ACP and supply the Nate OHA
    # launch settings required by :func:`build_nate_oha_launch_spec`.
    env_snapshot = dict(os.environ)
    env_snapshot.update(
        {
            "NATE_NTM_PROJECT_DIR": str(project),
            "NATE_NTM_ADAPTER_MODE": AdapterKind.REAL.value,
            # Minimal Nate OHA launch configuration: point Nate OHA at the
            # repository's sample profile and run in "echo" mode so the
            # test focuses on ACP/session semantics rather than model
            # behaviour.
            "NATE_NTM_NATE_OHA_CONFIG": str(base_config),
            "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
        }
    )

    # Resolve a REAL-adapter RuntimeConfig. For the REAL Agent Mail
    # adapter, the project key used for ensure_project is derived from
    # :attr:`RuntimeConfig.agent_mail_project` when set, otherwise from
    # the absolute project path. In this test we rely on that default so
    # that the swarm's ``agent_mail_project_id`` matches the project
    # directory path.
    config = load_runtime_config(
        project_path=project,
        env=env_snapshot,
    )

    store = MetadataStore(config=config)
    now = datetime(2026, 7, 3, 12, 0, 0)

    # Seed a single agent with no pre-existing ACP conversation
    # identifier so that start_agent_async must call ``session/new``.
    meta = AgentMetadata(
        agent_id="nav-async-1",
        display_name="Navigator Async 1",
        agent_mail_identity="",  # Agent Mail is optional for this test.
        conversation_id="",  # Force the "session/new" path.
    )

    swarm = SwarmMetadata(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        # For REAL adapters the Agent Mail project identifier used by the
        # runtime and persisted into swarm metadata is derived from
        # :attr:`RuntimeConfig.agent_mail_project` or, when unset, from the
        # absolute project path. Here we mirror the latter so that
        # RuntimeDaemon.resume's FR-009 project rebinding checks compare
        # the same key that :class:`McpAgentMailClient.ensure_project`
        # will derive.
        agent_mail_project_id=str(config.project_path),
        created_at=now,
        last_updated_at=now,
        agents={meta.agent_id: meta},
    )
    store.save_swarm_metadata(swarm)
    store.save_agent_metadata(meta)

    # Construct REAL adapters (McpAgentMailClient + NateOhaAcpClient) and
    # hand them to RuntimeDaemon so the daemon owns the integration
    # clients for this run.
    adapters = create_runtime_adapters(config)
    assert isinstance(adapters.acp, NateOhaAcpClient)

    # Point the ACP adapter at the installed ``nate-oha`` binary used in
    # this repository. The default ``"nate_OHA"`` name is correct for the
    # upstream CLI but does not match the local binary on all systems.
    adapters.acp.executable = "nate-oha"  # type: ignore[attr-defined]

    daemon = RuntimeDaemon.resume(config, adapters=adapters)

    # Sanity: the scheduler has not yet registered any runtime state
    # entries; this test focuses on metadata + ACP session semantics
    # rather than scheduler-driven status updates.
    assert daemon.state.agents == {}

    acp_client = daemon.acp_client
    assert isinstance(acp_client, NateOhaAcpClient)

    # Capture ACP events emitted during the initial async session so we
    # can assert on the translated event stream. This intentionally
    # overrides the daemon's default AgentSupervisor sink for the
    # duration of the test; the scheduler has not been started and no
    # runtime state entries exist yet, so this does not affect other
    # behavior under test.
    events_run1: list[AgentEvent] = []
    acp_client.on_event = events_run1.append

    # Establish a real ACP session for the agent using the async
    # lifecycle. This launches a nate-oha subprocess via the ACP SDK and
    # negotiates capabilities + a new session.
    await acp_client.start_agent_async(meta.agent_id, metadata=meta)

    try:
        # Allow a short window for ACP session updates to be delivered
        # through the protocol client. This keeps the test tolerant of
        # small scheduling differences without turning it into a
        # long-running soak.
        await asyncio.sleep(0.5)

        # After async start, the canonical conversation identifier is the
        # value persisted into :class:`AgentMetadata.conversation_id` on disk.
        # For Nate OHA / ACP this must be the opaque ``session_id`` assigned
        # by the server.
        reloaded_meta = store.load_agent_metadata(meta.agent_id)
        session_id = reloaded_meta.conversation_id
        assert isinstance(session_id, str) and session_id

        # If any ACP events were observed during the first run, they must
        # reference the same session identifier in their payloads.
        for event in events_run1:
            assert event.agent_id == meta.agent_id
            payload_session = event.payload.get("session_id")
            if payload_session is not None:
                assert payload_session == session_id

        # A fresh NateOhaAcpClient with the same configuration must reuse
        # that identifier via ensure_conversation, reflecting the Epic 005
        # invariant that ACP's opaque ``session_id`` is the canonical
        # conversation identifier.
        fresh_client = NateOhaAcpClient(config=config, executable="nate-oha")
        conv2 = fresh_client.ensure_conversation(meta.agent_id)
        assert conv2 == session_id

        # Second run: resume the existing ACP session from persisted
        # metadata using the async lifecycle. This exercises the
        # ``load_session`` path in start_agent_async against a real Nate
        # OHA instance.
        events_run2: list[AgentEvent] = []
        fresh_client.on_event = events_run2.append

        resume_meta = store.load_agent_metadata(meta.agent_id)
        assert resume_meta.conversation_id == session_id

        await fresh_client.start_agent_async(meta.agent_id, metadata=resume_meta)

        try:
            await asyncio.sleep(0.5)

            # ensure_conversation on the fresh adapter should also observe
            # the same ACP-owned identifier.
            conv3 = fresh_client.ensure_conversation(meta.agent_id)
            assert conv3 == session_id

            # All ACP events observed during the resumed run should
            # reference the same session identifier when they carry one.
            for event in events_run2:
                assert event.agent_id == meta.agent_id
                payload_session = event.payload.get("session_id")
                if payload_session is not None:
                    assert payload_session == session_id

            # Across both runs we expect to see at least one ACP-derived
            # event, confirming that the real ACP event translation path
            # was exercised.
            all_events = events_run1 + events_run2
            assert any(ev.type.startswith("acp.") for ev in all_events)

        finally:
            # Best-effort cleanup of the resumed session.
            try:
                await fresh_client.stop_agent_async(meta.agent_id, timeout=5.0)
            except Exception:
                # Cleanup failures should not mask assertion failures.
                pass

        # RuntimeDaemon.get_agent_detail should surface the persisted
        # conversation identifier even when no live runtime state exists
        # yet for the agent.
        detail = daemon.get_agent_detail(agent_id=meta.agent_id, max_events=10)
        agent_payload = detail["agent"]
        assert agent_payload["conversation_id"] == session_id

    finally:
        # Best-effort cleanup of the ACP session and underlying
        # subprocess so the test does not leak nate-oha processes.
        await acp_client.stop_agent_async(meta.agent_id, timeout=5.0)


@pytest.mark.asyncio
async def test_runtime_daemon_acp_async_with_agent_mail_real_path_epic005(tmp_path: Path) -> None:
    """REAL-path async ACP + Agent Mail integration (Epic 005).

    This test extends the main Epic 005 scenario to exercise the REAL
    :class:`McpAgentMailClient` alongside the REAL
    :class:`NateOhaAcpClient` and ACP SDK wiring. It assumes that a
    live Agent Mail MCP server is reachable at the URL resolved by
    :class:`McpAgentMailClient` (for example,
    ``http://127.0.0.1:8765/api``) and treats missing or unreachable
    Agent Mail as a **hard failure** rather than skipping the test.

    High-level invariants:

    * Agent Mail project/identity/credentials are allocated via
      :class:`McpAgentMailClient` and persisted into swarm + agent
      metadata before any ACP session is created.
    * :meth:`RuntimeDaemon.resume` revalidates the Agent Mail project
      and per-agent identity against the live service using the REAL
      adapter, enforcing FR-009.
    * :meth:`NateOhaAcpClient.start_agent_async` establishes a real ACP
      session for the agent, obtaining an opaque ``session_id`` that is
      persisted into :class:`AgentMetadata.conversation_id`.
    * A fresh :class:`NateOhaAcpClient` instance with the same
      :class:`RuntimeConfig` reuses the same identifier via
      :meth:`ensure_conversation`.
    * :meth:`RuntimeDaemon.get_agent_detail` surfaces the persisted
      ``conversation_id`` even when no live runtime state entry exists
      yet for the agent.

    Any misconfiguration of Agent Mail (for example missing project ID
    or upstream URL) or failure to reach the Agent Mail server is
    surfaced as an :class:`AgentMailClientError` or
    :class:`RuntimeStartupError`, causing this test to fail rather than
    being skipped.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    repo_root = Path(__file__).resolve().parents[3]
    base_config = repo_root / "nate-oha-profiles" / "profile1.json"

    # Configure REAL adapters, including Agent Mail. We explicitly set
    # the Agent Mail project key and upstream URL so that
    # :class:`RuntimeConfig` and :class:`McpAgentMailClient` derive the
    # same values and :meth:`NateOhaAcpClient._build_env` sees a fully
    # configured Agent Mail environment when an identity is present.
    env_snapshot = dict(os.environ)
    env_snapshot.update(
        {
            "NATE_NTM_PROJECT_DIR": str(project),
            "NATE_NTM_ADAPTER_MODE": AdapterKind.REAL.value,
            "NATE_NTM_NATE_OHA_CONFIG": str(base_config),
            "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
            # Agent Mail configuration: hard requirement for this test.
            # The project key is chosen to be the absolute project path
            # so that it matches the FR-009 rebinding semantics used by
            # other REAL-path tests.
            "NATE_NTM_AGENT_MAIL_PROJECT": str(project),
            # Upstream URL must point at a live Agent Mail MCP server.
            # If the server is unreachable or misconfigured, the test
            # fails via AgentMailClientError.
            "NATE_NTM_AGENT_MAIL_URL": "http://127.0.0.1:8765/api",
        }
    )

    config = load_runtime_config(
        project_path=project,
        env=env_snapshot,
    )

    store = MetadataStore(config=config)
    now = datetime(2026, 7, 3, 12, 0, 0)

    # Construct REAL adapters and use the REAL Agent Mail client to
    # allocate a project and per-agent identity/credentials **before**
    # seeding swarm metadata. This ensures that the on-disk metadata
    # reflects the live Agent Mail registration state.
    adapters = create_runtime_adapters(config)
    assert isinstance(adapters.agent_mail, McpAgentMailClient)
    assert isinstance(adapters.acp, NateOhaAcpClient)

    agent_mail_client = adapters.agent_mail
    acp_client = adapters.acp

    agent_id = "nav-async-mail-1"

    # Ensure the Agent Mail project exists and record the canonical
    # project identifier used by the runtime. Any network or
    # authentication error here is treated as a hard failure.
    agent_mail_project_id = agent_mail_client.ensure_project()

    # Allocate an Agent Mail identity + credentials for this agent via
    # the REAL adapter. The returned values are persisted into
    # AgentMetadata so that later resume flows and ACP launches can
    # reuse them.
    identity, token = agent_mail_client.ensure_agent_identity_with_credentials(agent_id)
    assert identity
    assert token

    meta = AgentMetadata(
        agent_id=agent_id,
        display_name="Navigator Async Mail 1",
        agent_mail_identity=identity,
        agent_mail_credentials_ref=token or "",
        conversation_id="",  # Force the ACP "session/new" path on first run.
    )

    swarm = SwarmMetadata(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id=agent_mail_project_id,
        created_at=now,
        last_updated_at=now,
        agents={meta.agent_id: meta},
    )

    store.save_swarm_metadata(swarm)
    store.save_agent_metadata(meta)

    # Reuse the already-constructed REAL adapters when resuming the
    # runtime so that :meth:`RuntimeDaemon.resume` can rebind Agent Mail
    # and ACP identifiers against the same adapter instances.
    daemon = RuntimeDaemon.resume(config, adapters=adapters)

    # Sanity: scheduler has not yet registered any runtime state
    # entries; this test focuses on metadata + Agent Mail + ACP session
    # semantics rather than scheduler-driven status updates.
    assert daemon.state.agents == {}

    assert isinstance(daemon.agent_mail_client, McpAgentMailClient)
    assert isinstance(daemon.acp_client, NateOhaAcpClient)

    # Align the ACP adapter's executable with the installed ``nate-oha``
    # binary used in this repository.
    daemon.acp_client.executable = "nate-oha"  # type: ignore[attr-defined]

    acp_client = daemon.acp_client

    events_run1: list[AgentEvent] = []
    acp_client.on_event = events_run1.append

    # First async run: establish a real ACP session for the agent using
    # the async lifecycle. This launches ``nate-oha acp`` via the ACP
    # SDK with Agent Mail integration enabled.
    start_meta = store.load_agent_metadata(agent_id)
    await acp_client.start_agent_async(agent_id, metadata=start_meta)

    try:
        await asyncio.sleep(0.5)

        # The canonical conversation identifier is the value persisted
        # into :class:`AgentMetadata.conversation_id` on disk. For Nate
        # OHA / ACP this must be the opaque ``session_id`` assigned by
        # the server.
        reloaded_meta = store.load_agent_metadata(agent_id)
        session_id = reloaded_meta.conversation_id
        assert isinstance(session_id, str) and session_id

        # Agent Mail identity and credentials must remain unchanged
        # across the ACP session.
        assert reloaded_meta.agent_mail_identity == identity
        assert reloaded_meta.agent_mail_credentials_ref == (token or "")

        # Any ACP events observed during the first run that carry a
        # ``session_id`` in their payload must reference the same
        # identifier.
        for event in events_run1:
            assert event.agent_id == agent_id
            payload_session = event.payload.get("session_id")
            if payload_session is not None:
                assert payload_session == session_id

        # A fresh NateOhaAcpClient with the same configuration must
        # reuse the same identifier via ensure_conversation, reflecting
        # the Epic 005 invariant that ACP's opaque ``session_id`` is the
        # canonical conversation identifier.
        fresh_client = NateOhaAcpClient(config=config, executable="nate-oha")
        conv2 = fresh_client.ensure_conversation(agent_id)
        assert conv2 == session_id

        # Second async run: resume the existing ACP session from
        # persisted metadata using the async lifecycle. This exercises
        # the ``load_session`` path in start_agent_async against a real
        # Nate OHA instance with Agent Mail enabled.
        events_run2: list[AgentEvent] = []
        fresh_client.on_event = events_run2.append

        resume_meta = store.load_agent_metadata(agent_id)
        assert resume_meta.conversation_id == session_id

        await fresh_client.start_agent_async(agent_id, metadata=resume_meta)

        try:
            await asyncio.sleep(0.5)

            conv3 = fresh_client.ensure_conversation(agent_id)
            assert conv3 == session_id

            # All ACP events observed during the resumed run should
            # reference the same session identifier when they carry one.
            for event in events_run2:
                assert event.agent_id == agent_id
                payload_session = event.payload.get("session_id")
                if payload_session is not None:
                    assert payload_session == session_id

            # Across both runs we expect to see at least one ACP-derived
            # event, confirming that the real ACP event translation path
            # was exercised with Agent Mail enabled.
            all_events = events_run1 + events_run2
            assert any(ev.type.startswith("acp.") for ev in all_events)

        finally:
            # Best-effort cleanup of the resumed session.
            try:
                await fresh_client.stop_agent_async(agent_id, timeout=5.0)
            except Exception:
                # Cleanup failures should not mask assertion failures.
                pass

        # RuntimeDaemon.get_agent_detail should surface both the
        # persisted Agent Mail identity and the ACP-owned conversation
        # identifier even when no live runtime state exists yet for the
        # agent.
        detail = daemon.get_agent_detail(agent_id=agent_id, max_events=10)
        agent_payload = detail["agent"]
        assert agent_payload["conversation_id"] == session_id
        assert agent_payload["agent_mail_identity"] == identity

    finally:
        # Best-effort cleanup of the initial ACP session and underlying
        # subprocess so the test does not leak nate-oha processes.
        await acp_client.stop_agent_async(agent_id, timeout=5.0)
