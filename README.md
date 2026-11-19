# HOI4-EYES Beta


Local AI diplomacy and government discussions for Hearts of Iron IV.
By **NightPulse** · [Original project](https://github.com/andreykli/hoi4-eyes-public).

[Русский](README_RU.md) · [Français](README_FR.md)

**0.9.0-beta.1 — public beta, not a finished 1.0.** Target: Windows,
HOI4 **1.19.5.0**, Python 3.10+, local Ollama `qwen3:4b-instruct`.
The desktop uses PyWebView/WebView2 and the existing Flask backend.
No paid API required. Default model inference is local.

## Start

1. Install Python with the Windows launcher, Ollama and WebView2 Runtime.
2. Run `ollama pull qwen3:4b-instruct` once. Downloading requires internet;
   model weights and their license are separate from this distribution.
3. Extract the Windows source archive to a writable folder. Run
   `setup_windows.bat` once, then `start.bat` for subsequent launches.
4. Inspect component readiness. Register the bundled local mod using the
   existing setup controls with HOI4 closed. Enable exactly one EYES copy.
5. Start a new disposable campaign for the first trial. The QA mod is separate.

Setup does not start HOI4. This is a Python source distribution, not a signed EXE.
Do not overwrite an installation without a backup. Settings and history remain
in `%LOCALAPPDATA%/HOI4-EYES`; extraction does not reset an existing profile.

## Features

- Diplomacy and conferences of 2–6 countries, exact-revision confirmations,
  typed obligations, structured Ollama decisions, refusals and counteroffers.
- Sign treaty, guarded packages, installation after exit, Bridge receipts,
  repeat protection, diplomatic memory and international news generation.
- Atlas, Recognition Atlas, map-source validation, Country Maker, government
  meetings, projects, press and Fleet Dashboard/templates.
- Selectable EN/RU/FR interface. Game events follow HOI4's language.

**Agreement is not execution.** All required parties must confirm the same
revision. SIGNED/PREPARED/SENT are not game confirmation. Unsupported terms block
execution. Some routes require installing scripts after closing HOI4 and restarting.
The Bridge observes results; it is not a general remote-effect API.
Fleet templates, meetings and reforms remain advisory without a supported,
player-confirmed executor. Unknown map geometry blocks territorial actions.

A previous isolated 1.19.5.0 QA run achieved **21/21 PASS, Bridge 21/21**;
that does not validate every feature or the current clean archives.
See [guide and limitations](docs/BETA_GUIDE.md), [acceptance](docs/BETA_ACCEPTANCE.md),
[changelog](CHANGELOG.md), [Workshop draft](docs/WORKSHOP.md).

Original code: [Apache-2.0](LICENSE). Preserve [NOTICE](NOTICE) and mark changes.
[Third-party licenses](THIRD-PARTY-NOTICES.md) remain separate. Unofficial project,
not affiliated with Paradox Interactive.

Public export checks: `python -m unittest discover -s tests -v`.
The historical private developer suite is not bundled.
Startup check: `python tools/release_preflight.py`.
Publication instructions: [docs/PUBLISH_STEPS_RU.md](docs/PUBLISH_STEPS_RU.md).

Author attribution and links: [NOTICE](NOTICE), [CREDITS](CREDITS.md), [ATTRIBUTION](ATTRIBUTION.md).

Original Steam Workshop: https://steamcommunity.com/sharedfiles/filedetails/?id=3842411221
