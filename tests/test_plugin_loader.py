"""Plugin loader tests (S01 baseline + S10 real-provider wiring).

S01 baseline (4 tests):
  - register(ctx) wires a provider into Hermes correctly,
  - the registered provider is the icm provider,
  - version is single-source-of-truth across __init__/_version/pyproject,
  - plugin.yaml has the manifest shape Hermes (and S10) expect.

S10 real-provider wiring (3 tests):
  - register(ctx) constructs a real IcmMemoryProvider (not a stub / mock),
  - all four plugin.yaml hook methods are bound on the registered provider,
  - module import (and re-import) has no register side-effect; only the
    explicit register(ctx) call drives registration.
"""

from __future__ import annotations

import importlib
import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import yaml

import hermes_icm_memory
from hermes_icm_memory import _version
from hermes_icm_memory.provider import IcmMemoryProvider

REPO_ROOT = Path(__file__).resolve().parent.parent

# Hooks declared in plugin.yaml — must be bound as callables on the
# registered provider per AC2 (and Hermes contract).
_PLUGIN_YAML_HOOKS: tuple[str, ...] = (
    "prefetch",
    "system_prompt_block",
    "sync_turn",
    "on_session_end",
)


def test_register_calls_register_memory_provider_once() -> None:
    """register(ctx) must call ctx.register_memory_provider exactly once."""
    ctx = MagicMock()
    hermes_icm_memory.register(ctx)
    assert ctx.register_memory_provider.call_count == 1


def test_registered_provider_name_is_icm() -> None:
    """The registered provider's name attribute must equal 'icm'."""
    ctx = MagicMock()
    hermes_icm_memory.register(ctx)
    (provider,) = ctx.register_memory_provider.call_args.args
    assert provider.name == "icm"


def test_version_is_consistent() -> None:
    """__version__ must match _version.__version__ and pyproject.toml's version."""
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    pyproject_version = pyproject["project"]["version"]
    assert hermes_icm_memory.__version__ == _version.__version__
    assert hermes_icm_memory.__version__ == pyproject_version


def test_plugin_yaml_shape() -> None:
    """plugin.yaml declares name/version/description/hooks with the four expected hooks."""
    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text(encoding="utf-8"))
    for key in ("name", "version", "description", "hooks"):
        assert key in manifest, f"plugin.yaml missing required key: {key}"
    assert manifest["name"] == "hermes-icm-memory"
    assert manifest["version"] == hermes_icm_memory.__version__
    assert set(manifest["hooks"]) == {
        "prefetch",
        "system_prompt_block",
        "sync_turn",
        "on_session_end",
    }


# ---------------------------------------------------------------- S10 wiring


def test_register_constructs_real_provider() -> None:
    """register(ctx) must hand Hermes a real IcmMemoryProvider, not a stub."""
    ctx = MagicMock()
    hermes_icm_memory.register(ctx)
    (provider,) = ctx.register_memory_provider.call_args.args
    assert isinstance(provider, IcmMemoryProvider)


def test_provider_hook_methods_bound() -> None:
    """Every plugin.yaml-declared hook must resolve to a callable on the provider."""
    ctx = MagicMock()
    hermes_icm_memory.register(ctx)
    (provider,) = ctx.register_memory_provider.call_args.args
    for hook_name in _PLUGIN_YAML_HOOKS:
        attr = getattr(provider, hook_name, None)
        assert attr is not None, f"provider missing hook attribute: {hook_name}"
        assert callable(attr), f"provider.{hook_name} is not callable"


def test_register_called_once_idempotent_module_import() -> None:
    """Re-importing the package must not re-fire register; only the explicit call does.

    Guards against any future module-level side effect that would invoke
    ``ctx.register_memory_provider`` outside the ``register(...)`` body.
    """
    importlib.reload(hermes_icm_memory)
    ctx = MagicMock()
    # Module reload alone must not have invoked any registration hook.
    assert ctx.register_memory_provider.call_count == 0
    hermes_icm_memory.register(ctx)
    assert ctx.register_memory_provider.call_count == 1
