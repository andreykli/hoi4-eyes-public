# Licenses and distribution

Original EYES code: Apache-2.0. NOTICE identifies NightPulse and the
original project. Section 4 requires retaining applicable attribution notices
and identifying modifications. This uses standard Apache terms, not an extra
advertising clause. The author has selected the public name NightPulse.
Third-party material is not relicensed as original EYES code.
Reference: [Apache-2.0, section 4](https://www.apache.org/licenses/LICENSE-2.0).

| Component | Attribution | License / provenance |
|---|---|---|
| Voices of History selected source | 2026 Voices of History — HOI4 AI Mod contributors | MIT; tools/voices_history/LICENSE, ORIGIN.md; revision 3226478a81edfc376d63502fa9708c48d309b447 |
| HOI4 Mod Maker brace scanner | 2026 Floxizdia | MIT; tools/third_party/modmaker_balance |
| HOI4 Agent Harness clock | 2026 Xocas Alvarez-Jorge | MIT; tools/third_party/harness_clock |
| Selected CWTools rules | 2018 tboby | MIT; tools/third_party/cwtools_hoi4 |
| HOI4 Live Tools hints | 2026 Klim Pashkov | Apache-2.0; tools/third_party/live_tools_hints/LICENSE, NOTICE, ORIGIN.json |
| Historical data | See data/history/WIKIDATA_NOTICE.md | Wikidata CC0; limited sourced profiles |
| PyWebView 6.2.1 | Roman Sirokov and contributors | BSD-3-Clause, separately installed; upstream linked in THIRD-PARTY-NOTICES.md |

Python, Flask and transitive dependencies, Requests, python-dotenv, Pillow and
pythonnet install separately with their package license metadata. No binary
dependency, WebView2, model, game or DLC is bundled. A future standalone EXE
needs a new audit of all frozen dependencies and their full license texts.
Ollama/Qwen download terms are separate. No Toolpack source was copied.
Game package retains licenses/ and THIRD_PARTY_NOTICES.md.

See [LICENSE](../LICENSE), [NOTICE](../NOTICE), [component notices](../THIRD-PARTY-NOTICES.md).

Public model: qwen3:4b-instruct, Apache-2.0, installed separately. Old qwen2.5-coder:3b is research/evaluation-only and is not the public default. See docs/MODEL_LICENSE.md.

## Negotiation research (2026-11-23; not incorporated)

[AI_Diplomacy](https://github.com/GoodStartLabs/AI_Diplomacy) uses the
[Good Start Labs Non-Commercial License](https://raw.githubusercontent.com/GoodStartLabs/AI_Diplomacy/main/LICENSE).
[End-to-end negotiator](https://github.com/facebookresearch/end-to-end-negotiator) uses
[CC BY-NC 4.0](https://raw.githubusercontent.com/facebookresearch/end-to-end-negotiator/master/LICENSE).
Their code, weights and dialogue data were not copied into EYES. Neither is
redistributed under EYES' Apache-2.0 license. They are not runtime dependencies.
The Hugging Face negotiation catalogue has no blanket dataset license. The
[negotiation-spec-experiment card](https://huggingface.co/datasets/spectralbranding/negotiation-spec-experiment)
states CC BY 4.0; its synthetic commercial bargaining transcripts are not
evidence of historical national interests. No dataset was imported or used
for training. EYES' new expression library is original NightPulse text,
Apache-2.0: 540 indexed entries, each in EN/RU/FR, constructed from six
register frames, five stances and eighteen subjects. This count describes
wording variants, not 540 independent diplomatic strategies.
