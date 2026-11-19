# Voices of History — selected source integration

Upstream: https://github.com/tierrewwalessio-crypto/voices-of-history

Pinned revision: `3226478a81edfc376d63502fa9708c48d309b447`.

Copyright (c) 2026 Voices of History — HOI4 AI Mod contributors.

The complete upstream MIT license, copyright, permission notice and disclaimer
are retained in [LICENSE](LICENSE). File-level notices are retained in the
derived Python files. `source_manifest.json` records upstream file hashes and
AST hashes for the unchanged functions. No upstream assets or dependencies
are bundled by this integration.

## Actual source copied

From [`middleware/game_state.py`](https://github.com/tierrewwalessio-crypto/voices-of-history/blob/3226478a81edfc376d63502fa9708c48d309b447/middleware/game_state.py):

- `diplomatic_contact` — unchanged function body.
- `diplomatic_context` — unchanged function body.
- `_nation_line` — unchanged function body, required by `diplomatic_context`.

These functions live in `context.py`. Their original `re` dependency is from
Python's standard library. EYES maps its existing authenticated/filtered pair
facts to the upstream field names in `../voices_adapter.py`. Missing country
statistics and leaders remain unknown. The adapter uses campaign/player scope;
it does not assign historical personalities or make decisions.

## Source adapted

From [`middleware/strategist.py`](https://github.com/tierrewwalessio-crypto/voices-of-history/blob/3226478a81edfc376d63502fa9708c48d309b447/middleware/strategist.py):

- `Strategist.due` — daily cursor, reload/gap handling, baseline, weekly signature,
  oldest-review-first country selection and pending reservation.
- `Strategist.finish` — bounded review result; EYES additionally requires the
  exact pending campaign/country/day/signature and disallows terminal rewrites.
- `Strategist.history` — country/game/day filter, last three summaries,
  chronological presentation and 900-character summary cap.

These adapted implementations live in `strategist.py`. They replace the old
single-country cooldown branch via the existing `initiative_review` adapter;
the old cooldown field is migrated as a cooldown record, not a fictional treaty.

### Intentional changes

- Existing `diplomatic_memory` and ChatStateStore/SQLite persist the journal.
  No additional SQLite database or agreement store is created.
- Campaign IDs are V5 UUID strings rather than native numeric `gid` values.
- Cursor state survives process restart, instead of remaining only in RAM.
- Selected AI countries need not be majors; the EYES adapter allows up to six.
- Historical ON/unknown, OFF toggle, unauthenticated/missing facts, occupied
  work slots, stale/human/capitulated countries all fail closed.
- One review per new game-day tick, protected by an adapter lock. No polling
  thread, model invocation, chat creation, console command or effect dispatch.
- Unknown pending work blocks new reservations until explicitly finished or
  cancelled. Failure/cancellation is not automatically retried.
- The batch API keeps the upstream first-tick delay. Only the old single-country
  API requests an initial *policy review* for backward compatibility; this is
  not delivery of a diplomatic initiative.
- Text substring crisis detection was not transferred: it does not establish a
  verified crisis. The adapter does not invent missing snapshot fields.
- Review summaries never become confirmed game facts, consent or trust changes.

## V6 additional source transfer (2026-11-08)

At the same pinned revision, `middleware/game_state.py` functions
`_nation_compact` and `world_snapshot_block` were copied unchanged into
`context.py`. Together with the three previously copied functions, their AST
hashes are recorded in `source_manifest.json` and checked by regression tests.
The V6 initiative inbox uses the snapshot formatter only on fresh authenticated
country observations; formatting does not turn unknown fields into facts.

`middleware/safe_actions.py` constant `CATALOG` was copied into
`action_catalog.py` for an isolated local-model comparison. It is not a runtime
action dispatcher or an authority for EYES decisions. No upstream executor,
injector, provider, model weights or new dependency was transferred.

V6 adds an explicitly authorized Historical ON review policy around the
adapted scheduler. Its default remains blocked; unknown Historical state is
always blocked. This supersedes the blanket ON prohibition above only when
the authenticated per-campaign actor/target/action allowlist is configured.

The full MIT license and upstream copyright remain in this directory. The
newly copied functions and constant use no additional third-party packages.

## Components intentionally not transferred

### V6 game-integration continuation

`diplomatic_schema.py:response_schema` adapts the nested `obj`/`variant`
builders and diplomatic action branch of upstream
`middleware/safe_actions.py:response_schema` at the pinned revision. Equipment,
production priorities, political power and profile dependencies are removed.
This schema is used only by the explicit `v6_strategy_eval.py --structured`
experiment. It grants no authority to model-written actions. The original MIT
copyright/permission notice is retained; no new dependency is introduced.

The remaining upstream console injector and effect execution route were
reviewed but not copied: they do not prove that the game console is open or
provide an engine-supported HTTP input channel. The new native observer and
Bridge snapshot protocol are original EYES code, not attributed to Voices.

`Strategist.save`, upstream settings-file management, the separate SQL database,
raw text crisis matching, `UnifiedRuntime` model-written `decision/actions`,
`functions.py` raw command escape hatches, `injector.py`, cloud providers,
Player2, graphics and source biographies from installed mods are not included.
Existing EYES strategic scoring, point plans, manual approval and result-marker
checks remain authoritative.

Upstream requirements include packages with separate licenses (requests,
PyDirectInput, PyGetWindow, PyAutoGUI, Pyperclip and optional auth packages).
None is introduced by these selected source functions: all their dependencies
are Python standard-library modules. This package's MIT notice is not a license
grant for Paradox assets, other mods, Player2 or model weights.

## Cabinet analysis and manual console probe

- `domestic_schema.py` adapts the nested object schema from
  `middleware/safe_actions.py:response_schema`. The categories and exact-quote
  validation are EYES code. The model returns discussion excerpts, without
  game parameters, consent, decisions, or commands.
- `console_probe.py` adapts the command-file format from
  `middleware/injector.py:Injector.write_run_file` and the nonce/result matching
  pattern from `middleware/safe_actions.py:ActionJournal.receive`. The EYES
  version writes only a fixed check of an existing pact in a temporary folder.
  It reads an explicit log path without changing the agreement lifecycle.
  No keyboard automation, privilege escalation, launcher, runtime localisation
  writes, or upstream game assets are included.
- All changes derive from the pinned revision documented above. The source
  manifest records normalized upstream file SHA-256 values. No new package
  dependency is required.

## Cabinet realism and role prompts

`cabinet_prompt.py` adapts the three realism modes in
`middleware/prompts.py:DIFFICULTY_MODES` and the campaign/role framing in
`middleware/unified_runtime.py:UnifiedRuntime._unified_system`. Sandbox
premises remain hypothetical in EYES. The meeting journal, staged projects and
single-confirmation cabinet motions are new EYES implementations.
