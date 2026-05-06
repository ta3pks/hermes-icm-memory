"""Config schema + validation + path-resolution tests (S05).

Covers FR2 (per-profile DB path) + FR6 (config schema) + FR7 (non-raising
validation) + AD-05 (`<hermes_home>/icm/<profile>.db`) + AD-06 (idempotent
mkdir; no `icm init`) + AD-18 (validate never raises, returns `{"error": ...}`).

The module under test is pure — no subprocess, no logging, no network. Tests
exercise the four public functions plus invalid-input edges.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hermes_icm_memory import config

# Architecture §10.1 — the original ten frozen config keys plus the two v0.1.1
# additions (``isolated``, ``use_embeddings``) and the v0.2 ``transport`` enum.
_EXPECTED_KEYS: set[str] = {
    "default_importance",
    "topic_prefix",
    "recall_limit",
    "prefetch_enabled",
    "sync_write_queue_size",
    "command_timeout_read_ms",
    "command_timeout_write_ms",
    "session_end_grace_ms",
    "periodic_progress_every_n_turns",
    "consolidate_on_session_end",
    # v0.1.1 additions:
    "isolated",
    "use_embeddings",
    # v0.2 addition:
    "transport",
}


def test_default_schema_has_thirteen_keys() -> None:
    """Schema covers exactly the thirteen config keys with required fields.

    Ten architecture §10.1 keys + two v0.1.1 additions
    (``isolated``, ``use_embeddings``) + one v0.2 addition (``transport``).
    """
    schema = config.get_default_schema()
    assert isinstance(schema, list)
    assert len(schema) == 13
    keys = {entry["key"] for entry in schema}
    assert keys == _EXPECTED_KEYS, f"unexpected schema keys: {keys ^ _EXPECTED_KEYS}"
    for entry in schema:
        # Required fields per AC1.
        for required_field in ("key", "description", "secret", "required", "type", "default"):
            assert required_field in entry, f"{entry['key']}: missing field {required_field!r}"
        assert entry["secret"] is False
        assert isinstance(entry["required"], bool)
        assert entry["type"] in {"int", "bool", "string", "enum"}
        # `choices` only on enum entries.
        if entry["type"] == "enum":
            assert "choices" in entry
            assert isinstance(entry["choices"], list)
            assert entry["choices"], f"{entry['key']}: enum with empty choices list"
        else:
            assert "choices" not in entry, (
                f"{entry['key']}: non-enum entry must not carry 'choices'"
            )

    # Mutating the returned schema must not affect subsequent calls (defensive copy).
    schema[0]["default"] = "MUTATED"
    fresh = config.get_default_schema()
    assert fresh[0]["default"] != "MUTATED"


def test_validate_accepts_default_values() -> None:
    """All architecture-locked defaults pass validate."""
    defaults = {entry["key"]: entry["default"] for entry in config.get_default_schema()}
    ok, normalized = config.validate(defaults)
    assert ok is True, f"defaults rejected: {normalized!r}"
    # Normalized payload preserves every key/value (no drops).
    assert set(normalized.keys()) == _EXPECTED_KEYS
    assert normalized["default_importance"] == "high"
    assert normalized["recall_limit"] == 5
    assert normalized["prefetch_enabled"] is True


def test_validate_rejects_negative_queue_size() -> None:
    """Negative queue size returns (False, {'error': '...sync_write_queue_size...'})."""
    ok, payload = config.validate({"sync_write_queue_size": -1})
    assert ok is False
    assert "error" in payload
    assert "sync_write_queue_size" in payload["error"], (
        f"error must name the bad key: {payload['error']!r}"
    )


def test_validate_rejects_unknown_importance() -> None:
    """`default_importance='weak'` is not in the enum choices → rejected."""
    ok, payload = config.validate({"default_importance": "weak"})
    assert ok is False
    assert "default_importance" in payload["error"]


def test_validate_coerces_strings_to_ints() -> None:
    """`'5'` for an int-typed key normalizes to int 5; `'true'` for bool → True."""
    ok, normalized = config.validate(
        {
            "recall_limit": "5",
            "sync_write_queue_size": "128",
            "prefetch_enabled": "true",
            "consolidate_on_session_end": "FALSE",
        }
    )
    assert ok is True, f"coercion rejected: {normalized!r}"
    assert normalized["recall_limit"] == 5
    assert isinstance(normalized["recall_limit"], int)
    assert normalized["sync_write_queue_size"] == 128
    assert normalized["prefetch_enabled"] is True
    assert normalized["consolidate_on_session_end"] is False


@pytest.mark.parametrize(
    "garbage",
    [
        None,
        [1, 2, 3],
        "not-a-dict",
        42,
        {"recall_limit": object()},  # nested junk inside an otherwise-flat dict
        {"recall_limit": [1, 2]},
    ],
)
def test_validate_never_raises_on_garbage_input(garbage: object) -> None:
    """validate(garbage) returns (False, {'error': ...}) — never raises."""
    ok, payload = config.validate(garbage)
    assert ok is False
    assert isinstance(payload, dict)
    assert "error" in payload
    assert isinstance(payload["error"], str)
    assert payload["error"], "error message must be non-empty"


def test_validate_rejects_bool_for_int_key() -> None:
    """`True`/`False` must NOT be silently accepted as `1`/`0` for int-typed keys."""
    ok, payload = config.validate({"recall_limit": True})
    assert ok is False
    assert "recall_limit" in payload["error"]


def test_validate_rejects_unparseable_int_string() -> None:
    """`'abc'` for an int key fails coercion, returns the named-key error."""
    ok, payload = config.validate({"recall_limit": "abc"})
    assert ok is False
    assert "recall_limit" in payload["error"]


def test_validate_rejects_non_string_for_string_key() -> None:
    """Non-string for a string-typed key (e.g. integer for `topic_prefix`) is rejected."""
    ok, payload = config.validate({"topic_prefix": 42})
    assert ok is False
    assert "topic_prefix" in payload["error"]


def test_validate_rejects_arbitrary_string_for_bool_key() -> None:
    """Random strings like `'maybe'` are not bools — rejected with the named key."""
    ok, payload = config.validate({"prefetch_enabled": "maybe"})
    assert ok is False
    assert "prefetch_enabled" in payload["error"]


def test_validate_rejects_non_string_non_bool_for_bool_key() -> None:
    """Numbers / other non-{bool, str} types for bool-typed keys are rejected."""
    ok, payload = config.validate({"prefetch_enabled": 42})
    assert ok is False
    assert "prefetch_enabled" in payload["error"]


def test_validate_passes_through_unknown_keys() -> None:
    """Forward-compat: unknown keys are preserved in `normalized_values`."""
    ok, normalized = config.validate({"future_key_v2": "anything", "recall_limit": 7})
    assert ok is True
    assert normalized["future_key_v2"] == "anything"
    assert normalized["recall_limit"] == 7


def test_isolated_default_is_false() -> None:
    """v0.1.1 — ``isolated`` defaults to False (brief's shared-with-editors path)."""
    schema = {entry["key"]: entry for entry in config.get_default_schema()}
    assert schema["isolated"]["type"] == "bool"
    assert schema["isolated"]["default"] is False


def test_use_embeddings_default_is_true() -> None:
    """v0.1.1 — ``use_embeddings`` defaults to True (Brief's semantic-recall value prop).

    Pi-class operators opt out via ``use_embeddings: false`` in their hermes
    config. The schema default favours desktop / cloud hosts (the majority).
    """
    schema = {entry["key"]: entry for entry in config.get_default_schema()}
    assert schema["use_embeddings"]["type"] == "bool"
    assert schema["use_embeddings"]["default"] is True


def test_validate_accepts_isolated_true_and_false() -> None:
    """``isolated`` accepts true/false bool values (and string coercion)."""
    ok, normalized = config.validate({"isolated": True})
    assert ok and normalized["isolated"] is True
    ok, normalized = config.validate({"isolated": False})
    assert ok and normalized["isolated"] is False
    ok, normalized = config.validate({"isolated": "true"})
    assert ok and normalized["isolated"] is True
    ok, normalized = config.validate({"isolated": "FALSE"})
    assert ok and normalized["isolated"] is False


def test_validate_rejects_non_bool_for_isolated() -> None:
    """Numbers / random strings for ``isolated`` are rejected."""
    ok, payload = config.validate({"isolated": 42})
    assert ok is False
    assert "isolated" in payload["error"]
    ok, payload = config.validate({"isolated": "maybe"})
    assert ok is False
    assert "isolated" in payload["error"]


def test_validate_accepts_use_embeddings_true_and_false() -> None:
    """``use_embeddings`` accepts true/false bool values (and string coercion)."""
    ok, normalized = config.validate({"use_embeddings": True})
    assert ok and normalized["use_embeddings"] is True
    ok, normalized = config.validate({"use_embeddings": False})
    assert ok and normalized["use_embeddings"] is False
    ok, normalized = config.validate({"use_embeddings": "true"})
    assert ok and normalized["use_embeddings"] is True


def test_validate_rejects_non_bool_for_use_embeddings() -> None:
    """Numbers / random strings for ``use_embeddings`` are rejected."""
    ok, payload = config.validate({"use_embeddings": 1})
    assert ok is False
    assert "use_embeddings" in payload["error"]
    ok, payload = config.validate({"use_embeddings": "maybe"})
    assert ok is False
    assert "use_embeddings" in payload["error"]


def test_transport_default_is_cli() -> None:
    """v0.2 — ``transport`` defaults to ``cli`` (no behaviour change for v0.1.x users)."""
    schema = {entry["key"]: entry for entry in config.get_default_schema()}
    transport_entry = schema["transport"]
    assert transport_entry["type"] == "enum"
    assert transport_entry["default"] == "cli"
    assert set(transport_entry["choices"]) == {"cli", "mcp"}


def test_validate_accepts_transport_cli_and_mcp() -> None:
    """``transport`` accepts both enum values."""
    ok, normalized = config.validate({"transport": "cli"})
    assert ok and normalized["transport"] == "cli"
    ok, normalized = config.validate({"transport": "mcp"})
    assert ok and normalized["transport"] == "mcp"


def test_validate_rejects_unknown_transport() -> None:
    """``transport`` is case-sensitive; unknown names rejected with the named key."""
    for bogus in ("MCP", "Cli", "stdio", "", 42, None):
        ok, payload = config.validate({"transport": bogus})
        assert ok is False, f"expected rejection for transport={bogus!r}"
        assert "transport" in payload["error"]


def test_resolve_db_path_default_profile(tmp_path: Path) -> None:
    """profile=None → <hermes_home>/icm/default.db."""
    db = config.resolve_db_path(str(tmp_path))
    assert db == tmp_path / "icm" / "default.db"
    assert db.is_absolute()


def test_resolve_db_path_named_profile(tmp_path: Path) -> None:
    """profile='work' → <hermes_home>/icm/work.db."""
    db = config.resolve_db_path(tmp_path, profile="work")
    assert db == tmp_path / "icm" / "work.db"


def test_resolve_db_path_expands_tilde(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`~/foo` resolves against $HOME and accepts os.PathLike (AC7 + AC8)."""
    monkeypatch.setenv("HOME", str(tmp_path))
    db_str = config.resolve_db_path("~/foo")
    assert db_str == tmp_path / "foo" / "icm" / "default.db"

    # PathLike form (AC8).
    db_pathlike = config.resolve_db_path(Path("~/foo"))
    assert db_pathlike == tmp_path / "foo" / "icm" / "default.db"


def test_resolve_db_path_makes_parent_idempotent(tmp_path: Path) -> None:
    """mkdir_parent twice → no exception, parent dir exists, no observable diff."""
    db_path = tmp_path / "icm" / "default.db"
    assert not db_path.parent.exists()

    config.mkdir_parent(db_path)
    assert db_path.parent.is_dir()

    # Second call must not raise (parents=True, exist_ok=True).
    config.mkdir_parent(db_path)
    assert db_path.parent.is_dir()
