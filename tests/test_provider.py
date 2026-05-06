"""Tests for ``hermes_icm_memory.provider.IcmMemoryProvider`` (S07).

Strict TDD: this file lands first (RED), then ``provider.py`` implements
exactly what these cases assert (GREEN). The 14 cases trace 1-to-1 to
ACs 1–14 of story 3.1; ACs 15 (no-subprocess invariant) and 16 (S11
forward-compat skips light up later) are validated by the existing
``tests/test_no_subprocess_outside_cli_runner.py`` and
``tests/test_no_network_calls.py`` invariant suites.
"""

from __future__ import annotations

import json
import logging
import shutil
import socket
from pathlib import Path
from typing import Any, NoReturn

import pytest

from hermes_icm_memory import config
from hermes_icm_memory.provider import IcmMemoryProvider

# ---------- AC1: name ---------------------------------------------------------


def test_name_is_icm() -> None:
    """AC1 — ``provider.name`` is the literal ``"icm"``."""
    assert IcmMemoryProvider().name == "icm"


# ---------- AC2/AC3/AC4: is_available + caching -------------------------------


def test_is_available_true_when_icm_on_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC2 — ``shutil.which("icm")`` truthy → ``True``."""
    monkeypatch.setattr(
        shutil, "which", lambda name: "/usr/local/bin/icm" if name == "icm" else None
    )
    assert IcmMemoryProvider().is_available() is True


def test_is_available_false_when_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC3 — ``shutil.which`` returns ``None`` → ``False``."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert IcmMemoryProvider().is_available() is False


def test_is_available_caches_result(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC4 — second call must not invoke ``shutil.which`` again."""
    calls: list[str] = []

    def _which(name: str) -> str | None:
        calls.append(name)
        return "/usr/local/bin/icm"

    monkeypatch.setattr(shutil, "which", _which)
    provider = IcmMemoryProvider()
    assert provider.is_available() is True
    assert provider.is_available() is True
    assert calls == ["icm"], f"expected exactly one which() call, got {calls!r}"


# ---------- AC5: no socket during is_available --------------------------------


def _raise_on_socket(*args: object, **kwargs: object) -> NoReturn:
    raise RuntimeError("network forbidden during plugin lifecycle (NFR-SEC-1)")


def test_is_available_no_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC5 — patching ``socket.socket`` to raise must not break ``is_available()``."""
    monkeypatch.setattr(socket, "socket", _raise_on_socket)
    # is_available must work whether or not icm is on PATH; the contract is
    # "no network", not "always True". Force a deterministic path via which patch.
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/icm")
    IcmMemoryProvider().is_available()


# ---------- AC6/AC7/AC8: initialize -------------------------------------------


def test_initialize_default_shared_db_path_stays_none(tmp_hermes_home: Path) -> None:
    """v0.1.1 — default config keeps ``_db_path = None`` (brief's shared-with-editors).

    ``initialize`` records ``_session_id`` + ``_init_args`` but performs no
    path resolution and no filesystem touch under ``<hermes_home>/icm/``. This
    is the brief's value-prop default — the plugin shells out to ``icm`` with
    no ``--db`` so icm uses its canonical OS-default DB (the same file Claude
    Code, Cursor, OpenCode, etc. share).
    """
    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="work")

    assert provider._db_path is None
    assert provider._session_id == "s1"
    assert provider._init_args == ("s1", str(tmp_hermes_home), "work")
    # No filesystem touch under hermes_home/icm/ in default-shared mode.
    assert not (tmp_hermes_home / "icm").exists()


def test_initialize_isolated_resolves_db_path(tmp_hermes_home: Path) -> None:
    """v0.1.1 — opt-in ``isolated=True`` restores the v0.1.0 per-profile DB path."""
    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="work")

    expected = config.resolve_db_path(tmp_hermes_home, "work")
    assert provider._db_path == expected
    assert provider._session_id == "s1"


def test_initialize_creates_parent_dir_when_isolated(tmp_hermes_home: Path) -> None:
    """v0.1.1 — under ``isolated=True``, ``<hermes_home>/icm/`` is created (no .db file).

    Default-shared mode performs no mkdir; this AC fires only on opt-in.
    """
    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")

    icm_dir = tmp_hermes_home / "icm"
    assert icm_dir.is_dir(), f"expected {icm_dir} to be created by initialize"
    assert not (icm_dir / "default.db").exists(), "plugin must not invoke icm init"


def test_initialize_idempotent_isolated(
    tmp_hermes_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """AC8 — second initialize() call with same args performs no extra mkdir.

    Pinned under ``isolated=True`` because mkdir only runs in the isolated
    branch — default-shared mode skips mkdir entirely and is trivially
    idempotent. The (session_id, hermes_home, profile) guard still fires in
    both modes.
    """
    real_mkdir = Path.mkdir
    calls: list[Path] = []

    def _counting_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
        calls.append(self)
        real_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", _counting_mkdir)

    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="work")
    first_count = len(calls)
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="work")
    second_count = len(calls)
    assert second_count == first_count, (
        f"expected idempotent re-init (same args) to skip mkdir; "
        f"first={first_count}, second={second_count}"
    )


def test_initialize_idempotent_default_shared(tmp_hermes_home: Path) -> None:
    """v0.1.1 — default-shared re-init guard fires (no rework on duplicate call)."""
    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile=None)
    first_args = provider._init_args
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile=None)
    assert provider._init_args == first_args
    assert provider._db_path is None


def test_initialize_with_unwritable_hermes_home_self_disables_when_isolated(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """AC9 — OSError from mkdir under ``isolated=True`` → WARNING + self-disable.

    Default-shared mode never touches the filesystem in ``initialize`` so the
    self-disable branch is unreachable there; this AC pins the v0.1.0 row 8
    failure-mode behavior under the new opt-in.
    """

    def _raise_oserror(_db_path: Path) -> None:
        raise OSError("read-only filesystem")

    monkeypatch.setattr(config, "mkdir_parent", _raise_oserror)
    # Pretend icm is on PATH so any non-self-disable bug would have surfaced
    # is_available()=True before we asserted False.
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/icm")

    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.provider"):
        provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")

    assert any(
        "hermes_home" in record.message or "initialize" in record.message
        for record in caplog.records
    ), f"expected a WARNING about init failure; got {[r.message for r in caplog.records]!r}"
    assert provider.is_available() is False, "self-disable: is_available must return False"


def test_initialize_default_shared_no_mkdir_on_unwritable(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.1.1 — default-shared mode never calls mkdir, so a read-only FS is benign.

    If an operator points Hermes at a read-only ``hermes_home`` and stays in
    the default-shared mode, ``initialize`` is a no-op as far as the filesystem
    is concerned: the plugin will shell out to ``icm`` with no ``--db`` and let
    icm read/write its own canonical DB instead.
    """

    def _raise_oserror(_db_path: Path) -> None:
        raise OSError("never-fires-in-default-shared")

    monkeypatch.setattr(config, "mkdir_parent", _raise_oserror)
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/local/bin/icm")

    provider = IcmMemoryProvider()
    provider.initialize(session_id="s1", hermes_home=tmp_hermes_home, profile="default")

    # No self-disable: the OSError path was never reached.
    assert provider.is_available() is True
    assert provider._db_path is None


# ---------- AC10: get_config_schema ------------------------------------------


def test_get_config_schema_matches_defaults() -> None:
    """AC10 — ``get_config_schema()`` returns ``config.get_default_schema()`` (deep-equal)."""
    provider = IcmMemoryProvider()
    first = provider.get_config_schema()
    assert first == config.get_default_schema()
    # Defensive copy: mutating the returned list must not poison the next call.
    first.clear()
    second = provider.get_config_schema()
    assert second == config.get_default_schema()
    assert second is not first


# ---------- AC11/AC12: save_config -------------------------------------------


def test_save_config_accepts_valid(tmp_hermes_home: Path) -> None:
    """AC11 — valid values → returns None, JSON sidecar written, ``_config`` reflects them."""
    provider = IcmMemoryProvider()
    result = provider.save_config(
        {"recall_limit": 7, "default_importance": "high"},
        hermes_home=tmp_hermes_home,
    )
    assert result is None

    sidecar = tmp_hermes_home / "icm" / "config.json"
    assert sidecar.is_file(), f"expected JSON sidecar at {sidecar}"
    persisted = json.loads(sidecar.read_text(encoding="utf-8"))
    assert persisted["recall_limit"] == 7
    assert persisted["default_importance"] == "high"
    assert provider._config["recall_limit"] == 7
    assert provider._config["default_importance"] == "high"


def test_save_config_rejects_invalid_returns_error_dict(tmp_hermes_home: Path) -> None:
    """AC12 — invalid values → ``{"error": ...}``, never raises, no sidecar written."""
    provider = IcmMemoryProvider()
    result = provider.save_config({"recall_limit": -1}, hermes_home=tmp_hermes_home)

    assert isinstance(result, dict)
    assert "error" in result
    assert isinstance(result["error"], str)
    assert "recall_limit" in result["error"]

    sidecar = tmp_hermes_home / "icm" / "config.json"
    assert not sidecar.exists(), "invalid save_config must not write the sidecar"
    assert provider._config == {}


# ---------- v0.3 — provider exposes no LLM tool surface ----------------------
# AC13 / AC14 from the v0.1.x story (handle_tool_call dispatch, get_tool_schemas
# shape) are obsolete in v0.3. The corresponding invariant tests now live in
# tests/test_no_tool_surface.py: the provider has no handle_tool_call /
# get_tool_schemas, and the tools module is deleted.


# ---------- Extra coverage: save_config with no hermes_home -----------------


def test_save_config_without_hermes_home_skips_disk_write() -> None:
    """``save_config({}, hermes_home=None)`` validates + updates state, no sidecar.

    This branch is exercised by the S11 NFR-SEC-1 invariant
    ``test_save_config_no_socket`` once S10 wires the real provider into
    ``register(ctx)``. Until then, this test pins the contract directly.
    """
    provider = IcmMemoryProvider()
    result = provider.save_config({"recall_limit": 3})
    assert result is None
    assert provider._config == {"recall_limit": 3}


# ---------- Extra coverage: save_config with unwritable hermes_home ----------


# ---------- v0.3 — MCP transport removed ------------------------------------
# v0.2 added a plugin-managed ``icm serve`` daemon (transport: mcp);
# v0.3 deletes it because hermes-agent v0.3.0+ owns the
# ``mcp_servers.icm:`` surface natively. The lifecycle tests that pinned
# the start/stop wiring are obsolete; tests/test_cli_only_transport.py
# pins the inverse invariant (no transport kwarg, no mcp_* helpers).


def test_on_session_end_does_not_invoke_subprocess(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``on_session_end`` no longer manages an icm-serve daemon (AC1).

    Pins that no AttributeError is raised because someone left a stale
    ``cli_runner.mcp_stop`` call in the teardown path.
    """
    from hermes_icm_memory import cli_runner

    # If a stale mcp_stop reference survived the v0.3 trim, monkeypatching
    # it would surface — but the attribute itself must be absent now.
    assert not hasattr(cli_runner, "mcp_stop")
    assert not hasattr(cli_runner, "mcp_start")

    provider = IcmMemoryProvider()
    # Must not raise even though there is no daemon and no worker yet.
    provider.on_session_end()


def test_save_config_returns_error_dict_on_oserror(
    tmp_hermes_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """OSError from sidecar write → return ``{"error": "could not persist config: …"}``.

    Pins AD-07 / AD-18 boundary: validation already passed (so the values are
    in ``_config``), but the disk write fails. The caller sees an error-dict
    just like a validation failure, never an exception.
    """
    real_mkdir = config.mkdir_parent

    def _raise_on_write(path: Path) -> None:
        # Let the *initialize*-style call succeed if needed (none in this test)
        # — but raise specifically for save_config's mkdir_parent prep call.
        raise OSError("Read-only file system")

    monkeypatch.setattr(config, "mkdir_parent", _raise_on_write)

    provider = IcmMemoryProvider()
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.provider"):
        result = provider.save_config(
            {"recall_limit": 3}, hermes_home=tmp_hermes_home
        )

    assert isinstance(result, dict)
    assert "error" in result
    assert "could not persist config" in result["error"]
    assert any(
        "could not persist sidecar" in record.message for record in caplog.records
    )
    # In-memory state still updated (validation succeeded before the write).
    assert provider._config == {"recall_limit": 3}

    # Sanity: real_mkdir is used so no global state leaks beyond monkeypatch.
    assert real_mkdir is not _raise_on_write
