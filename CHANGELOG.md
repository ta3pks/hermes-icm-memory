# Changelog

All notable changes to this project are documented in this file.

The format is loosely based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project follows [Semantic Versioning](https://semver.org/).

## [0.5.7] — 2026-05-17

**UX — surface inferred topic in the recall half even when ``-t``
filter narrowed results to zero.**

### Symptom

Live turn on Telegram: query ``"check icm on fedora vs ours"`` →
prefetch correctly inferred topic ``context-fedora``, but ICM's
``-t context-fedora`` filter combined with the ``"fedora"`` recall
query returned zero entries (the topic's content doesn't have heavy
``fedora`` keyword overlap). Footer rendered as ``📚 — · 💾 —``
(pure heartbeat) — visually indistinguishable from a turn where
recall never even tried.

### Changed

- **``_render_indicator_footer`` now emits ``📚 0 <topic>`` when
  recall=0 AND a topic was inferred** (instead of collapsing to
  ``📚 —``). Distinguishes "tried + matched nothing" from
  "nothing ran". Bare ``📚 —`` still shown when no topic was
  inferred (legitimate heartbeat case).
- **``_render_indicator_directive`` applies the same logic** for
  the recall-half placeholder it tells the model to copy. Without
  this the directive would still say ``📚 —`` and the model would
  emit that, giving the hook a "well-formed" footer it trusts —
  bypassing the v0.5.7 fix.

### Verified

Interactive CLI in tmux: same ``check icm on fedora`` query →
prefetch log ``raw_hits=0 inferred_topic='context-fedora'`` →
footer in TUI now ``📚 0 context-fedora · 💾 —``.

## [0.5.6] — 2026-05-17

**Auto-save via LLM tool call + footer always shows both halves.**

### Why

The classifier-based auto-save (added in v0.4 series) was not firing
reliably — async LLM call to classify each turn was either being
skipped or returning unactionable results, so the ``💾`` half of the
footer was almost always empty. Per the operator's suggestion, shift
the save decision from an async classifier to the SAME LLM that's
already producing the response. It already has access to the
``mcp_icm_*`` tools, so it can call ``mcp_icm_icm_memory_store``
directly when an exchange is worth remembering and self-report what
it saved in the footer's ``💾`` half. No new plugin path needed —
piggybacks on the v0.5.5 per-turn directive injection.

### Changed

- **``_render_indicator_footer`` always emits BOTH halves** —
  ``📚 <recall> · 💾 <save>``. Heartbeat is now ``📚 — · 💾 —``
  (operator request: per-turn liveness for the save side too, not
  just recall). Single-half formats from v0.5.3 are gone.
- **``_render_indicator_directive`` extended with auto-save block.**
  Tells the LLM: decide if the exchange is worth remembering (list of
  triggers: preferences, errors, decisions, learnings, gotchas,
  context); if yes, call ``mcp_icm_icm_memory_store(topic, content,
  importance)`` then put the topic in the footer's ``💾`` slot; if
  no, emit ``💾 —``. Topic format: free-form kebab-case (LLM picks —
  operator chose 2026-05-17 not to constrain to a vocabulary that may
  not cover novel exchanges).
- **``_do_indicator_transform`` trusts well-formed model footers.**
  If the model emits a footer matching ``📚 … · 💾 …``, the hook
  returns ``None`` (leaves response unchanged) — the ``💾`` half is
  the model's authoritative save self-report and the plugin doesn't
  intercept the tool call to verify it. The hook still replaces stale
  half-footers and silent-turn omissions with the canonical
  plugin-state heartbeat. New ``_WELL_FORMED_FOOTER_RE`` matches the
  full ``📚 ... · 💾 ...`` shape.
- **v0.5.4 INFO log gains a ``model_complied=`` field** so operators
  can see at a glance whether the model followed the directive
  format this turn.

### Verified end-to-end

Interactive CLI in tmux: query ``"i decided to always use bun not
npm remember this"`` →
- Model called ``mcp_icm_icm_memory_store`` (visible in TUI tool-call
  bubble)
- Reply ended with ``📚 10 · 💾 preferences``
- ``icm recall`` confirms the new ``preferences`` entry is in the DB
  (ULID ``01KRV4YT0M...``)
- Hook log: ``model_complied=True``

## [0.5.5] — 2026-05-17

**Indicator footer now appears in streamed clients (TUI / Telegram) —
inject the directive into the per-turn prefetch return so the model
copies the right footer BEFORE streaming starts.**

### Symptom

After v0.5.3 the ``transform_llm_output`` hook was firing correctly
with the right per-session snapshot (verified via v0.5.4 INFO log),
yet the user-visible footer in interactive TUI and Telegram still
showed ``📚 —``. The one-shot path (``hermes -z PROMPT``, which
prints ``final_response`` after the hook runs) showed the correct
``📚 N <topic>``.

### Root cause

The transform hook fires AFTER the model's response has streamed to
the user. Streaming clients (TUI, Telegram) render text live; once a
chunk is on screen the hook's later re-rewrite of ``final_response``
never re-renders. The model was copying a stale ``📚 —`` from the
indicator directive — because that directive was rendered inside
``system_prompt_block``, which is built ONCE per session and cached
(``conversation_loop.py`` reuses ``stored_prompt`` on every turn).
At session-start time, prefetch hadn't fired yet → directive had
``recall_count=0`` → stale heartbeat for the entire session.

### Fixed

- **``provider.prefetch`` now appends the indicator directive (with
  THIS turn's recall_count + recall_topic) to its return value.**
  Hermes pipes ``prefetch()``'s return through ``_ext_prefetch_cache``
  into every turn's user message (``conversation_loop.py:687-695``),
  so the model sees a fresh directive each turn and copies the right
  footer into its (streamed) response.
- **``_render_indicator_directive`` accepts ``recall_topic=`` kwarg**
  so the directive's footer line includes the inferred topic when
  one matched (matches the format emitted by the
  ``transform_llm_output`` hook → no double-stamp if both fire).

### Behaviour change worth noting

- Two existing degrade tests (``test_prefetch_swallows_icm_errors_returns_empty``,
  ``test_mcp_client_empty_response_produces_empty_prefetch``) updated:
  ``prefetch()`` no longer returns ``""`` on no-hits / icm-error
  paths — it returns the heartbeat directive (so the per-turn
  injection still carries a fresh ``📚 —`` even when recall fails).
  Tests now assert the heartbeat is present, not that the string is
  empty.

## [0.5.4] — 2026-05-17

**Observability — INFO log on every transform_llm_output invocation.**

### Why

v0.5.3 hit a paradox: ``provider.prefetch`` log shows ``raw_hits=3``
+ correct topic, but the Telegram footer still shows ``📚 —``. Two
non-overlapping causes are observable from the OUTSIDE only as
"footer wrong":

1. ``transform_llm_output`` hook silently never fires → model's stale
   ``📚 —`` from the system_prompt_block fallback directive sticks.
2. The hook fires but Hermes passes a ``session_id`` that doesn't
   match the slot ``provider.prefetch`` wrote to → the hook reads an
   empty per-session slot and renders the heartbeat.

### Added

- **``_do_indicator_transform`` INFO log:**
  ``transform_llm_output: session_id='...' known_slots=N
  snapshot=(recall=N, topic='...', save='...')``. Fires on every
  invocation; absence in the log proves the hook isn't being called,
  presence with mismatched session_id confirms a key mismatch.

## [0.5.3] — 2026-05-17

**Two operator-driven fixes: matched-keyword recall + footer shows
inferred topic.**

### Why

v0.5.2's topic-filtered recall worked for clean keyword queries
(``hair iron``) but returned 0 hits for noisy natural-language ones
(``whats going on with hair iron``). Root cause: ICM scores all
entries against the full query FIRST, THEN applies the ``-t``
filter. Stopword-heavy queries pump up unrelated entries' scores and
push topic-tagged entries below threshold, so nothing makes it past
the filter. The fix isn't a generic stopword strip (rejected
upstream as too lossy) but a SURGICAL substitution that only kicks
in when the query already proves overlap with a real topic name in
the operator's corpus.

### Changed

- **``provider.prefetch`` now replaces the recall query with the
  matched keywords when a topic is inferred.** Example: query
  ``"whats going on with hair iron"`` infers topic
  ``context-hair-iron`` with matched keywords ``["hair", "iron"]``,
  so ``cli_runner.run_recall`` is invoked with
  ``query="hair iron" -t context-hair-iron`` — the topic-tagged
  entries score highest, the filter keeps them, ranking is clean.
  When no topic is inferred, the original query passes through
  unchanged. Cache key uses the substituted ``recall_query`` so the
  same English question hits the same cache slot turn-over-turn.
- **Footer now shows the inferred topic next to ``📚``** (operator
  request). Format:
  - ``📚 N <topic>`` when recall matched a topic
  - ``📚 N`` when recall fired without a topic match
  - ``📚 N <topic> · 💾 <save>`` for both halves
  - ``📚 —`` heartbeat unchanged

### Added

- **``mapping.infer_topic_and_keywords(query, keyword_map)``** —
  returns ``(topic, matched_keywords)`` tuple. The keywords are the
  query tokens that explicitly mapped onto the chosen topic's name
  (alphabetised for determinism — same English question reproduces
  the same recall query on every turn).
  ``mapping.infer_topic_from_query`` becomes a thin wrapper.
- **``_INDICATOR_STATE`` per-session slot gains a ``recall_topic``
  field** plus the ``_capture_recall_count(..., topic=...)`` kwarg.
  ``_do_indicator_transform`` reads it; ``_render_indicator_footer``
  accepts an optional ``recall_topic`` keyword. All resets clear all
  three fields together.

Regression guards added to ``tests/test_mapping.py`` and
``tests/test_provider.py`` for both behaviours.

## [0.5.2] — 2026-05-17

**Bug fix — indicator footer cross-contaminated between concurrent
sessions; cosmetic fix to ``inferred_topic=`` log.**

### Symptom

After v0.5.1 deployed, recall worked perfectly for "wheres hair iron
left" (``inferred_topic='context-hair-iron'``, 3 hits all
``context-hair-iron``) — but the reply's footer showed ``📚 —``
instead of ``📚 3``. A separate session's system-note prefetch (10
unrelated hits) had fired right after the hair-iron prefetch and
overwritten the module-level state. By the time the hair-iron turn's
transform fired, the slot had been reset by the other session's
transform.

### Fixed

- **``provider._INDICATOR_STATE`` is now keyed by ``session_id``**
  (was a single shared dict). New ``_indicator_slot(session_id)``
  helper centralises the default shape so producers and the transform
  hook all access the same structure.
- ``_capture_recall_count``, ``_capture_save_topic``, and
  ``_do_indicator_transform`` all take ``session_id`` and read/write
  the matching slot.
- ``provider.prefetch`` passes ``self._session_id`` to the recall
  capture; ``provider.sync_turn`` extracts ``session_id`` from kwargs
  (with a fall-back to ``self._session_id``) and threads it through
  ``hooks.submit_triggers``.
- ``hooks.submit_triggers`` and ``hooks._submit_classify_task`` take
  ``session_id`` and pass it to ``_capture_save_topic``.
- ``classifier.ClassifyTask`` gains a ``session_id`` field so the
  background classifier worker can stamp the right slot when its
  classification eventually produces a save topic (it runs on a
  daemon thread and only sees data baked onto the task at enqueue
  time).

Regression guard: ``tests/test_provider.py::
test_indicator_state_per_session_no_contamination`` exercises the
exact two-session interleave that bit the live deployment.

### Changed

- **``provider.initialize`` strips ICM's ``": N memories"`` suffix
  from each topic name** before indexing them into the
  ``topic_keyword_map``. Pre-v0.5.2 the suffixed string flowed into
  the keyword index and ``-t <topic>`` filter; icm's substring match
  accepted it but the ``inferred_topic='context-hair-iron: 3
  memories'`` log line was ugly. Now it's clean and the ``-t`` filter
  uses an exact topic-name match.

## [0.5.1] — 2026-05-17

**Topic-aware recall — when the user's query overlaps a project topic
name, recall with ``-t <topic>`` to focus past the bad ranker.**

### Why

v0.5.0 switched recall to the CLI subprocess hoping that would fix
ICM's bad ranking on natural-language queries; live testing showed
the CLI ranker is no better than MCP for full sentences. v0.4.8's
stopword stripping helped but the operator rejected it as too lossy
(curated wordlist will always drop something that matters for some
query). This release derives recall keywords from the LIVE corpus —
the actual topic names ``icm topics`` reports — and uses them only as
a filter hint (``-t <topic>``), never to mutate the query itself.

### Added

- **``mapping.build_topic_keyword_map(topics)``** — pure helper that
  indexes every hyphenated topic name (e.g. ``context-hair-iron``
  contributes keywords ``hair`` + ``iron``) into a ``{keyword:
  [topic, ...]}`` map. Segments < 3 chars dropped. No magic wordlist —
  keywords come from the operator's own corpus.
- **``mapping.infer_topic_from_query(query, keyword_map)``** — picks
  the single topic with the highest keyword-overlap score against the
  query. Alphabetical tie-break for determinism. Returns ``None`` when
  no token in the query matches any indexed keyword.
- **``hooks.run_prefetch``** now accepts an optional ``topic`` kwarg
  threaded through to ``cli_runner.run_recall`` as ``-t <topic>``.
- **``provider.initialize``** fetches ``cli_runner.run_topics`` and
  builds the keyword map once per session. INFO log reports how many
  topics produced how many keywords. Falls back to an empty map on
  any topics-fetch failure (so prefetch keeps working).
- **``provider.prefetch``** runs ``infer_topic_from_query`` against the
  cached keyword map and threads the result to ``hooks.run_prefetch``.
  The v0.4.7 INFO log gains an ``inferred_topic=`` field so operators
  can see when topic-filtered recall fired.

### Operational notes

- **Topic index refreshes only at session boundary** (each new
  ``initialize`` call). Topics added mid-session won't be visible
  until next init.
- **Single topic per recall** — ICM's ``-t`` flag takes one topic at a
  time, and the inference picks the highest-overlap one. Queries that
  span multiple unrelated topics will only get one of them
  topic-filtered; the rest fall through general recall on the next
  turn or via the LLM's own ``mcp_icm_*`` tool calls.

## [0.5.0] — 2026-05-17

**Recall correctness — route ``run_recall`` through the ``icm`` CLI
subprocess instead of the warm MCP daemon (partial revert of v0.4
transport change).**

### Why

The v0.4.x → v0.4.8 investigation surfaced an ICM-side ranking bug:
the MCP-served recall path consistently ranks empty-topic memoir
entries and large consolidated ``context-nikos`` blobs ABOVE
topic-tagged memories (``context-hair-iron``, ``learnings-bmad``,
etc.). The CLI path on the same data ranks correctly — running
``icm --no-embeddings recall "hair iron"`` returns five
``context-hair-iron`` entries up top, while the plugin's MCP-based
recall returns 46 hits with all empty-topic memoirs above any
topic-tagged hair-iron memory. The plugin can't fix the upstream
ranker, but it can route around it. v0.4.8's stopword-stripping
workaround helped raw_hits jump from 1 → 17 but didn't address the
underlying ranking problem. v0.5.0 fixes it at the transport layer.

### Changed

- **``cli_runner.run_recall``** now spawns ``icm recall <query>
  --format json --limit N`` (plus ``--no-embeddings`` / ``-t topic`` /
  ``--db PATH`` when those apply) as a one-shot subprocess and parses
  the JSON output, instead of calling the MCP client's
  ``call_recall``. Speed cost vs MCP: ~150ms cold subprocess vs ~10ms
  warm MCP call. Correctness wins.
- **``cli_runner`` imports ``subprocess`` directly** for this one
  call. The existing ``tests/test_no_subprocess_outside_cli_runner.py``
  AST guard already allowed both ``cli_runner.py`` and
  ``mcp_client.py``; an additional pin
  (``test_cli_runner_subprocess_use_is_scoped_to_recall``) asserts
  there's exactly ONE ``subprocess.run(`` call in cli_runner so a
  future broadening of subprocess use back into store/topics/health
  is a deliberate decision, not accidental.
- **``provider.prefetch`` no longer pre-strips stopwords** from the
  query. The v0.4.8 stopword-stripping was a workaround for the
  MCP-recall ranker's misbehaviour on full-sentence input; the CLI
  ranker handles natural-language queries correctly. The
  ``mapping.extract_recall_query`` helper stays in the codebase
  (still useful for future opt-in use) but is no longer called.
- **``cli_runner.run_recall`` no longer requires the MCP daemon to
  be running.** Store / topics / health still use the warm MCP daemon
  (those paths aren't affected by the recall-ranking bug).

### Operational notes

- **Per-turn recall latency increases** by the icm-binary subprocess
  startup cost (~100-300ms on Pi 4, sub-100ms on Fedora). If recall
  cold-start ever becomes a bottleneck, a forked subprocess pool is
  the natural next optimisation — but it's not needed today.
- **No gateway restart needed for store/topics/health.** They keep
  using the existing warm daemon. Only recall semantics change.

## [0.4.8] — 2026-05-17

**Recall quality fix — strip stopwords from the prefetch query before
hitting ICM.**

### Why

The v0.4.7 INFO log made the failure mode unambiguous: natural-
language queries like ``"what's the state of hair iron"`` returned a
single unrelated ``preferences`` blob from ICM's MCP recall, while a
bare ``"hair iron"`` returned 46 hits (including the relevant
``context-hair-iron`` entries, although still poorly ranked). ICM's
MCP-served recall tanks badly on full-sentence input vs keyword-only
input. Until that's fixed upstream, the workaround belongs in the
plugin.

### Added

- **``mapping.extract_recall_query(text)``** — pure heuristic that
  lowercases, splits on the existing alnum-token regex, drops a
  curated set of high-frequency English stopwords + sub-3-char
  tokens, and joins the remainder with single spaces. Falls back to
  the original ``text`` (trimmed) when extraction would yield an empty
  string (so a fully-stopword query like ``"what is it"`` doesn't
  collapse to zero hits).
- **``mapping._STOPWORDS``** — short, deliberately conservative set
  (articles, auxiliary verbs, pronouns + contractions, question
  determiners, prepositions, ultra-common filler). Project-named
  tokens (``hair``, ``iron``, ``nano``, etc.) MUST NOT appear here —
  the comment in the module is explicit.

### Changed

- **``provider.prefetch`` now pre-processes the incoming query**
  through ``extract_recall_query`` before passing it to
  ``hooks.run_prefetch``. The new ``v0.4.7`` INFO log shows BOTH the
  original message and the stripped form so operators can spot a
  ``recall_query=''``-style misfire if the stopword list ever
  over-strips.
- **Cache key changed** from ``hash(query)`` to ``hash(recall_query)``
  so the in-memory prefetch cache and ``_latest_prefetch_key`` line
  up with what ``hooks.run_prefetch`` actually writes under.
- Three pre-existing ``tests/test_hooks.py`` tests updated to expect
  the post-strip query (``"how do I bun?"`` → ``"bun"``, etc.) — same
  behaviour, just observable under the new strip.

## [0.4.7] — 2026-05-17

**Observability — INFO log on every prefetch showing query + hit count
+ top topics.**

### Why

Operator hit a case where the indicator footer showed ``📚 —`` even
though the model clearly used hair-iron memories in its reply (turned
out the model fell back to calling the ``mcp_icm_*`` LLM tools
directly, which don't flow through plugin state). Diagnosis required
re-running the plugin's recall path manually and comparing against
direct ``icm`` CLI behaviour — discovered that ICM's MCP-served
recall path ranks results VERY differently from the CLI path on
keyword-only mode (CLI returns hair-iron entries; MCP returns
``context-nikos`` blob + empty-topic memoir entries first).

The new log surfaces the actual query Hermes passes to ``prefetch``
plus what ICM returned, so future "why didn't recall fire?" cases
don't require manual replay.

### Added

- **`provider.prefetch` INFO log:**
  ``prefetch: query='<truncated>' raw_hits=N capped=K top_topics=[t1, t2, t3]``.
  Emitted after every recall. ``raw_hits`` is what ICM returned;
  ``capped`` is what fed the indicator (after the ``recall_limit``
  truncation). ``top_topics`` exposes ranking so operators can spot
  ICM-side ranker drift.

## [0.4.6] — 2026-05-17

**Bug fix — double indicator footer (stale ``📚 —`` heartbeat + fresh
``📚 N`` both ended up in the reply).**

### Symptom

Telegram replies were showing two ``📚 …`` lines stacked at the bottom:

    📚 —

    📚 1

### Root cause

Hook fire order. ``system_prompt_block`` renders its directive AT
START of turn — at which point ``_INDICATOR_STATE['recall_count']`` is
still 0 from the previous turn's reset, so the directive instructed
the LLM to copy the heartbeat ``📚 —``. THEN prefetch fired and
populated ``recall_count = 1``. At the end of the turn,
``transform_llm_output`` rendered the fresh ``📚 1`` footer and tried
to de-dupe with an exact-match check — but ``📚 —`` ≠ ``📚 1``, so it
didn't skip; it appended its own footer. Two indicators in the reply.

### Fixed

- **Hook now strips any trailing ``📚 …`` line and re-appends a fresh
  one,** making the hook the single source of truth for the footer.
  Whether the model copied a stale heartbeat, an exact match, or
  nothing at all, the output ends up with exactly one
  freshly-rendered footer.
- The ``system_prompt_block`` directive remains as a fallback for code
  paths the hook can't reach (e.g. streamed partials) — when the hook
  IS wired, anything the directive made the model produce gets
  collapsed into the canonical footer.

Regression guards in ``tests/test_provider.py``:
- ``test_indicator_transform_strips_stale_footer_and_replaces``
- ``test_indicator_transform_strips_exact_match_too``

## [0.4.5] — 2026-05-17

**Bug fix — operator config under `plugins.hermes-icm-memory.*` in
`config.yaml` was silently ignored.**

### Symptom

Pi operators set `plugins.hermes-icm-memory.use_embeddings: false` in
`config.yaml` (per the documented "never embed on Pi" rule) but the
plugin still spawned `icm serve` without `--no-embeddings`, and the
warm daemon was reported with `use_embeddings=True` in the v0.4.4
`mcp_start` INFO log. Recall quality suffered as a consequence (the
multilingual-e5-base ONNX model isn't realistic on a Pi 4).

### Root cause

The plugin's `IcmMemoryProvider.__init__` initialised `self._config`
as `{}` and only ever populated it via `save_config()` — but Hermes
never auto-calls `save_config` on plugin load. So every `_config_bool`
lookup fell through to the schema default. The
`plugins.hermes-icm-memory.*` section of `config.yaml` was read
nowhere.

### Fixed

- **`IcmMemoryProvider._load_hermes_plugin_config()`** — new helper
  that reads `plugins.<manifest-name>.*` from the parsed Hermes
  `config.yaml`, filters to keys present in the schema (forward-compat
  / typo guard), and merges into `self._config`. Called from
  `initialize()` immediately after `_read_hermes_config()`.
- INFO log on load reports which keys were merged so operators can
  confirm their settings took effect:
  `config: loaded 4 key(s) from plugins.hermes-icm-memory.* in hermes
  config.yaml: ['classifier_enabled', 'isolated', 'recall_limit',
  'use_embeddings']`.

Regression guards in `tests/test_provider.py`:
- `test_initialize_loads_plugin_config_from_hermes_yaml`
- `test_load_plugin_config_no_op_on_missing_section`

## [0.4.4] — 2026-05-17

**Bug fix — recall stopped working mid-session after a sub-agent
shutdown killed the shared MCP daemon.**

### Symptom

After v0.4.3 deployed and the gateway restarted, the indicator footer
showed up reliably but the `📚 N` half stayed at `📚 —` (heartbeat) on
every turn. Logs revealed the cause:
`prefetch: recall failed; returning empty:
ICMConnectionError('MCP client not started — call mcp_start first')`.
The icm-serve daemon spawned at first init was no longer running, and
re-init was short-circuiting via `args_key` match without re-spawning.

### Root cause

The pre-v0.4.4 lifecycle:

1. Main agent's `provider.initialize()` called `cli_runner.mcp_start()`
   → spawned warm icm-serve daemon → set module-level `_client`.
2. A short-lived sub-agent (Hermes' `review_agent` flow) loaded its own
   `IcmMemoryProvider` instance, did its work, then called
   `provider.shutdown()` → `cli_runner.mcp_stop()` → killed the
   subprocess and nulled the shared `_client`.
3. Next main-agent turn called `memory_manager.initialize_all` again →
   `provider.initialize` hit the `args_key` idempotent early-return →
   `mcp_start` was NOT re-called → `_client` stayed `None`.
4. Every subsequent `prefetch` raised `ICMConnectionError`.

### Fixed

- **`IcmMemoryProvider.shutdown` no longer calls `cli_runner.mcp_stop`.**
  The warm daemon is a process-wide singleton; per-instance shutdown
  must not kill it. Cleanup moved to an `atexit` hook registered in
  `cli_runner` so the daemon is closed deterministically on process
  exit. Regression guard:
  `tests/test_provider.py::test_shutdown_does_not_kill_shared_mcp_daemon`.
- **`IcmMemoryProvider.initialize` now always re-calls `cli_runner.
  mcp_start`,** even on the idempotent `args_key` re-init path.
  `mcp_start` is itself idempotent (no-op when `_client` is set) so
  this is cheap on the happy path but re-spawns the daemon if anything
  nulled `_client` between turns. Regression guard:
  `tests/test_provider.py::test_initialize_idempotent_path_still_revalidates_mcp_client`.

### Added

- **`cli_runner.mcp_start` INFO log** on actual daemon spawn (with
  subprocess pid) — pre-v0.4.4 a silent `_client is None` was
  indistinguishable from "never tried"; the pid stamp closes that
  ambiguity for future post-mortems.
- **`cli_runner.mcp_stop` INFO log** on actual teardown (also with pid).
- **`atexit` hook** in `cli_runner` to ensure the daemon is closed on
  interpreter shutdown.

## [0.4.3] — 2026-05-17

**Programmatic indicator footer — append the `📚 N · 💾 topic` line via
`transform_llm_output` so it appears regardless of LLM compliance.**

### Why

v0.4.2 shipped the indicator as a directive inside `system_prompt_block`
asking the LLM to copy a footer verbatim. The smaller models in
rotation (e.g. `big-pickle` via opencode-zen) silently ignored it — the
user-visible footer never appeared. v0.4.3 makes the append
programmatic via the `transform_llm_output` plugin hook; the directive
remains as a fallback for code paths the hook can't reach.

### Added

- **`transform_llm_output` plugin hook.** Module-level
  `_do_indicator_transform(response_text, ...)` reads the captured
  per-turn state and appends the footer to the LLM's reply before
  Hermes ships it to the user. Detects when the LLM already complied
  with the fallback directive (response ends with the same footer) and
  skips to avoid double-appending.
- **Module-level `provider._INDICATOR_STATE` dict.** Source of truth for
  the per-turn recall count + last save topic. Module-level on purpose
  — under `kind=standalone` dual-load there are TWO `IcmMemoryProvider`
  instances in the gateway process (one owned by `memory_manager`, one
  by the general `PluginManager`), and the transform hook fires on the
  PluginManager one while prefetch/sync_turn fire on the memory_manager
  one. Module-level state bridges them.
- **Producer helpers `_capture_recall_count` / `_capture_save_topic`.**
  Called from `provider.prefetch` and `hooks.submit_triggers` /
  `hooks._classifier_worker` respectively.

### Changed

- **`plugin.yaml`: `kind: standalone` set explicitly + `transform_llm_output`
  added to the hooks list.** Without `kind: standalone` the manifest
  auto-coerces to `exclusive` (because the plugin registers a memory
  provider), which causes `hermes_cli.plugins.PluginManager` to skip
  loading the plugin entirely — and the memory-manager loading path
  uses a `_ProviderCollector` ctx whose `register_hook` is a no-op.
  Standalone unblocks `register_hook`.
- **`register(ctx)` made defensive.** Uses `hasattr` to detect which ctx
  surface is available, so the same function works for both loaders
  without raising on either:
  - `PluginContext` (general PluginManager) — has `register_hook`, no
    `register_memory_provider`.
  - `_ProviderCollector` (memory_manager) — has
    `register_memory_provider`, `register_hook` is a no-op.
- **`system_prompt_block` directive language strengthened.** Uppercased
  "MANDATORY OUTPUT FORMAT" framing with explicit non-negotiable wording.
  Kept as belt-and-suspenders fallback when the hook can't reach
  (streamed partials, etc.).

## [0.4.2] — 2026-05-17

**User-visible per-turn indicator + corpus-aligned scoped topics.**

### Added

- **`system_prompt_block` indicator footer (📚 N · 💾 topic).** The block
  now appends a directive telling the LLM to copy a literal liveness
  footer to the end of its reply, so the user sees evidence the memory
  plugin actually ran each turn. Heartbeat (`📚 —`) is emitted on
  silent turns so the indicator is never missing. Fed by a new
  `WorkerState.recent_recall_count` (set in `provider.prefetch`) and the
  existing `recent_stores` buffer (now populated by both the classifier
  worker and the regex `submit_triggers` path). No new Hermes hook
  registration needed — uses the existing `system_prompt_block` channel.
- **`gotchas-{project}` category** in `mapping.MAPPING` to match the
  corpus convention (`gotchas-pi-hole`, `gotchas-claude-code`, etc.).
  Classifier prompt also lists `gotchas` as a valid category.
- **`ClassifierResult.project` field** — the LLM is asked to suggest a
  project slug; the classifier worker passes it through
  `mapping._resolve_topic` so async-classified writes land in the same
  scoped buckets as regex-detected writes.

### Changed

- **`errors-resolved` and `learnings` topic templates are now
  project-scoped** (`errors-resolved-{project}`, `learnings-{project}`)
  to match the ICM corpus convention (`errors-resolved-hermes`,
  `learnings-bmad`, etc.) instead of dumping every cross-project write
  into one overcrowded bucket. `preferences` intentionally stays
  unscoped — the corpus treats it as one global bucket.
- **`mapping._DEFAULT_PROJECT`** changed from `"default"` to
  `"hermes-chat"` so unscoped saves land in a greppable bucket
  (`errors-resolved-hermes-chat`) rather than `errors-resolved-default`.
- **`hooks._submit_periodic_context`** now resolves the periodic-context
  topic via `mapping._resolve_topic` instead of duplicating the literal
  `"default"` fallback that drifted from `_DEFAULT_PROJECT`.

## [0.4.1] — 2026-05-16

**Bug fix — auto-store no longer silently dropped in default-shared mode.**

### Fixed

- **`provider._ensure_worker` no longer short-circuits when `_db_path is
  None`.** The v0.1.1 guard against a missing `_db_path` predated the v0.4
  MCP migration; once `initialize()` started spawning a warm `icm serve`
  daemon (which owns its own DB at startup) the guard turned into dead
  code that silently no-op'd every `sync_turn` for users on the
  recommended `isolated: false` config. The worker now spawns regardless
  of `_db_path`, and `cli_runner.run_store` routes writes through the
  daemon. Regression guard: `tests/test_hooks.py::
  test_sync_turn_enqueues_in_default_shared_mode`.

### Removed

- **README "Known limitations" caveat** about writes needing a concrete
  `_db_path` — invalidated by this fix.

## [0.3.1] — 2026-05-07

**License change — Apache-2.0 → BSD 3-Clause "New" or "Revised" License.**

### Changed

- **LICENSE** replaced with the canonical BSD 3-Clause text (filled with
  `Copyright (c) 2026, Nikos Efthias`). GitHub auto-detects as
  `BSD-3-Clause`.
- **README**, **CONTRIBUTING.md**, project metadata updated to reflect
  the new license. Badge, "Features" bullet, "License" section, and
  contributor agreement all point at BSD 3-Clause.

### Removed

- **NOTICE file** — Apache-2.0–specific convention; not used by BSD 3-Clause.

### Migration

This is a license change, not a code change — no behavioural impact.
Downstream users who pinned hermes-icm-memory under Apache-2.0 should
review whether BSD 3-Clause is acceptable for their project. The two
licenses are similar in spirit (permissive, attribution-required), but
BSD 3-Clause adds the no-endorsement clause and drops the Apache patent
grant — consult counsel if patent posture matters to your use case.

## [0.3.0] — 2026-05-07

**Architecture pivot — hermes-native MCP for tools, lifecycle-only plugin.**

Hermes-Agent v0.3.0 (March 2026) shipped first-class `mcp_servers.<name>:` config. This release deletes the plugin's duplicate `transport: mcp` machinery so hermes is the single source of truth for `icm` tool exposure, and the plugin keeps only what it alone can do: auto-injection of recalled memories on prompt-submit (`prefetch()` → `system_prompt_block()`) and auto-store on triggered turns (`sync_turn()`).

Net diff: **−2484 lines** of code. Auto-injection contract preserved bit-for-bit. The LLM now sees ~30 native `icm_memory_*` / `icm_memoir_*` / `icm_feedback_*` / `icm_transcript_*` / `icm_learn` / `icm_consolidate` tools instead of the plugin's previous 4-tool wrapper surface.

### Removed

- **`transport` config key** — the v0.2 enum (`cli` / `mcp`) is gone.
  v0.2-era configs that still carry `transport: ...` validate as a pass-
  through unknown key (forward-compat); the runtime ignores it.
- **MCP transport in `cli_runner`** — `mcp_start`, `mcp_stop`, `_McpDaemon`,
  `_mcp_call`, `_mcp_recall` / `_mcp_store` / `_mcp_topics` / `_mcp_health`
  and the JSON-RPC plumbing (`_MCP_PROTOCOL_VERSION`, `_MCP_TOOL_*`,
  `_MCP_MAX_RESPONSE_LINES`, the lifecycle lock, the `atexit` backstop)
  are deleted. `cli_runner` now only uses `subprocess.run` for one-shot
  CLI invocations (no `subprocess.Popen`). The `transport` kwarg on
  `run_recall` / `run_store` / `run_topics` / `run_health` is removed.
- **LLM-tool surface (`tools.py`)** — `IcmMemoryProvider.handle_tool_call`
  and `IcmMemoryProvider.get_tool_schemas` are removed; the entire
  `hermes_icm_memory/tools.py` module is deleted along with
  `tests/test_tools.py` and `tests/test_cli_runner_mcp.py`. Tool exposure
  to the LLM is now hermes-native via `mcp_servers.icm:` (auto-discovers
  `icm_memory_recall`, `icm_memory_store`, `icm_memory_list_topics`,
  `icm_memory_health`).
- **`hooks.WorkerState.transport` field** — single CLI write path; no
  branch in `worker_loop` / `ensure_worker` / `run_prefetch`.
- **`provider.initialize` MCP startup branch** and **`provider.on_session_end`
  `cli_runner.mcp_stop()` call** are gone.

### Added

- **`IcmMemoryProvider.shutdown()`** — Hermes lifecycle hook (no-op in
  v0.3, no daemon to manage). Defined explicitly so hermes-agent's
  `memory_manager` no longer logs
  `'IcmMemoryProvider' object has no attribute 'shutdown'` on every
  gateway restart.
- **Inline `%r` in WARNING log messages.** Every public boundary that
  catches and degrades (`hooks.run_prefetch`, `hooks.submit_triggers`,
  `hooks.worker_loop`, `provider.prefetch`, `provider.sync_turn`,
  `provider.on_session_end`, `provider.shutdown`,
  `provider.initialize`, `provider.save_config`, `provider.is_available`)
  now includes the exception text in the format string itself (e.g.
  `"prefetch failed: %r"` with `exc` as positional arg) **in addition to**
  the existing `extra={"err": repr(exc), ...}`. The default Python
  logging formatter does not render `extra={...}`, which made silent-
  degrade incidents undiagnosable in the field. AD-13's structured logs
  stay (for operators using JSON log formatters), but the human-readable
  exception text is now also present.
- **New invariant tests** —
  `tests/test_no_tool_surface.py` pins that the provider has no
  `handle_tool_call` / `get_tool_schemas` and `tools.py` is deleted from
  the package; `tests/test_cli_only_transport.py` pins that none of the
  `run_*` helpers carries a `transport=` kwarg, no `mcp_*` symbols
  remain, and `subprocess.Popen` is absent from the source.

### Changed

- **`config.get_default_schema()` returns twelve entries** (down from 13);
  the v0.2 `transport` enum is removed (AC2).
- **`hooks.run_prefetch` always uses CLI subprocess.** With
  `use_embeddings: false` (the recommended Pi-class setting for the
  prefetch hot-path) each call is < 100 ms — fine for the prompt-prepend
  hot path. Semantic recall on demand is delivered by hermes-native
  `mcp_servers.icm:` when the LLM calls `icm_memory_recall`.

### Migration from v0.2

1. **Remove `transport` from `plugins.hermes-icm-memory:`** in
   `~/.hermes/config.yaml` (it's now ignored; passes through as an
   unknown key, no error).

2. **Add `mcp_servers.icm:`** to `~/.hermes/config.yaml` if not already
   present:

   ```yaml
   mcp_servers:
     icm:
       command: icm
       args: [serve, --no-embeddings]   # or omit --no-embeddings if your hardware has fast model load
       timeout: 120
       connect_timeout: 30
   ```

   Hermes auto-discovers `icm_memory_recall` / `icm_memory_store` /
   `icm_memory_list_topics` / `icm_memory_health` and registers them
   alongside built-ins.

3. **Restart hermes-gateway.** The LLM now uses the hermes-native
   `icm_memory_*` tools (prefixed with `icm_memory_` per hermes
   convention). Auto-injection on prompt-submit continues unchanged via
   the plugin's lifecycle hooks (`prefetch` → `system_prompt_block`).

### Limitations / Out of scope

- **Plugin-side writes still require a concrete `_db_path`.** Under the recommended `isolated: false` (shared DB) the worker no-ops and `sync_turn` writes are silently dropped — same v0.1.1 limitation. Set `isolated: true` to restore plugin writes today; the LLM can still write via `icm_memory_store` over hermes-native MCP. Concurrent-writer semantics against the canonical icm SQLite file is a v0.4 problem.
- **Honcho memory provider integration** (unrelated; hermes 0.3.0 ships its own).
- **Reusing hermes' MCP-managed daemon for plugin-side prefetch** (would couple the plugin to hermes internals; rejected — keyword-only CLI is fast enough on Pi).
- **Replacing the bounded-queue worker** with hermes' async write infrastructure (potential v0.4).

## [0.2.0] — 2026-05-06

`icm-serve` MCP transport — amortize the embedding-model load across calls.
Pi-class hosts can now run semantic recall: first call ~50 s (warmup),
every subsequent recall ~50 ms.

### Added

- New config key `transport` (enum, default `"cli"`, choices
  `["cli", "mcp"]`). `cli` keeps the v0.1.x fresh-subprocess path;
  `mcp` spawns one long-lived `icm serve` subprocess per provider
  lifetime and reuses it via JSON-RPC over stdin/stdout.
- New module-level helpers `cli_runner.mcp_start(db_path, use_embeddings)`
  and `cli_runner.mcp_stop()`. Provider's `initialize` calls
  `mcp_start` when `transport: mcp`; `on_session_end` always calls
  `mcp_stop` (no-op when transport is `cli`). An `atexit` hook is a
  belt-and-braces backstop so torn-down sessions never leak orphan
  `icm serve` processes.
- New integration test `tests/integration/test_real_icm_serve.py` —
  spawns a real `icm serve` daemon and asserts two consecutive recalls
  reuse the same subprocess (gated on `shutil.which("icm")`).
- `cli_runner.run_recall` / `run_topics` / `run_health` / `run_store`
  accept a `transport: str = "cli"` keyword. The MCP path internally
  dispatches to `_mcp_recall` / `_mcp_topics` / `_mcp_health` /
  `_mcp_store` (all private to `cli_runner.py`, AD-12 unchanged).

### Changed

- `provider.initialize` now branches on `_config_str("transport")`. If
  `"mcp"`, it spawns the daemon during initialize so the embedding-model
  warmup happens once at startup rather than on the first recall. On
  `mcp_start` failure the provider logs a WARNING and flips
  `_config["transport"]` to `"cli"` for the rest of the lifetime —
  graceful degrade-to-cli, never degrade-to-empty.
- `hooks.WorkerState` gained a `transport: str = "cli"` field captured at
  worker-spawn time so the daemon worker forwards the transport to
  `cli_runner.run_store`. Worker re-reads happen at spawn, not per-task,
  so a config edit mid-session won't race the worker.

### Failure-mode policy (MCP transport)

- **Daemon dies mid-call** → `cli_runner` logs a WARNING, respawns once
  with the cached args, retries the request.
- **Second consecutive death** → `_mcp_disabled` sentinel set, every
  subsequent `_mcp_*` call short-circuits to `ICMNotFoundError`. Upstream
  `tools._run_read` already catches `ICMError` and degrades to the
  documented empty-payload shape; `provider.prefetch` returns `""`.
- **`mcp_start` fails at initialize time** → provider falls back to
  `transport: cli` and continues operating. Operators see one WARNING
  per session; no exception escapes the provider boundary (AD-07
  invariant preserved).
- **JSON-RPC response never arrives** → `ICMTimeoutError` after the
  per-call `timeout_ms` budget elapses; same upstream degrade as a
  CLI-path timeout.

### Pi-friendly recipe

Add this to your Hermes memory-provider config to get fast semantic
recall on Pi-class hardware (4 GB Raspberry Pi 4 or similar):

```yaml
transport: mcp
use_embeddings: true
```

First recall: ~50 s (model cold-start). Every subsequent recall: <1 s.
Operators on desktop / cloud can leave both settings at default —
`transport: cli` + `use_embeddings: true` already gets them semantic
recall with no behavioural change from v0.1.1.

### Migration from v0.1.1

No action required. Default settings (`transport: cli`,
`use_embeddings: true`, `isolated: false`) preserve v0.1.1 behaviour
bit-for-bit.

### Limitations / Out of scope

- `transport: mcp` works for both reads (recall / topics / health /
  prefetch) and writes (`icm_memory_store` over MCP). However, in the
  default-shared mode (`isolated: false`) the worker still no-ops
  because `_ensure_worker` short-circuits when `_db_path is None` —
  carry-over from v0.1.1's "shared-DB writes need a v0.3 review"
  position. Operators wanting MCP-mediated writes today should set
  `isolated: true`.
- Auto-detection of Pi-class hardware (to default to `mcp` there) is a
  v0.3 concern.
- Windows is unsupported. `icm serve` spawning + signal handling tested
  on Linux + macOS only.

## [0.1.1] — 2026-05-06

Pi-deployment fixes + restoration of the brief's "shared memory with editors"
value prop.

### Changed (default-flip — see Migration below)

- **DB sharing is now opt-out instead of opt-in.** `provider.initialize` no
  longer eagerly resolves `<hermes_home>/icm/<profile>.db`. By default
  (`isolated=false`) the plugin omits `--db` so the `icm` CLI uses its
  OS-canonical default DB — the same SQLite file Claude Code, Cursor,
  OpenCode, Codex CLI, etc. already share. Recovers the original brief's
  promise: "Shared memory with editors, not a parallel silo."
- **`icm recall` runs semantic search by default; Pi users opt out.**
  The new `use_embeddings` config key defaults to `true` (the Brief's
  value prop — semantic recall via the multilingual-e5-base ONNX model).
  Set to `false` to fall back to keyword-only recall. The Pi 4 deploy
  surfaced the trade-off: the ONNX model loads from scratch on every
  subprocess invocation (~50 s on a 4 GB Pi 4), which blows past the
  default 2000 ms read timeout. Pi-class operators should set
  `use_embeddings: false` in their hermes config until v0.2's
  `icm-serve` MCP transport amortizes the model load. Desktop / cloud
  hosts are fine with the default.

### Added

- New config key `isolated` (bool, default `false`). Set to `true` to
  restore the v0.1.0 silo behaviour (`<hermes_home>/icm/<profile>.db`
  per-profile DB path, `--db` forwarded, profile isolation enforced).
- New config key `use_embeddings` (bool, default `true`). Set to `false`
  on Pi-class hardware (or any host that can't sustain the ONNX cold
  start inside `command_timeout_read_ms`) to fall back to keyword-only
  recall.
- `cli_runner.run_recall` accepts `use_embeddings: bool = True` kwarg
  (keyword-only) and conditionally appends `--no-embeddings` when
  ``False``.
- Default-shared mode flows `db_path=None` end-to-end: `cli_runner` omits
  `--db`, `hooks.run_prefetch` and `hooks.worker_loop` accept
  `Path | None`, and `tools._run_read` passes the same `None` through to
  `cli_runner`.
- `tests/conftest.py` ships an `isolated_provider` fixture for tests that
  need a concrete `_db_path` (write-path coverage, profile-isolation tests).

### Fixed

- `tools._run_read`'s "provider not initialized" guard now keys off
  `_init_args` instead of `_db_path`. Default-shared mode legitimately has
  `_db_path is None` after a successful `initialize`; the previous guard
  short-circuited every read tool to the empty-payload degrade shape.
- `provider.prefetch` no longer rejects `_db_path is None`. The
  `or self._db_path is None` clause was the read-path counterpart of the
  same regression and is removed.
- `hooks.{run_prefetch, worker_loop, ensure_worker, _spawn_worker}` and
  `cli_runner.{run_recall, run_topics}` typings allow `Path | None` so
  `mypy --strict` passes on the full `hermes_icm_memory tests` scope.

### Limitations / Out of Scope

- Default-shared mode supports **reads** (recall / topics / health /
  prefetch / system_prompt_block) end-to-end. **Writes** (sync_turn →
  bounded queue → worker) still require a concrete `_db_path`; in
  default-shared mode `_ensure_worker` short-circuits and writes silently
  no-op. Operators who need writes today must set `isolated: true`.
  Shared-DB writes against the canonical SQLite file (concurrent-writer
  semantics, schema-version coordination with Claude Code et al.) are a
  v0.2 concern.

### Migration from v0.1.0

If you relied on the v0.1.0 default behaviour (per-profile parallel silo
under `<hermes_home>/icm/<profile>.db`), set `isolated: true` in your
Hermes memory-provider config to restore it:

```yaml
# Restores v0.1.0 silo behaviour
isolated: true
```

If you're on Pi-class hardware (4 GB Raspberry Pi 4 or similar where the
ONNX model load blows past `command_timeout_read_ms`), additionally set:

```yaml
# Pi-class escape hatch — keyword-only recall
use_embeddings: false
```

Desktop / cloud hosts are fine with the default `use_embeddings: true`
and gain the Brief's semantic-recall value prop out of the box.

## [0.1.0] — 2026-05-05

Initial release. 14-story BMAD sprint (S01–S14) shipping a
`MemoryProvider` plugin for Hermes Agent backed by the local `icm` CLI.
Provides `prefetch` / `system_prompt_block` / `sync_turn` /
`on_session_end` hooks, four LLM-facing tools (`icm_recall`, `icm_store`,
`icm_topics`, `icm_health`), bounded-queue daemon writer, profile
isolation, full failure-mode degrade matrix, and integration tests against
a real `icm` binary.
