"""S14 — Integration: store via plugin → drain → recall via plugin.

Round-trips a memory through the real ``icm`` binary against a
``tmp_path``-bound DB. Verifies FR12/SM2 cross-session recall semantics
end-to-end: the plugin's ``icm_store`` tool enqueues, the worker drains via
``cli_runner.run_store``, and the plugin's ``icm_recall`` tool reads the
freshly-written row back.

Embedding-model download is dodged via a ``cli_runner._run`` wrapper that
injects ``--no-embeddings`` after the ``icm`` argv head — production
``cli_runner`` is unchanged. The assertion is on keyword-search hits.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from hermes_icm_memory.provider import IcmMemoryProvider

pytestmark = pytest.mark.skipif(
    shutil.which("icm") is None, reason="icm not on PATH"
)


def _drain_and_join(provider: IcmMemoryProvider) -> None:
    """Drain the write queue and wait for in-flight tasks to complete.

    Production ``drain_with_grace`` returns when the queue is empty, which
    can happen *before* the worker finishes the popped task. Tests need the
    stronger sync, so we follow up with ``Queue.join()``.
    """
    provider.on_session_end()
    write_queue = provider._write_queue
    if write_queue is not None:
        write_queue.join()
    provider._stop_event.set()


def test_store_then_recall_returns_hit(
    tmp_path: Path,
    no_embeddings_subprocess: None,  # noqa: ARG001 — fixture is set-up only
) -> None:
    provider = IcmMemoryProvider()
    assert provider.is_available()
    # v0.1.1: writes need a concrete ``_db_path`` (worker spawn gated on
    # ``_db_path is not None``); the integration round-trip uses a
    # ``tmp_path``-bound file, so opt into ``isolated=True``.
    provider._config["isolated"] = True
    provider.initialize("s14-recall", str(tmp_path))

    # Spawn the worker (icm_store needs the queue to exist).
    provider.sync_turn(user_content="", assistant_content="")

    marker = "s14fingerprint-import-bug-recallable"
    store_payload = json.loads(
        provider.handle_tool_call(
            "icm_store",
            {
                "topic": "errors-resolved",
                "content": f"Fixed import bug in S14 {marker}",
                "importance": "high",
                "keywords": ["import", "s14", marker],
            },
        )
    )
    assert store_payload["accepted"] is True
    assert "queued_at" in store_payload

    _drain_and_join(provider)

    recall_payload = json.loads(
        provider.handle_tool_call(
            "icm_recall",
            {"query": marker, "limit": 5},
        )
    )
    hits = recall_payload["hits"]
    assert isinstance(hits, list) and hits, f"recall returned no hits: {recall_payload!r}"
    assert any(
        marker in (h.get("summary") or "") for h in hits
    ), f"no hit contained marker {marker!r}; hits={hits!r}"
