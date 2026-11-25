Installer repair: a verified previous managed release can recover its stale registration after an interrupted upgrade. The active application and descriptor no longer need to point at the same release during recovery. Unrecognized or modified sources remain rejected. Python setup failures identify their setup step.

Validated: 83 installer tests; fresh offline dependency installation; application preflight; WebView2; English, Russian and French installer layouts; rollback and uninstall data preservation. Existing local beta.6 registration repaired and startup gate reports ready. Historical component_exit_1 was not reproduced. Real gameplay and clean Windows VM acceptance were not run.

HOI4-EYES 0.9.0-beta.6 by NightPulse
- Read-only save browser: standard Documents/OneDrive, local Steam Cloud, custom user directories and additional folders.
- Plain-text and ZIP metadata: original filename, game date and player tag when readable. Binary/unknown formats are listed with a reason; no guessing or save conversion.
- Explicit save-to-history association. Different files receive separate conversations and diplomatic memory; existing transcripts and treaty approvals remain intact. Inactive save histories cannot be modified accidentally.
- Localized full country names from active directory/ZIP mods, including generated Country Maker definitions; safe tag fallback when unavailable.
- EN/RU/FR, persistent save filter, save labels in native chat and desktop cards. Language changes preserve other connection settings.
Upgrade from beta.5: close EYES, HOI4 and the Paradox launcher, then manually run Install HOI4-EYES.exe and choose Update / repair. Existing history, settings, models and Acceptance are preserved. Protocol 2 blocks incompatible old app/mod combinations before opening the database.
Limits: only local save copies are discoverable; Steam server-only saves must first be downloaded by Steam. Binary or unsupported metadata may be unavailable. Scans are bounded and report truncation. Selecting a filename is NOT proof of the loaded campaign; Bridge/Atlas checks and player approval remain mandatory.
HOI4 1.20 remains beta; new live-game acceptance is pending. Known Qwen timeout/fallback limitations remain.
Unsigned Installer C2 requires manual execution and user consent. Archives, source, licenses and SHA-256: https://github.com/andreykli/hoi4-eyes-public/releases/tag/v0.9.0-beta.6


Beta distribution: original EYES code Apache-2.0; dependency notices retained. No saves, personal profiles or model weights are distributed. Clean Windows acceptance and new in-game acceptance remain incomplete.

Original Steam Workshop: https://steamcommunity.com/sharedfiles/filedetails/?id=3842411221
