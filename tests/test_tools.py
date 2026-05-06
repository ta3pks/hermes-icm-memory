"""Tests for ``hermes_icm_memory.tools`` (S09).

The 16 ACs trace 1-to-1 to story 4.2 (epic 4 / S09); the rest of the file
covers defensive paths surfaced during code review.

S08 owns ``provider._worker_state.write_queue`` (typed
``queue.Queue[hooks.WriteTask] | None``); ``provider._write_queue`` on the
provider is a read-only property over that field. Tests therefore install
their own ``queue.Queue`` on ``_worker_state.write_queue`` and assert on
``WriteTask`` field access, not tuple indexing.
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
from hermes_icm_memory.hooks import WriteTask
from hermes_icm_memory.provider import IcmMemoryProvider

# ---------- helpers -----------------------------------------------------------

#: Sentinel DB path used by isolated-mode tests — non-None so write-path
#: handlers can pin the queued task. Read-path handlers in v0.1.1 guard on
#: ``_init_args is None`` (not ``_db_path is None``); a default-shared provider
#: with ``_init_args`` set + ``_db_path is None`` is a legitimate state.
_FAKE_DB = Path("/tmp/__hermes_icm_test__/default.db")


def _read_provider() -> IcmMemoryProvider:
    """Fresh provider with ``_init_args`` set so read-path handlers don't short-circuit.

    v0.1.1: read tools guard on ``_init_args is None`` (provider never
    initialized), not ``_db_path is None`` (which is the legitimate
    default-shared sentinel). Setting ``_init_args`` mimics a post-initialize
    default-shared state — ``_db_path`` stays ``None``.
    """
    p = IcmMemoryProvider()
    p._init_args = ("test-session", "/tmp/test-hermes-home", None)
    return p


def _provider_with_queue(qsize: int = 8) -> IcmMemoryProvider:
    """Fresh provider with a bounded write queue + ``_db_path`` attached.

    Used for write-path tests that need a concrete queue + ``_db_path`` (the
    worker spawns only when ``_db_path`` is non-None — v0.1.1 keeps writes
    isolated-only). Mocks at the source: ``_worker_state.write_queue`` is the
    writable field that ``provider._write_queue`` (read-only property) reads
    from.
    """
    p = IcmMemoryProvider()
    p._init_args = ("test-session", "/tmp/test-hermes-home", "default")
    p._db_path = _FAKE_DB
    p._worker_state.write_queue = queue.Queue(maxsize=qsize)
    return p


def _drain_queue(provider: IcmMemoryProvider) -> WriteTask:
    """Pop the next task from the provider's write queue (test helper)."""
    q = provider._worker_state.write_queue
    assert q is not None, "test fixture must install a queue first"
    return q.get_nowait()


def _queue_size(provider: IcmMemoryProvider) -> int:
    """Return the current queue depth (0 if no queue installed)."""
    q = provider._worker_state.write_queue
    return 0 if q is None else q.qsize()


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
        use_embeddings: bool = False,
        transport: str = "cli",
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
# AC5 + AC6 — store enqueues a WriteTask (topic, importance, content, keywords)
# =============================================================================


def test_store_enqueues_and_returns_immediately() -> None:
    """AC5/AC6 — exactly one WriteTask enqueued; return shape correct."""
    provider = _provider_with_queue()
    result = provider.handle_tool_call(
        "icm_store",
        {"topic": "preferences", "content": "Always use bun"},
    )

    assert isinstance(result, str)
    payload = json.loads(result)
    assert payload["accepted"] is True
    assert isinstance(payload["queued_at"], str)

    # Single task enqueued with the documented WriteTask shape.
    assert _queue_size(provider) == 1
    task = _drain_queue(provider)
    assert isinstance(task, WriteTask)
    assert task.topic == "preferences"
    assert task.importance == "high"  # default
    assert task.content == "Always use bun"
    assert task.keywords == ()  # empty tuple, not list


# =============================================================================
# AC5 latency clause — store p95 < 5 ms over 200 invocations
# =============================================================================


def test_store_p95_under_5ms() -> None:
    """AC5 latency clause — handler completes p95 < 5 ms (relaxed to 25 ms ceiling on Pi).

    Generous threshold per team-lead matches the sibling
    ``test_sync_turn_p95_under_5ms`` in ``tests/test_hooks.py``: the 5 ms NFR-PERF-1
    target is the documented design goal; this ceiling guards against gross perf
    cliffs (e.g. accidentally blocking on a subprocess call from the hot path)
    while staying robust to Pi-class CI under parallel test load.
    """
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
    assert p95 < 25.0, f"icm_store p95 was {p95:.2f}ms (expected < 25ms ceiling)"


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
    assert _queue_size(provider) == 0


# =============================================================================
# AC8 — topics success
# =============================================================================


def test_topics_returns_topics_key(monkeypatch: pytest.MonkeyPatch) -> None:
    """AC8 — cli_runner.run_topics list passes through wrapped in {"topics": ...}."""
    topics_list = [{"topic": "preferences"}, {"topic": "errors-resolved"}]

    def _fake_run_topics(
        db_path: Any, timeout_ms: int, *, transport: str = "cli"
    ) -> list[dict[str, Any]]:
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
        db_path: Any,
        timeout_ms: int,
        topic: str | None = None,
        *,
        transport: str = "cli",
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
        db_path: Any,
        timeout_ms: int,
        topic: str | None = None,
        *,
        transport: str = "cli",
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
    task = _drain_queue(provider)
    assert task.importance == "critical"


def test_store_keywords_list_is_stringified() -> None:
    """Caller list[str] keywords flow through as a tuple[str, ...]."""
    provider = _provider_with_queue()
    provider.handle_tool_call(
        "icm_store",
        {"topic": "p", "content": "c", "keywords": ["bun", "package-manager"]},
    )
    task = _drain_queue(provider)
    assert task.keywords == ("bun", "package-manager")


def test_store_keywords_csv_string_is_split() -> None:
    """Caller comma-separated string keywords are split + trimmed."""
    provider = _provider_with_queue()
    provider.handle_tool_call(
        "icm_store",
        {"topic": "p", "content": "c", "keywords": "bun, package-manager,  npm"},
    )
    task = _drain_queue(provider)
    assert task.keywords == ("bun", "package-manager", "npm")


def test_store_keywords_unknown_type_falls_back_to_empty() -> None:
    """Caller-supplied junk → empty tuple (no crash)."""
    provider = _provider_with_queue()
    provider.handle_tool_call(
        "icm_store",
        {"topic": "p", "content": "c", "keywords": 12345},
    )
    task = _drain_queue(provider)
    assert task.keywords == ()


def test_store_queue_full_returns_error_json(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """`queue.Full` → drop-with-WARNING + error JSON."""
    provider = _provider_with_queue(qsize=1)
    # Fill the queue with a real WriteTask so the type matches the queue's hint.
    filler = WriteTask(topic="a", importance="high", content="x", keywords=())
    assert provider._worker_state.write_queue is not None
    provider._worker_state.write_queue.put_nowait(filler)

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
    """No write queue installed → error JSON, no raise (defense-in-depth)."""
    provider = _read_provider()
    # _read_provider() leaves _worker_state.write_queue=None (default).
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        result = provider.handle_tool_call(
            "icm_store",
            {"topic": "p", "content": "c"},
        )
    payload = json.loads(result)
    assert "error" in payload
    assert "queue unavailable" in payload["error"]


def test_recall_rejects_bool_limit_falls_back_to_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`limit=True` is `isinstance(True, int)` but a meaningless cap; reject it.

    Pins the bool-rejection branch in `_positive_int` so a caller passing
    `True`/`False` for `limit` falls back to the configured/default value.
    """
    captured: dict[str, Any] = {}

    def _fake_run_recall(
        query: str,
        limit: int,
        db_path: Any,
        timeout_ms: int,
        use_embeddings: bool = False,
        transport: str = "cli",
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["limit"] = limit
        return []

    monkeypatch.setattr(tools, "run_recall", _fake_run_recall)
    provider = _read_provider()
    provider.handle_tool_call("icm_recall", {"query": "x", "limit": True})
    assert captured["limit"] == 5  # _DEFAULT_RECALL_LIMIT, not 1


def test_recall_uses_configured_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    """Provider-config `command_timeout_read_ms` flows into cli_runner."""
    captured: dict[str, Any] = {}

    def _fake_run_recall(
        query: str,
        limit: int,
        db_path: Any,
        timeout_ms: int,
        use_embeddings: bool = False,
        transport: str = "cli",
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
        use_embeddings: bool = False,
        transport: str = "cli",
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
    """Bare provider (``_init_args is None``) → degrade JSON + WARNING, no raise.

    v0.1.1: the guard now hangs off ``_init_args`` rather than ``_db_path``
    because the latter is a legitimate ``None`` in default-shared mode. The
    "actually never initialized" condition is ``_init_args is None``.
    """
    provider = IcmMemoryProvider()  # _init_args stays None
    with caplog.at_level(logging.WARNING, logger="hermes_icm_memory.tools"):
        result = provider.handle_tool_call(name, args)
    assert isinstance(result, str)
    assert json.loads(result) == expected_payload
    assert any(
        "not initialized" in record.getMessage() for record in caplog.records
    )


@pytest.mark.parametrize(
    ("name", "args"),
    [
        ("icm_recall", {"query": "x"}),
        ("icm_topics", {}),
        ("icm_health", {}),
    ],
)
def test_read_handlers_proceed_when_initialized_default_shared(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, Any],
) -> None:
    """v0.1.1 — initialized default-shared provider (``_db_path is None``) does NOT degrade.

    The "not initialized" guard fires only on a *truly* uninitialized
    provider; a post-initialize default-shared provider is a legitimate
    state and read calls flow through to ``cli_runner`` with ``db_path=None``.
    """
    captured: dict[str, Any] = {"db_path": "<unset>"}

    def _fake_run_recall(
        query: str,
        limit: int,
        db_path: Any,
        timeout_ms: int,
        use_embeddings: bool = False,
        transport: str = "cli",
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["db_path"] = db_path
        return [{"id": "m1"}]

    def _fake_run_topics(
        db_path: Any, timeout_ms: int, *, transport: str = "cli"
    ) -> list[dict[str, Any]]:
        captured["db_path"] = db_path
        return [{"topic": "p"}]

    def _fake_run_health(
        db_path: Any,
        timeout_ms: int,
        topic: str | None = None,
        *,
        transport: str = "cli",
    ) -> dict[str, Any]:
        captured["db_path"] = db_path
        return {"stale": "0"}

    monkeypatch.setattr(tools, "run_recall", _fake_run_recall)
    monkeypatch.setattr(tools, "run_topics", _fake_run_topics)
    monkeypatch.setattr(tools, "run_health", _fake_run_health)

    provider = _read_provider()  # _init_args set, _db_path is None
    result = provider.handle_tool_call(name, args)
    payload = json.loads(result)
    assert "error" not in payload, f"unexpected error from {name}: {payload!r}"
    assert captured["db_path"] is None


def test_recall_threads_use_embeddings_from_provider_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """v0.1.1 — provider config ``use_embeddings`` flows to ``cli_runner.run_recall``.

    Schema default is ``True`` (Brief's semantic-recall value prop). Pi-class
    operators opt out via ``use_embeddings: false`` in their hermes config;
    the test pins both directions.
    """
    captured: dict[str, Any] = {}

    def _fake_run_recall(
        query: str,
        limit: int,
        db_path: Any,
        timeout_ms: int,
        use_embeddings: bool = True,
        transport: str = "cli",
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["use_embeddings"] = use_embeddings
        return []

    monkeypatch.setattr(tools, "run_recall", _fake_run_recall)

    # Default (no key set on _config) → True (matches schema default).
    provider = _read_provider()
    provider.handle_tool_call("icm_recall", {"query": "x"})
    assert captured["use_embeddings"] is True

    # Pi-class opt-out → False.
    provider._config["use_embeddings"] = False
    provider.handle_tool_call("icm_recall", {"query": "x"})
    assert captured["use_embeddings"] is False


# =============================================================================
# Hardening tests — defensive paths beyond the 16 ACs
# =============================================================================


@pytest.mark.parametrize(
    ("importance_arg", "config_default", "expected"),
    [
        # Caller wins over config and ultimate default.
        ("critical", "low", "critical"),
        # Config wins when caller omits / passes bogus value.
        (None, "medium", "medium"),
        ("URGENT", "medium", "medium"),  # bogus caller value, config valid
        # Ultimate default kicks in when both miss.
        ("URGENT", "URGENT", "high"),  # bogus everywhere
        (None, "URGENT", "high"),  # config bogus, no caller arg
    ],
)
def test_store_importance_fallback_ladder(
    importance_arg: str | None,
    config_default: str,
    expected: str,
) -> None:
    """Importance follows caller → config → default; bogus enum values fall back."""
    provider = _provider_with_queue()
    provider._config["default_importance"] = config_default
    args: dict[str, Any] = {"topic": "p", "content": "c"}
    if importance_arg is not None:
        args["importance"] = importance_arg
    provider.handle_tool_call("icm_store", args)
    task = _drain_queue(provider)
    assert task.importance == expected


def test_store_with_non_dict_config_falls_back_to_default_importance() -> None:
    """Pathological non-dict `_config` is tolerated by `_importance_for`."""
    provider = _provider_with_queue()
    provider._config = None  # type: ignore[assignment]
    provider.handle_tool_call("icm_store", {"topic": "p", "content": "c"})
    task = _drain_queue(provider)
    assert task.importance == "high"


@pytest.mark.parametrize(
    ("name", "args", "expected_payload"),
    [
        ("icm_recall", {"query": "x"}, {"hits": []}),
        ("icm_topics", {}, {"topics": []}),
        ("icm_health", {}, {"report": {}}),
    ],
)
def test_read_handlers_degrade_on_untyped_exception(
    monkeypatch: pytest.MonkeyPatch,
    name: str,
    args: dict[str, Any],
    expected_payload: dict[str, Any],
) -> None:
    """Untyped exception (OSError / MemoryError / etc.) → per-tool degrade shape."""

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise OSError("untyped runtime explosion")

    monkeypatch.setattr(tools, "run_recall", _boom)
    monkeypatch.setattr(tools, "run_topics", _boom)
    monkeypatch.setattr(tools, "run_health", _boom)

    provider = _read_provider()
    result = provider.handle_tool_call(name, args)
    assert json.loads(result) == expected_payload


@pytest.mark.parametrize("bad_args", [None, "garbage", 12345, [1, 2, 3], 0.5])
def test_handle_tool_call_coerces_non_dict_args(bad_args: Any) -> None:
    """Non-dict `args` are coerced to `{}` so the per-tool degrade still wins."""
    provider = _read_provider()
    result = provider.handle_tool_call("icm_recall", bad_args)
    assert isinstance(result, str)
    assert json.loads(result) == {"hits": []}


def test_dispatch_outer_net_catches_handler_bugs(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Defense-in-depth: a handler that raises is trapped before reaching Hermes."""

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
    """A non-dict `_config` is tolerated; read paths still degrade cleanly."""
    captured: dict[str, Any] = {}

    def _fake_run_recall(
        query: str,
        limit: int,
        db_path: Any,
        timeout_ms: int,
        use_embeddings: bool = False,
        transport: str = "cli",
        topic: str | None = None,
        project: str | None = None,
    ) -> list[dict[str, Any]]:
        captured["timeout_ms"] = timeout_ms
        return []

    monkeypatch.setattr(tools, "run_recall", _fake_run_recall)

    provider = _read_provider()
    provider._config = None  # type: ignore[assignment]
    result = provider.handle_tool_call("icm_recall", {"query": "x"})
    assert json.loads(result) == {"hits": []}
    assert captured["timeout_ms"] == 2000  # default


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
