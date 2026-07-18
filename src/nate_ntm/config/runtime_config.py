"""Runtime configuration model and loader for nate_ntm.

This module defines :class:`RuntimeConfig` and a small loader helper
:func:`load_runtime_config` that resolve the project directory, metadata
location, and runtime control API configuration from a combination of
explicit arguments and environment variables.

It is intentionally small and conservative so it can be used across the
runtime daemon, CLI entrypoints, and tests without pulling in additional
configuration frameworks.

Key references:
- specs/001-swarm-runtime-orchestrator/spec.md (requirements, especially FR-013)
- specs/001-swarm-runtime-orchestrator/data-model.md
- .beads/desc-sro-b1-config.md
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Mapping, Optional, Union

import os
from dotenv import dotenv_values, find_dotenv

__all__ = ["AdapterKind", "RuntimeConfig", "load_runtime_config"]


_DEFAULT_CONTROL_HOST = "127.0.0.1"
_DEFAULT_CONTROL_PORT = 8765
_DEFAULT_SWARM_ID = "default"


class AdapterKind(str, Enum):
    """Adapter selection for runtime integrations.

    The runtime supports multiple families of integration adapters (for
    example, Agent Mail and ACP clients). ``AdapterKind`` provides a
    small, explicit vocabulary for selecting which implementation to use
    for a given run.

    For T100 and the US1–US3 baseline the only fully implemented mode is
    ``"fake"``, which uses in-memory, dev-mode adapters that do not
    perform any external I/O. The ``"real"`` mode is reserved for
    production-ready adapters that talk to actual services and will
    raise a clear ``NotImplementedError`` until the corresponding Phase
    6 tasks are completed.
    """

    FAKE = "fake"
    REAL = "real"



@dataclass(frozen=True, slots=True)
class RuntimeConfig:
    """Configuration for a single nate_ntm runtime instance.

    Parameters are resolved from a combination of explicit arguments,
    environment variables, and safe defaults. The resulting object is
    immutable and safe to share across the runtime daemon and clients.
    """

    project_path: Path
    """Absolute path to the project directory managed by this runtime."""

    metadata_dir: Path
    """Directory used for nate_ntm's project-local metadata (e.g. ``.nate_ntm/``)."""

    control_api_host: str = _DEFAULT_CONTROL_HOST
    """Host/interface for the runtime control API (loopback by default)."""

    control_api_port: int = _DEFAULT_CONTROL_PORT
    """TCP port for the runtime control API (non-privileged by default)."""

    swarm_id: str = _DEFAULT_SWARM_ID
    """Logical identifier for the swarm within the project (e.g. ``"default"``)."""


    adapter_mode: AdapterKind = AdapterKind.FAKE
    """Default adapter selection for runtime integrations.

    This field controls which concrete adapter implementations are used
    for integration points such as Agent Mail and ACP. It provides a
    coarse global default that can be overridden per adapter via
    :attr:`agent_mail_adapter` and :attr:`acp_adapter`.
    """

    agent_mail_adapter: Optional[AdapterKind] = None
    """Optional override for the Agent Mail adapter kind.

    When ``None`` (the default), :attr:`adapter_mode` is used to select
    the implementation. When set, this value takes precedence over the
    global adapter mode.
    """

    acp_adapter: Optional[AdapterKind] = None
    """Optional override for the ACP adapter kind.

    When ``None`` (the default), :attr:`adapter_mode` is used to select
    the implementation. When set, this value takes precedence over the
    global adapter mode.
    """

    agent_mail_project: Optional[str] = None
    """Optional Agent Mail project identifier used for nate-oha launches.

    This is a swarm-level identifier used when launching ``nate-oha acp``
    with Agent Mail integration enabled. It is resolved by
    :func:`load_runtime_config` from, in order of precedence:

    * the explicit ``agent_mail_project`` argument
    * ``NATE_NTM_AGENT_MAIL_PROJECT``
    * ``AGENT_MAIL_PROJECT``

    When an agent has ``AgentMetadata.agent_mail_identity`` configured,
    this field must be non-empty; launching nate-oha with Agent Mail
    enabled but without a project identifier is considered a
    misconfiguration and may cause the runtime to fail agent startup.
    """

    agent_mail_upstream_url: Optional[str] = None
    """Optional Agent Mail upstream URL used for nate-oha launches.

    The URL of the upstream Agent Mail MCP endpoint that nate-oha should
    connect to when Agent Mail integration is enabled. It is resolved by
    :func:`load_runtime_config` from, in order of precedence:

    * the explicit ``agent_mail_upstream_url`` argument
    * ``NATE_NTM_AGENT_MAIL_URL``
    * ``AGENT_MAIL_UPSTREAM_URL``
    * ``AGENT_MAIL_URL`` (legacy/compatibility alias)
    """

    nate_oha_executable: str = "nate-oha"
    """Executable used to launch nate-oha (for example, ``"nate-oha"``)."""

    nate_oha_config_path: Path | None = None
    """Optional base nate-oha JSON configuration file passed via ``--config``.

    When :data:`None`, higher-level components are responsible for selecting a
    suitable default or refusing to launch until one is provided.
    """

    nate_oha_runtime_mode: str | None = None
    """Optional default nate-oha ``runtime.mode`` (for example, ``"echo"``).

    When :data:`None`, the adapter layer may select a mode-specific default
    based on the current feature or test scenario.
    """

    llm_model: str | None = None
    """Optional default model identifier supplied via ``llm.model`` overrides."""

    llm_api_key: str | None = None
    """Optional API key for the configured LLM (``llm.api_key``).

    In most environments this should be provided via a process environment
    variable rather than a command-line option for security reasons.
    """

    prompt_soul_content: str | None = None
    """Optional ``prompt.soul_content`` override for nate-oha launches."""

    agent_mail_enabled: bool | None = None
    """Optional flag indicating whether Agent Mail integration is enabled.

    When :data:`None`, Agent Mail enablement is left to higher-level
    defaults. When :data:`True` or :data:`False`, this value is used
    directly by adapter construction and nate-oha launch helpers.
    """



def load_runtime_config(
    *,
    project_path: Optional[Path | str] = None,
    metadata_dir: Optional[Path | str] = None,
    control_api_host: Optional[str] = None,
    control_api_port: Optional[int | str] = None,
    swarm_id: Optional[str] = None,
    adapter_mode: Optional[Union[str, AdapterKind]] = None,
    agent_mail_adapter: Optional[Union[str, AdapterKind]] = None,
    acp_adapter: Optional[Union[str, AdapterKind]] = None,
    agent_mail_project: Optional[str] = None,
    agent_mail_upstream_url: Optional[str] = None,
    nate_oha_executable: Optional[str] = None,
    nate_oha_config_path: Optional[Path | str] = None,
    nate_oha_runtime_mode: Optional[str] = None,
    llm_model: Optional[str] = None,
    llm_api_key: Optional[str] = None,
    prompt_soul_content: Optional[str] = None,
    agent_mail_enabled: Optional[bool] = None,
    env: Optional[Mapping[str, str]] = None,
) -> RuntimeConfig:
    """Construct :class:`RuntimeConfig` from arguments and environment.

    Resolution order for each field:

    * Explicit function argument (if provided).
    * Environment variable (from ``env`` when provided, otherwise from a
      combination of a local ``.env`` file and ``os.environ``).
    * Safe default (for host/port/swarm_id/adapter_mode) or derived value
      (for paths).

    When ``env`` is :data:`None`, a ``.env`` file in the current working
    directory (or its parents) is loaded with :func:`python_dotenv.dotenv_values`
    and then overlaid with the real process environment. This gives the
    precedence order:

    * explicit arguments
    * real environment variables
    * values from the ``.env`` file

    Environment variable names:

    * ``NATE_NTM_PROJECT_DIR`` – project directory
    * ``NATE_NTM_METADATA_DIR`` – metadata directory
    * ``NATE_NTM_CONTROL_HOST`` – control API host
    * ``NATE_NTM_CONTROL_PORT`` – control API port
    * ``NATE_NTM_SWARM_ID`` – swarm identifier
    * ``NATE_NTM_ADAPTER_MODE`` – default adapter kind (e.g. ``"fake"``)
    * ``NATE_NTM_AGENT_MAIL_ADAPTER`` – Agent Mail adapter override
    * ``NATE_NTM_ACP_ADAPTER`` – ACP adapter override
    * ``NATE_NTM_AGENT_MAIL_PROJECT`` – Agent Mail project identifier
    * ``NATE_NTM_AGENT_MAIL_URL`` – Agent Mail upstream URL (MCP endpoint)
    * ``NATE_NTM_NATE_OHA_EXECUTABLE`` – nate-oha executable name/path
    * ``NATE_NTM_NATE_OHA_CONFIG`` – base nate-oha JSON config (``--config``)
    * ``NATE_NTM_NATE_OHA_RUNTIME_MODE`` – default nate-oha ``runtime.mode``
    * ``NATE_NTM_LLM_MODEL`` – default model identifier (``llm.model``)
    * ``NATE_NTM_LLM_API_KEY`` – API key for the configured LLM (``llm.api_key``)
    * ``NATE_NTM_PROMPT_SOUL_CONTENT`` – ``prompt.soul_content`` override
    * ``NATE_NTM_AGENT_MAIL_ENABLED`` – explicit Agent Mail enabled/disabled flag

    In addition to the ``NATE_NTM_*`` variables, the loader also honors
    the following Agent Mail compatibility variables as fallbacks:

    * ``AGENT_MAIL_PROJECT`` – fallback source for the project identifier
    * ``AGENT_MAIL_UPSTREAM_URL`` – fallback source for the upstream URL
    * ``AGENT_MAIL_URL`` – legacy/short alias for the upstream URL
    """

    env_mapping: Mapping[str, str]
    if env is None:
        # Start with values from a local .env file (if present, discovered
        # from the current working directory) and then overlay the real
        # process environment so that ``os.environ`` wins over the file.
        # This snapshot is then used for all subsequent resolution so later
        # mutations to ``os.environ`` do not affect an already-computed
        # configuration.
        dotenv_path = find_dotenv(usecwd=True)
        if dotenv_path:
            file_env = {k: v for k, v in dotenv_values(dotenv_path).items() if v is not None}
        else:
            file_env = {}
        merged: dict[str, str] = dict(file_env)
        merged.update(os.environ)  # type: ignore[arg-type]
        env_mapping = merged
    else:
        env_mapping = env

    resolved_project_path = _resolve_project_path(project_path, env_mapping)
    resolved_metadata_dir = _resolve_metadata_dir(metadata_dir, resolved_project_path, env_mapping)
    resolved_host = _resolve_control_host(control_api_host, env_mapping)
    resolved_port = _resolve_control_port(control_api_port, env_mapping)
    resolved_swarm_id = _resolve_swarm_id(swarm_id, env_mapping)
    resolved_adapter_mode = _resolve_adapter_kind_option(
        adapter_mode,
        env_mapping.get("NATE_NTM_ADAPTER_MODE"),
        field_name="adapter_mode",
        default=AdapterKind.FAKE,
    )
    resolved_agent_mail_adapter = _resolve_adapter_kind_option(
        agent_mail_adapter,
        env_mapping.get("NATE_NTM_AGENT_MAIL_ADAPTER"),
        field_name="agent_mail_adapter",
        default=None,
    )
    resolved_acp_adapter = _resolve_adapter_kind_option(
        acp_adapter,
        env_mapping.get("NATE_NTM_ACP_ADAPTER"),
        field_name="acp_adapter",
        default=None,
    )
    resolved_agent_mail_project = _resolve_agent_mail_project(agent_mail_project, env_mapping)
    resolved_agent_mail_upstream = _resolve_agent_mail_upstream_url(
        agent_mail_upstream_url, env_mapping
    )
    resolved_nate_oha_executable = _resolve_nate_oha_executable(
        nate_oha_executable, env_mapping
    )
    resolved_nate_oha_config_path = _resolve_nate_oha_config_path(
        nate_oha_config_path, resolved_project_path, env_mapping
    )
    resolved_nate_oha_runtime_mode = _resolve_optional_str_option(
        nate_oha_runtime_mode,
        env_mapping.get("NATE_NTM_NATE_OHA_RUNTIME_MODE"),
    )
    resolved_llm_model = _resolve_optional_str_option(
        llm_model,
        env_mapping.get("NATE_NTM_LLM_MODEL"),
    )
    resolved_llm_api_key = _resolve_optional_str_option(
        llm_api_key,
        env_mapping.get("NATE_NTM_LLM_API_KEY"),
    )
    resolved_prompt_soul_content = _resolve_optional_str_option(
        prompt_soul_content,
        env_mapping.get("NATE_NTM_PROMPT_SOUL_CONTENT"),
    )
    resolved_agent_mail_enabled = _resolve_agent_mail_enabled_option(
        agent_mail_enabled,
        env_mapping,
    )

    return RuntimeConfig(
        project_path=resolved_project_path,
        metadata_dir=resolved_metadata_dir,
        control_api_host=resolved_host,
        control_api_port=resolved_port,
        swarm_id=resolved_swarm_id,
        adapter_mode=resolved_adapter_mode,
        agent_mail_adapter=resolved_agent_mail_adapter,
        acp_adapter=resolved_acp_adapter,
        agent_mail_project=resolved_agent_mail_project,
        agent_mail_upstream_url=resolved_agent_mail_upstream,
        nate_oha_executable=resolved_nate_oha_executable,
        nate_oha_config_path=resolved_nate_oha_config_path,
        nate_oha_runtime_mode=resolved_nate_oha_runtime_mode,
        llm_model=resolved_llm_model,
        llm_api_key=resolved_llm_api_key,
        prompt_soul_content=resolved_prompt_soul_content,
        agent_mail_enabled=resolved_agent_mail_enabled,
    )



def _coerce_adapter_kind(raw: str, *, field_name: str) -> AdapterKind:
    """Parse ``raw`` into an :class:`AdapterKind`.

    This helper centralizes validation of adapter selection values from
    function arguments or environment variables. It accepts the
    lower-case string names of :class:`AdapterKind` members and raises a
    :class:`ValueError` for anything else so that misconfiguration fails
    fast and clearly.
    """

    normalized = raw.strip().lower()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty when provided")

    for kind in AdapterKind:
        if kind.value == normalized:
            return kind

    valid = ", ".join(sorted(k.value for k in AdapterKind))
    raise ValueError(f"Unsupported {field_name!s} {raw!r}; expected one of: {valid}")


def _resolve_adapter_kind_option(
    value: Optional[Union[str, AdapterKind]],
    env_value: Optional[str],
    *,
    field_name: str,
    default: Optional[AdapterKind],
) -> Optional[AdapterKind]:
    """Resolve an optional :class:`AdapterKind` from args/env/default.

    ``value`` (an explicit function argument) wins over ``env_value``
    (from the environment). If neither is provided, ``default`` is
    returned as-is.
    """

    if isinstance(value, AdapterKind):
        return value
    if value is not None:
        return _coerce_adapter_kind(str(value), field_name=field_name)

    if env_value is not None:
        return _coerce_adapter_kind(env_value, field_name=f"env:{field_name}")

    return default


def _resolve_project_path(
    project_path: Optional[Path | str], env: Mapping[str, str]
) -> Path:
    raw = project_path
    if raw is None:
        env_value = env.get("NATE_NTM_PROJECT_DIR")
        if env_value:
            raw = env_value

    if raw is None:
        # Fall back to the current working directory if nothing was provided.
        raw = os.getcwd()

    path = Path(raw).expanduser().resolve()

    if not path.exists() or not path.is_dir():
        raise ValueError(f"Project path does not exist or is not a directory: {path}")

    return path


def _resolve_metadata_dir(
    metadata_dir: Optional[Path | str], project_path: Path, env: Mapping[str, str]
) -> Path:
    raw: Optional[Path | str] = metadata_dir
    if raw is None:
        env_value = env.get("NATE_NTM_METADATA_DIR")
        if env_value:
            raw = env_value

    if raw is None:
        # Default to a `.nate_ntm` subdirectory within the project.
        raw_path = project_path / ".nate_ntm"
    else:
        raw_path = Path(raw)
        if not raw_path.is_absolute():
            # Interpret relative paths as relative to the project directory.
            raw_path = (project_path / raw_path).resolve()
        else:
            raw_path = raw_path.expanduser().resolve()

    _validate_metadata_dir_location(raw_path, project_path)
    return raw_path


def _validate_metadata_dir_location(metadata_dir: Path, project_path: Path) -> None:
    """Ensure ``metadata_dir`` is under or adjacent to ``project_path``.

    * "Under" means ``metadata_dir`` is located within ``project_path``.
    * "Adjacent" means ``metadata_dir`` shares the same parent directory
      as ``project_path``.
    """

    def _is_subpath(child: Path, parent: Path) -> bool:
        try:
            child.relative_to(parent)
            return True
        except ValueError:
            return False

    if _is_subpath(metadata_dir, project_path):
        return

    if metadata_dir.parent == project_path.parent:
        return

    raise ValueError(
        "metadata_dir must be located under the project directory or share "
        "the same parent directory as the project directory"
    )


def _resolve_control_host(host: Optional[str], env: Mapping[str, str]) -> str:
    raw = host or env.get("NATE_NTM_CONTROL_HOST") or _DEFAULT_CONTROL_HOST
    # For now we do not attempt strict validation beyond using a loopback
    # default, but callers may apply stricter policies if desired.
    return raw


def _resolve_control_port(port: Optional[int | str], env: Mapping[str, str]) -> int:
    raw: Optional[int | str] = port
    if raw is None:
        env_value = env.get("NATE_NTM_CONTROL_PORT")
        if env_value is not None:
            raw = env_value

    if raw is None:
        value = _DEFAULT_CONTROL_PORT
    else:
        try:
            value = int(raw)
        except (TypeError, ValueError) as exc:  # pragma: no cover - defensive
            raise ValueError(f"Invalid control API port value: {raw!r}") from exc

    if not (1 <= value <= 65535):
        raise ValueError(f"Control API port must be between 1 and 65535, got {value}")

    if value <= 1024:
        # Reserve privileged ports for future use; the runtime control API
        # should default to and typically use non-privileged ports.
        raise ValueError(
            "Control API port must be a non-privileged TCP port (>1024) for the MVP; "
            f"got {value}"
        )

    return value


def _resolve_swarm_id(swarm_id: Optional[str], env: Mapping[str, str]) -> str:
    raw = swarm_id or env.get("NATE_NTM_SWARM_ID") or _DEFAULT_SWARM_ID
    return raw


def _resolve_agent_mail_project(
    project: Optional[str], env: Mapping[str, str]
) -> Optional[str]:
    """Resolve the Agent Mail project identifier from args/env.

    The precedence order is:

    * explicit ``agent_mail_project`` argument
    * ``NATE_NTM_AGENT_MAIL_PROJECT`` environment variable
    * ``AGENT_MAIL_PROJECT`` environment variable
    """

    if project is not None:
        value = project.strip()
        return value or None

    raw = (
        env.get("NATE_NTM_AGENT_MAIL_PROJECT")
        or env.get("AGENT_MAIL_PROJECT")
        or ""
    )
    value = raw.strip()
    return value or None



def _resolve_agent_mail_upstream_url(
    url: Optional[str], env: Mapping[str, str]
) -> Optional[str]:
    """Resolve the Agent Mail upstream URL from args/env.

    The precedence order is:

    * explicit ``agent_mail_upstream_url`` argument
    * ``NATE_NTM_AGENT_MAIL_URL`` environment variable
    * ``AGENT_MAIL_UPSTREAM_URL`` environment variable
    * ``AGENT_MAIL_URL`` environment variable (legacy alias)
    """

    if url is not None:
        value = url.strip()
        return value or None

    raw = (
        env.get("NATE_NTM_AGENT_MAIL_URL")
        or env.get("AGENT_MAIL_UPSTREAM_URL")
        or env.get("AGENT_MAIL_URL")
        or ""
    )
    value = raw.strip()
    return value or None



def _resolve_nate_oha_executable(
    executable: Optional[str], env: Mapping[str, str]
) -> str:
    """Resolve the nate-oha executable from args/env.

    The precedence order is:

    * explicit ``nate_oha_executable`` argument
    * ``NATE_NTM_NATE_OHA_EXECUTABLE`` environment variable
    * hard-coded default ``"nate-oha"``
    """

    if executable is not None:
        value = executable.strip()
        return value or "nate-oha"

    raw = env.get("NATE_NTM_NATE_OHA_EXECUTABLE", "nate-oha")
    value = raw.strip()
    return value or "nate-oha"



def _resolve_nate_oha_config_path(
    config_path: Optional[Path | str], project_path: Path, env: Mapping[str, str]
) -> Path | None:
    """Resolve the base nate-oha JSON config path from args/env.

    The precedence order is:

    * explicit ``nate_oha_config_path`` argument
    * ``NATE_NTM_NATE_OHA_CONFIG`` environment variable

    When a relative path is provided, it is interpreted as relative to the
    resolved project directory so that tests and simple projects can use
    project-local configuration files.
    """

    raw: Optional[Path | str] = config_path
    if raw is None:
        env_value = env.get("NATE_NTM_NATE_OHA_CONFIG")
        if env_value:
            raw = env_value

    if raw is None:
        return None

    path = Path(raw)
    if not path.is_absolute():
        path = (project_path / path).resolve()
    else:
        path = path.expanduser().resolve()
    return path



def _resolve_optional_str_option(
    value: Optional[str], env_value: Optional[str]
) -> Optional[str]:
    """Resolve an optional string configuration field from args/env.

    ``value`` (an explicit function argument) wins over ``env_value`` (from the
    environment). Empty strings are normalized to :data:`None`.
    """

    if value is not None:
        v = value.strip()
        return v or None

    if env_value is None:
        return None

    v = env_value.strip()
    return v or None



def _parse_bool(raw: str, *, field_name: str) -> bool:
    """Parse a boolean value from a string with helpful errors."""

    normalized = raw.strip().lower()
    if normalized in {"1", "true", "yes", "y", "on"}:
        return True
    if normalized in {"0", "false", "no", "n", "off"}:
        return False

    raise ValueError(f"Invalid boolean value for {field_name}: {raw!r}")



def _resolve_agent_mail_enabled_option(
    value: Optional[bool], env: Mapping[str, str]
) -> Optional[bool]:
    """Resolve the optional Agent Mail enabled flag from args/env.

    ``value`` (an explicit function argument) wins over the
    ``NATE_NTM_AGENT_MAIL_ENABLED`` environment variable. When neither is
    provided, :data:`None` is returned so that higher-level components can
    apply their own defaults.
    """

    if isinstance(value, bool):
        return value

    raw = env.get("NATE_NTM_AGENT_MAIL_ENABLED")
    if raw is None:
        return None

    return _parse_bool(raw, field_name="env:agent_mail_enabled")

