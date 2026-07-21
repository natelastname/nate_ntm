from __future__ import annotations

from datetime import datetime

from nate_ntm.api.runtime_api import create_runtime_api_app
from nate_ntm.api.server import RuntimeApiServer
from nate_ntm.config.runtime_config import load_runtime_config
from nate_ntm.runtime.daemon import RuntimeDaemon, StartupMode
from nate_ntm.runtime.metadata_store import MetadataStore
from nate_ntm.runtime.state import RuntimeState
from nate_ntm.runtime.swarm_state import SwarmState


def test_runtime_api_is_command_only(tmp_path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    config = load_runtime_config(project_path=project)
    now = datetime(2026, 7, 3, 12, 0, 0)
    daemon = RuntimeDaemon(
        config=config,
        metadata_store=MetadataStore(config=config),
        swarm_state=SwarmState(
            swarm_id=config.swarm_id,
            project_path=config.project_path,
            agent_mail_project_id="mail",
            created_at=now,
            last_updated_at=now,
        ),
        state=RuntimeState(config=config),
        startup_mode=StartupMode.RESUME,
    )

    app = create_runtime_api_app(RuntimeApiServer(daemon))

    assert not hasattr(app.state, "publish_event")
    assert not hasattr(app.state, "subscription_clients")
    assert not hasattr(app.state, "client_subscriptions")
