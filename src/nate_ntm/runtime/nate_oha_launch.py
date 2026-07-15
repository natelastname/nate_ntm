from __future__ import annotations

"""Nate OHA launch specification and command construction helpers.

This module centralizes the construction of the command-line arguments used
to launch the Nate OHA ACP runtime (``nate-oha acp``).

It is designed around the base-config-plus-overrides model described in
``specs/005-nate-oha-migration/spec.md`` (see FR-012 "+ FR-013" and
user story P5):

* The runtime always launches Nate OHA from a base JSON configuration
  supplied via ``--config``.
* Swarm- and agent-specific configuration is supplied via repeated
  ``--set path=value`` arguments.
* When resuming an existing conversation, the opaque, ACP-owned
  conversation identifier is passed via ``--resume``.

The :class:`NateOhaLaunchSpec` dataclass is an internal, runtime-owned
representation of a single Nate OHA process launch. It is intentionally
narrow and independent of the ACP SDK and runtime metadata types so it can
be unit tested in isolation and reused across different integration points.
"""

from dataclasses import dataclass, field
import json
from pathlib import Path
import tempfile
from typing import Mapping, MutableMapping, Sequence

__all__ = ["NateOhaLaunchSpec", "build_nate_oha_launch_spec"]


@dataclass(frozen=True, slots=True)
class NateOhaLaunchSpec:
    """Specification for launching a single Nate OHA ACP process.

    This structure captures the runtime-owned inputs that determine how a
    Nate OHA agent process should be launched. It does **not** attempt to
    model the full Nate OHA or OpenHands configuration; instead it focuses
    on the subset of values the nate_ntm runtime is responsible for:

    * Selecting the executable and working directory.
    * Choosing the base JSON configuration file.
    * Selecting the runtime mode (for example, ``"echo"`` or ``"agent"``).
    * Providing an optional ACP-owned conversation identifier to resume.
    * Supplying optional model, API key, and prompt overrides.
    * Supplying optional Agent Mail overrides when the integration is
      enabled for a swarm.

    The :meth:`to_argv` helper renders this specification into a concrete
    ``argv`` sequence suitable for :class:`subprocess.Popen` without going
    through an intermediate shell.
    """

    executable: str
    """Executable used to launch Nate OHA (for example, ``"nate-oha"``)."""

    base_config: Path
    """Path to the base Nate OHA JSON configuration file passed via
    ``--config``.
    """

    cwd: Path
    """Working directory for the launched process.

    This is not encoded into the ``argv`` itself but is expected to be
    supplied as ``cwd=`` to :func:`subprocess.Popen` by callers.
    """

    runtime_mode: str
    """Nate OHA runtime mode (for example, ``"echo"`` or ``"agent"``)."""

    # Optional, ACP-owned conversation identifier used when resuming an
    # existing session. This is supplied to Nate OHA via ``--resume``
    # exactly as returned by the ACP ``session/new`` flow.
    conversation_id: str | None = None

    # Optional model and credential overrides. The API key is currently
    # passed via a ``--set llm.api_key=...`` argument, which may expose it
    # to local process inspectors. Where possible, prefer secrets embedded
    # in the base Nate OHA configuration or environment variables rather
    # than per-launch overrides.
    model: str | None = None
    api_key: str | None = None

    # Optional prompt soul content override.
    prompt_soul_content: str | None = None

    # Agent Mail integration flags and overrides.
    agent_mail_enabled: bool | None = None
    agent_mail_project: str | None = None
    agent_mail_agent_identity: str | None = None
    agent_mail_credentials_ref: str | None = None
    agent_mail_upstream_url: str | None = None

    # Additional, low-level ``--set`` overrides. These are applied on top of
    # the well-known fields above and can be used to support new configuration
    # paths without changing the public dataclass. Keys are configuration
    # paths (for example, ``"features.some_flag.enabled"``).
    #
    # ``extra_overrides`` MUST NOT attempt to replace values derived from the
    # structured fields on this dataclass (such as ``runtime.mode`` or
    # ``features.agent_mail.enabled``); callers should set the corresponding
    # typed fields instead. :meth:`to_argv` enforces this by raising
    # :class:`ValueError` when a conflicting key is provided.
    extra_overrides: Mapping[str, str] = field(default_factory=dict)

    def _build_override_mapping(self) -> dict[str, str]:
        """Return a mapping of Nate OHA config overrides for this spec.

        Keys are configuration paths (for example, ``"runtime.mode"``) and
        values are their corresponding stringified overrides. This helper is
        shared between :meth:`to_argv` and higher-level configuration helpers
        that need a structured view of the overrides.
        """

        sets: MutableMapping[str, str] = {}

        # Always set runtime.mode explicitly so that echo vs agent behavior is
        # driven entirely by configuration rather than by separate launch
        # paths.
        sets["runtime.mode"] = self.runtime_mode

        if self.model:
            sets["llm.model"] = self.model

        if self.api_key:
            sets["llm.api_key"] = self.api_key

        if self.prompt_soul_content is not None:
            # Allow empty-string souls but still distinguish from "unset".
            sets["prompt.soul_content"] = self.prompt_soul_content

        # Agent Mail configuration: when agent_mail_enabled is explicitly set,
        # emit a corresponding ``features.agent_mail.enabled`` override. When
        # True, also propagate any supplied project/identity/credentials fields.
        if self.agent_mail_enabled is not None:
            sets["features.agent_mail.enabled"] = "true" if self.agent_mail_enabled else "false"

            if self.agent_mail_enabled:
                if self.agent_mail_project:
                    sets["features.agent_mail.project"] = self.agent_mail_project
                if self.agent_mail_agent_identity:
                    sets["features.agent_mail.agent_identity"] = self.agent_mail_agent_identity
                if self.agent_mail_credentials_ref:
                    sets["features.agent_mail.credentials_ref"] = self.agent_mail_credentials_ref
                if self.agent_mail_upstream_url:
                    sets["features.agent_mail.upstream_url"] = self.agent_mail_upstream_url

        # Record the set of configuration paths derived from typed fields so
        # that ``extra_overrides`` cannot silently replace them.
        structured_paths = set(sets.keys())

        # Apply any additional overrides for *new* configuration paths. When an
        # override attempts to target a structured path, raise an error so that
        # callers must instead adjust the corresponding typed field on this
        # dataclass.
        if self.extra_overrides:
            for key, value in self.extra_overrides.items():
                key_str = str(key)
                if key_str in structured_paths:
                    raise ValueError(
                        "extra_overrides may not override structured configuration path "
                        f"{key_str!r}; set the corresponding NateOhaLaunchSpec field instead."
                    )
                sets[key_str] = str(value)

        return dict(sets)

    def iter_overrides(self) -> Sequence[str]:
        """Yield ``"path=value"`` override strings in deterministic order."""

        sets = self._build_override_mapping()
        for path in sorted(sets.keys()):
            yield f"{path}={sets[path]}"

    def to_argv(self) -> Sequence[str]:
        """Render this launch specification as a Nate OHA ``argv`` list.

        The resulting sequence has the general form:

        .. code-block:: text

            <executable> acp \
                --config BASE_CONFIG \
                [--resume CONVERSATION_ID] \
                [--set path=value]...

        ``--set`` arguments are emitted in a deterministic order so that
        tests can assert on the exact argument vector.
        """

        argv: list[str] = [self.executable, "acp", "--config", str(self.base_config)]

        if self.conversation_id:
            argv.extend(["--resume", self.conversation_id])

        for override in self.iter_overrides():
            argv.extend(["--set", override])

        return argv


from ..config.runtime_config import RuntimeConfig
from .metadata_store import AgentMetadata
from .nate_oha_config_compat import NateOhaConfig, load_nate_oha_config


def build_nate_oha_launch_spec(
    *,
    config: RuntimeConfig,
    metadata: AgentMetadata,
) -> NateOhaLaunchSpec:
    """Construct a :class:`NateOhaLaunchSpec` from runtime config and metadata.

    This helper provides the canonical translation from the runtime's
    configuration and per-agent metadata into a Nate OHA launch
    specification. It does **not** perform any subprocess I/O; callers
    are responsible for passing :meth:`NateOhaLaunchSpec.to_argv` and the
    working directory into :class:`subprocess.Popen`.

    The mapping is intentionally conservative and keeps configuration
    ownership aligned with Epic 005:

    * :attr:`RuntimeConfig.nate_oha_executable` selects the binary.
    * :attr:`RuntimeConfig.nate_oha_config_path` provides the base JSON
      configuration passed via ``--config``.
    * :attr:`RuntimeConfig.nate_oha_runtime_mode` (when set) selects the
      runtime mode; callers may enforce additional defaults.
    * :class:`AgentMetadata.conversation_id` (when non-empty) is treated
      as an opaque, ACP-owned session identifier and passed through to
      Nate OHA via ``--resume``.
    * LLM and prompt overrides are taken from
      :attr:`RuntimeConfig.llm_model`, :attr:`RuntimeConfig.llm_api_key`,
      and :attr:`RuntimeConfig.prompt_soul_content`.
    * Agent Mail configuration is derived from
      :attr:`RuntimeConfig.agent_mail_enabled`,
      :attr:`RuntimeConfig.agent_mail_project`,
      :attr:`RuntimeConfig.agent_mail_upstream_url`, and the per-agent
      identity and credentials ref stored in :class:`AgentMetadata`.
    """

    if config.nate_oha_config_path is None:
        raise ValueError(
            "RuntimeConfig.nate_oha_config_path must be set to build a Nate OHA launch spec"
        )

    executable = config.nate_oha_executable
    base_config = config.nate_oha_config_path
    cwd = config.project_path

    # For now we treat the runtime mode as a required value supplied by
    # higher layers (CLI or environment). This keeps the builder simple
    # and leaves room for future policy (for example, echo vs agent
    # defaults based on adapter selection) without baking those
    # decisions in here.
    if not config.nate_oha_runtime_mode:
        raise ValueError(
            "RuntimeConfig.nate_oha_runtime_mode must be set to build a Nate OHA launch spec"
        )

    runtime_mode = config.nate_oha_runtime_mode

    # Conversation identifiers are treated as opaque and passed through
    # exactly as stored in metadata, without generating or inferring
    # values locally.
    conversation_id = metadata.conversation_id or None

    # LLM and prompt overrides are taken directly from the runtime
    # configuration; unset fields are left as ``None`` so that
    # :class:`NateOhaLaunchSpec` omits the corresponding ``--set``.
    model = config.llm_model
    api_key = config.llm_api_key
    prompt_soul_content = config.prompt_soul_content

    # Agent Mail integration. When agent_mail_enabled is explicitly
    # ``False``, we mark it as disabled in the launch spec regardless of
    # any per-agent metadata. When it is explicitly ``True``, we require
    # a minimal set of configuration to be present; otherwise we leave it
    # as ``None`` and allow Nate OHA's defaults to apply.
    agent_mail_enabled = config.agent_mail_enabled
    agent_mail_project = None
    agent_mail_agent_identity = None
    agent_mail_credentials_ref = None
    agent_mail_upstream_url = None

    if agent_mail_enabled:
        agent_mail_project = config.agent_mail_project
        agent_mail_upstream_url = config.agent_mail_upstream_url
        agent_mail_agent_identity = metadata.agent_mail_identity or None
        agent_mail_credentials_ref = metadata.agent_mail_credentials_ref or None

    return NateOhaLaunchSpec(
        executable=executable,
        base_config=base_config,
        cwd=cwd,
        runtime_mode=runtime_mode,
        conversation_id=conversation_id,
        model=model,
        api_key=api_key,
        prompt_soul_content=prompt_soul_content,
        agent_mail_enabled=agent_mail_enabled,
        agent_mail_project=agent_mail_project,
        agent_mail_agent_identity=agent_mail_agent_identity,
        agent_mail_credentials_ref=agent_mail_credentials_ref,
        agent_mail_upstream_url=agent_mail_upstream_url,
    )


def build_effective_nate_oha_config(*, config: RuntimeConfig, metadata: AgentMetadata) -> NateOhaConfig:
    """Build the effective :class:`NateOhaConfig` for an agent.

    This helper mirrors the base-config-plus-overrides model used by
    :func:`build_nate_oha_launch_spec` but returns a validated Nate OHA
    configuration object instead of an ``argv`` list. It is intended for
    persistence via :class:`~nate_ntm.runtime.swarm_state.AgentState` and
    for call sites that prefer to read configuration directly rather than
    re-deriving it from :class:`RuntimeConfig` on each launch.

    The resulting configuration is derived as follows:

    * ``config.nate_oha_config_path`` provides the base JSON file.
    * Overrides are taken from :class:`NateOhaLaunchSpec._build_override_mapping`,
      which corresponds exactly to the ``--set path=value`` arguments that
      would be passed to the Nate OHA CLI.
    * The ACP-owned conversation/session identifier is **not** embedded in
    the configuration; it remains a separate field on
      :class:`AgentMetadata` / :class:`AgentState`.
    """

    spec = build_nate_oha_launch_spec(config=config, metadata=metadata)
    overrides = list(spec.iter_overrides())

    # Delegate validation and override application to nate_oha.config.
    return load_nate_oha_config(spec.base_config, overrides=overrides)



def materialize_nate_oha_config(*, config: NateOhaConfig, prefix: str = "nate-ntm-nate-oha-config-") -> Path:
    """Materialize a :class:`NateOhaConfig` into a temporary JSON file.

    The returned path points to a JSON configuration file placed in a
    dedicated temporary directory created via :func:`tempfile.mkdtemp`.
    Callers are responsible for cleaning up the directory when the
    configuration is no longer needed; it MUST NOT be treated as durable
    project metadata.

    The helper is deliberately tolerant of different configuration model
    implementations. It first prefers Pydantic v2's ``model_dump`` API
    (using ``mode=\"json\"`` when available) and falls back to ``dict()``
    when necessary.
    """

    # Create an isolated temporary directory for this materialized config
    # so callers can safely remove it without affecting any other files.
    tmpdir = Path(tempfile.mkdtemp(prefix=prefix))
    path = tmpdir / "nate-oha-config.json"

    # Accept both Pydantic-style models and plain mappings.
    try:
        # Pydantic v2-style API.
        data = config.model_dump(mode="json")  # type: ignore[attr-defined]
    except AttributeError:  # pragma: no cover - defensive
        if hasattr(config, "dict"):
            data = config.dict()  # type: ignore[call-arg]
        else:  # pragma: no cover - defensive
            raise TypeError(
                "NateOhaConfig instance does not support model_dump() or dict(); "
                "cannot materialize configuration to JSON."
            )

    # Write a stable JSON representation to disk. Sorting keys keeps the
    # output deterministic for tests while remaining a valid Nate OHA
    # configuration file for the CLI.
    path.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
    return path

