"""Command-line interface for nate-ntm."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv
from nate_oha.config import NateOHAConfig
from pydantic import ValidationError

from .api.client import JsonRpcClientError, JsonRpcHttpClient
from .api.models import AgentDetailResult, RuntimeStatusResult, SwarmOverviewResult
from .config.runtime_config import RuntimeConfig, load_runtime_config
from .runtime.daemon import MetadataAlreadyExistsError, MetadataMissingError, StartupMode
from .runtime.metadata_store import MetadataStore
from .runtime.runner import run_runtime_with_control_api
from .runtime.swarm_state import AgentState, SwarmState

load_dotenv()

app = typer.Typer(help="nate_ntm command-line interface")
runtime_app = typer.Typer(help="Runtime daemon commands")
swarm_app = typer.Typer(help="Swarm metadata commands")
api_app = typer.Typer(help="Runtime control API commands")
app.add_typer(runtime_app, name="runtime")
app.add_typer(swarm_app, name="swarm")
app.add_typer(api_app, name="api")


class CliStartupMode(str, Enum):
    CREATE = "create"
    RESUME = "resume"


def _resolve_runtime_config(
    project: Path,
    *,
    nate_oha_config: Path | None = None,
    nate_oha_runtime_mode: str | None = None,
    llm_model: str | None = None,
    llm_api_key: str | None = None,
    prompt_soul_content: str | None = None,
) -> RuntimeConfig:
    return load_runtime_config(
        project_path=project,
        nate_oha_config_path=nate_oha_config,
        nate_oha_runtime_mode=nate_oha_runtime_mode,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        prompt_soul_content=prompt_soul_content,
    )


@swarm_app.command("create")
def swarm_create(
    project: Path = typer.Option(
        ..., "--project", "-p", exists=True, file_okay=False, dir_okay=True
    ),
    agent: list[Path] = typer.Option(
        ..., "--agent", exists=True, file_okay=True, dir_okay=False, resolve_path=True
    ),
    swarm_id: str = typer.Option("default", "--swarm-id"),
    force: bool = typer.Option(False, "--force"),
    dry_run: bool = typer.Option(False, "--dry-run"),
) -> None:
    """Create one swarm from complete nate-oha JSON configurations."""

    config = load_runtime_config(project_path=project, swarm_id=swarm_id)
    store = MetadataStore(config)
    swarm_path = store.metadata_dir / "swarm.json"
    if swarm_path.exists() and not force:
        raise typer.BadParameter(f"swarm metadata already exists: {swarm_path}")

    agents: dict[str, AgentState] = {}
    for path in agent:
        agent_id = path.stem.strip()
        if not agent_id:
            raise typer.BadParameter(f"invalid agent config filename: {path}")
        if agent_id in agents:
            raise typer.BadParameter(f"duplicate agent id {agent_id!r}")
        try:
            nate_oha_config = NateOHAConfig.model_validate_json(path.read_text(encoding="utf-8"))
        except (OSError, ValidationError, ValueError) as exc:
            raise typer.BadParameter(f"invalid agent config {path}: {exc}") from exc
        agents[agent_id] = AgentState(
            agent_id=agent_id,
            display_name=agent_id.replace("-", " ").replace("_", " ").title(),
            nate_oha_config=nate_oha_config,
        )

    if not agents:
        raise typer.BadParameter("at least one --agent config is required")

    now = datetime.now(timezone.utc)
    swarm = SwarmState(
        swarm_id=config.swarm_id,
        project_path=config.project_path,
        created_at=now,
        last_updated_at=now,
        agents=agents,
    )

    if dry_run:
        typer.echo(swarm.model_dump_json(indent=2))
        return

    store.save_swarm_state(swarm)
    typer.echo(f"Created swarm {swarm.swarm_id!r} with {len(agents)} agents")
    typer.echo(f"Metadata: {swarm_path}")


@runtime_app.command("start")
def runtime_start(
    project: Path = typer.Option(
        ..., "--project", "-p", exists=True, file_okay=False, dir_okay=True
    ),
    mode: CliStartupMode = typer.Option(CliStartupMode.RESUME, "--mode"),
    agents: int | None = typer.Option(None, "--agents", "-n"),
    nate_oha_config: Path | None = typer.Option(
        None,
        "--nate-oha-config",
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
    ),
    nate_oha_runtime_mode: str | None = typer.Option(None, "--nate-oha-runtime-mode"),
    llm_model: str | None = typer.Option(None, "--llm-model"),
    llm_api_key: str | None = typer.Option(
        None, "--llm-api-key", envvar="NATE_NTM_LLM_API_KEY"
    ),
    prompt_soul_content: str | None = typer.Option(None, "--prompt-soul-content"),
    acp_host: str = typer.Option("127.0.0.1", "--acp-host", envvar="NATE_NTM_ACP_HOST"),
    acp_port: int = typer.Option(8766, "--acp-port", envvar="NATE_NTM_ACP_PORT"),
    control_host: str | None = typer.Option(None, "--control-host"),
    control_port: int | None = typer.Option(None, "--control-port"),
) -> None:
    """Create or resume a swarm runtime with TCP ACP and control endpoints."""

    if agents is not None:
        if mode is CliStartupMode.RESUME:
            raise typer.BadParameter("--agents can only be used with --mode=create")
        if agents <= 0:
            raise typer.BadParameter("--agents must be a positive integer")
    if not 0 <= acp_port <= 65535:
        raise typer.BadParameter("--acp-port must be between 0 and 65535")

    config = _resolve_runtime_config(
        project,
        nate_oha_config=nate_oha_config,
        nate_oha_runtime_mode=nate_oha_runtime_mode,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        prompt_soul_content=prompt_soul_content,
    )
    startup_mode = (
        StartupMode.CREATE if mode is CliStartupMode.CREATE else StartupMode.RESUME
    )

    typer.echo(f"Swarm ACP: tcp://{acp_host}:{acp_port}", err=True)
    typer.echo(
        f"Control API: http://{control_host or config.control_api_host}:"
        f"{control_port if control_port is not None else config.control_api_port}",
        err=True,
    )
    try:
        run_runtime_with_control_api(
            config,
            startup_mode,
            host=control_host,
            port=control_port,
            acp_host=acp_host,
            acp_port=acp_port,
            agent_count=agents,
        )
    except (MetadataAlreadyExistsError, MetadataMissingError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1) from exc
    except OSError as exc:
        typer.echo(f"Failed to start runtime: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def _parse_params(pairs: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {}
    for item in pairs:
        if "=" not in item:
            raise typer.BadParameter(
                f"Invalid parameter {item!r}; expected key=value syntax."
            )
        key, raw = (part.strip() for part in item.split("=", 1))
        if not key:
            raise typer.BadParameter("Parameter key must not be empty")
        try:
            params[key] = json.loads(raw)
        except json.JSONDecodeError:
            params[key] = raw
    return params


@api_app.command("call")
def api_call(
    method: str = typer.Argument(...),
    param: list[str] = typer.Option([], "--param", "-P"),  # type: ignore[assignment]
    host: str = typer.Option("127.0.0.1", "--host", envvar="NATE_NTM_CONTROL_HOST"),
    port: int = typer.Option(8765, "--port", envvar="NATE_NTM_CONTROL_PORT"),
) -> None:
    """Invoke one runtime control API method."""

    client = JsonRpcHttpClient(host=host, port=port)
    try:
        result = asyncio.run(client.call_for_result(method, _parse_params(param)))
    except JsonRpcClientError as exc:
        payload: dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            payload["data"] = exc.data
        typer.echo(json.dumps(payload, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        typer.echo(f"Error calling runtime API: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    model = {
        "runtime.get_status": RuntimeStatusResult,
        "swarm.get_overview": SwarmOverviewResult,
        "agent.get_detail": AgentDetailResult,
    }.get(method)
    payload = model.model_validate(result).model_dump() if model else result
    typer.echo(json.dumps(payload, indent=2, sort_keys=True))


@app.command("console")
def console(
    host: str = typer.Option("127.0.0.1", "--host", envvar="NATE_NTM_CONTROL_HOST"),
    port: int = typer.Option(8765, "--port", envvar="NATE_NTM_CONTROL_PORT"),
) -> None:
    """Launch the Textual runtime console."""

    from .api.runtime_client import RuntimeClient
    from .tui.app import ConsoleApp
    from .tui.runtime_session import RuntimeSession

    async def run() -> None:
        session = RuntimeSession(client=RuntimeClient(host=host, port=port))
        await session.connect()
        try:
            await ConsoleApp(session=session).run_async()
        finally:
            await session.disconnect()

    try:
        asyncio.run(run())
    except Exception as exc:
        typer.echo(f"Failed to connect to runtime at {host}:{port}: {exc}", err=True)
        raise typer.Exit(code=1) from exc


def cli() -> None:
    app()
