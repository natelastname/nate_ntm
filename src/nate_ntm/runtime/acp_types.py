from __future__ import annotations

"""ACP type aliases used by the runtime.

This module isolates the concrete ACP SDK import paths so that the rest of the
runtime can depend on a single, internal abstraction rather than importing
from :mod:`acp` directly.

In particular, ``SessionUpdate`` represents the typed ACP models delivered to
``Client.session_update`` callbacks. The exact type is provided by the ACP SDK
and may evolve over time; this module should be kept in sync with the
installed SDK version.
"""

from acp import schema as acp_schema

# NOTE:
# -----
# The ACP SDK used by this project does not currently expose a dedicated
# ``SessionUpdate`` union type. Instead, individual update models such as
# ``UserMessageChunk``, ``UsageUpdate``, and ``ToolCallStart`` all inherit
# from ``acp.schema.BaseModel``.
#
# We therefore treat ``BaseModel`` as the common supertype for all
# ``session/update`` payload models. This keeps the runtime strongly typed
# against ACP SDK models (rather than ``Any``) while remaining forward
# compatible with additional update variants.
SessionUpdate = acp_schema.BaseModel

# Typed notification model used when forwarding updates to external ACP
# clients via the concrete Swarm ACP server adapter. This wraps a concrete
# ``SessionUpdate`` together with the ACP session identifier.
SessionNotification = acp_schema.SessionNotification

__all__ = ["SessionUpdate", "SessionNotification"]
