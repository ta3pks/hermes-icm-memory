"""hermes-icm-memory — Hermes Agent memory provider plugin backed by ICM.

Hermes calls :func:`register` with a plugin context after loading
``plugin.yaml``. The function constructs an :class:`IcmMemoryProvider` (which
binds the four declared hook callbacks: ``prefetch``, ``system_prompt_block``,
``sync_turn``, ``on_session_end``) and hands it to
``ctx.register_memory_provider``.

Module-level side effects are forbidden — registration happens only inside
the explicit :func:`register` call (test:
``test_register_called_once_idempotent_module_import``). Per AD-12 this
module MUST NOT import ``subprocess``.
"""

from __future__ import annotations

from typing import Any

from ._version import __version__
from .provider import IcmMemoryProvider

__all__ = ["IcmMemoryProvider", "__version__", "register"]


def register(ctx: Any) -> None:
    """Hermes plugin entry point. Construct the provider and hand it to ctx."""
    ctx.register_memory_provider(IcmMemoryProvider())
