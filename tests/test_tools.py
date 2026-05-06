"""Tests for ``hermes_icm_memory.tools`` (S09).

Strict TDD: this file lands first (RED), then ``tools.py`` implements
exactly what these cases assert (GREEN). The 16 cases trace 1-to-1 to
ACs 1–16 of story 4.2 (epic 4 / S09).

Coordination with S08 (running in parallel) — the provider's
``_write_queue`` attribute is owned by S08. Until merge, these tests
mock it directly via ``monkeypatch.setattr(provider, "_write_queue", ...)``.
The assumed contract is

    Trigger = tuple[str, str, str, list[str]]   # (topic, importance, content, keywords)
    provider._write_queue: queue.Queue[Trigger]

— recorded in the story spec so the manager can validate at merge time.
"""

from __future__ import annotations

import json
import logging
import queue
import time
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from hermes_icm_memory import tools
from hermes_icm_memory.errors import (
    ICMMalformedOutputError,
    ICMNonZeroExitError,
    ICMNotFoundError,
    ICMTimeoutError,
)
from hermes_icm_memory.provider import IcmMemoryProvider

# ---------- helpers -----------------------------------------------------------

#: Sentinel DB path used by read-path tests — sole purpose is to be non-None
#: so the "provider not initialized" guard in tools.py does not short-circuit.
#: Tests still mock ``run_recall`` / ``run_topics`` / ``run_health`` so the
#: path never reaches subprocess.
_FAKE_DB = Path("/tmp/__hermes_icm_test__/default.db")


def _read_provider() -> IcmMemoryProvider:
    """Fresh provider with `_db_path` set so read-path handlers don't short-circuit."""
    p = IcmMemoryProvider()
    p._db_path = _FAKE_DB
    return p


def _provider_with_queue(qsize: int = 8) -> IcmMemoryProvider:
    """Fresh provider with a real bounded queue + `_db_path` attached.

    Sets `_db_path` so cross-handler tests (AC14, AC15) that exercise both
    read-paths and the store-path on the same provider don't short-circuit
    on the read-path's "provider not initialized" guard.
    """
    p = IcmMemoryProvider()
    p._db_path = _FAKE_DB
    # AD-12 / S08 contract — see module docstring.
    p._write_queue = queue.Queue(maxsize=qsize)  # type: ignore[attr-defined]
    return p


# =============================================================================
# AC1 — schemas: four with the canonical names
# =============================================================================


def test_get_tool_schemas_has_four_with_correct_names() -> None:
    """AC1 — exactly four schemas, names from the PRD §8.6 table, in order."""
    schemas = tools.get_tool_schemas()
    assert isinstance(schemas, list)
    assert len(schemas) == 4
    names = [s["name"] for s in schemas]
    assert names == ["icm_recall", "icm_store", "icm_topics", "icm_health"]


# =============================================================================
# AC2 — schema shape
# =============================================================================


def test_each_schema_has_required_keys() -> None:
    """AC2 — every schema has name/description/parameters{type,properties,required}."""
    schemas = tools.get_tool_schemas()
    expected_required = {
        "icm_recall": ["query"],
        "icm_store": ["topic", "content"],
        "icm_topics": [],
        "icm_health": [],
    }
    for schema in schemas:
        assert isinstance(schema["name"], str)
        assert isinstance(schema["description"], str) and schema["description"]
        params = schema["parameters"]
        assert isinstance(params, dict)
        assert params["type"] == "object"
        assert isinstance(params["properties"], dict)
        assert isinstance(params["required"], list)
        assert params["required"] == expected_required[schema["name"]]


# =============================================================================
# AC3 — recall success → json.dumps({"hits": [...]})
# =============================================================================


def test_recall_returns_json_string_with_hits_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AC3 — cli_runner.run_recall mocked → list; handler returns the wrapped JSON."""
    hits = [{"id": "m1", "score": 0.9, "topic": "preferences", "summary": "use bun"}]
    captured: dict[str, Any] = {}

    def _fake_run_recall(
        query: str,
        limit: int,
        db_path: Any,
        timeout_ms: int,
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        captured.update(
            {
                "query": query,
                "limit": limit,
                "topic": topic,
                "project": project,
                "timeout_ms": timeout_ms,
            }
        )
        return hits

    monkeypatch.setattr(tools, "run_recall", _fake_run_recall)

    provider = _read_provider()
    result = provider.handle_tool_call(
        "icm_recall",
        {"query": "what does Nikos prefer?", "topic": "preferences", "limit": 3},
    )
    assert isinstance(result, str)
    assert json.loads(result) == {"hits": hits}
    assert captured["query"] == "what does Nikos prefer?"
    assert captured["limit"] == 3
    assert captured["topic"] == "preferences"


# =============================================================================
# AC4 — recall failure → empty hits + WARNING
# =============================================================================


@pytest.mark.parametrize(
    "exc",
    [
        ICMNotFoundError("icm not on PATH"),
        ICMTimeoutError("read timeout"),
        ICMNonZeroExitError("non-zero"),
        ICMMalformedOutputError("bad json"),
    ],
)
def test_recall_failure_returns_empty_hits_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exc: Exception,
) -> None:
    """AC4 — every ICMError variant degrades to {"hits": []} with one WARNING."""

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise exc

    monkeypatch.setattr(tools, "run_recall", _raise)

    provider = _read_provider()
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        result = provider.handle_tool_call("icm_recall", {"query": "x"})

    assert isinstance(result, str)
    assert json.loads(result) == {"hits": []}
    warning_records = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warning_records) == 1, (
        f"expected exactly one WARNING, got {len(warning_records)}: "
        f"{[r.getMessage() for r in warning_records]!r}"
    )


# =============================================================================
# AC5 + AC6 — store enqueues a (topic, importance, content, keywords) tuple
# =============================================================================


def test_store_enqueues_and_returns_immediately() -> None:
    """AC5/AC6 — exactly one tuple enqueued; return shape correct."""
    provider = _provider_with_queue()
    result = provider.handle_tool_call(
        "icm_store",
        {"topic": "preferences", "content": "Always use bun"},
    )

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["accepted"] is True
    assert isinstance(payload["queued_at"], str)

    # Single task enqueued with the documented 4-tuple shape.
    assert provider._write_queue.qsize() == 1  # type: ignore[attr-defined]
    task = provider._write_queue.get_nowait()  # type: ignore[attr-defined]
    assert isinstance(task, tuple) and len(task) == 4
    topic, importance, content, keywords = task
    assert topic == "preferences"
    assert importance == "high"  # default
    assert content == "Always use bun"
    assert keywords == []


# =============================================================================
# AC5 latency clause — store p95 < 5 ms over 200 invocations
# =============================================================================


def test_store_p95_under_5ms() -> None:
    """AC5 latency clause — handler completes p95 < 5 ms across 200 runs."""
    provider = _provider_with_queue(qsize=512)

    elapsed: list[float] = []
    for _ in range(200):
        started = time.perf_counter()
        provider.handle_tool_call(
            "icm_store",
            {"topic": "preferences", "content": "x"},
        )
        elapsed.append((time.perf_counter() - started) * 1000)

    elapsed.sort()
    p95 = elapsed[int(len(elapsed) * 0.95) - 1]
    assert p95 < 5.0, f"icm_store p95 was {p95:.2f}ms (expected < 5ms)"


# =============================================================================
# AC5 — queued_at parses as ISO 8601
# =============================================================================


def test_store_returns_accepted_true_with_iso_timestamp() -> None:
    """AC5 — queued_at must round-trip through datetime.fromisoformat."""
    provider = _provider_with_queue()
    result = provider.handle_tool_call(
        "icm_store",
        {"topic": "preferences", "content": "Always use bun"},
    )
    payload = json.loads(result)
    assert payload["accepted"] is True
    # Must not raise — proves the timestamp is valid ISO 8601.
    parsed = datetime.fromisoformat(payload["queued_at"])
    assert parsed is not None


# =============================================================================
# AC7 — store with missing required arg → error JSON, no enqueue
# =============================================================================


@pytest.mark.parametrize(
    ("args", "missing_key"),
    [
        ({"content": "hi"}, "topic"),
        ({"topic": "x"}, "content"),
        ({}, "topic"),  # both missing — first reported is "topic"
    ],
)
def test_store_invalid_args_returns_error_json(
    args: dict[str, Any], missing_key: str
) -> None:
    """AC7 — missing required arg → error JSON, no enqueue, no raise."""
    provider = _provider_with_queue()
    result = provider.handle_tool_call("icm_store", args)

    assert isinstance(result, str)
    payload = json.loads(result)
    assert "error" in payload
    assert missing_key in payload["error"]
    assert provider._write_queue.qsize() == 0  # type: ignore[attr-defined]


# =============================================================================
# AC8 — topics success
# =============================================================================


def test_topics_returns_topics_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC8 — cli_runner.run_topics list passes through wrapped in {"topics": ...}."""
    topics_list = [{"topic": "preferences"}, {"topic": "errors-resolved"}]

    def _fake_run_topics(db_path: Any, timeout_ms: int) -> list[dict[str, Any]]:
        return topics_list

    monkeypatch.setattr(tools, "run_topics", _fake_run_topics)

    provider = _read_provider()
    result = provider.handle_tool_call("icm_topics", {})
    assert isinstance(result, str)
    assert json.loads(result) == {"topics": topics_list}


# =============================================================================
# AC9 — topics failure → empty topics + WARNING
# =============================================================================


def test_topics_failure_returns_empty_topics(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC9 — ICMError → {"topics": []} + exactly one WARNING."""

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise ICMNonZeroExitError("non-zero")

    monkeypatch.setattr(tools, "run_topics", _raise)

    provider = _read_provider()
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        result = provider.handle_tool_call("icm_topics", {})

    assert isinstance(result, str)
    assert json.loads(result) == {"topics": []}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        f"expected exactly one WARNING, got {len(warnings)}: "
        f"{[r.getMessage() for r in warnings]!r}"
    )


# =============================================================================
# AC10 — health no topic
# =============================================================================


def test_health_no_topic(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC10 — args={} → run_health called with topic=None; success returns report."""
    captured: dict[str, Any] = {}

    def _fake_run_health(
        db_path: Any, timeout_ms: int, topic: str | None = None
    ) -> dict[str, Any]:
        captured["topic"] = topic
        return {"stale": "0", "total": "12"}

    monkeypatch.setattr(tools, "run_health", _fake_run_health)

    provider = _read_provider()
    result = provider.handle_tool_call("icm_health", {})
    assert isinstance(result, str)
    assert json.loads(result) == {"report": {"stale": "0", "total": "12"}}
    assert captured["topic"] is None


# =============================================================================
# AC11 — health with topic arg
# =============================================================================


def test_health_with_topic_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC11 — args={"topic":"preferences"} forwarded to run_health."""
    captured: dict[str, Any] = {}

    def _fake_run_health(
        db_path: Any, timeout_ms: int, topic: str | None = None
    ) -> dict[str, Any]:
        captured["topic"] = topic
        return {"stale": "1"}

    monkeypatch.setattr(tools, "run_health", _fake_run_health)

    provider = _read_provider()
    result = provider.handle_tool_call("icm_health", {"topic": "preferences"})
    assert isinstance(result, str)
    assert json.loads(result) == {"report": {"stale": "1"}}
    assert captured["topic"] == "preferences"


# =============================================================================
# AC12 — health failure → empty report + WARNING
# =============================================================================


def test_health_failure_returns_empty_report(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """AC12 — ICMError → {"report": {}} + exactly one WARNING."""

    def _raise(*args: Any, **kwargs: Any) -> Any:
        raise ICMTimeoutError("read timeout")

    monkeypatch.setattr(tools, "run_health", _raise)

    provider = _read_provider()
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        result = provider.handle_tool_call("icm_health", {})

    assert isinstance(result, str)
    assert json.loads(result) == {"report": {}}
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, (
        f"expected exactly one WARNING, got {len(warnings)}: "
        f"{[r.getMessage() for r in warnings]!r}"
    )


# =============================================================================
# AC13 — unknown tool name
# =============================================================================


def test_unknown_tool_name_returns_error_json() -> None:
    """AC13 — unknown name → {"error": "unknown tool: ..."} JSON, no raise."""
    provider = _read_provider()
    result = provider.handle_tool_call("anything-at-all", {})
    assert isinstance(result, str)
    payload = json.loads(result)
    assert "error" in payload
    assert "unknown tool" in payload["error"]
    assert "anything-at-all" in payload["error"]


# =============================================================================
# AC14 — every handler returns str (parametrized over every code path)
# =============================================================================


def test_no_tool_returns_dict(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC14 — `isinstance(result, str)` holds across every dispatch code path."""
    monkeypatch.setattr(
        tools, "run_recall", lambda *a, **kw: [{"id": "m1"}]
    )
    monkeypatch.setattr(tools, "run_topics", lambda *a, **kw: [{"topic": "p"}])
    monkeypatch.setattr(
        tools, "run_health", lambda *a, **kw: {"stale": "0"}
    )

    provider = _provider_with_queue()

    paths: list[tuple[str, dict[str, Any]]] = [
        ("icm_recall", {"query": "x"}),
        ("icm_store", {"topic": "p", "content": "c"}),
        ("icm_store", {}),  # validation failure path
        ("icm_topics", {}),
        ("icm_health", {}),
        ("icm_health", {"topic": "p"}),
        ("anything-at-all", {}),
    ]
    for name, args in paths:
        out = provider.handle_tool_call(name, args)
        assert isinstance(out, str), f"handler for {name!r} returned {type(out).__name__}"
        # Also: every output is valid JSON.
        json.loads(out)


# =============================================================================
# AC15 — no exception escapes any handler boundary, even with garbage args
# =============================================================================


# =============================================================================
# Coverage extras — branches not exercised by the 16 ACs but required to push
# tools.py ≥ 95 % (queue-full, queue-missing, explicit importance, keyword
# coercion, configured read timeout). Each pins a documented degrade path.
# =============================================================================


def test_store_with_explicit_importance_uses_caller_value() -> None:
    """Caller-supplied importance overrides default."""
    provider = _provider_with_queue()
    provider.handle_tool_call(
        "icm_store",
        {"topic": "p", "content": "c", "importance": "critical"},
    )
    task = provider._write_queue.get_nowait()  # type: ignore[attr-defined]
    assert task[1] == "critical"


def test_store_keywords_list_is_stringified() -> None:
    """Caller list[str] keywords flow through as list[str]."""
    provider = _provider_with_queue()
    provider.handle_tool_call(
        "icm_store",
        {"topic": "p", "content": "c", "keywords": ["bun", "package-manager"]},
    )
    task = provider._write_queue.get_nowait()  # type: ignore[attr-defined]
    assert task[3] == ["bun", "package-manager"]


def test_store_keywords_csv_string_is_split() -> None:
    """Caller comma-separated string keywords are split + trimmed."""
    provider = _provider_with_queue()
    provider.handle_tool_call(
        "icm_store",
        {"topic": "p", "content": "c", "keywords": "bun, package-manager,  npm"},
    )
    task = provider._write_queue.get_nowait()  # type: ignore[attr-defined]
    assert task[3] == ["bun", "package-manager", "npm"]


def test_store_keywords_unknown_type_falls_back_to_empty() -> None:
    """Caller-supplied junk → empty list (no crash)."""
    provider = _provider_with_queue()
    provider.handle_tool_call(
        "icm_store",
        {"topic": "p", "content": "c", "keywords": 12345},
    )
    task = provider._write_queue.get_nowait()  # type: ignore[attr-defined]
    assert task[3] == []


def test_store_queue_full_returns_error_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`queue.Full` → drop-with-WARNING + error JSON."""
    provider = _provider_with_queue(qsize=1)
    # Fill the queue.
    provider._write_queue.put_nowait(("a", "high", "x", []))  # type: ignore[attr-defined]

    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        result = provider.handle_tool_call(
            "icm_store",
            {"topic": "p", "content": "c"},
        )
    payload = json.loads(result)
    assert "error" in payload
    assert "queue full" in payload["error"]
    assert any("queue full" in r.getMessage() for r in caplog.records)


def test_store_without_queue_returns_error_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Missing `_write_queue` (S08 not yet merged) → error JSON, no raise."""
    provider = _read_provider()
    # Note: no _provider_with_queue helper — bare provider has no _write_queue.
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        result = provider.handle_tool_call(
            "icm_store",
            {"topic": "p", "content": "c"},
        )
    payload = json.loads(result)
    assert "error" in payload
    assert "queue unavailable" in payload["error"]


def test_recall_uses_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider-config `command_timeout_read_ms` flows into cli_runner."""
    captured: dict[str, Any] = {}

    def _fake_run_recall(
        query: str,
        limit: int,
        db_path: Any,
        timeout_ms: int,
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["timeout_ms"] = timeout_ms
        return []

    monkeypatch.setattr(tools, "run_recall", _fake_run_recall)

    provider = _read_provider()
    provider._config["command_timeout_read_ms"] = 1234
    provider.handle_tool_call("icm_recall", {"query": "x"})
    assert captured["timeout_ms"] == 1234


def test_recall_uses_configured_recall_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Provider-config `recall_limit` is honoured when caller omits `limit`."""
    captured: dict[str, Any] = {}

    def _fake_run_recall(
        query: str,
        limit: int,
        db_path: Any,
        timeout_ms: int,
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["limit"] = limit
        return []

    monkeypatch.setattr(tools, "run_recall", _fake_run_recall)

    provider = _read_provider()
    provider._config["recall_limit"] = 11
    provider.handle_tool_call("icm_recall", {"query": "x"})
    assert captured["limit"] == 11


@pytest.mark.parametrize(
    ("name", "args", "expected_payload"),
    [
        ("icm_recall", {"query": "x"}, {"hits": []}),
        ("icm_topics", {}, {"topics": []}),
        ("icm_health", {}, {"report": {}}),
    ],
)
def test_read_handlers_degrade_when_provider_not_initialized(
    name: str,
    args: dict[str, Any],
    expected_payload: dict[str, Any],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Bare provider (no `_db_path`) → degrade JSON + WARNING, no raise."""
    provider = IcmMemoryProvider()  # _db_path stays None
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        result = provider.handle_tool_call(name, args)
    assert isinstance(result, str)
    assert json.loads(result) == expected_payload
    assert any(
        "not initialized" in record.getMessage() for record in caplog.records
    )


# =============================================================================
# Phase 3 review follow-ups — close gaps surfaced by Auditor + Edge Case Hunter
# =============================================================================


def test_store_uses_config_default_importance() -> None:
    """AC6 (config-default branch) — `_config["default_importance"]` wins over fallback.

    Auditor partial-coverage finding: AC6 mandates the config-supplied default
    branch, but the original suite only tested the explicit-arg path and the
    pure-default path. This pins the middle branch.
    """
    provider = _provider_with_queue()
    provider._config["default_importance"] = "medium"
    provider.handle_tool_call("icm_store", {"topic": "p", "content": "c"})
    task = provider._write_queue.get_nowait()  # type: ignore[attr-defined]
    assert task[1] == "medium"


def test_store_with_non_dict_config_falls_back_to_default_importance() -> None:
    """`_config` rebound to non-dict → `_importance_for` skips the cfg branch.

    Pins the `isinstance(cfg, dict) is False` branch in `_importance_for`
    so a corrupt provider state still produces a documented task tuple
    rather than raising.
    """
    provider = _provider_with_queue()
    provider._config = None  # type: ignore[assignment] — pathological
    provider.handle_tool_call("icm_store", {"topic": "p", "content": "c"})
    task = provider._write_queue.get_nowait()  # type: ignore[attr-defined]
    assert task[1] == "high"


def test_store_invalid_config_default_importance_falls_back() -> None:
    """Garbage `_config["default_importance"]` → coerced to fallback default.

    Defensive: a stale or hand-edited sidecar might set
    `default_importance="urgent"` outside the validated enum. The handler
    must not propagate that bogus value to the worker.
    """
    provider = _provider_with_queue()
    provider._config["default_importance"] = "URGENT"  # not in enum
    provider.handle_tool_call("icm_store", {"topic": "p", "content": "c"})
    task = provider._write_queue.get_nowait()  # type: ignore[attr-defined]
    assert task[1] == "high"  # ultimate fallback


def test_store_invalid_importance_falls_back_to_default() -> None:
    """Bogus importance value → coerced to default rather than passed to worker.

    Closes Phase 3 Edge Case Hunter F3: schema declares an enum, handler must
    enforce it. Otherwise a typo (`"urgent"`) silently reaches `icm store -i
    urgent` and fails on the worker, after the agent already saw `accepted=True`.
    """
    provider = _provider_with_queue()
    provider.handle_tool_call(
        "icm_store",
        {"topic": "p", "content": "c", "importance": "URGENT"},
    )
    task = provider._write_queue.get_nowait()  # type: ignore[attr-defined]
    assert task[1] == "high"  # default fallback, not "URGENT"


@pytest.mark.parametrize(
    ("name", "expected_payload"),
    [
        ("icm_recall", {"hits": []}),
        ("icm_topics", {"topics": []}),
        ("icm_health", {"report": {}}),
    ],
)
def test_read_handlers_degrade_on_untyped_exception(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    name: str,
    expected_payload: dict[str, Any],
) -> None:
    """Untyped exception from cli_runner → per-tool degrade shape, not generic crash.

    Closes Phase 3 Edge Case Hunter F7/F8: only catching `ICMError` lets
    `OSError` / `MemoryError` / `UnicodeError` fall through to the outer net,
    which returned the generic `{"error": "tool handler crashed"}` instead of
    the documented `{"hits": []}` / `{"topics": []}` / `{"report": {}}`.
    """

    def _untyped_boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("untyped runtime explosion")

    monkeypatch.setattr(tools, "run_recall", _untyped_boom)
    monkeypatch.setattr(tools, "run_topics", _untyped_boom)
    monkeypatch.setattr(tools, "run_health", _untyped_boom)

    provider = _read_provider()
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        result = provider.handle_tool_call(name, {"query": "x"})

    assert json.loads(result) == expected_payload


@pytest.mark.parametrize("bad_args", [None, "garbage", 12345, [1, 2, 3], 0.5])
def test_handle_tool_call_coerces_non_dict_args(bad_args: Any) -> None:
    """`args` coerced to {} when not a dict; per-tool degrade shape still wins.

    Closes Phase 3 Edge Case Hunter F11: the truthiness coercion `args or {}`
    let truthy non-dict values through (e.g. `"foo" or {}` → `"foo"`), then
    `args.get("query")` raised `AttributeError`, surfacing as the generic
    crash JSON instead of the documented recall-degrade.
    """
    provider = _read_provider()
    result = provider.handle_tool_call("icm_recall", bad_args)
    # Non-dict args means no `query` → recall validation degrades to {hits: []}
    assert isinstance(result, str)
    assert json.loads(result) == {"hits": []}


def test_dispatch_outer_net_catches_handler_bugs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """If a future handler ever forgets its inner try/except, the outer net traps it.

    Defense-in-depth for AD-07 / NFR-REL-1: even when a handler raises an
    untyped exception (a regression bug, not a documented failure mode), the
    dispatch returns a documented error JSON rather than letting the exception
    escape into Hermes's turn loop.
    """

    def _bug(provider: Any, args: Any) -> str:
        raise RuntimeError("regression: handler forgot its try/except")

    monkeypatch.setitem(tools._DISPATCH, "icm_recall", _bug)

    provider = _read_provider()
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        result = provider.handle_tool_call("icm_recall", {"query": "x"})

    payload = json.loads(result)
    assert "error" in payload
    assert "tool handler crashed" in payload["error"]
    assert any("tool handler crashed" in r.getMessage() for r in caplog.records)


def test_corrupt_config_does_not_crash_read_handlers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`_config` rebound to non-dict → read handlers still degrade documented shape.

    Closes Phase 3 Edge Case Hunter F5/F6: `_read_timeout_ms` (and the limit
    helper) used to call `.get(...)` directly, raising `AttributeError` which
    the outer net converted into the generic crash JSON instead of the
    per-tool degrade shape.
    """
    captured: dict[str, Any] = {}

    def _fake_run_recall(
        query: str,
        limit: int,
        db_path: Any,
        timeout_ms: int,
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["timeout_ms"] = timeout_ms
        return []

    monkeypatch.setattr(tools, "run_recall", _fake_run_recall)

    provider = _read_provider()
    provider._config = None  # type: ignore[assignment] — pathological
    result = provider.handle_tool_call("icm_recall", {"query": "x"})
    # No crash; documented degrade shape; default timeout used.
    assert json.loads(result) == {"hits": []}
    assert captured["timeout_ms"] == 2000  # _DEFAULT_READ_TIMEOUT_MS


def test_no_tool_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC15 — pathological args + cli_runner failures: handler must not raise."""

    # Make every cli_runner shim raise something nasty.
    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("untyped runtime explosion")

    monkeypatch.setattr(tools, "run_recall", _boom)
    monkeypatch.setattr(tools, "run_topics", _boom)
    monkeypatch.setattr(tools, "run_health", _boom)

    provider = _provider_with_queue()

    # Garbage args matrix — None / wrong type / empty / unexpected nested junk.
    matrix: list[tuple[str, Any]] = [
        ("icm_recall", {"query": "x"}),
        ("icm_recall", {}),
        ("icm_recall", {"query": None}),
        ("icm_recall", {"query": "x", "limit": "not-an-int"}),
        ("icm_store", {"topic": "p", "content": "c"}),
        ("icm_store", {"topic": None, "content": None}),
        ("icm_store", {"topic": ["a"], "content": {"x": 1}}),
        ("icm_topics", {}),
        ("icm_topics", {"junk": [1, 2, 3]}),
        ("icm_health", {}),
        ("icm_health", {"topic": 42}),
        ("anything-at-all", {}),
        ("", {}),
    ]
    for name, args in matrix:
        # Must not raise for any of them.
        result = provider.handle_tool_call(name, args)
        assert isinstance(result, str)
        json.loads(result)  # parses cleanly
