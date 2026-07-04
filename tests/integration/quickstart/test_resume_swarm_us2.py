"""Quickstart-style integration tests for US2 swarm resume semantics.

These tests correspond to T025 in ``tasks.md`` and exercise a thin
end-to-end path from a project directory on disk through:

* ``RuntimeConfig`` resolution for that project.
* ``RuntimeDaemon.create`` in ``create`` mode with a small agent set.
* Clean shutdown of the initial runtime instance.
* ``RuntimeDaemon.resume`` startup semantics against the same metadata.
* ``RuntimeApiServer`` handlers for ``runtime.get_status`` and
  ``swarm.get_overview``.

The goal for this first US2 slice is to validate FR-009 and SC-002 at a
basic level: when a swarm is created and later resumed, all agents must
reuse their persisted Agent Mail identities and ACP conversation
identifiers, and the runtime API must continue to expose consistent
swarm/agent views for the resumed instance.
"""

from __future__ import annotations

from pathlib import Path
from typing import Dict, Tuple

from nate_ntm.api.server import RuntimeApiServer
from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.daemon import RuntimeDaemon
from nate_ntm.runtime.state import RuntimeStatus


def _create_swarm_with_agents(tmp_path: Path, agent_count: int) -> Tuple[RuntimeConfig, Dict[str, Tuple[str, str]]]:
    """Create a new swarm with ``agent_count`` agents via RuntimeDaemon.create.

    This helper mirrors the US1 quickstart behavior but returns the
    persisted identity/conversation tuples for each agent so that US2
    tests can assert that those values are reused on resume.
    """

    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)

    config: RuntimeConfig = load_runtime_config(project_path=project)

    # Construct and start a fresh runtime in ``create`` mode.
    daemon = RuntimeDaemon.create(config, agent_count=agent_count)
    daemon.start()

    # Capture the persisted Agent Mail identities and ACP conversation IDs
    # from the swarm metadata. These are expected to be durable across
    # resume and must not be regenerated.
    swarm = daemon.swarm_metadata
    identities: Dict[str, Tuple[str, str]] = {}
    for agent_id, meta in swarm.agents.items():
        identities[agent_id] = (meta.agent_mail_identity, meta.conversation_id)

    # Drive a clean, in-process shutdown to mirror the quickstart flow.
    daemon.request_shutdown()
    daemon.mark_stopped()

    return config, identities


def test_resume_swarm_us2_reuses_agent_identities_and_conversations(tmp_path: Path) -> None:
    """US2: resume reuses Agent Mail identities and ACP conversations.

    This test exercises a simple create → shutdown → resume cycle for a
    small fake swarm and asserts that the resumed runtime observes the
    same Agent Mail identities and ACP conversation identifiers for each
    agent as were persisted at creation time.
    """

    # Arrange: create a swarm with two agents and capture their identities.
    config, identities_before = _create_swarm_with_agents(tmp_path, agent_count=2)

    # Act: resume the swarm from the same project metadata.
    daemon = RuntimeDaemon.resume(config)
    daemon.start()

    # The resumed daemon should report ``Running`` at the runtime level.
    assert daemon.state.status is RuntimeStatus.RUNNING

    # The swarm metadata loaded on resume must contain the same agents and
    # the same identity/conversation tuples as at creation time.
    swarm_after = daemon.swarm_metadata
    identities_after: Dict[str, Tuple[str, str]] = {}
    for agent_id, meta in swarm_after.agents.items():
        identities_after[agent_id] = (meta.agent_mail_identity, meta.conversation_id)

    assert identities_after == identities_before

    # Sanity-check: the runtime API still exposes consistent status and
    # overview information for the resumed swarm.
    server = RuntimeApiServer(daemon=daemon)

    status = server.get_runtime_status()
    assert status["status"] == RuntimeStatus.RUNNING.value
    assert status["project_path"] == str(config.project_path)
    assert status["swarm_id"] == config.swarm_id

    counts = status["agent_counts"]
    assert counts["total"] == len(identities_before)

    overview = server.get_swarm_overview()
    assert overview["swarm_id"] == config.swarm_id
    assert overview["project_path"] == str(config.project_path)
    assert overview["runtime_status"] == RuntimeStatus.RUNNING.value
    assert len(overview["agents"]) == len(identities_before)
