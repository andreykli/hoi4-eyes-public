# Changelog

## 0.9.0-beta.1 — 2026-11-19 — local release candidate

- Public model changed to local qwen3:4b-instruct (Apache-2.0), with thinking
  disabled and explicit local rollback retained. No model weights bundled.
- Model votes remain independent; Python derives the overall treaty status.
  Canonical contradiction, identity, direction and consent checks remain active.
- Six controlled model cases passed schema/semantic validation; behavior rubric
  5/6 matched the old model. Full suite: 1408 tests; no new HOI4 run claimed.
- Removed unused historical recovery/evaluation scripts from the public allowlist,
  including the recovery script containing fixed old conversation identifiers.

- Prepared a separate public export with a new root commit/tag, NightPulse
  attribution, publication descriptions and SHA-256 inventories; no old Git history.
- Normalized setup_windows.bat to CRLF and added a public-export launcher check.
- Preserved standard Apache-2.0 terms and documented required NOTICE retention
  versus optional public link credit; Workshop ID remains unassigned until publication.

- Reviewed-file Windows-source/Workshop archives with CRC and SHA-256 verification.
- Personal generated treaties, Country Maker events, flags, profiles, snapshots,
  saves, databases, test fixtures and backups excluded, including tracked files.
- EN/RU/FR quick starts, installation/use/limits, acceptance and publication drafts.
- Apache-2.0, author NOTICE and third-party provenance preserved.
- Fixed installed offline diagnostics depending on omitted developer tests.
- Fixed quality-gate startup without its temporary directory.
- Centralized setup dependencies in requirements.txt.
- Isolated startup preflight from the real user profile; tested extracted paths with spaces.
- Fixed recognition map's unknown trigger_if/trigger_else commands and countryless
  diplomatic allowed scope found in the real error log. Extended static auditing
  to these categories; display and behavior still need in-game retesting.
- Added missing native diplomacy/observer/settings translations and French Atlas strings.

Existing diplomacy, memory, Bridge, Atlas, Country Maker, capital selection,
meetings and fleet templates retained. Historical 21/21 live QA remains separate
from automated release tests. Not a claim of completed 1.0 or universal compatibility.
