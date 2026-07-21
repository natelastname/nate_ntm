"""Command-line interface for nate-ntm."""

from __future__ import annotations

import asyncio
import json
from enum import Enum
from pathlib import Path
from typing import Any

import typer
from dotenv import load_dotenv

from .api.client import JsonRpcClientError, JsonRpcHttpClient
from .api.models import AgentDetailResult, RuntimeStatusResult, SwarmOverviewResult
from .config.runtime_config import RuntimeConfig, load_runtime_config
from .runtime.daemon import (
    MetadataAlreadyExistsError,
    MetadataMissingError,
    RuntimeDaemon,
    StartupMode,
)
from .runtime.runner import run_runtime_with_control_api

load_dotenv()

app = typer.Typer(help="nate_ntm command-line interface")
runtime_app = typer.Typer(help="Runtime daemon commands")
api_app = typer.Typer(help="Runtime control API commands")
app.add_typer(runtime_app, name="runtime")
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
    with_control_api: bool = typer.Option(False, "--with-control-api"),
) -> None:
    """Create or resume a swarm runtime."""

    if agents is not None:
        if mode is CliStartupMode.RESUME:
            raise typer.BadParameter("--agents can only be used with --mode=create")
        if agents <= 0:
            raise typer.BadParameter("--agents must be a positive integer")

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

    try:
        if with_control_api:
            run_runtime_with_control_api(config, startup_mode, agent_count=agents)
            return

        daemon = (
            RuntimeDaemon.create(config, agent_count=agents)
            if startup_mode is StartupMode.CREATE
            else RuntimeDaemon.resume(config)
        )
    except (MetadataAlreadyExistsError, MetadataMissingError) as exc:
        raise typer.Exit(code=1) from exc

    daemon.start()
    daemon.request_shutdown()
    daemon.mark_stopped()


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
