from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from nate_ntm.config.runtime_config import RuntimeConfig, load_runtime_config
from nate_ntm.runtime.acp_client import AcpAgentStatus, BaseAcpClient
from nate_ntm.runtime.adapters import RuntimeAdapters
from nate_ntm.runtime.agent_mail_client import BaseAgentMailClient
from nate_ntm.runtime.daemon import (
    MetadataAlreadyExistsError,
    MetadataMissingError,
    RuntimeDaemon,
    RuntimeStartupError,
    StartupMode,
    check_startup_preconditions,
)
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.state import AgentStatus, RuntimeStatus
from nate_ntm.runtime.swarm_state import AgentState, SwarmState
from nate_oha.config import build_default_config


def _config(tmp_path: Path) -> RuntimeConfig:
    project = tmp_path / "project"
    project.mkdir(parents=True, exist_ok=True)
    config_path = project / "nate-oha.json"
    config_path.write_text(build_default_config().model_dump_json(), encoding="utf-8")
    return load_runtime_config(
        project_path=project,
        env={
            "NATE_NTM_PROJECT_DIR": str(project),
            "NATE_NTM_NATE_OHA_CONFIG": str(config_path),
            "NATE_NTM_NATE_OHA_RUNTIME_MODE": "echo",
        },
    )


def _save_empty_swarm(config: RuntimeConfig) -> None:
    now = datetime(2026, 7, 3, 12, 0, 0)
    MetadataStore(config=config).save_swarm_state(
        SwarmState(
            swarm_id=config.swarm_id,
            project_path=config.project_path,
            agent_mail_project_id="mail",
            created_at=now,
            last_updated_at=now,
        )
    )


@dataclass(slots=True)
class _Mail(BaseAgentMailClient):
    config: RuntimeConfig

    def ensure_project(self) -> str:
        return "mail"

    def ensure_agent_identity(self, agent_id: str) -> str:
        return f"mail:{agent_id}"

    def ensure_agent_identity_with_credentials(
        self,
        agent_id: str,
        credentials_hint: str | None = None,
    ) -> tuple[str, str | None]:
        return self.ensure_agent_identity(agent_id), credentials_hint

    def get_unread_mail_flags(self, agent_ids):
        return {agent_id: False for agent_id in agent_ids}


class _Acp(BaseAcpClient):
    async def start_agent_async(self, agent_id: str, *, metadata: AgentState) -> None:
        return None

    async def stop_agent_async(self, agent_id: str, *, timeout: float) -> None:
        return None

    async def prompt(self, agent_id: str, prompt: str | None = None) -> str | None:
        return None

    async def interrupt(self, agent_id: str) -> None:
        return None

    def get_status(self, agent_id: str) -> AcpAgentStatus:
        return AcpAgentStatus(agent_id=agent_id, state="idle")


def _adapters(config: RuntimeConfig) -> RuntimeAdapters:
    return RuntimeAdapters(agent_mail=_Mail(config), acp=_Acp())


def test_startup_preconditions(tmp_path: Path) -> None:
    config = _config(tmp_path)
    with pytest.raises(MetadataMissingError):
        check_startup_preconditions(config, StartupMode.RESUME)

    _save_empty_swarm(config)
    with pytest.raises(MetadataAlreadyExistsError):
        check_startup_preconditions(config, StartupMode.CREATE)


def test_create_persists_agents_and_resume_loads_them(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = RuntimeDaemon.create(config, agent_count=2, adapters=_adapters(config))

    assert set(daemon.swarm_state.agents) == {"agent-1", "agent-2"}
    assert daemon.swarm_state.agents["agent-1"].nate_oha_config is not None
    assert RuntimeDaemon.resume(config, adapters=_adapters(config)).swarm_state == daemon.swarm_state


def test_lifecycle_registers_agents_and_stops_scheduler(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = RuntimeDaemon.create(config, agent_count=1, adapters=_adapters(config))

    daemon.start()
    assert daemon.state.status is RuntimeStatus.RUNNING
    assert daemon.state.agents["agent-1"].status is AgentStatus.IDLE
    assert daemon.scheduler is not None and daemon.scheduler.running is True

    daemon.request_shutdown()
    assert daemon.state.status is RuntimeStatus.SHUTTING_DOWN
    daemon.mark_stopped()
    assert daemon.state.status is RuntimeStatus.STOPPED
    assert daemon.scheduler.running is False


def test_start_rejects_invalid_transition(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = RuntimeDaemon.create(config, adapters=_adapters(config))
    daemon.mark_stopped()
    with pytest.raises(RuntimeStartupError):
        daemon.start()


def test_status_and_agent_detail_have_no_event_history(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = RuntimeDaemon.create(config, agent_count=1, adapters=_adapters(config))
    daemon.start()

    detail = daemon.get_agent_detail("agent-1")

    assert detail["agent_id"] == "agent-1"
    assert detail["status"] == AgentStatus.IDLE.value
    assert detail["agent_mail_identity"] == "mail:agent-1"
    assert "events" not in detail
    assert daemon.get_swarm_status()["agent_counts"]["total"] == 1


def test_unknown_agent_detail_raises(tmp_path: Path) -> None:
    config = _config(tmp_path)
    daemon = RuntimeDaemon.create(config, adapters=_adapters(config))
    with pytest.raises(KeyError):
        daemon.get_agent_detail("missing")
