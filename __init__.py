"""Top-level Hermes plugin entry shim.

Hermes Agent's memory-provider discovery loads ``<plugin-dir>/__init__.py`` and
calls ``register(ctx)``. The actual implementation lives in the
``hermes_icm_memory`` package one directory deeper. This shim makes the plugin
**dual-discoverable**:

* **Hermes flat layout** — ``hermes plugins install`` clones into
  ``~/.hermes/plugins/hermes-icm-memory/`` and Hermes imports this file.
* **PyPI installable** — the ``hermes_icm_memory`` package is exposed via
  ``pyproject.toml`` and importable by name after ``pip install``.

Adds the plugin directory to ``sys.path`` so ``hermes_icm_memory`` is importable
inside this module even when Hermes loads us via ``importlib.util.spec_from_file_location``.
"""

from __future__ import annotations

import sys
from pathlib import Path

_PLUGIN_DIR = Path(__file__).parent
if str(_PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_DIR))

from hermes_icm_memory import (  # noqa: E402, I001  # path mutation above must precede the import
    IcmMemoryProvider,
    __version__,
    register,
)

__all__ = ["IcmMemoryProvider", "__version__", "register"]
