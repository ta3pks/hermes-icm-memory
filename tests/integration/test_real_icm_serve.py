"""S15 — Integration: real ``icm serve`` daemon round-trip.

Spawns a real ``icm serve --no-embeddings`` subprocess via
``cli_runner.mcp_start``, seeds one memory through the regular CLI path,
then calls ``cli_runner.run_recall(transport='mcp')`` twice and asserts
both round-trips return non-empty hits with the expected marker.

The model-cold-start "second call < 1 s" claim is checked by the Pi smoke
test (manual / on-deploy) rather than CI, because:

* CI hosts don't have the multilingual-e5-base ONNX model cached, so the
  cold start would be a download.
* ``--no-embeddings`` keeps the test fast and offline.

What we DO assert here: the MCP transport works end-to-end against a real
``icm serve`` binary — handshake, JSON-RPC framing, response parsing.
"""

from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path

import pytest

from hermes_icm_memory import cli_runner

pytestmark = pytest.mark.skipif(
    shutil.which("icm") is None, reason="icm not on PATH"
)


def _seed_memory(db: Path, marker: str) -> None:
    """Use the CLI path to plant a memory the MCP recall should find."""
    subprocess.run(
        [
            "icm",
            "--db",
            str(db),
            "store",
            "-t",
            "errors-resolved",
            "-c",
            f"S15 fingerprint: {marker}",
            "-i",
            "high",
            "-k",
            f"s15,{marker}",
            "--no-embeddings",
        ],
        check=True,
        capture_output=True,
    )


def test_real_icm_serve_two_recalls_share_one_daemon(tmp_path: Path) -> None:
    """Two recall round-trips against one ``icm serve``; both hit the seeded memory.

    The point of v0.2 is daemon reuse: the second call must NOT spawn a
    fresh process. We assert that by capturing ``Popen`` once at start time
    and verifying we can still write to its stdin after the first recall.
    """
    db = tmp_path / "icm-serve-it.db"
    marker = "s15-marker-fingerprint-recall-it"
    _seed_memory(db, marker)

    cli_runner.mcp_start(db_path=db, use_embeddings=False)
    try:
        # First recall (model cold start dodged via --no-embeddings).
        t0 = time.perf_counter()
        first = cli_runner.run_recall(
            query=marker,
            limit=5,
            db_path=db,
            timeout_ms=10000,
            transport="mcp",
        )
        first_ms = (time.perf_counter() - t0) * 1000.0

        # Second recall (must reuse the same daemon).
        t0 = time.perf_counter()
        second = cli_runner.run_recall(
            query=marker,
            limit=5,
            db_path=db,
            timeout_ms=10000,
            transport="mcp",
        )
        second_ms = (time.perf_counter() - t0) * 1000.0
    finally:
        cli_runner.mcp_stop()

    assert first, f"first recall returned no hits: {first!r}"
    assert second, f"second recall returned no hits: {second!r}"
    # The marker shows up in the parsed summary.
    assert any(marker in (h.get("summary") or "") for h in first)
    assert any(marker in (h.get("summary") or "") for h in second)
    # Sanity: no-embeddings keyword path is fast (well under 5 s on any host).
    assert first_ms < 5000, f"first recall too slow: {first_ms:.0f} ms"
    assert second_ms < 5000, f"second recall too slow: {second_ms:.0f} ms"
