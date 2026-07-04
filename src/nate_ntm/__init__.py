"""Top-level :mod:`nate_ntm` package.

The package root is intentionally lightweight; submodules such as
:mod:`nate_ntm.cli` and :mod:`nate_ntm.util` should be imported directly by
callers that need them. This avoids pulling in optional runtime
dependencies during simple tasks like configuration or data-model tests.
"""

__all__ = [
    "__version__",
]

__version__ = "0.1.0"