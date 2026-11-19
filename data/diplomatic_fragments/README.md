# Multilingual diplomatic corpus

All new expansion text is original editorial dialogue, not historical quotations or factual reports about a campaign. The source file expanded_ten_locales.tsv contains 138 parallel entries in 10 languages (1,380 strings). Each entry is written and translated as a complete sentence; no runtime word permutations or combinatorial multiplication are counted.

Columns: id, category, en, ru, fr, de, es, pl, ja, ko, pt, zh, separated by a literal vertical bar. Encoding: UTF-8. Every entry needs all ten translations. IDs must remain stable and unique. PT denotes Brazilian Portuguese; ZH denotes Simplified Chinese. Italian and Ukrainian are not part of this corpus expansion.

There are 23 domains, with six new entries in each. An entry is assigned one of three uses by tools/diplomatic_catalog.py:

- conference_notice: opening, internal procedure and conclusion; may appear in the existing matching conference phase.
- decision_suffix: acceptance, refusal and counter-offer; may follow the exact immutable decision core in the existing RU/EN fallback. These suffixes add no treaty terms, game facts or signing authority.
- contextual_fragment: questions and observations about particular topics. Available by explicit category lookup and parallel translation, but excluded from automatic expression/notice lookup. This prevents an already-understood guarantee from receiving an unsolicited clarification question.

A phrase about a possible arrangement is not evidence of a proposal or agreement. Neither lookup nor translation can modify a decision, agreement, approval, campaign or game effect. Complex dialogue retains the existing local Ollama route; this expansion adds no required model call.

The automated audit checks normalised exact and close duplicates, counts, script sanity, field completeness, punctuation, numeric correspondence, lengths and placeholders. Language-script checks cannot identify every mislabeled Latin-language translation, and these checks do not certify grammar or semantic equivalence. Text has received an editorial pass; independent native-language review remains outstanding. Do not advertise full language support solely because these fragments exist.

Tools:

- tools/diplomatic_catalog.py --output <report.json>: offline audit, nonzero exit on errors or near-duplicate warnings.
- select_fragment(category, language, selection_key, recent): deterministic contextual lookup with recent-phrase avoidance. Returns None if unavailable or exhausted, never silently changes language.
- translated_fragment(id, original_language, display_language, mode): parallel original/translation lookup; no invented translation or language fallback.

All library counts exclude Cartesian combinations of existing openings and closings. Audit output combines the original indexed libraries and the new parallel entries. Research caches and test output under runtime/tmp are not release assets.

## Mechanic-specific extension

mechanic_ten_locales.tsv adds 72 further parallel entries (720 strings) for 13 registry actions. mechanic_bindings.json defines their action and phase mapping. New totals, including all preceding libraries: EN/RU 424 each, FR/DE/ES/PL/JA/KO/PT/ZH 240 each; 2768 strings overall.

The additional usage guarded_decision_suffix is excluded from generic texts() lookup. It is eligible only through mechanic_expression.suffixes for the same saved action and stance, with no side-clause term_id and with the canonical core preserved. File presence never enables an executor. Templates for actions without an EYES execution adapter remain negotiation text only.

A cached index by category/language and stable ID is used for lookup. Phrase usage is bounded inside the existing chat snapshot; no new database is created. See CONTEXTUAL_DIPLOMACY_INTEGRATION_REPORT.md for limits, tests and performance. Future batches can be checked read-only with tools/validate_phrase_batch.py before editorial review and integration. No extra model call is required.

## Parallel completion, 2026-11-09

The remaining 96 RU/EN-only entries in parallel_en_ru.tsv now have French, German, Spanish, Polish, Japanese, Korean, Brazilian Portuguese and Simplified Chinese translations in parallel_other_locales.tsv. No existing English/Russian string was recounted as new. Every one of the 336 primary catalog records now has ten language versions. The old standalone RU/EN openings/closings/conference library still contributes 88 additional indexed fragments per RU/EN language.

Current unique totals: RU/EN 424 each, the other eight languages 336 each, overall 3536. The target remains unmet. read_parallel_translations rejects duplicate IDs, missing columns, wrong scripts, numeric mismatches and missing terminal punctuation. Grammar certification by native speakers is not claimed. See PARALLEL_COMPLETION_REPORT.md and PARALLEL_COMPLETION_AUDIT.json.
