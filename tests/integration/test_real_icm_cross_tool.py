"""S14 — Integration: external ``icm`` writer + plugin recall reader (FR12).

Simulates a Claude Code (or any other ICM client) write by invoking
``subprocess.run(["icm", "--no-embeddings", "--db", <db>, "store", ...])``
directly against the same DB the provider is initialized over. Then asserts
the plugin's ``icm_recall`` tool path returns the externally-written memory.

Note on the ``subprocess`` import: S11's AST invariant test is scoped to
``hermes_icm_memory/`` source files only — test files are exempt by design.
The cross-tool scenario inherently requires simulating an external writer,
which means a direct subprocess call from the test side.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # external-write simulation; see module docstring.
from pathlib import Path
from typing import Any

import pytest

from hermes_icm_memory import cli_runner, config
from hermes_icm_memory.provider import IcmMemoryProvider

pytestmark = pytest.mark.skipif(
    shutil.which("icm") is None, reason="icm not on PATH"
)


@pytest.fixture
def no_embeddings_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Same ``--no-embeddings`` injection as ``test_real_icm_recall``."""
    real_run = cli_runner._run

    def patched(argv: list[str], timeout_ms: int) -> Any:
        new_argv = list(argv)
        if new_argv and new_argv[0] == "icm" and "--no-embeddings" not in new_argv:
            new_argv.insert(1, "--no-embeddings")
        return real_run(new_argv, timeout_ms)

    monkeypatch.setattr(cli_runner, "_run", patched)


def test_external_write_visible_to_plugin(
    tmp_path: Path,
    no_embeddings_subprocess: None,  # noqa: ARG001 — fixture is set-up only
) -> None:
    provider = IcmMemoryProvider()
    assert provider.is_available()
    provider.initialize("s14-cross-tool", str(tmp_path))

    # Resolve the same DB path the provider derived from `tmp_path` so the
    # external write lands in the exact file the plugin will recall against.
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

    recall_payload = json.loads(
        provider.handle_tool_call(
            "icm_recall",
            {"query": marker, "limit": 5},
        )
    )
    hits = recall_payload["hits"]
    assert isinstance(hits, list) and hits, (
        f"plugin recall saw no hits for external write: {recall_payload!r}"
    )
    assert any(
        marker in (h.get("summary") or "") for h in hits
    ), f"no hit contained marker {marker!r}; hits={hits!r}"
