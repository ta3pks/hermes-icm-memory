"""Trigger detection for ``sync_turn`` (FR14, FR16, AD-17, AD-20).

Pure heuristics — no I/O, no logging, no dependencies on cli_runner / provider.
The :data:`MAPPING` dict literal locks the FR16 matrix (category → topic, importance);
:func:`detect_triggers` is the single entry point that ``hooks.sync_turn`` (S08)
calls each turn to decide which ICM writes to enqueue.

A "trigger" is a 4-tuple ``(topic, importance, content, keywords)`` ready to be
shaped into a write task by the caller. Multiple triggers may fire from one turn
(e.g. an assistant message that both fixes a bug and records a decision); the
caller is responsible for deduplication if any.
"""

from __future__ import annotations

import re
from typing import Final

# (topic, importance, content, keywords)
Trigger = tuple[str, str, str, list[str]]

#: FR16 topic ↔ importance matrix. Frozen public surface (NFR-MAINT-1).
#: v0.4.2 — every project-scoped category uses ``{project}`` so writes land in
#: topics like ``errors-resolved-hermes-chat`` instead of a single overcrowded
#: ``errors-resolved`` bucket (matches the convention already established in
#: the user's ICM corpus: ``errors-resolved-hermes``, ``learnings-bmad``,
#: ``gotchas-pi-hole``, etc.). ``preferences`` intentionally stays unscoped
#: because the corpus treats it as one global bucket.
MAPPING: Final[dict[str, dict[str, str]]] = {
    "decisions": {"topic_template": "decisions-{project}", "importance": "high"},
    "errors-resolved": {"topic_template": "errors-resolved-{project}", "importance": "high"},
    "preferences": {"topic_template": "preferences", "importance": "critical"},
    "context": {"topic_template": "context-{project}", "importance": "high"},
    "learnings": {"topic_template": "learnings-{project}", "importance": "high"},
    "gotchas": {"topic_template": "gotchas-{project}", "importance": "high"},
}

#: Default scope when no project is inferred. Chosen over the previous
#: ``"default"`` so unscoped saves land in a meaningful, greppable bucket
#: (``errors-resolved-hermes-chat``) rather than ``errors-resolved-default``.
_DEFAULT_PROJECT: Final[str] = "hermes-chat"
_CONTENT_LIMIT: Final[int] = 500
_KEYWORDS_LIMIT: Final[int] = 5

# Compiled patterns. Word boundaries (\b) keep partial matches like "fixedly" out.
# All searched case-insensitively.
_ERRORS_RESOLVED_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(fixed|resolved|the bug was|root cause|fix(?:ed)? it|"
    r"the issue was|that explains|the problem was|caused by|"
    r"turns? out the|was due to|reason it (?:failed|broke|didn'?t))",
    re.IGNORECASE,
)
_DECISIONS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(decided to|going with|we'll use|let's use|chose to|"
    r"let'?s go with|we should use|we should go|"
    r"I'm going to use|the approach is)",
    re.IGNORECASE,
)
_PREFERENCES_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(always (?:use|do)|never (?:use|do)|prefer|"
    r"I (?:like|love|hate|can'?t stand) when|"
    r"my (?:favourite|favorite|preferred)|"
    r"don'?t (?:like|use|do)|won'?t use)",
    re.IGNORECASE,
)
_LEARNINGS_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(learned|turns out|TIL|now I understand|"
    r"interestingly|actually it (?:works|seems|turns)|"
    r"the key insight|what I didn'?t know|"
    r"it (?:turns|turned) out that)",
    re.IGNORECASE,
)

# Word tokenizer for keywords extraction. Lowercase a-z plus digits, length ≥ 3.
_WORD_TOKEN: Final[re.Pattern[str]] = re.compile(r"[a-z0-9]{3,}")

#: Stopwords stripped by :func:`extract_recall_query` before sending a query
#: to ICM. ICM's MCP recall ranker tanks badly on natural-language phrasing
#: (a query like "what's the state of hair iron" returns a single unrelated
#: ``preferences`` blob, while "hair iron" returns the relevant entries) —
#: this list lets the plugin pre-process the user's message into a
#: keyword-only form before recall. Intentionally short: only the highest-
#: frequency English function words. Project-named tokens (hair, iron,
#: nano, etc.) MUST NOT appear here.
_STOPWORDS: Final[frozenset[str]] = frozenset({
    # articles
    "the", "a", "an",
    # auxiliary / linking verbs
    "is", "are", "was", "were", "be", "been", "being",
    "am", "do", "does", "did", "doing", "done",
    "has", "have", "had", "having",
    "can", "could", "may", "might", "must", "shall", "should",
    "will", "would",
    # personal pronouns + common contractions
    "i", "me", "my", "mine", "myself",
    "we", "us", "our", "ours", "ourselves",
    "you", "your", "yours", "yourself", "yourselves",
    "he", "him", "his", "himself",
    "she", "her", "hers", "herself",
    "it", "its", "itself",
    "they", "them", "their", "theirs", "themselves",
    "ive", "youre", "youve", "hes", "shes", "weve", "theyre",
    "thats", "whats", "wheres", "hows", "lets",
    # question / determiners
    "what", "who", "whom", "whose", "which", "where", "when",
    "why", "how", "this", "that", "these", "those",
    # prepositions / conjunctions
    "of", "in", "on", "at", "by", "to", "for", "with", "from",
    "about", "into", "onto", "over", "under", "out", "off",
    "up", "down", "as", "than", "then", "but", "and", "or", "if",
    "so", "yet", "nor", "not", "any", "all", "some",
    # ultra-common filler
    "just", "now", "still", "also", "very", "too", "more", "much",
    "really", "okay", "ok", "well", "right",
})


def build_topic_keyword_map(topics: list[str]) -> dict[str, list[str]]:
    """Build a keyword → matching-topics index from icm topic names (v0.5.1).

    Used by :func:`infer_topic_from_query` to decide whether a user query
    overlaps a specific topic well enough to add ``-t <topic>`` to the
    ``icm recall`` invocation. The keywords come from the LIVE corpus
    (the actual topic names ``icm topics`` reports), so we never need a
    hard-coded wordlist that risks drifting from the operator's data.

    Splits each topic name on ``"-"`` and indexes every segment ≥
    ``min_token_len`` (3) characters. A topic like ``context-hair-iron``
    therefore contributes the keyword ``"hair"`` AND the keyword
    ``"iron"`` (the ``"context"`` segment is also indexed but is
    generic — scoring in :func:`infer_topic_from_query` deals with
    that). Topics with no ``"-"`` (e.g. bare ``"preferences"``) are
    skipped — they have no project handle to match on.

    Returns ``{keyword: [topic, ...]}``; a single keyword can map to
    multiple topics (``"hair"`` → both ``"context-hair-iron"`` and
    ``"learnings-hair-iron"``).
    """
    keyword_map: dict[str, list[str]] = {}
    for topic in topics:
        if not topic or "-" not in topic:
            continue
        for kw in topic.lower().split("-"):
            if len(kw) >= 3:
                keyword_map.setdefault(kw, []).append(topic)
    return keyword_map


def infer_topic_from_query(
    query: str, keyword_map: dict[str, list[str]],
) -> str | None:
    """Return the single best-matching topic for ``query``, or ``None``.

    Thin wrapper around :func:`infer_topic_and_keywords` kept for callers
    that only need the topic. New code should prefer the
    ``(topic, matched_keywords)`` tuple variant.
    """
    topic, _kw = infer_topic_and_keywords(query, keyword_map)
    return topic


def infer_topic_and_keywords(
    query: str, keyword_map: dict[str, list[str]],
) -> tuple[str | None, list[str]]:
    """Pick best-matching topic for ``query`` AND return the matched keywords.

    The keywords are the subset of query tokens that actually mapped onto
    the chosen topic's name. v0.5.3 uses this list to REPLACE the recall
    query (icm's natural-language ranking buries topic-tagged entries
    below noise; substituting the matched keywords as the query lets icm
    score the right entries highest before its ``-t <topic>`` filter
    applies).

    Scoring: count of query-tokens (alphanumeric runs ≥3 chars,
    case-insensitive) that appear as keywords in the topic's name.
    Highest score wins; alphabetical tie-break for determinism.

    Returns ``(None, [])`` when no token matches any indexed keyword,
    when ``query`` is empty, or when ``keyword_map`` is empty.
    """
    if not query or not keyword_map:
        return None, []
    tokens = set(_WORD_TOKEN.findall(query.lower()))
    # Score topics + remember which tokens scored against each.
    topic_scores: dict[str, int] = {}
    topic_keywords: dict[str, list[str]] = {}
    for tok in tokens:
        for topic in keyword_map.get(tok, ()):
            topic_scores[topic] = topic_scores.get(topic, 0) + 1
            topic_keywords.setdefault(topic, []).append(tok)
    if not topic_scores:
        return None, []
    best = min(topic_scores.items(), key=lambda kv: (-kv[1], kv[0]))[0]
    # Keep deterministic order for the recall query: alphabetical so a
    # given user query always produces the same recall string (cache-
    # friendly and reproducible in logs).
    return best, sorted(topic_keywords[best])


def extract_recall_query(text: str, *, min_token_len: int = 3) -> str:
    """Strip stopwords + short tokens from a natural-language message.

    ICM's MCP recall ranker behaves poorly on full-sentence queries; bare
    keywords give dramatically better hit ranking on keyword-only mode
    (which is what the plugin runs on Pi-class hardware per the operator's
    ``use_embeddings: false`` setting). This helper produces the
    keyword-only form.

    Behaviour:

    - Lowercases.
    - Splits on the same ``_WORD_TOKEN`` regex as
      :func:`_extract_keywords` (alphanumeric runs ≥ ``min_token_len``).
    - Drops tokens in :data:`_STOPWORDS`.
    - Joins remaining tokens with a single space.
    - **Falls back to the original** ``text`` (trimmed) when extraction
      yields an empty string — a fully-stopword query (e.g. ``"what is
      it"``) becomes nothing, and an empty recall query would return
      zero hits, which is strictly worse than the original behaviour.

    Pure heuristic — no I/O, no logging, no dependencies.
    """
    if not text:
        return text
    tokens = [
        t for t in _WORD_TOKEN.findall(text.lower())
        if len(t) >= min_token_len and t not in _STOPWORDS
    ]
    if not tokens:
        return text.strip()
    return " ".join(tokens)


def _resolve_topic(category: str, project: str | None) -> str:
    """Format the category's topic_template, substituting 'default' for None."""
    template = MAPPING[category]["topic_template"]
    return template.format(project=project or _DEFAULT_PROJECT)


def _extract_keywords(text: str) -> list[str]:
    """Lower-cased word tokens, deduped, first-seen order, capped at ``_KEYWORDS_LIMIT``."""
    seen: set[str] = set()
    out: list[str] = []
    for token in _WORD_TOKEN.findall(text.lower()):
        if token in seen:
            continue
        seen.add(token)
        out.append(token)
        if len(out) >= _KEYWORDS_LIMIT:
            break
    return out


def detect_triggers(
    user_text: str,
    assistant_text: str,
    project: str | None = None,
    turn_index: int = 0,
    every_n_turns: int = 20,
) -> list[Trigger]:
    """Detect store triggers in a single turn.

    :param user_text: User message that opened the turn.
    :param assistant_text: Assistant message that closed the turn.
    :param project: Project name for ``{project}``-templated topics. ``None`` →
        ``"default"`` (per AC8).
    :param turn_index: Per-session turn counter, owned by the provider.
    :param every_n_turns: Periodic-progress cadence (AD-20). Default 20.
    :returns: List of ``(topic, importance, content, keywords)`` tuples. Empty
        list when no pattern matches and no periodic boundary is hit. Never
        returns ``None``.

    Order of emitted tuples (deterministic for tests / FIFO writes):

    1. periodic context (if applicable)
    2. errors-resolved
    3. decisions
    4. learnings
    5. preferences
    """
    out: list[Trigger] = []

    # 1. Periodic context — fires every N turns, never on turn_index == 0.
    if every_n_turns > 0 and turn_index > 0 and turn_index % every_n_turns == 0:
        topic = _resolve_topic("context", project)
        content = f"periodic progress checkpoint: turn {turn_index}"
        out.append((topic, MAPPING["context"]["importance"], content, []))

    # 2. errors-resolved (assistant_text).
    if _ERRORS_RESOLVED_PATTERN.search(assistant_text):
        out.append(
            (
                _resolve_topic("errors-resolved", project),
                MAPPING["errors-resolved"]["importance"],
                assistant_text[:_CONTENT_LIMIT],
                _extract_keywords(assistant_text),
            )
        )

    # 3. decisions (assistant_text).
    if _DECISIONS_PATTERN.search(assistant_text):
        out.append(
            (
                _resolve_topic("decisions", project),
                MAPPING["decisions"]["importance"],
                assistant_text[:_CONTENT_LIMIT],
                _extract_keywords(assistant_text),
            )
        )

    # 4. learnings (assistant_text).
    if _LEARNINGS_PATTERN.search(assistant_text):
        out.append(
            (
                _resolve_topic("learnings", project),
                MAPPING["learnings"]["importance"],
                assistant_text[:_CONTENT_LIMIT],
                _extract_keywords(assistant_text),
            )
        )

    # 5. preferences (user_text only — see story spec rationale).
    if _PREFERENCES_PATTERN.search(user_text):
        out.append(
            (
                _resolve_topic("preferences", project),
                MAPPING["preferences"]["importance"],
                user_text[:_CONTENT_LIMIT],
                _extract_keywords(user_text),
            )
        )

    return out
