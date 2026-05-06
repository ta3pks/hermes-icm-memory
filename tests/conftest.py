"""Shared pytest fixtures.

Populated by S07 (``tmp_hermes_home``); v0.1.1 adds ``isolated_provider`` for
tests that need a concrete ``_db_path`` (write-path / profile-isolation
coverage). Later stories add ``mock_icm_subprocess``, ``real_icm_db``,
``capture_logs`` etc.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_icm_memory.provider import IcmMemoryProvider


@pytest.fixture
def tmp_hermes_home(tmp_path: Path) -> Path:
    """Per-test ``hermes_home`` directory under pytest's tmp_path.

    Returns the ``hermes_home`` root (already created); the provider's
    ``initialize`` is responsible for creating the ``icm/`` subdirectory.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    return home


@pytest.fixture
def isolated_provider(tmp_hermes_home: Path) -> IcmMemoryProvider:
    """Provider initialised in opt-in profile-isolation mode (v0.1.1).

    Sets ``_config["isolated"] = True`` *before* ``initialize`` so the
    isolated branch fires and ``_db_path`` becomes ``<hermes_home>/icm/<profile>.db``.
    Use this fixture in tests that need a concrete ``_db_path`` (worker /
    write-queue / profile-isolation coverage). Tests of the v0.1.1
    default-shared behavior should construct ``IcmMemoryProvider()``
    directly (or use ``initialized_provider`` in test_hooks.py) so
    ``_db_path`` stays ``None`` per the brief's "shared with editors" promise.
    """
    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    provider.initialize(
        session_id="s1", hermes_home=tmp_hermes_home, profile="default"
    )
    provider._available = True
    return provider
