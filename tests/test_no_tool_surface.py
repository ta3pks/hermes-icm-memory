"""v0.3 invariant — provider exposes no LLM tool surface (AC3, AC9).

The plugin's value-add reduced to lifecycle hooks (prefetch + sync_turn);
``icm_memory_*`` tool exposure to the LLM is owned by hermes-native
``mcp_servers.icm:`` config (hermes-agent v0.3.0+). Pins:

1. ``IcmMemoryProvider`` no longer carries ``handle_tool_call`` /
   ``get_tool_schemas`` methods.
2. The ``hermes_icm_memory/tools.py`` source file is deleted from the
   package directory (catches a regression where a future PR re-adds it).
"""

from __future__ import annotations

from pathlib import Path

import hermes_icm_memory
from hermes_icm_memory.provider import IcmMemoryProvider


def test_provider_has_no_handle_tool_call() -> None:
    """``handle_tool_call`` is removed (AD-19)."""
    assert not hasattr(IcmMemoryProvider, "handle_tool_call"), (
        "IcmMemoryProvider must not expose handle_tool_call in v0.3 — "
        "tool exposure is now hermes-native via mcp_servers.icm:"
    )


def test_provider_has_get_tool_schemas() -> None:
    """``get_tool_schemas`` is present and returns empty list (v0.4).

    Hermes ``MemoryManager.register_provider()`` calls
    ``get_tool_schemas()`` on every provider during registration
    — without it the provider cannot be activated.
    """
    assert hasattr(IcmMemoryProvider, "get_tool_schemas"), (
        "IcmMemoryProvider must expose get_tool_schemas in v0.4 — "
        "Hermes MemoryManager requires it for provider registration."
    )
    instance = IcmMemoryProvider()
    assert instance.get_tool_schemas() == [], (
        "ICM provider exposes no agent-callable tools directly — "
        "memory ops happen via prefetch/sync_turn internally."
    )


def test_tools_module_file_deleted_from_package() -> None:
    """``hermes_icm_memory/tools.py`` source file is deleted in v0.3.

    Asserts at the filesystem level rather than via ``importlib.import_module``
    so the test stays meaningful even when Python's editable-install finder
    is configured for the parent worktree (which can mask a missing file in
    another worktree's package directory).
    """
    package_dir = Path(hermes_icm_memory.__file__).parent
    tools_file = package_dir / "tools.py"
    assert not tools_file.exists(), (
        f"hermes_icm_memory/tools.py must be deleted in v0.3 "
        f"(found at {tools_file})"
    )


def test_provider_has_shutdown_method() -> None:
    """``shutdown()`` exists as a no-op so hermes' memory_manager doesn't WARN.

    The previous v0.2 codebase silently raised AttributeError on every
    gateway restart because hermes calls ``provider.shutdown()`` on
    teardown. Fixed by adding the method as an explicit no-op.
    """
    provider = IcmMemoryProvider()
    # Returns ``None`` without raising on a fresh (uninitialised) provider —
    # mypy disallows ``assert provider.shutdown() is None`` because the
    # method's return type is ``None`` (an empty type), so we just call.
    provider.shutdown()
    # …and on an already-shutdown one (idempotent).
    provider.shutdown()
