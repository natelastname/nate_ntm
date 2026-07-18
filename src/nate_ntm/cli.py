"""CLI entrypoint for the nate_ntm runtime and API.

For this feature branch we migrate to a Typer-based CLI as described in
`specs/001-swarm-runtime-orchestrator/tasks.md` (T009 and T010).

The CLI currently exposes a small `runtime` command group with a
`start` subcommand that wires through to the `RuntimeDaemon` startup
semantics without yet starting a real event loop or API server. This is
sufficient for early, API-first validation and can be extended in later
phases.
"""

from __future__ import annotations

from enum import Enum
from pathlib import Path
from typing import Any, Dict, Optional

import asyncio
import json

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
    check_startup_preconditions,
)

# Load a local .env file (if present) before Typer evaluates any
# environment-backed options. This keeps CLI behavior aligned with the
# precedence rules used by :func:`load_runtime_config` while still letting
# real environment variables and explicit CLI arguments win.
load_dotenv()

from .runtime.runner import run_runtime_with_control_api

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
    adapter_mode: Optional[str] = None,
    agent_mail_adapter: Optional[str] = None,
    acp_adapter: Optional[str] = None,
    nate_oha_config: Optional[Path] = None,
    nate_oha_runtime_mode: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    prompt_soul_content: Optional[str] = None,
) -> RuntimeConfig:
    """Resolve a RuntimeConfig from CLI options.

    For now we require an explicit `--project` path to keep behavior
    simple and predictable. Adapter-related options and the small set of
    nate-oha launch-related options are forwarded to :func:`load_runtime_config`,
    which is responsible for validating and normalizing them.
    """

    return load_runtime_config(
        project_path=project,
        adapter_mode=adapter_mode,
        agent_mail_adapter=agent_mail_adapter,
        acp_adapter=acp_adapter,
        nate_oha_config_path=nate_oha_config,
        nate_oha_runtime_mode=nate_oha_runtime_mode,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        prompt_soul_content=prompt_soul_content,
    )


@runtime_app.command("start")
def runtime_start(
    project: Path = typer.Option(
        ..., "--project", "-p", exists=True, file_okay=False, dir_okay=True,
        help="Project directory containing or adjacent to .nate_ntm/",
    ),
    mode: CliStartupMode = typer.Option(
        CliStartupMode.RESUME,
        "--mode",
        help="Startup mode: create a new swarm or resume an existing one.",
    ),
    agents: int | None = typer.Option(
        None,
        "--agents",
        "-n",
        help=(
            "Number of agents to create when starting in create mode. "
            "Must not be used with --mode=resume."
        ),
    ),
    adapter_mode: Optional[str] = typer.Option(
        None,
        "--adapter-mode",
        help=(
            "Default adapter mode for runtime integrations (for this "
            "release, typically 'fake')."
        ),
    ),
    agent_mail_adapter: Optional[str] = typer.Option(
        None,
        "--agent-mail-adapter",
        help=(
            "Override adapter implementation for Agent Mail (e.g. 'fake'). "
            "Defaults to the value of --adapter-mode when omitted."
        ),
    ),
    acp_adapter: Optional[str] = typer.Option(
        None,
        "--acp-adapter",
        help=(
            "Override adapter implementation for ACP/OpenHands (e.g. 'fake'). "
            "Defaults to the value of --adapter-mode when omitted."
        ),
    ),
    nate_oha_config: Optional[Path] = typer.Option(
        None,
        "--nate-oha-config",
        exists=True,
        file_okay=True,
        dir_okay=False,
        resolve_path=True,
        help=(
            "Base nate-oha JSON configuration file to pass to `nate-oha acp` "
            "via --config."
        ),
    ),
    nate_oha_runtime_mode: Optional[str] = typer.Option(
        None,
        "--nate-oha-runtime-mode",
        help=(
            "Default nate-oha runtime.mode for agents (for example, 'echo' "
            "or 'agent')."
        ),
    ),
    llm_model: Optional[str] = typer.Option(
        None,
        "--llm-model",
        help="Default LLM model identifier to pass as llm.model to nate-oha.",
    ),
    llm_api_key: Optional[str] = typer.Option(
        None,
        "--llm-api-key",
        envvar="NATE_NTM_LLM_API_KEY",
        help=(
            "API key for the configured LLM (llm.api_key). Prefer the "
            "environment variable for production setups."
        ),
    ),
    prompt_soul_content: Optional[str] = typer.Option(
        None,
        "--prompt-soul-content",
        help="Override prompt.soul_content passed to nate-oha.",
    ),
    with_control_api: bool = typer.Option(
        False,
        "--with-control-api",
        help=(
            "Run the FastAPI/JSON-RPC control API alongside the daemon and "
            "block until a shutdown is requested via the runtime API."
        ),
    ),
) -> None:
    """Start the nate_ntm runtime daemon for a given project.

    In ``create`` mode this will create fresh swarm metadata under the
    project's metadata directory. In ``resume`` mode it will load
    existing metadata.

    When ``--with-control-api`` is provided, this command also starts the
    FastAPI/JSON-RPC control API bound to the configured host/port and
    blocks until a graceful shutdown is requested via the runtime API.
    Without this flag, it exercises a short start → shutdown cycle for
    smoke-testing daemon wiring.
    """

    config = _resolve_runtime_config(
        project,
        adapter_mode=adapter_mode,
        agent_mail_adapter=agent_mail_adapter,
        acp_adapter=acp_adapter,
        nate_oha_config=nate_oha_config,
        nate_oha_runtime_mode=nate_oha_runtime_mode,
        llm_model=llm_model,
        llm_api_key=llm_api_key,
        prompt_soul_content=prompt_soul_content,
    )

    # Map CLI startup mode onto the runtime's StartupMode enum.
    runtime_mode = (
        StartupMode.CREATE if mode is CliStartupMode.CREATE else StartupMode.RESUME
    )

    # ``--agents`` is only meaningful when creating a new swarm. Reject it
    # explicitly in resume mode so that users do not assume it will resize an
    # existing swarm, and require a positive value when provided.
    if agents is not None:
        if mode is CliStartupMode.RESUME:
            raise typer.BadParameter(
                "--agents can only be used with --mode=create",  # type: ignore[arg-type]
            )
        if agents <= 0:
            raise typer.BadParameter(
                "--agents must be a positive integer when used with --mode=create",  # type: ignore[arg-type]
            )

    try:
        if with_control_api:
            # Delegate to the higher-level runner, which will construct the
            # daemon, start the scheduler, and serve the control API until a
            # shutdown is requested.
            run_runtime_with_control_api(
                config,
                runtime_mode,
                agent_count=agents,
            )
            return

        if mode is CliStartupMode.CREATE:
            daemon = RuntimeDaemon.create(config, agent_count=agents)
        else:
            daemon = RuntimeDaemon.resume(config)
    except (MetadataAlreadyExistsError, MetadataMissingError) as exc:
        # Surface startup precondition failures as a non-zero exit code
        # without printing a full stack trace.
        raise typer.Exit(code=1) from exc

    # For now we do not run a long-lived loop; simply exercise the state
    # transitions to ensure wiring is correct.
    daemon.start()
    daemon.request_shutdown()
    daemon.mark_stopped()



@api_app.command("call")
def api_call(
    method: str = typer.Argument(
        ..., help="JSON-RPC method name, e.g. runtime.get_status"
    ),
    param: list[str] = typer.Option(  # type: ignore[assignment]
        [],
        "--param",
        "-P",
        help=(
            "Request parameter in key=value form. Values are interpreted as "
            "JSON when possible (for example, --param max_events=10 or "
            "--param agent_ids='[\"a1\", \"a2\"]')."
        ),
    ),
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        envvar="NATE_NTM_CONTROL_HOST",
        help="Control API host (default: 127.0.0.1)",
    ),
    port: int = typer.Option(  # type: ignore[assignment]
        8765,
        "--port",
        envvar="NATE_NTM_CONTROL_PORT",
        help="Control API TCP port (default: 8765)",
    ),
) -> None:
    """Invoke a runtime control API method via JSON-RPC over HTTP.

    This command is a thin wrapper over :class:`JsonRpcHttpClient` and
    is primarily intended for quickstart-style inspection and debugging.
    """

    def _parse_params(pairs: list[str]) -> Dict[str, Any]:
        params: Dict[str, Any] = {}
        for item in pairs:
            if "=" not in item:
                raise typer.BadParameter(
                    f"Invalid parameter {item!r}; expected key=value syntax."
                )

            key, raw = item.split("=", 1)
            key = key.strip()
            raw = raw.strip()

            if not key:
                raise typer.BadParameter("Parameter key must not be empty")

            # Attempt to parse as JSON first so that callers can pass
            # numbers, booleans, objects, and arrays without additional
            # quoting. Fall back to the raw string if parsing fails.
            try:
                value = json.loads(raw)
            except json.JSONDecodeError:
                value = raw

            params[key] = value

        return params

    params = _parse_params(param)

    client = JsonRpcHttpClient(host=host, port=port)

    try:
        # Use the higher-level ``call_for_result`` helper so JSON-RPC
        # errors are surfaced as :class:`JsonRpcClientError` exceptions.
        result = asyncio.run(client.call_for_result(method, params or {}))
    except JsonRpcClientError as exc:
        # Render the structured error payload to stderr and exit with a
        # non-zero status code, mirroring the behaviour for raw
        # ``error`` envelopes in earlier iterations.
        error_payload: Dict[str, Any] = {"code": exc.code, "message": exc.message}
        if exc.data is not None:
            error_payload["data"] = exc.data
        typer.echo(json.dumps(error_payload, indent=2, sort_keys=True), err=True)
        raise typer.Exit(code=1) from exc
    except Exception as exc:  # pragma: no cover - defensive
        typer.echo(f"Error calling runtime API: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    # Where practical, normalise known method results through the shared
    # Pydantic models so the CLI relies on the same schema as server and
    # client code.
    if method == "runtime.get_status":
        payload = RuntimeStatusResult.model_validate(result).model_dump()
    elif method == "swarm.get_overview":
        payload = SwarmOverviewResult.model_validate(result).model_dump()
    elif method == "agent.get_detail":
        payload = AgentDetailResult.model_validate(result).model_dump()
    else:
        payload = result

    typer.echo(json.dumps(payload, indent=2, sort_keys=True))




@app.command("console")
def console(
    host: str = typer.Option(
        "127.0.0.1",
        "--host",
        envvar="NATE_NTM_CONTROL_HOST",
        help="Runtime control API host (default: 127.0.0.1)",
    ),
    port: int = typer.Option(  # type: ignore[assignment]
        8765,
        "--port",
        envvar="NATE_NTM_CONTROL_PORT",
        help="Runtime control API TCP port (default: 8765)",
    ),
) -> None:
    """Launch the Textual runtime console.

    This command starts the Textual-based monitoring console connected to a
    single runtime instance. The console uses one shared :class:`RuntimeSession`
    provided by this entrypoint and shares it across all screens and widgets.
    """

    # Import locally so that Textual and TUI dependencies are only loaded when
    # the console command is actually invoked.
    from .api.runtime_client import RuntimeClient
    from .tui.app import ConsoleApp
    from .tui.runtime_session import RuntimeSession

    async def _run_console() -> None:
        client = RuntimeClient(host=host, port=port)
        session = RuntimeSession(client=client)
        try:
            await session.connect()
        except Exception as exc:
            # Surface connection failures as a clean, non-zero exit code rather
            # than starting Textual and then failing immediately.
            typer.echo(f"Failed to connect to runtime at {host}:{port}: {exc}", err=True)
            raise typer.Exit(code=1) from exc

        app_instance = ConsoleApp(session=session)
        try:
            await app_instance.run_async()
        finally:
            # Best-effort disconnect; errors here should not mask the original
            # reason for application shutdown.
            try:
                await session.disconnect()
            except Exception:
                pass

    asyncio.run(_run_console())


def cli() -> None:
    """Primary console_script entrypoint (for pyproject.toml)."""

    app()