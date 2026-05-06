"""Profile-isolation tests (S12 / Story 5.2).

Locks the FR2 / NFR-SEC-2 / SM5 contract that two Hermes profiles produce two
distinct ICM databases and never read each other's data. As of v0.1.1, profile
isolation is **opt-in** via the ``isolated`` config key — the plugin's default
behaviour is to share the canonical icm DB with editors (Brief §"Shared memory
with editors"). Every test here therefore enables ``isolated=True`` before
calling ``initialize``; this is the configuration under which profile isolation
is meaningful in v0.1.1+.

Four tests:

1. ``test_two_hermes_homes_two_dbs`` — distinct ``hermes_home`` values resolve to
   distinct ``_db_path``s, each contained inside its own ``hermes_home``.
2. ``test_two_profiles_one_hermes_home_two_dbs`` — distinct profile names under
   one shared ``hermes_home`` resolve to ``<hh>/icm/work.db`` and
   ``<hh>/icm/personal.db``.
3. ``test_no_cross_profile_recall_leak`` — integration; gated on a real ``icm``
   binary on ``PATH``. Writes through provider A's DB, recalls through provider
   B's DB, asserts zero hits. Uses ``--no-embeddings`` so no embedding model
   download occurs in CI.
4. ``test_db_path_inside_hermes_home_only`` — ``db_path.is_relative_to(...)``.
"""

from __future__ import annotations

import json
import shutil
import subprocess  # noqa: S404 — test-only; AD-12 allow-list applies to package source.
import uuid
from pathlib import Path

import pytest

from hermes_icm_memory.provider import IcmMemoryProvider


def _isolated_provider() -> IcmMemoryProvider:
    """Construct a provider with v0.1.1's opt-in ``isolated`` mode enabled.

    Profile isolation is the explicit non-default behaviour as of v0.1.1; every
    test in this module enables it before calling ``initialize`` so the
    isolated-DB path branch fires.
    """
    provider = IcmMemoryProvider()
    provider._config["isolated"] = True
    return provider


# ---------- AC1: two hermes_homes → two distinct dbs --------------------------


def test_two_hermes_homes_two_dbs(tmp_path: Path) -> None:
    """AC1 — distinct ``hermes_home`` ⇒ distinct ``_db_path``, each inside its own home."""
    home_a = tmp_path / "hh-A"
    home_a.mkdir()
    home_b = tmp_path / "hh-B"
    home_b.mkdir()

    provider_a = _isolated_provider()
    provider_a.initialize(session_id="s1", hermes_home=home_a, profile="default")

    provider_b = _isolated_provider()
    provider_b.initialize(session_id="s1", hermes_home=home_b, profile="default")

    assert provider_a._db_path is not None
    assert provider_b._db_path is not None
    assert provider_a._db_path != provider_b._db_path, (
        f"two hermes_homes must yield two distinct db paths; "
        f"got both = {provider_a._db_path!r}"
    )
    assert provider_a._db_path.is_relative_to(home_a.resolve())
    assert provider_b._db_path.is_relative_to(home_b.resolve())


# ---------- AC2: two profiles, one hermes_home → two distinct dbs -------------


def test_two_profiles_one_hermes_home_two_dbs(tmp_path: Path) -> None:
    """AC2 — distinct ``profile`` under one ``hermes_home`` ⇒ ``work.db`` + ``personal.db``."""
    home = tmp_path / "hermes_home"
    home.mkdir()

    provider_work = _isolated_provider()
    provider_work.initialize(session_id="s1", hermes_home=home, profile="work")

    provider_personal = _isolated_provider()
    provider_personal.initialize(session_id="s1", hermes_home=home, profile="personal")

    expected_work = home.resolve() / "icm" / "work.db"
    expected_personal = home.resolve() / "icm" / "personal.db"

    assert provider_work._db_path == expected_work
    assert provider_personal._db_path == expected_personal
    assert provider_work._db_path != provider_personal._db_path


# ---------- AC3: cross-profile recall does not leak (integration) -------------


def _icm_recall_hits(db_path: Path, query: str) -> list[dict[str, object]]:
    """Run ``icm recall`` with ``--no-embeddings`` and return the parsed hit list.

    icm 0.10.43 prints ``"No memories found."`` in plain text on zero hits even
    under ``--format json``; treat that sentinel as the empty-list shape.
    """
    proc = subprocess.run(  # noqa: S603 — argv list, shell=False.
        [
            "icm",
            "--db",
            str(db_path),
            "recall",
            query,
            "--no-embeddings",
            "--limit",
            "5",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert proc.returncode == 0, (
        f"icm recall against {db_path} failed: rc={proc.returncode}, "
        f"stderr={proc.stderr!r}"
    )
    stdout = proc.stdout.strip()
    if not stdout or stdout == "No memories found.":
        return []
    parsed = json.loads(stdout)
    assert isinstance(parsed, list), f"icm recall returned non-list JSON: {parsed!r}"
    return parsed


@pytest.mark.skipif(
    shutil.which("icm") is None,
    reason="integration test: requires real `icm` binary on PATH",
)
def test_no_cross_profile_recall_leak(tmp_path: Path) -> None:
    """AC3 — write through A's DB, recall through B's DB ⇒ zero hits.

    Uses ``--no-embeddings`` so CI does not download the embedding model;
    keyword search is sufficient because the assertion is "zero hits", which
    is even more easily violated under keyword-only matching. A positive-control
    recall against A's own DB asserts ≥ 1 hit so a vacuously passing leak test
    (e.g. broken store, swallowed query) lights up rather than hides.
    """
    home = tmp_path / "hermes_home"
    home.mkdir()

    provider_a = _isolated_provider()
    provider_a.initialize(session_id="s1", hermes_home=home, profile="alpha")
    provider_b = _isolated_provider()
    provider_b.initialize(session_id="s1", hermes_home=home, profile="beta")

    assert provider_a._db_path is not None
    assert provider_b._db_path is not None

    # Unique token so even ambient ICM history (none here, fresh tmp_path) cannot pollute.
    token = f"profile-isolation-token-{uuid.uuid4().hex}"

    store_proc = subprocess.run(  # noqa: S603 — argv list, shell=False.
        [
            "icm",
            "--db",
            str(provider_a._db_path),
            "store",
            "--no-embeddings",
            "-t",
            "context-test",
            "-c",
            f"profile-isolation marker {token}",
            "-i",
            "low",
            "-k",
            token,
        ],
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )
    assert store_proc.returncode == 0, (
        f"icm store (profile A) failed: rc={store_proc.returncode}, "
        f"stderr={store_proc.stderr!r}"
    )

    # Positive control: recall against A's own DB must find the memory we just
    # wrote. If this fails, the test machinery itself is broken (bad argv,
    # store not committed, ...) and the leak assertion below would be vacuous.
    own_hits = _icm_recall_hits(provider_a._db_path, token)
    assert len(own_hits) >= 1, (
        f"positive control failed: profile A recalled {len(own_hits)} hit(s) for "
        f"the memory it just wrote (token={token!r}). Test machinery is broken; "
        f"the cross-profile assertion below would be vacuous."
    )

    # Leak assertion: profile B must see zero hits.
    leak_hits = _icm_recall_hits(provider_b._db_path, token)
    assert leak_hits == [], (
        f"cross-profile leakage: profile B saw {len(leak_hits)} hit(s) for profile A's "
        f"token {token!r}; expected 0. hits={leak_hits!r}"
    )


# ---------- AC4: every db_path is inside its hermes_home ----------------------


@pytest.mark.parametrize("profile", [None, "default", "work", "personal"])
def test_db_path_inside_hermes_home_only(tmp_path: Path, profile: str | None) -> None:
    """AC4 — ``_db_path.is_relative_to(Path(hermes_home).resolve())`` for every profile shape."""
    home = tmp_path / "hermes_home"
    home.mkdir()

    provider = _isolated_provider()
    provider.initialize(session_id="s1", hermes_home=home, profile=profile)

    assert provider._db_path is not None
    assert provider._db_path.is_relative_to(home.resolve()), (
        f"db_path {provider._db_path!r} escaped hermes_home {home.resolve()!r} "
        f"(profile={profile!r})"
    )
