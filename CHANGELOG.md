# Changelog

## 0.9.0-beta.5 — native chat and explicit treaty understanding

HOI4-EYES 0.9.0-beta.5 by NightPulse
- Compact native diplomatic chat with message timestamps, multiline input and preserved reading position.
- Corrected explicit GER/POL treaty directions, shared country aliases, and negative reciprocal obligations.
- Connected point replies without identical repeated openings; Russian country labels fixed.
- Independent diplomatic approval, player confirmation and Bridge verification remain mandatory.
- HOI4 1.20 support remains beta; full live-game acceptance is pending.
Known limitations: Qwen can still time out and use safe engine-owned wording. Natural-language peace/faction offers, duration exclusions and embargo responses may need clarification. Ten sequential isolated trials do not mean ten successful Qwen replies or verified game effects.
Unsigned Installer C2 requires manual launch and consent. Existing profiles and Acceptance are preserved.
Downloads, source, licenses and SHA-256: https://github.com/andreykli/hoi4-eyes-public/releases/tag/v0.9.0-beta.5

## 0.9.0-beta.4 — compact UI and verified service states

- Compact command-room styling, responsive header and unified Recognition modes.
- Live Flask and Bridge readiness/revision checks; independent Ollama API,
  Qwen installation and memory-residency checks. Missing inventory is not success.
- EN/RU/FR state reasons, manual recheck, accessible indicators and reduced motion.
- No diplomatic-engine, database-schema or treaty-execution changes.
- Experimental unsigned C2 remains available; clean-Windows and new HOI4 1.20
  gameplay acceptance remain pending. Preserve existing profiles and Acceptance.

## 0.9.0-beta.2 — local candidate, publication blocked pending live acceptance

- Translate internal meeting/fleet keys in EN/RU/FR; provide readable treaty
  captions without rewriting saved agreements. Public builds exclude native QA
  executors and developer panels; private desktop controls require a local grant.
- Preserve the same versioned mod name in internal and launcher descriptors,
  including repeat installation; consolidate local registrations with backups.
- Mandatory typed diplomatic authorization for all launch paths; no environment
  flag can bypass signing checks. The strategic planner incorporates attested
  ideology, campaign memory and interests. Repeating requests cannot improve
  the policy relationship or create consent.
- Restore context and campaign memory across both temporary term compilers.
  Validate signed contracts on preparation and native delivery; preserve cached
  retries and existing signatures without issuing new consent.
- Separate unknown technical facts (defer) from strategic rejection and requests
  for compromise. Block unknown actions and unresolved nested conditions.
- Extend the policy to peace, faction operations, relationship cancellation and
  embargoes where their required observations exist. Territorial/sovereignty
  proposals without sufficient verified facts remain under review.
- Add RU/EN/FR explanations and migration/revalidation regressions. Release
  archives now derive their version and exact engine profile consistently.
- Main candidate targets HOI4 1.20.0.0. Reviewed 1.19.5.0 capabilities remain;
  no new live game test is claimed. Acceptance remains a separate fallback.

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

### Beta.2 fixes — 2026-11-23

- Preload the configured local model without blocking desktop startup; retain
  its two-thread/2048-context profile and ten-minute residency.
- Resolve typed proposals before generation. A failed unknown analysis no
  longer launches a second large prose request or becomes a cached empty offer.
- Keep country decisions in the mandatory engine. Qwen selects only bounded
  safe wording indices; timeout/invalid output preserves the engine stance.
- Add 540 original indexed EN/RU/FR expression entries; retrieve at most three
  per clause. Unknown territory allocation requires clarification.
- Log model queue/load/prefill/generation, Flask and Bridge rendering timings.
- Add native recognition implicit TITLE/DESC keys in all three game languages;
  recognition remains the player's unilateral EYES position, not treaty assent.
- No Workshop/GitHub update; real-game acceptance of these changes is pending.

### Beta.2 publication cleanup — 2026-11-23

- Remove eleven unused imports; keep active legacy-named runtime modules and phrase consumers.
- Stop tracking a personal generated treaty archive without deleting the local file.
- Refresh EN/RU/FR release documentation and English Workshop change notes.
- Update only the existing public repository and Workshop item; verify staged bytes,
  archive checksums and service confirmations. No private history or QA executors shipped.
- Main-mod live acceptance remains incomplete; this release is explicitly beta.
