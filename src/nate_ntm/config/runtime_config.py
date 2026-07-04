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
from pathlib import Path
from typing import Mapping, Optional

import os

__all__ = ["RuntimeConfig", "load_runtime_config"]


_DEFAULT_CONTROL_HOST = "127.0.0.1"
_DEFAULT_CONTROL_PORT = 8765
_DEFAULT_SWARM_ID = "default"


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


def load_runtime_config(
    *,
    project_path: Optional[Path | str] = None,
    metadata_dir: Optional[Path | str] = None,
    control_api_host: Optional[str] = None,
    control_api_port: Optional[int | str] = None,
    swarm_id: Optional[str] = None,
    env: Optional[Mapping[str, str]] = None,
) -> RuntimeConfig:
    """Construct :class:`RuntimeConfig` from arguments and environment.

    Resolution order for each field:

    * Explicit function argument (if provided).
    * Environment variable (if provided in ``env`` or ``os.environ``).
    * Safe default (for host/port/swarm_id) or derived value (for paths).

    Environment variable names:

    * ``NATE_NTM_PROJECT_DIR`` – project directory
    * ``NATE_NTM_METADATA_DIR`` – metadata directory
    * ``NATE_NTM_CONTROL_HOST`` – control API host
    * ``NATE_NTM_CONTROL_PORT`` – control API port
    * ``NATE_NTM_SWARM_ID`` – swarm identifier
    """

    env_mapping: Mapping[str, str]
    if env is None:
        # Copy to a plain dict so later mutations to os.environ do not
        # affect an already-computed configuration.
        env_mapping = dict(os.environ)  # type: ignore[arg-type]
    else:
        env_mapping = env

    resolved_project_path = _resolve_project_path(project_path, env_mapping)
    resolved_metadata_dir = _resolve_metadata_dir(metadata_dir, resolved_project_path, env_mapping)
    resolved_host = _resolve_control_host(control_api_host, env_mapping)
    resolved_port = _resolve_control_port(control_api_port, env_mapping)
    resolved_swarm_id = _resolve_swarm_id(swarm_id, env_mapping)

    return RuntimeConfig(
        project_path=resolved_project_path,
        metadata_dir=resolved_metadata_dir,
        control_api_host=resolved_host,
        control_api_port=resolved_port,
        swarm_id=resolved_swarm_id,
    )


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
