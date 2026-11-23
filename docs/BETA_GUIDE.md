# Beta guide / Инструкция / Guide beta

Read the [English](../README.md), [Russian](../README_RU.md) or [French](../README_FR.md)
quick start. Setup needs no ExecutionPolicy change and does not launch HOI4.

## Distributions

- Windows source: application, local services and a clean writable unified mod.
  Python, dependencies, WebView2, Ollama and weights install separately.
- Workshop: game scripts only, not a full AI installation or an upload tool.
  Enable the application's local mod for managed package installation.
  Never enable both local and Workshop copies; internal QA tools are not distributed to players.
- Personal generated events, treaties, flags, portraits, databases, snapshots,
  saves, profiles, backups and developer fixtures are omitted, including tracked ones.
- RELEASE-MANIFEST.json lists every file's SHA-256; checksums.json hashes archives.
  A source revision is a build identity, not game certification.

## First treaty / Первый договор / Premier traité

1. Start EYES and a disposable GER campaign; check Flask, Bridge and Ollama.
2. Open EYES → target in HOI4. Export a fresh Atlas observation and bind the
   matching branch/session. Do not substitute another campaign's export.
3. Propose one pact; inspect participants and terms before confirming.
4. All required countries accept the exact same revision; use Sign treaty.
5. If installation awaits exit, close HOI4, let EYES install the supported scripts,
   then reopen the matching campaign. Confirm its native review/event if offered.
6. Inspect the actual diplomatic relation and matching Bridge receipt. Generated
   or sent is not confirmed. A news database row is not proof of a visible popup.

For GER–POL compound tests: mutual pact; GER guarantees POL; GER grants POL
access to German territory. Refusal, partial acceptance and counteroffers are
valid AI behavior. Tests must not force acceptance.

## Errors

Ollama unavailable/timeout/invalid JSON: retain the draft, read the error, start
Ollama and retry once. Model failure must never grant consent.
Session/map mismatch: export from the actual campaign, bind its branch. Review-only
output does not authorize execution. Missing desktop: install WebView2 and rerun setup.
Clean-distribution automatic checks cover integrity, Python imports and static
scripts, not the full developer regressions. Live QA is available only in the owner’s private developer build, with a disposable campaign.

## Limitations

- Candidate target: 1.20.0.0; the reviewed 1.19.5.0 engine profile remains available. Both need applicable live acceptance. Matching descriptors
  do not prove compatibility. Static validation covers a subset of engine semantics.
- Negotiation scope exceeds execution scope. Unsupported obligations, arbitrary
  durations, contradictory terms and unsupported bundle types must block execution.
- No universal runtime command transport. Some generated scripts require a restart.
- Static character definitions do not prove appointment. Unknown identities stay unnamed.
- Fleet missions, redistribution and repair are not certified controls. Meetings,
  projects and press include EYES-only state, not automatically executed reforms.
- Recognition is an EYES-defined position, not invented historical fact. Its
  native diplomacy panel/custom map rendering still needs acceptance testing.
- Modified map previews do not authorize territorial changes. More Provinces,
  Millennium Dawn, Kaiserreich and TNO are not blanket-certified.
- Multiplayer, Ironman/achievements, all DLC combinations, clean-machine packaging
  and extended performance are not certified.

## Update and uninstall

Close HOI4. Back up the application and profile. Extract to a separate folder and
review setup preview/install targets before registering the new local mod.
Use existing removal/restore controls only for EYES-owned files. Keep one EYES
entry active. Never delete the HOI4 documents folder, user saves, models or third-party
mods. Do not redistribute a personal installation folder.
