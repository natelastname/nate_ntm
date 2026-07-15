from __future__ import annotations

"""Compatibility helpers for importing Nate OHA configuration models.

This module provides a stable alias :class:`NateOhaConfig` that maps onto the
actual configuration class exported by :mod:`nate_oha.config`.

The upstream library currently exposes :class:`NateOHAConfig`; design docs in
this repository refer to :class:`NateOhaConfig`. Importing via this module
avoids coupling the runtime to the exact upstream class name.
"""

from typing import TYPE_CHECKING

try:  # Prefer the PEP-8 style name when available.
    from nate_oha.config import NateOhaConfig as _NateOhaConfig  # type: ignore[attr-defined]
except Exception:  # pragma: no cover - fallback for older nate_oha releases
    from nate_oha.config import NateOHAConfig as _NateOhaConfig  # type: ignore[import-not-found]

from nate_oha.config import load_nate_oha_config as _load_nate_oha_config

if TYPE_CHECKING:  # pragma: no cover - type checkers see the concrete type
    from nate_oha.config import NateOHAConfig as NateOhaConfig
else:
    NateOhaConfig = _NateOhaConfig

load_nate_oha_config = _load_nate_oha_config

__all__ = ["NateOhaConfig", "load_nate_oha_config"]
