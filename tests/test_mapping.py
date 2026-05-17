"""Trigger detection mapping tests (S06).

Covers FR14 (five-trigger detection) + FR16 (topic ↔ importance matrix) +
AD-17 (data-driven MAPPING dict + pure detect_triggers function) +
AD-20 (periodic-progress every-N-turns).

The module under test is pure heuristics — no I/O, no logging, no subprocess.
Tests assert on tuple shape, returned categories, and edge-case boundaries.
"""

from __future__ import annotations

from hermes_icm_memory import mapping


def test_mapping_dict_has_expected_categories() -> None:
    """MAPPING locks the v0.4.2 category set — five original + gotchas."""
    assert set(mapping.MAPPING.keys()) == {
        "decisions",
        "errors-resolved",
        "preferences",
        "context",
        "learnings",
        "gotchas",
    }


def test_mapping_topic_and_importance_for_each_category() -> None:
    """Each category maps to the v0.4.2 (topic_template, importance) pair.

    v0.4.2: errors-resolved and learnings became project-scoped to match the
    existing ICM corpus convention (errors-resolved-hermes, learnings-bmad,
    etc.). preferences stays unscoped — the corpus treats it as one bucket.
    """
    expected: dict[str, tuple[str, str]] = {
        "decisions": ("decisions-{project}", "high"),
        "errors-resolved": ("errors-resolved-{project}", "high"),
        "preferences": ("preferences", "critical"),
        "context": ("context-{project}", "high"),
        "learnings": ("learnings-{project}", "high"),
        "gotchas": ("gotchas-{project}", "high"),
    }
    for category, (topic_template, importance) in expected.items():
        entry = mapping.MAPPING[category]
        got_template = entry["topic_template"]
        assert got_template == topic_template, (
            f"{category}: expected topic_template {topic_template!r}, got {got_template!r}"
        )
        assert entry["importance"] == importance, (
            f"{category}: expected importance {importance!r}, got {entry['importance']!r}"
        )


def test_detect_errors_resolved_pattern() -> None:
    """Assistant text with a fix-it phrase emits the errors-resolved tuple."""
    triggers = mapping.detect_triggers(
        user_text="why is the import failing?",
        assistant_text="Fixed the import error - root cause was the missing __init__.py.",
        project="hermes-icm-memory",
    )
    matched = [t for t in triggers if t[0].startswith("errors-resolved-")]
    assert len(matched) == 1, f"expected exactly one errors-resolved trigger, got {triggers!r}"
    topic, importance, content, keywords = matched[0]
    assert topic == "errors-resolved-hermes-icm-memory"
    assert importance == "high"
    assert isinstance(content, str) and content
    assert isinstance(keywords, list)
    assert all(isinstance(k, str) for k in keywords)


def test_detect_decisions_pattern() -> None:
    """Decision phrasing in assistant text emits decisions-<project> with high importance."""
    triggers = mapping.detect_triggers(
        user_text="what queue strategy should we use?",
        assistant_text="We decided to go with the bounded queue + drop-on-full strategy.",
        project="hermes-icm-memory",
    )
    matched = [t for t in triggers if t[0].startswith("decisions-")]
    assert len(matched) == 1, f"expected one decisions trigger, got {triggers!r}"
    topic, importance, content, keywords = matched[0]
    assert topic == "decisions-hermes-icm-memory"
    assert importance == "high"
    assert isinstance(content, str) and content
    assert isinstance(keywords, list)


def test_detect_preferences_critical() -> None:
    """User text with preference phrasing emits preferences with critical importance."""
    triggers = mapping.detect_triggers(
        user_text="I always use bun, never npm. Please prefer bun.",
        assistant_text="Got it.",
    )
    matched = [t for t in triggers if t[0] == "preferences"]
    assert len(matched) == 1, f"expected one preferences trigger, got {triggers!r}"
    topic, importance, content, keywords = matched[0]
    assert topic == "preferences"
    assert importance == "critical"
    assert "bun" in content.lower() or "always" in content.lower()


def test_detect_context_periodic() -> None:
    """turn_index == every_n_turns (and > 0) emits a periodic context tuple."""
    triggers = mapping.detect_triggers(
        user_text="ok",
        assistant_text="ok",
        project="hermes-icm-memory",
        turn_index=20,
        every_n_turns=20,
    )
    matched = [t for t in triggers if t[0].startswith("context-")]
    assert len(matched) == 1, f"expected one periodic context trigger, got {triggers!r}"
    topic, importance, _, _ = matched[0]
    assert topic == "context-hermes-icm-memory"
    assert importance == "high"

    # Boundary: turn_index == 0 must NOT fire periodic.
    zero = mapping.detect_triggers(
        user_text="ok",
        assistant_text="ok",
        project="hermes-icm-memory",
        turn_index=0,
        every_n_turns=20,
    )
    assert all(not t[0].startswith("context-") for t in zero)


def test_detect_no_match_returns_empty() -> None:
    """Neutral text + non-periodic turn_index → empty list (not None)."""
    triggers = mapping.detect_triggers(
        user_text="What time is it?",
        assistant_text="I don't have access to a clock.",
        project="hermes-icm-memory",
        turn_index=3,
        every_n_turns=20,
    )
    assert triggers == [], f"expected [] for neutral text, got {triggers!r}"


def test_detect_multiple_triggers_in_one_turn() -> None:
    """Independent triggers from one turn all fire (errors-resolved + decisions + learnings)."""
    triggers = mapping.detect_triggers(
        user_text="how did you fix it?",
        assistant_text=(
            "Fixed the bug. We decided to go with bun instead of npm. "
            "Turns out shell=False is the only safe subprocess form."
        ),
        project="hermes-icm-memory",
    )
    topics = {t[0] for t in triggers}
    assert "errors-resolved-hermes-icm-memory" in topics
    assert "decisions-hermes-icm-memory" in topics
    assert "learnings-hermes-icm-memory" in topics


def test_topic_template_with_default_project() -> None:
    """project=None substitutes the v0.4.2 default 'hermes-chat' into templates."""
    # Decisions trigger with project=None.
    decisions = mapping.detect_triggers(
        user_text="",
        assistant_text="We decided to use the bounded queue.",
        project=None,
    )
    decision_topics = [t[0] for t in decisions if t[0].startswith("decisions-")]
    assert decision_topics == ["decisions-hermes-chat"], (
        f"expected ['decisions-hermes-chat'], got {decision_topics!r}"
    )

    # Periodic context with project=None.
    context = mapping.detect_triggers(
        user_text="",
        assistant_text="",
        project=None,
        turn_index=20,
        every_n_turns=20,
    )
    context_topics = [t[0] for t in context if t[0].startswith("context-")]
    assert context_topics == ["context-hermes-chat"], (
        f"expected ['context-hermes-chat'], got {context_topics!r}"
    )

    # And the literal '{project}' must never appear in any emitted topic.
    for trigger in decisions + context:
        assert "{project}" not in trigger[0]


# ---------- v0.4.8: extract_recall_query (stopword stripping) ----------------


def test_extract_recall_query_strips_stopwords() -> None:
    """Natural-language question reduces to bare keywords."""
    out = mapping.extract_recall_query("what's the state of hair iron")
    # 'state', 'hair', 'iron' should survive; question words / 'of' should not.
    assert out == "state hair iron"


def test_extract_recall_query_keeps_keyword_only_input() -> None:
    """Already-keyword input is unchanged (idempotent on clean queries)."""
    out = mapping.extract_recall_query("hair iron project")
    assert out == "hair iron project"


def test_extract_recall_query_handles_punctuation_and_case() -> None:
    """Punctuation drops; case normalises to lower."""
    out = mapping.extract_recall_query("How's the Hair-Iron Project going?")
    # Hyphenated 'Hair-Iron' tokenises as hair, iron via the alnum regex.
    assert "hair" in out and "iron" in out and "project" in out
    assert "?" not in out and "how" not in out


def test_extract_recall_query_falls_back_when_all_stopwords() -> None:
    """A fully-stopword input must NOT collapse to '' (would zero recall)."""
    src = "what is it"
    out = mapping.extract_recall_query(src)
    assert out == "what is it", "must fall back to original when extraction empty"


def test_extract_recall_query_empty_input_returns_empty() -> None:
    """Empty input is preserved; not transformed into something weird."""
    assert mapping.extract_recall_query("") == ""


def test_extract_recall_query_drops_short_tokens() -> None:
    """Tokens shorter than min_token_len (default 3) are dropped."""
    out = mapping.extract_recall_query("io v0 ab hair iron")
    # 'io', 'v0', 'ab' are < 3 chars; 'hair' and 'iron' survive.
    assert out == "hair iron"


# ---------- v0.5.1: topic-aware recall helpers -------------------------------


def test_build_topic_keyword_map_indexes_hyphenated_topics() -> None:
    """Hyphenated topic names contribute one keyword per segment ≥3 chars."""
    m = mapping.build_topic_keyword_map([
        "context-hair-iron",
        "learnings-hair-iron",
        "decisions-moon-backend",
        "preferences",  # no '-' → skipped
    ])
    # Same keyword maps to all topics containing it.
    assert set(m["hair"]) == {"context-hair-iron", "learnings-hair-iron"}
    assert set(m["iron"]) == {"context-hair-iron", "learnings-hair-iron"}
    assert m["moon"] == ["decisions-moon-backend"]
    assert m["backend"] == ["decisions-moon-backend"]
    # Category prefix is also indexed — generic, but scoring filters it out.
    assert "context" in m
    assert "preferences" not in m  # no hyphen, not indexed


def test_build_topic_keyword_map_skips_short_segments() -> None:
    """Segments shorter than 3 chars don't get indexed (avoid 'a', 'in', etc.)."""
    m = mapping.build_topic_keyword_map(["a-b-cd-test"])
    assert "a" not in m and "b" not in m and "cd" not in m
    assert "test" in m


def test_build_topic_keyword_map_empty_input() -> None:
    """Empty topic list → empty map (no crash)."""
    assert mapping.build_topic_keyword_map([]) == {}


def test_infer_topic_picks_max_overlap_topic() -> None:
    """Query 'hair iron status' → context-hair-iron / learnings-hair-iron tie
    (both score 2 — 'hair' + 'iron'); tie-break alphabetical wins for
    context-hair-iron < learnings-hair-iron."""
    m = mapping.build_topic_keyword_map([
        "context-hair-iron",
        "learnings-hair-iron",
        "context-moon-backend",
    ])
    assert mapping.infer_topic_from_query("hair iron status", m) == "context-hair-iron"


def test_infer_topic_returns_none_when_no_match() -> None:
    """Query with no overlapping keyword → None (caller falls back to general recall)."""
    m = mapping.build_topic_keyword_map(["context-hair-iron"])
    assert mapping.infer_topic_from_query("what time is it", m) is None


def test_infer_topic_empty_map_returns_none() -> None:
    """Empty keyword map → None (no topics indexed)."""
    assert mapping.infer_topic_from_query("hair iron", {}) is None


def test_infer_topic_distinguishes_better_match() -> None:
    """When two topics share one keyword but only one shares both, that one wins."""
    m = mapping.build_topic_keyword_map([
        "context-hair-iron",  # 'hair' + 'iron'
        "context-iron",       # 'iron' only
    ])
    assert mapping.infer_topic_from_query("hair iron", m) == "context-hair-iron"


# ---------- v0.5.3: infer_topic_and_keywords -----------------------


def test_infer_topic_and_keywords_returns_matched_tokens() -> None:
    """v0.5.3 — returns BOTH the chosen topic AND the query tokens that
    explicitly mapped to it. Used to replace the recall query so icm
    scores the right entries highest (icm's natural-language ranker
    craters topic-tagged entries' scores below threshold otherwise)."""
    m = mapping.build_topic_keyword_map([
        "context-hair-iron",
        "context-moon-backend",
    ])
    topic, kws = mapping.infer_topic_and_keywords(
        "whats going on with hair iron", m,
    )
    assert topic == "context-hair-iron"
    assert kws == ["hair", "iron"]  # alphabetical for determinism


def test_infer_topic_and_keywords_no_match_returns_empty() -> None:
    """No topic overlap → (None, []) so caller can fall back to original query."""
    m = mapping.build_topic_keyword_map(["context-hair-iron"])
    assert mapping.infer_topic_and_keywords("what time is it", m) == (None, [])


def test_infer_topic_and_keywords_picks_matched_subset_only() -> None:
    """Only the topic's matched tokens are returned — not all topic keywords.

    'hair' only matches; 'iron' is part of the topic name but missing from
    query → not in the returned matched list. (Future: could expand to
    full topic keywords; v0.5.3 conservative — only what mapped.)
    """
    m = mapping.build_topic_keyword_map(["context-hair-iron"])
    topic, kws = mapping.infer_topic_and_keywords("hair stuff", m)
    assert topic == "context-hair-iron"
    assert kws == ["hair"]
