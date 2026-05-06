"""S14 — Integration: store via plugin → drain → recall via cli_runner.

Round-trips a memory through the real ``icm`` binary against a
``tmp_path``-bound DB. v0.3 removed the LLM-tool surface (``handle_tool_call``);
writes are still exercised end-to-end via the trigger → worker → ``run_store``
path, and the read-back uses ``cli_runner.run_recall`` directly (the same
function the prefetch hook uses internally).

Embedding-model download is dodged via a ``cli_runner._run`` wrapper that
injects ``--no-embeddings`` after the ``icm`` argv head — production
``cli_runner`` is unchanged. The assertion is on keyword-search hits.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from hermes_icm_memory import cli_runner
from hermes_icm_memory.provider import IcmMemoryProvider

pytestmark = pytest.mark.skipif(
    shutil.which("icm") is None, reason="icm not on PATH"
)


def _drain_and_join(provider: IcmMemoryProvider) -> None:
    """Drain the write queue and wait for in-flight tasks to complete."""
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
    # ``_db_path is not None``).
    provider._config["isolated"] = True
    provider.initialize("s14-recall", str(tmp_path))

    # Trigger one ``errors-resolved`` write through the lifecycle hook
    # (the v0.3 plugin no longer exposes a direct ``icm_store`` LLM tool).
    marker = "s14fingerprint-import-bug-recallable"
    provider.sync_turn(
        user_content="",
        assistant_content=f"Fixed import bug in S14 {marker}",
    )

    _drain_and_join(provider)

    db_path = provider._db_path
    assert db_path is not None
    hits = cli_runner.run_recall(
        query=marker,
        limit=5,
        db_path=db_path,
        timeout_ms=10000,
        use_embeddings=False,
    )
    assert hits, f"recall returned no hits for marker {marker!r}"
    assert any(
        marker in (h.get("summary") or "") for h in hits
    ), f"no hit contained marker {marker!r}; hits={hits!r}"
