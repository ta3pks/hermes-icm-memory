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
from typing import Any

import pytest

from hermes_icm_memory import cli_runner
from hermes_icm_memory.provider import IcmMemoryProvider

pytestmark = pytest.mark.skipif(
    shutil.which("icm") is None, reason="icm not on PATH"
)


@pytest.fixture
def no_embeddings_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject ``--no-embeddings`` into every ``cli_runner._run`` invocation.

    The real ``icm`` CLI loads its embeddings model lazily on first
    embedding-using call; CI hosts do not have it cached and would either
    download or OOM. ``--no-embeddings`` is a top-level flag accepted by
    every subcommand we exercise, and turns ICM into pure keyword search —
    sufficient for the recall assertions.
    """
    real_run = cli_runner._run

    def patched(argv: list[str], timeout_ms: int) -> Any:
        new_argv = list(argv)
        if new_argv and new_argv[0] == "icm" and "--no-embeddings" not in new_argv:
            new_argv.insert(1, "--no-embeddings")
        return real_run(new_argv, timeout_ms)

    monkeypatch.setattr(cli_runner, "_run", patched)


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
