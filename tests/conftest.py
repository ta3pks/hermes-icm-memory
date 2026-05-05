"""Shared pytest fixtures.

Populated by S07 (``tmp_hermes_home``); later stories add
``mock_icm_subprocess``, ``real_icm_db``, ``capture_logs`` etc.
"""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_hermes_home(tmp_path: Path) -> Path:
    """Per-test ``hermes_home`` directory under pytest's tmp_path.

    Returns the ``hermes_home`` root (already created); the provider's
    ``initialize`` is responsible for creating the ``icm/`` subdirectory.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()
    return home
