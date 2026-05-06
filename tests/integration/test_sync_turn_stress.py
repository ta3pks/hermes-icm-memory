"""S14 — Integration: ``sync_turn`` stress under bounded-queue overflow (FR15).

Fires ``2 * N`` ``sync_turn`` calls into an ``N``-deep queue with the worker
gated mid-store, so the queue saturates and producers see ``queue.Full``.
Verifies:

* (a) FIFO order on accepted items (worker processes in queue order),
* (b) at least one item dropped (accepted < ``2 * N``),
* (c) exactly one ``WARNING`` per overflow burst (rate-limited via the
  ``overflow_burst`` flag),
* (d) no exception escapes any ``sync_turn`` / ``on_session_end`` call,
* (e) the eventually-drained ICM DB contains exactly ``accepted`` rows
  (verified through the plugin's recall path).
"""

from __future__ import annotations

import json
import logging
import shutil
import threading
from pathlib import Path
from typing import Any

import pytest

from hermes_icm_memory import cli_runner
from hermes_icm_memory.provider import IcmMemoryProvider

pytestmark = pytest.mark.skipif(
    shutil.which("icm") is None, reason="icm not on PATH"
)

_QUEUE_CAP = 4
_BURST_FACTOR = 2  # 2 × capacity per the locked spec


@pytest.fixture
def no_embeddings_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject ``--no-embeddings`` into every ``cli_runner._run`` invocation."""
    real_run = cli_runner._run

    def patched(argv: list[str], timeout_ms: int) -> Any:
        new_argv = list(argv)
        if new_argv and new_argv[0] == "icm" and "--no-embeddings" not in new_argv:
            new_argv.insert(1, "--no-embeddings")
        return real_run(new_argv, timeout_ms)

    monkeypatch.setattr(cli_runner, "_run", patched)


def test_overflow_fifo_warning_no_exception(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
    no_embeddings_subprocess: None,  # noqa: ARG001 — fixture is set-up only
) -> None:
    provider = IcmMemoryProvider()
    assert provider.is_available()

    # Shrink the queue to make overflow tractable in test time.
    err = provider.save_config(
        {"sync_write_queue_size": _QUEUE_CAP},
        hermes_home=str(tmp_path),
    )
    assert err is None
    provider.initialize("s14-stress", str(tmp_path))

    # Gate the worker mid-store so it cannot drain while producers fire.
    # Capture the order in which items are eventually processed (FIFO check).
    drain_gate = threading.Event()
    real_run_store = cli_runner.run_store
    processed: list[str] = []

    def gated_run_store(
        topic: str,
        content: str,
        importance: str,
        db_path: Path,
        timeout_ms: int,
        keywords: str | None = None,
        raw: str | None = None,
    ) -> None:
        # Block the worker until producers finish + we explicitly release.
        # 30 s ceiling guards against test-author errors leaving the gate shut.
        if not drain_gate.wait(timeout=30):
            raise RuntimeError("gate never opened — test bug")
        processed.append(content)
        return real_run_store(
            topic, content, importance, db_path, timeout_ms,
            keywords=keywords, raw=raw,
        )

    monkeypatch.setattr(cli_runner, "run_store", gated_run_store)

    # Capture every WARNING emitted under the package logger.
    caplog.set_level(logging.WARNING, logger="hermes_icm_memory")

    # Build distinct trigger contents — each matches the errors-resolved
    # pattern (\bfixed\b) so detect_triggers emits exactly one WriteTask
    # per sync_turn call, with a unique marker we can grep in DB rows.
    burst_size = _BURST_FACTOR * _QUEUE_CAP
    contents_fired = [
        f"Fixed bug s14stress-marker-{i:03d} in module"
        for i in range(burst_size)
    ]

    for content in contents_fired:
        provider.sync_turn(user_content="", assistant_content=content)

    # Open the gate; worker drains the queued (accepted) items in FIFO order.
    drain_gate.set()
    provider.on_session_end()
    write_queue = provider._write_queue
    assert write_queue is not None
    write_queue.join()
    provider._stop_event.set()

    accepted = len(processed)
    # The worker pops at most one item before blocking on the gate, so the
    # queue accommodates `_QUEUE_CAP` more — accepted is `_QUEUE_CAP` or
    # `_QUEUE_CAP + 1` depending on the spawn race. Either way: dropped > 0.
    assert _QUEUE_CAP <= accepted <= _QUEUE_CAP + 1, (
        f"unexpected accepted count {accepted} (cap={_QUEUE_CAP})"
    )
    assert accepted < burst_size, "no overflow — test setup did not stress the queue"

    # (a) FIFO is preserved AMONG ACCEPTED items: the relative order of
    # processed entries matches the order in which they were fired. The
    # *which* items got dropped is a race outcome (worker may have popped
    # between producer N and producer N+1 freeing a slot mid-burst); FIFO
    # only requires that whatever did make it through stays ordered.
    fired_index = {c: i for i, c in enumerate(contents_fired)}
    processed_positions = [fired_index[c] for c in processed]
    assert processed_positions == sorted(processed_positions), (
        f"FIFO violated: processed positions={processed_positions!r}"
    )

    # (c) Exactly one WARNING for the overflow burst (rate-limited).
    overflow_warnings = [
        rec for rec in caplog.records
        if rec.levelname == "WARNING" and "overflow" in rec.getMessage().lower()
    ]
    assert len(overflow_warnings) == 1, (
        f"expected exactly 1 overflow WARNING, got {len(overflow_warnings)}: "
        f"{[r.getMessage() for r in overflow_warnings]!r}"
    )

    # (d) No exception escaped: implicit — we got here without raising.

    # (e) DB contains exactly `accepted` rows. Recall via the plugin and
    # match against the unique marker substring shared by every fired item.
    recall_payload = json.loads(
        provider.handle_tool_call(
            "icm_recall",
            {"query": "s14stress-marker", "limit": burst_size + 5},
        )
    )
    hits = recall_payload["hits"]
    assert len(hits) == accepted, (
        f"DB row count {len(hits)} != accepted {accepted}; "
        f"hits={[h.get('summary') for h in hits]!r}"
    )
