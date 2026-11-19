# Selected third-party components

No packages, extensions, binaries, model weights or graphical resources are
installed by these source transfers. Preserve each component's LICENSE and
ORIGIN.json when redistributing it.

| Directory | Upstream source | License | Usage |
|---|---|---|---|
| `modmaker_balance` | Floxizdia/hoi4-modmaker, `app/validator.py:_brace_balance` and `_BALANCE_SCAN_RE` | MIT, Copyright (c) 2026 Floxizdia | Quote/comment-aware static precheck |
| `harness_clock` | Xocas12/hoi4-agent-harness, `src/hoi4_harness/adapters/clock.py` | MIT, Copyright (c) 2026 Xocas Alvarez-Jorge | Injected game clock in the isolated lab; GameState import adapted to Any |
| `cwtools_hoi4` | cwtools/cwtools-hoi4-config, selected `Config/` rule data | MIT, Copyright (c) 2018 tboby | Literal aliases and relation enumeration; optional external CWTools workspace |
| `live_tools_hints` | klimPaskov/HOI4-Live-Tools, `hoi4_live/game/log_hints.py` | Apache-2.0, Copyright 2026 Klim Pashkov | Read-only diagnostics; complete upstream LICENSE/NOTICE and pinned origin retained |

The EYES validator is a **subset consumer** of CWT data, not the CWTools engine.
It does not interpret the entire schema or replace in-engine testing.

Unlicensed or unclear-license research snapshots under `runtime/tmp` are NOT
part of these redistributed components and must not be bundled into releases.
No code from hoi4-ai-modding-kit, hoi4-dai or the Olander extension is included.
Reading their architecture did not confer permission to redistribute source.

Voices of History source and its independent notices remain under
`tools/voices_history` and [THIRD-PARTY-NOTICES.md](../../THIRD-PARTY-NOTICES.md).
