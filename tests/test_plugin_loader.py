"""Plugin loader baseline tests (S01).

These are the only tests that exist after S01. They prove:
  - register(ctx) wires a provider into Hermes correctly,
  - the registered provider is the icm provider,
  - version is single-source-of-truth across __init__/_version/pyproject,
  - plugin.yaml has the manifest shape Hermes (and S10) expect.
"""

from __future__ import annotations

import tomllib
from pathlib import Path
from unittest.mock import MagicMock

import yaml

import hermes_icm_memory
from hermes_icm_memory import _version

REPO_ROOT = Path(__file__).resolve().parent.parent


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
    pyproject = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text())
    pyproject_version = pyproject["project"]["version"]
    assert hermes_icm_memory.__version__ == _version.__version__
    assert hermes_icm_memory.__version__ == pyproject_version


def test_plugin_yaml_shape() -> None:
    """plugin.yaml declares name/version/description/hooks with the four expected hooks."""
    manifest = yaml.safe_load((REPO_ROOT / "plugin.yaml").read_text())
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
