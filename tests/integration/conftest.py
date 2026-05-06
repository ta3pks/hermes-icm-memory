"""Shared fixtures for the S14 integration suite.

The :func:`no_embeddings_subprocess` fixture lifts a duplicated test-side
monkeypatch out of the three integration files. Production
:mod:`hermes_icm_memory.cli_runner` is unchanged — the embedding-model
download dodge is purely test-scoped argv injection.
"""

from __future__ import annotations

from typing import Any

import pytest

from hermes_icm_memory import cli_runner


@pytest.fixture
def no_embeddings_subprocess(monkeypatch: pytest.MonkeyPatch) -> None:
    """Inject ``--no-embeddings`` into every ``cli_runner._run`` invocation.

    The real ``icm`` CLI loads its embeddings model lazily on first
    embedding-using call; CI hosts do not have it cached and would either
    download or OOM. ``--no-embeddings`` is a top-level flag accepted by
    every subcommand the plugin uses, and turns ICM into pure keyword
    search — sufficient for the recall assertions in S14.
    """
    real_run = cli_runner._run

    def patched(argv: list[str], timeout_ms: int) -> Any:
        new_argv = list(argv)
        if new_argv and new_argv[0] == "icm" and "--no-embeddings" not in new_argv:
            new_argv.insert(1, "--no-embeddings")
        return real_run(new_argv, timeout_ms)

    monkeypatch.setattr(cli_runner, "_run", patched)
