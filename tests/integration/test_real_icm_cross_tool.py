"""S14 — Integration: external ``icm`` writer + plugin recall reader (FR12).

Simulates a Claude Code (or any other ICM client) write by invoking
``subprocess.run(["icm", "--no-embeddings", "--db", <db>, "store", ...])``
directly against the same DB the provider is initialized over. Then asserts
the plugin's ``cli_runner.run_recall`` (same code path the prefetch hook
uses) returns the externally-written memory.

v0.3 — the LLM-tool surface (``handle_tool_call``) was removed; this test
now exercises the read path through ``cli_runner.run_recall`` directly.

Note on the ``subprocess`` import: S11's AST invariant test is scoped to
``hermes_icm_memory/`` source files only — test files are exempt by design.
The cross-tool scenario inherently requires simulating an external writer.
"""

from __future__ import annotations

import shutil
import subprocess  # external-write simulation; see module docstring.
from pathlib import Path

import pytest

from hermes_icm_memory import cli_runner, config
from hermes_icm_memory.provider import IcmMemoryProvider

pytestmark = pytest.mark.skipif(
    shutil.which("icm") is None, reason="icm not on PATH"
)


def test_external_write_visible_to_plugin(
    tmp_path: Path,
    no_embeddings_subprocess: None,  # noqa: ARG001 — fixture is set-up only
) -> None:
    provider = IcmMemoryProvider()
    assert provider.is_available()
    # v0.1.1: pin the plugin's DB to ``tmp_path`` (isolated mode) so the
    # external write below targets the same file the plugin recalls from.
    provider._config["isolated"] = True
    provider.initialize("s14-cross-tool", str(tmp_path))

    db_path = config.resolve_db_path(tmp_path, profile=None)
    config.mkdir_parent(db_path)

    marker = "s14crosstoolfingerprint-decision-from-claudecode"
    external_argv = [
        "icm",
        "--no-embeddings",
        "--db",
        str(db_path),
        "store",
        "-t",
        "decisions-default",
        "-c",
        f"External write from Claude Code: {marker}",
        "-i",
        "high",
        "-k",
        f"crosstool,{marker}",
    ]
    proc = subprocess.run(
        external_argv,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
        shell=False,
    )
    assert proc.returncode == 0, (
        f"external icm store failed: rc={proc.returncode} stderr={proc.stderr!r}"
    )

    hits = cli_runner.run_recall(
        query=marker,
        limit=5,
        db_path=db_path,
        timeout_ms=10000,
        use_embeddings=False,
    )
    assert hits, f"plugin recall saw no hits for external write: {hits!r}"
    assert any(
        marker in (h.get("summary") or "") for h in hits
    ), f"no hit contained marker {marker!r}; hits={hits!r}"
