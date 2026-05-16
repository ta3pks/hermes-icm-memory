"""hermes-icm-memory — Hermes Agent memory provider plugin backed by ICM.

Hermes calls :func:`register` after loading ``plugin.yaml``; the function
constructs an :class:`IcmMemoryProvider` (which binds the four declared
hook callbacks ``prefetch`` / ``system_prompt_block`` / ``sync_turn`` /
``on_session_end``) and hands it to ``ctx.register_memory_provider``.

Invariants:

* Module import has no side effects — registration happens only inside the
  explicit :func:`register` call.
* Per AD-12, this module MUST NOT import ``subprocess``.
"""

from __future__ import annotations

from typing import Any

from ._version import __version__
from .provider import IcmMemoryProvider

__all__ = ["IcmMemoryProvider", "__version__", "register"]


def register(ctx: Any) -> None:
    """Hermes plugin entry point.

    v0.4.3 — defensive: called by TWO loaders under ``kind=standalone``:

    1. ``hermes_cli.plugins.PluginManager`` — its ``PluginContext`` has
       ``register_hook`` but NO ``register_memory_provider``.
    2. ``plugins.memory._load_provider_from_dir`` — its ``_ProviderCollector``
       has ``register_memory_provider`` but ``register_hook`` is a no-op.

    Both paths call us; ``hasattr`` checks let each contribute what its ctx
    supports. The ``transform_llm_output`` hook is the primary indicator-
    footer path (programmatic, model-independent); the directive in
    ``system_prompt_block`` is the fallback when the hook isn't wired.
    """
    if hasattr(ctx, "register_memory_provider"):
        ctx.register_memory_provider(IcmMemoryProvider())
    if hasattr(ctx, "register_hook"):
        from .provider import _do_indicator_transform

        ctx.register_hook("transform_llm_output", _do_indicator_transform)
