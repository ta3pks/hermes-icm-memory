"""Typed exceptions raised inside ``cli_runner``.

Caught at the cli_runner boundary and translated into the AD-07 degrade
response by ``tools.py`` / ``hooks.py`` (S08, S09). ``cli_runner`` is the
only module allowed to raise these; downstream modules catch ``ICMError``
or its subtypes broadly at their public boundary.

Architecture invariant (§4.1 invariant 3): this module imports nothing from
the rest of the package — it must remain a leaf in the dependency graph.
"""

from __future__ import annotations

__all__ = [
    "ICMError",
    "ICMMalformedOutputError",
    "ICMNonZeroExitError",
    "ICMNotFoundError",
    "ICMTimeoutError",
]


class ICMError(Exception):
    """Base class for every typed error raised by ``cli_runner``."""


class ICMNotFoundError(ICMError):
    """Raised when the ``icm`` binary cannot be found on PATH."""


class ICMTimeoutError(ICMError):
    """Raised when an ``icm`` invocation exceeds its configured timeout."""


class ICMNonZeroExitError(ICMError):
    """Raised when ``icm`` exits with a non-zero return code; ``args[0]`` carries stderr."""


class ICMMalformedOutputError(ICMError):
    """Raised when ``icm`` stdout cannot be parsed (JSON decode or table parse)."""
