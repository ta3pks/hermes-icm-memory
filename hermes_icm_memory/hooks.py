"""Hot-path hooks: ``prefetch``, ``system_prompt_block``, ``sync_turn``,
``on_session_end`` + the bounded-queue daemon worker (S08, FR5/FR9/FR10/
FR13/FR14/FR15, NFR-PERF-1, NFR-REL-1, NFR-REL-2).

Architecture invariants:

* AD-12 — this module MUST NOT ``import subprocess`` (S11 AST test enforces);
  every ICM invocation flows through :mod:`hermes_icm_memory.cli_runner`.
* AD-07 / NFR-REL-1 — every public hook catches at the boundary and returns
  the documented degraded shape. No exception ever propagates into the
  Hermes turn loop.
* AD-13 — module-level ``logger = logging.getLogger(__name__)``; structured
  ``extra={...}`` dicts on every WARNING / CRITICAL **and** the exception
  text inlined into the format string via ``%r`` so the default Python
  formatter (which drops ``extra={...}``) still surfaces it (AC8 — the
  Pi outage on 2026-05-06 was undebuggable until this was hand-patched).

Worker model (locked by planner memo `01KQWT5T9EEEFGQYWKGVQPR5G3`):

* Single ``threading.Thread(daemon=True)`` worker draining
  ``queue.Queue(maxsize=N)`` (default 64; configurable via
  ``sync_write_queue_size``).
* Producer policy: ``put_nowait``; on ``queue.Full`` → log one WARNING per
  overflow burst (rate-limited by ``_overflow_burst[0]`` flag cleared by the
  worker after the next successful drain) and drop the task.
* Worker death policy: lazy-respawn at most once per process; second death
  sets ``_writes_disabled = True`` and CRITICAL-logs; subsequent enqueues
  no-op.

The four public hook callables are exposed as **methods** on
:class:`hermes_icm_memory.provider.IcmMemoryProvider`; this module hosts
the helpers + worker-loop body so the provider class stays small and the
worker state lives on the instance (per-process, not per-module).
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import cli_runner, classifier, mapping
from .errors import ICMError

# Re-export so consumers can import either from hooks or classifier.
ClassifyTask = classifier.ClassifyTask

__all__ = [
    "ClassifyTask",
    "WriteTask",
    "WorkerState",
    "classifier_loop",
    "drain_with_grace",
    "ensure_classifier",
    "ensure_worker",
    "format_block",
    "run_prefetch",
    "submit_triggers",
    "worker_loop",
]

logger = logging.getLogger(__name__)


# ---------- Data shapes ------------------------------------------------------


@dataclass(frozen=True, slots=True)
class WriteTask:
    """Single ICM write task drained by the worker thread.

    ``keywords`` is a tuple (immutable) so :class:`WriteTask` stays hashable
    and safe to hand off across threads without defensive copies.
    """

    topic: str
    importance: str
    content: str
    keywords: tuple[str, ...]


@dataclass(slots=True)
class WorkerState:
    """Mutable worker-state bundle held by the provider.

    A single dataclass keeps the related fields adjacent — the producer
    (``submit_triggers``) and the consumers (``worker_loop``,
    ``classifier_loop``) both read+write a subset, and grouping them here
    removes scattered instance attributes from :class:`IcmMemoryProvider`.
    """

    write_queue: queue.Queue[WriteTask] | None = None
    worker: threading.Thread | None = None
    stop_event: threading.Event = field(default_factory=threading.Event)
    overflow_burst: list[bool] = field(default_factory=lambda: [False])
    respawn_count: int = 0
    writes_disabled: bool = False
    turn_index: int = 0

    # v0.4 — classifier worker (async LLM-based trigger detection).
    classify_queue: queue.Queue[classifier.ClassifyTask] | None = None
    class_worker: threading.Thread | None = None
    class_respawn_count: int = 0
    class_disabled: bool = False


# ---------- Worker loop ------------------------------------------------------


def worker_loop(
    *,
    write_queue: queue.Queue[WriteTask],
    db_path: Path | None,
    timeout_ms: int,
    overflow_burst: list[bool],
    stop_event: threading.Event,
) -> None:
    """Daemon worker body. FIFO drain via blocking ``get`` with a 100 ms tick.

    Per-task ``try/except`` covers ``ICMError`` (the documented failure
    surface) and ``Exception`` (defensive — must not let the thread die).
    Each successful drain clears ``overflow_burst[0]`` so the next overflow
    burst gets exactly one WARNING.
    """
    while not stop_event.is_set():
        try:
            task = write_queue.get(timeout=0.1)
        except queue.Empty:
            continue
        try:
            cli_runner.run_store(
                task.topic,
                task.content,
                task.importance,
                db_path,
                timeout_ms,
                keywords=",".join(task.keywords) if task.keywords else None,
            )
        except ICMError as exc:
            logger.warning(
                "worker: store failed: %r",
                exc,
                extra={"err": repr(exc), "topic": task.topic},
            )
        except Exception as exc:  # defensive — see docstring
            logger.warning(
                "worker: unexpected error: %r",
                exc,
                extra={"err": repr(exc), "topic": task.topic},
            )
        finally:
            write_queue.task_done()
            overflow_burst[0] = False


# ---------- Classifier loop ----------------------------------------------------


def classifier_loop(
    *,
    classify_queue: queue.Queue[classifier.ClassifyTask],
    write_queue: queue.Queue[WriteTask],
    endpoint: str,
    model: str,
    api_key: str,
    timeout_s: float,
    stop_event: threading.Event,
) -> None:
    """Background classifier worker. Drains the classify queue, calls the LLM,
    and enqueues ``WriteTask`` when the LLM returns something to store.

    Never raises — every failure is caught locally and degraded to a DEBUG log.
    """
    while not stop_event.is_set():
        try:
            task = classify_queue.get(timeout=0.1)
        except queue.Empty:
            continue

        try:
            result = classifier.classify_exchange(
                task.user_text,
                task.assistant_text,
                endpoint=endpoint,
                model=model,
                api_key=api_key,
                timeout_s=timeout_s,
            )
        except Exception as exc:
            logger.debug(
                "classifier: unexpected error: %r",
                exc,
                extra={"err": repr(exc)},
            )
            classify_queue.task_done()
            continue

        if result is None:
            classify_queue.task_done()
            continue

        # LLM wants to store something — enqueue a write task.
        write_task = WriteTask(
            topic=result.topic,
            importance=result.importance,
            content=result.content,
            keywords=result.keywords,
        )
        try:
            write_queue.put_nowait(write_task)
        except queue.Full:
            logger.debug(
                "classifier: write queue full; dropping classified memory",
                extra={"topic": result.topic},
            )
        except Exception as exc:
            logger.debug(
                "classifier: enqueue failed: %r",
                exc,
                extra={"err": repr(exc), "topic": result.topic},
            )

        classify_queue.task_done()


# ---------- Worker lifecycle helpers ----------------------------------------


def ensure_worker(
    state: WorkerState,
    *,
    queue_size: int,
    db_path: Path | None,
    write_timeout_ms: int,
) -> bool:
    """Create the queue + spawn the worker on first need; respawn if dead.

    Returns ``True`` when the worker is running on exit, ``False`` when
    writes are permanently disabled (post-second-death). Intended to be
    invoked from the producer side before each ``put_nowait``.
    """
    if state.writes_disabled:
        return False

    if state.write_queue is None:
        state.write_queue = queue.Queue(maxsize=queue_size)

    if state.worker is None:
        state.worker = _spawn_worker(state, db_path, write_timeout_ms)
        return True

    if not state.worker.is_alive():
        if state.respawn_count >= 1:
            state.writes_disabled = True
            logger.critical(
                "worker: second death — writes disabled for the rest of the process",
                extra={"respawn_count": state.respawn_count},
            )
            return False
        state.respawn_count += 1
        state.stop_event.clear()
        state.worker = _spawn_worker(state, db_path, write_timeout_ms)
        logger.warning(
            "worker: respawned after death",
            extra={"respawn_count": state.respawn_count},
        )

    return True


def _spawn_worker(
    state: WorkerState,
    db_path: Path | None,
    timeout_ms: int,
) -> threading.Thread:
    """Construct and start a fresh daemon worker bound to ``state``."""
    assert state.write_queue is not None  # ensure_worker guarantees this
    thread = threading.Thread(
        target=worker_loop,
        kwargs={
            "write_queue": state.write_queue,
            "db_path": db_path,
            "timeout_ms": timeout_ms,
            "overflow_burst": state.overflow_burst,
            "stop_event": state.stop_event,
        },
        name="hermes-icm-writer",
        daemon=True,
    )
    thread.start()
    return thread


# ---------- Classifier lifecycle helpers ------------------------------------


def ensure_classifier(
    state: WorkerState,
    *,
    classify_queue_size: int,
    endpoint: str,
    model: str,
    api_key: str,
    timeout_s: float,
) -> bool:
    """Create the classify queue + spawn the classifier worker.

    Returns ``True`` when the classifier is running on exit, ``False`` when
    classifier is disabled (post-second-death).
    """
    if state.class_disabled:
        return False

    if state.classify_queue is None:
        state.classify_queue = queue.Queue(maxsize=classify_queue_size)

    if state.class_worker is None:
        state.class_worker = _spawn_classifier(state, endpoint, model, api_key, timeout_s)
        return True

    if not state.class_worker.is_alive():
        if state.class_respawn_count >= 1:
            state.class_disabled = True
            logger.critical(
                "classifier: second death — classifier disabled",
                extra={"class_respawn_count": state.class_respawn_count},
            )
            return False
        state.class_respawn_count += 1
        state.stop_event.clear()
        state.class_worker = _spawn_classifier(state, endpoint, model, api_key, timeout_s)
        logger.warning(
            "classifier: respawned after death",
            extra={"class_respawn_count": state.class_respawn_count},
        )

    return True


def _spawn_classifier(
    state: WorkerState,
    endpoint: str,
    model: str,
    api_key: str,
    timeout_s: float,
) -> threading.Thread:
    """Construct and start a fresh daemon classifier worker."""
    assert state.classify_queue is not None
    assert state.write_queue is not None
    thread = threading.Thread(
        target=classifier_loop,
        kwargs={
            "classify_queue": state.classify_queue,
            "write_queue": state.write_queue,
            "endpoint": endpoint,
            "model": model,
            "api_key": api_key,
            "timeout_s": timeout_s,
            "stop_event": state.stop_event,
        },
        name="hermes-icm-classifier",
        daemon=True,
    )
    thread.start()
    return thread


# ---------- Recall helpers (prefetch + system_prompt_block) -----------------


def run_prefetch(
    *,
    query: str,
    db_path: Path | None,
    limit: int,
    timeout_ms: int,
    cache: dict[int, list[dict[str, Any]]],
    use_embeddings: bool = False,
) -> list[dict[str, Any]]:
    """Run a single recall, cache hits keyed by ``hash(query)``.

    On any :class:`ICMError` (or unexpected ``Exception``), returns ``[]``
    and stores ``[]`` in the cache so :func:`format_block` does not retry.
    Logs WARNING with ``extra={"err": ..., "query_hash": ...}`` and inlines
    the exception via ``%r`` so the default formatter surfaces it.
    **Never raises.**

    v0.1.1 — ``db_path`` may be ``None`` (default-shared mode lets ``icm``
    use its canonical OS-default DB) and ``use_embeddings`` controls the
    ``--no-embeddings`` flag in ``cli_runner.run_recall``.

    v0.3 — single CLI subprocess path. The semantic-recall fast-path
    previously offered by ``transport: mcp`` is now delivered by
    hermes-native ``mcp_servers.icm:`` (LLM-side ``icm_memory_recall``),
    so the plugin's prefetch hot-path uses fresh keyword-only subprocesses
    on Pi (sub-100 ms per call).
    """
    key = hash(query)
    try:
        hits = cli_runner.run_recall(
            query,
            limit=limit,
            db_path=db_path,
            timeout_ms=timeout_ms,
            use_embeddings=use_embeddings,
        )
    except ICMError as exc:
        logger.warning(
            "prefetch: recall failed; returning empty: %r",
            exc,
            extra={"err": repr(exc), "query_hash": key},
        )
        cache[key] = []
        return []
    except Exception as exc:  # defensive boundary
        logger.warning(
            "prefetch: unexpected error: %r",
            exc,
            extra={"err": repr(exc), "query_hash": key},
        )
        cache[key] = []
        return []

    cache[key] = hits
    return hits


def format_block(
    *,
    cache: dict[int, list[dict[str, Any]]],
    latest_key: int | None,
    recall_limit: int,
) -> str:
    """Compose top-K block + project-context line from cached hits only.

    No subprocess, no recall — pure dictionary read + string formatting
    (NFR-PERF-4). Empty cache or missing latest key → returns ``""``.
    """
    if latest_key is None:
        return ""
    hits = cache.get(latest_key) or []
    if not hits:
        return ""

    capped = hits[: max(1, recall_limit)]
    lines: list[str] = ["Recalled memories:"]
    topics: list[str] = []
    seen_topics: set[str] = set()
    for hit in capped:
        topic = str(hit.get("topic") or "")
        summary = str(hit.get("summary") or hit.get("content") or "")
        lines.append(f"- [{topic}] {summary}".rstrip())
        if topic and topic not in seen_topics:
            seen_topics.add(topic)
            topics.append(topic)

    summary_line = (
        f"\nProject context: {', '.join(topics)}." if topics else ""
    )
    return "\n".join(lines) + summary_line


# ---------- sync_turn body --------------------------------------------------


def submit_triggers(
    state: WorkerState,
    *,
    user_content: str,
    assistant_content: str,
    project: str | None,
    every_n_turns: int,
    classifier_enabled: bool = False,
) -> None:
    """``sync_turn`` body: detect → enqueue → drop on full with one WARN per burst.

    Two modes:
    1. **Classifier mode** (``classifier_enabled=True``): enqueue a
       :class:`ClassifyTask` for async LLM-based classification. The
       background classifier worker calls the LLM and may enqueue a
       :class:`WriteTask` if the exchange is worth remembering.
    2. **Regex mode** (default): use :func:`mapping.detect_triggers` to
       produce inline write tasks (existing v0.3 behaviour).

    Delegated to from :meth:`IcmMemoryProvider.sync_turn` so the provider
    method stays a thin wrapper that owns only state lookup. Catches
    broadly at the boundary; never raises.
    """
    if state.writes_disabled:
        return
    state.turn_index += 1

    if classifier_enabled:
        _submit_classify_task(state, user_content, assistant_content, project)
        # Also fire periodic progress check regardless of content.
        _submit_periodic_context(state, turn_index=state.turn_index, every_n_turns=every_n_turns, project=project)
        return

    # Regex path (existing behaviour).
    if state.write_queue is None:
        return
    try:
        triggers = mapping.detect_triggers(
            user_content,
            assistant_content,
            project=project,
            turn_index=state.turn_index,
            every_n_turns=every_n_turns,
        )
    except Exception as exc:
        logger.warning(
            "sync_turn: detect_triggers raised; dropping turn: %r",
            exc,
            extra={"err": repr(exc)},
        )
        return

    for topic, importance, content, keywords in triggers:
        task = WriteTask(
            topic=topic,
            importance=importance,
            content=content,
            keywords=tuple(keywords),
        )
        try:
            state.write_queue.put_nowait(task)
        except queue.Full:
            _warn_overflow_once(state)
        except Exception as exc:  # defensive — never raise into the turn
            logger.warning(
                "sync_turn: enqueue raised; dropping task: %r",
                exc,
                extra={"err": repr(exc), "topic": topic},
            )


def _submit_classify_task(
    state: WorkerState,
    user_content: str,
    assistant_content: str,
    project: str | None,
) -> None:
    """Enqueue a ``ClassifyTask`` — drop on full with DEBUG log."""
    if state.classify_queue is None or state.class_disabled:
        return
    task = classifier.ClassifyTask(
        user_text=user_content,
        assistant_text=assistant_content,
        project=project,
    )
    try:
        state.classify_queue.put_nowait(task)
    except queue.Full:
        logger.debug("sync_turn: classify queue full; dropping exchange")
    except Exception as exc:
        logger.debug(
            "sync_turn: classify enqueue error: %r",
            exc,
            extra={"err": repr(exc)},
        )


def _submit_periodic_context(
    state: WorkerState,
    *,
    turn_index: int,
    every_n_turns: int,
    project: str | None,
) -> None:
    """Fire periodic progress checkpoint via the write queue."""
    if state.write_queue is None:
        return
    if every_n_turns <= 0 or turn_index <= 0 or turn_index % every_n_turns != 0:
        return
    topic = mapping.MAPPING["context"]["topic_template"].format(project=project or "default")
    importance = mapping.MAPPING["context"]["importance"]
    content = f"periodic progress checkpoint: turn {turn_index}"
    task = WriteTask(topic=topic, importance=importance, content=content, keywords=())
    try:
        state.write_queue.put_nowait(task)
    except queue.Full:
        _warn_overflow_once(state)
    except Exception as exc:
        logger.warning(
            "sync_turn: periodic enqueue error: %r",
            exc,
            extra={"err": repr(exc)},
        )


def _warn_overflow_once(state: WorkerState) -> None:
    """Rate-limited overflow WARNING.

    First overflow in a burst → flip ``overflow_burst[0] = True`` and log
    once. Subsequent overflows in the same burst are silent. The worker
    clears the flag after each successful drain (re-arming the next burst
    for exactly one WARNING).
    """
    if state.overflow_burst[0]:
        return
    state.overflow_burst[0] = True
    cap = state.write_queue.maxsize if state.write_queue is not None else 0
    logger.warning(
        "sync_turn: write queue overflow; dropping task",
        extra={"queue_size": cap},
    )


# ---------- on_session_end body ---------------------------------------------


def drain_with_grace(
    state: WorkerState,
    *,
    grace_ms: int,
) -> None:
    """Wait up to ``grace_ms`` for the queue to empty; drop with one WARN if not.

    The daemon worker exits at process shutdown; this method does NOT join
    the worker — only drains. Bounded by ``grace_ms + 100 ms``.
    """
    if state.write_queue is None:
        return

    deadline = time.monotonic() + grace_ms / 1000.0
    while time.monotonic() < deadline:
        if state.write_queue.empty():
            return
        time.sleep(0.02)

    remaining = state.write_queue.qsize()
    if remaining > 0:
        logger.warning(
            "on_session_end: grace expired; dropping remaining tasks",
            extra={"remaining": remaining, "grace_ms": grace_ms},
        )
