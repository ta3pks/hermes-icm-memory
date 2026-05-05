"""hermes-icm-memory — Hermes Agent memory provider plugin backed by ICM.

Hermes calls ``register(ctx)`` after loading ``plugin.yaml``. For S01 we
register a placeholder provider so the entry-point + plugin-manifest plumbing
is exercised end-to-end with a passing baseline test. S10 replaces
``_StubProvider`` with the real ``IcmMemoryProvider`` from ``provider.py``.
"""

from __future__ import annotations

from typing import Any

from ._version import __version__

__all__ = ["__version__", "register"]


class _StubProvider:
    """Placeholder memory provider. Replaced in S10 by IcmMemoryProvider."""

    name = "icm"


def register(ctx: Any) -> None:
    """Plugin entry point invoked by Hermes after loading ``plugin.yaml``.

    Constructs a memory provider and registers it with the Hermes context
    exactly once. S01 ships a stub; S10 swaps in the real provider.
    """
    provider = _StubProvider()
    ctx.register_memory_provider(provider)
