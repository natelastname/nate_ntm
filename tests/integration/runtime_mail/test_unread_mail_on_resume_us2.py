"""US2/T024: unread Agent Mail on resume surfaces as agent events.

These tests exercise the integration between:

* :class:`RuntimeDaemon` resume semantics.
* The dev-mode :class:`FakeAgentMailClient`.
* :class:`RuntimeScheduler`'s startup-time unread-mail polling.
* The runtime control API's ``agent.get_detail`` handler.

The goal is to validate US2 acceptance scenario 2 in a thin slice:
when a swarm is resumed and the Agent Mail adapter reports unread
messages for some agents, the runtime should surface corresponding
``MailReceived`` events in the per-agent event streams that back
``agent.get_detail``.
"""

from __future__ import annotations

from pathlib import Path
from datetime import datetime

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.agent_mail_client import FakeAgentMailClient
from nate_ntm.runtime.daemon import RuntimeDaemon
from nate_ntm.runtime.metadata_store import AgentMetadata, MetadataStore, SwarmMetadata
from nate_ntm.runtime.state import RuntimeStatus
from nate_ntm.api.server import RuntimeApiServer


def _make_project(tmp_path: Path) -> Path:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    return project


def _seed_swarm_metadata(config: RuntimeConfig) -> None:
    """Create minimal swarm + agent metadata for resume tests.

    We deliberately leave Agent Mail identity and ACP conversation
    fields empty so that the stricter FR-009 resume-time rebinding
    checks are not triggered; this test focuses solely on unread-mail
    behavior.
    """

    store = MetadataStore(config=config)

    a1 = AgentMetadata(agent_id="nav-1", display_name="Navigator 1")
    a2 = AgentMetadata(agent_id="nav-2", display_name="Navigator 2")

    now = datetime(2026, 7, 3, 12, 0, 0)
    swarm = SwarmMetadata(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        agent_mail_project_id="mail-project-1",
        created_at=now,
        last_updated_at=now,
        agents={a1.agent_id: a1, a2.agent_id: a2},
    )

    store.save_swarm_metadata(swarm)
    store.save_agent_metadata(a1)
    store.save_agent_metadata(a2)


def test_unread_mail_on_resume_surfaces_as_mail_received_events(tmp_path: Path) -> None:
    """Agents with unread mail at resume get ``MailReceived`` events.

    Scenario:

    * A project has swarm metadata for two agents.
    * We construct a :class:`RuntimeDaemon` in ``resume`` mode.
    * Before calling :meth:`RuntimeDaemon.start`, we seed the
      dev-mode :class:`FakeAgentMailClient` with unread mail for
      ``nav-1`` only.
    * Once the daemon starts, we query ``agent.get_detail`` via
      :class:`RuntimeApiServer`.

    Expected behavior:

    * ``nav-1`` has at least one event with ``source == "AgentMail"``
      and ``type == "MailReceived"``.
    * ``nav-2`` either has no events or has events with different
      types/sources, but not ``MailReceived`` from Agent Mail.
    """

    project = _make_project(tmp_path)
    config: RuntimeConfig = load_runtime_config(project_path=project)

    _seed_swarm_metadata(config)

    daemon = RuntimeDaemon.resume(config)

    # Sanity-check initial state.
    assert daemon.state.status is RuntimeStatus.STARTING
    assert daemon.scheduler is not None
    assert daemon.agent_mail_client is not None

    # Seed unread mail for one agent via the dev-mode fake client. The
    # scheduler will consult this adapter at startup.
    assert isinstance(daemon.agent_mail_client, FakeAgentMailClient)
    daemon.agent_mail_client.ensure_project()
    daemon.agent_mail_client.set_unread_count_for_test("nav-1", 2)

    # Start the runtime, which will in turn start the scheduler and
    # enqueue MailReceived events for agents with unread mail.
    daemon.start()
    assert daemon.state.status is RuntimeStatus.RUNNING

    server = RuntimeApiServer(daemon=daemon)

    detail_nav1 = server.get_agent_detail(agent_id="nav-1", max_events=20)
    detail_nav2 = server.get_agent_detail(agent_id="nav-2", max_events=20)

    events_nav1 = detail_nav1["events"]
    events_nav2 = detail_nav2["events"]

    assert any(
        e["source"] == "AgentMail" and e["type"] == "MailReceived"
        for e in events_nav1
    )

    assert all(
        not (e["source"] == "AgentMail" and e["type"] == "MailReceived")
        for e in events_nav2
    )
