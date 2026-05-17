"""S13 / v0.4 — Failure-mode degrade matrix end-to-end (FR19, NFR-REL-1).

The plugin-internal read path is ``provider.prefetch`` → ``hooks.run_prefetch``
→ ``cli_runner.run_recall`` → ``mcp_client.IcmMcpClient.call_recall``.

v0.4 transport: all subprocess work is managed by the warm MCP daemon inside
:mclass:`mcp_client.IcmMcpClient`. cli_runner delegates to it.

Failure modes exercised:

* Mode 1: icm not on PATH → provider self-disables on initialize.
* Modes 2/3/4: cli_runner/ICM failures → prefetch returns "" with WARNING.
* Mode 5: happy path → prefetch returns a formatted block.
* Modes 6/7: worker death → respawn (once) then degrade.
* Mode 8: filesystem unwritable → self-disable.

Each entry-point must (a) return its documented degraded shape, (b) emit one
WARNING/CRITICAL with the exception text inline (AC8), (c) never raise.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from hermes_icm_memory import cli_runner
from hermes_icm_memory.provider import IcmMemoryProvider

# ---------- Shared fixtures ------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_mcp_client() -> None:
    """Ensure a clean MCP client state before each test."""
    cli_runner.mcp_stop()
    cli_runner._client = None
    yield
    cli_runner.mcp_stop()
    cli_runner._client = None


@pytest.fixture
def initialized_provider(tmp_hermes_home: Path) -> IcmMemoryProvider:
    """Provider with initialize + worker spun up.

    Patches the MCP client so no real icm serve subprocess is spawned.
    """
    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    with patch("hermes_icm_memory.cli_runner.mcp_client.IcmMcpClient") as MockClient:
        instance = MockClient.return_value
        instance.is_available.return_value = True
        instance.call_recall.return_value = []
        instance.call_store.return_value = True
        instance.call_topics.return_value = []
        instance.call_health.return_value = {}

        provider.initialize(
            session_id="s1",
            hermes_home=tmp_hermes_home,
            profile="default",
        )
        provider._available = True
        provider._ensure_worker()
    return provider


def _kill_worker(provider: IcmMemoryProvider) -> None:
    """Force the worker thread off without blocking on a real drain."""
    provider._stop_event.set()
    if provider._worker is not None:
        provider._worker.join(timeout=1.0)
    provider._stop_event.clear()


# ---------- Mode 1: icm not on PATH -----------------------------------------


def test_mode1_icm_not_on_path_degrades_silently(
    monkeypatch: pytest.MonkeyPatch,
    tmp_hermes_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 1 — ``shutil.which("icm")`` → None, MCP start fails.

    Asserts:
        * ``is_available()`` is ``False``.
        * ``prefetch()`` returns ``""`` (short-circuits).
    """
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    provider = IcmMemoryProvider()
    provider._config["isolated"] = True

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.provider"):
        provider.initialize(
            session_id="s1",
            hermes_home=tmp_hermes_home,
            profile="default",
        )

    assert provider.is_available() is False

    with caplog.at_level(logging.WARNING):
        prefetch_out = provider.prefetch(query="anything")

    assert prefetch_out == ""


# ---------- Modes 2/3/4: MCP failures degrade prefetch -----------------------


def _make_mock_client(call_recall_return: Any = None) -> MagicMock:
    inst = MagicMock()
    inst.is_available.return_value = True
    inst.call_recall.return_value = call_recall_return if call_recall_return is not None else []
    inst.call_store.return_value = True
    inst.call_topics.return_value = []
    inst.call_health.return_value = {}
    return inst


def test_mcp_client_empty_response_produces_empty_prefetch(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """MCP client returning [] produces empty prefetch.

    v0.4: the MCP client always returns [] on failure (AD-07 degrade),
    so prefetch gets a valid empty list and produces no recalled-memories
    block. v0.5.5: prefetch still emits the indicator directive so the
    per-turn user-message injection carries a fresh footer instruction;
    on the no-hits path it's the heartbeat (📚 —).
    """
    client = _make_mock_client(call_recall_return=[])
    monkeypatch.setattr(cli_runner, "_client", client)

    result = initialized_provider.prefetch(query="x")
    assert "📖 Recalled memories" not in result  # no hits to show
    assert "📚 —" in result  # heartbeat directive


# ---------- Mode 5: happy path -----------------------------------------------


def test_mode5_happy_path(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 5 — happy path returns a formatted prefetch block.

    v0.5.0 — recall now goes through a subprocess CLI call, not the MCP
    client. Mock ``subprocess.run`` in cli_runner accordingly.
    """
    import json as _json
    from unittest.mock import MagicMock as _MM
    hits = [{"topic": "preferences", "summary": "use bun"}]
    fake = _MM()
    fake.returncode = 0
    fake.stdout = _json.dumps(hits)
    fake.stderr = ""
    monkeypatch.setattr(
        "hermes_icm_memory.cli_runner.subprocess.run", lambda *a, **kw: fake,
    )

    with caplog.at_level(logging.DEBUG):
        block = initialized_provider.prefetch(query="anything")

    assert "use bun" in block, (
        f"happy path must return a populated prefetch block; got {block!r}"
    )

    bad_levels = [
        r for r in caplog.records if r.levelno >= logging.WARNING
    ]
    assert bad_levels == [], (
        f"happy path must NOT degrade (no WARNING/CRITICAL); got "
        f"{[(r.levelname, r.message) for r in bad_levels]!r}"
    )


# ---------- Mode 6: worker dies once → lazy respawn -------------------------


def test_mode6_worker_dies_once_lazy_respawn(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 6 — worker thread dies once → respawned + WARNING."""
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.detect_triggers",
        lambda *_a, **_kw: [("preferences", "critical", "x", ["x"])],
    )
    monkeypatch.setattr(cli_runner, "run_store", lambda *_a, **_kw: None)

    first_worker = initialized_provider._worker
    assert first_worker is not None and first_worker.is_alive()

    _kill_worker(initialized_provider)
    assert not first_worker.is_alive()

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.hooks"):
        initialized_provider.sync_turn(user_content="u", assistant_content="a")

    assert initialized_provider._respawn_count == 1
    assert initialized_provider._worker is not None
    assert initialized_provider._worker is not first_worker
    assert initialized_provider._worker.is_alive()
    assert initialized_provider._writes_disabled is False
    assert any(
        r.levelno == logging.WARNING and "respawn" in r.message.lower()
        for r in caplog.records
    )


# ---------- Mode 7: worker dies twice → degrade-to-drop ----------------------


def test_mode7_worker_dies_twice_degrades_with_critical(
    initialized_provider: IcmMemoryProvider,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 7 — second death sets _writes_disabled + CRITICAL."""
    monkeypatch.setattr(
        "hermes_icm_memory.hooks.mapping.detect_triggers",
        lambda *_a, **_kw: [("preferences", "critical", "x", ["x"])],
    )
    monkeypatch.setattr(cli_runner, "run_store", lambda *_a, **_kw: None)

    _kill_worker(initialized_provider)
    initialized_provider.sync_turn(user_content="u", assistant_content="a")
    assert initialized_provider._respawn_count == 1
    second_worker = initialized_provider._worker
    assert second_worker is not None

    _kill_worker(initialized_provider)
    assert not second_worker.is_alive()

    with caplog.at_level(logging.CRITICAL, logger="hermes_icm_memory.hooks"):
        initialized_provider.sync_turn(user_content="u", assistant_content="a")

    assert initialized_provider._writes_disabled is True
    critical_records = [
        r for r in caplog.records
        if r.levelno == logging.CRITICAL and r.name == "hermes_icm_memory.hooks"
        and "second death" in r.message.lower()
    ]
    assert critical_records

    pre_count = initialized_provider._respawn_count
    queue_obj = initialized_provider._write_queue
    pre_qsize = queue_obj.qsize() if queue_obj is not None else 0

    initialized_provider.sync_turn(user_content="u", assistant_content="a")

    assert initialized_provider._respawn_count == pre_count
    assert initialized_provider._writes_disabled is True
    post_qsize = queue_obj.qsize() if queue_obj is not None else 0
    assert post_qsize == pre_qsize


# ---------- Mode 8: hermes_home parent unwritable ---------------------------


def test_mode8_hermes_home_unwritable_self_disables(
    monkeypatch: pytest.MonkeyPatch,
    tmp_hermes_home: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Mode 8 — initialize catches OSError from mkdir_parent."""

    def _raise_perm(_db_path: Path) -> None:
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr("hermes_icm_memory.config.mkdir_parent", _raise_perm)

    provider = IcmMemoryProvider()
    provider._config["isolated"] = True

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.provider"):
        provider.initialize(
            session_id="s1",
            hermes_home=tmp_hermes_home,
            profile="default",
        )

    assert provider.is_available() is False
    assert any(
        r.levelno == logging.WARNING and "hermes_home not writable" in r.message
        for r in caplog.records
    )


def test_mode8_self_disable_is_sticky_across_reinit(
    monkeypatch: pytest.MonkeyPatch,
    tmp_hermes_home: Path,
) -> None:
    """Mode 8 follow-up — _available=False survives a successful re-init."""
    mkdir_calls = {"n": 0}

    def _raise_perm_first_call_only(_db_path: Path) -> None:
        mkdir_calls["n"] += 1
        if mkdir_calls["n"] == 1:
            raise PermissionError("read-only filesystem")

    monkeypatch.setattr(
        "hermes_icm_memory.config.mkdir_parent", _raise_perm_first_call_only
    )

    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    provider.initialize(
        session_id="s1", hermes_home=tmp_hermes_home, profile="default"
    )
    assert provider.is_available() is False

    provider.initialize(
        session_id="s2", hermes_home=tmp_hermes_home, profile="other"
    )
    assert provider.is_available() is False
