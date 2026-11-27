"""Fingerprint the source files that determine the running Flask behavior."""

from __future__ import annotations

import hashlib
from pathlib import Path


_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_RUNTIME_COMPONENTS = (
    "tools/save_catalogue.py",
    "tools/chat_presentation.py",
    "tools/group_negotiation.py",
    "tools/single_mod.py",
    "tools/treaty_interest_gate.py",
    "tools/diplomatic_expression_library.py",
    "data/diplomatic_expression_library.json",
    "tools/hoi4_compatibility.py",
    "tools/model_profile.py",
    "tools/runtime_revision.py",
    "tools/mock_ai_server.py",
    "tools/templates/desktop.html",
    "tools/static/desktop.js",
    "tools/static/game_actions.js",
    "tools/developer_access.py",
    "tools/domestic_advice.py",
    "tools/government_meetings.py",
    "tools/cabinet_roster.py",
    "tools/voices_history/cabinet_prompt.py",
    "tools/templates/meetings.html",
    "tools/static/meetings.js",
    "tools/voices_history/domestic_schema.py",
    "tools/world_diplomacy.py",
    "tools/native_recognition.py",
    "tools/fleet_dashboard.py",
    "tools/templates/fleet.html",
    "tools/static/fleet.js",
    "tools/static/fleet.css",
    "tools/atlas_map_sources.py",
    "tools/world_snapshot.py",
    "tools/third_country_reactions.py",
    "tools/diplomatic_templates.py",
    "tools/diplomatic_response.py",
    "tools/news_templates.py",
    "tools/memory_voice.py",
    "tools/template_catalog/observed.json",
    "tools/treaty_package_install.py",
    "tools/treaty_bundle.py",
    "tools/treaty_dialogue.py",
    "tools/player_native_actions.py",
    "tools/execution_markers.py",
    "tools/territory_execution.py",
    "tools/guarded_treaty_effects.py",
    "tools/game_actions.py",
    "tools/state_operations.py",
    "tools/hoi4_windows_adapter.py",
    "tools/world_freshness.py",
    "tools/user_storage.py",
    "tools/world_sessions.py",
    "tools/saved_campaign_binding.py",
    "tools/ui_locale.py",
    "tools/ui_text.json",
    "tools/country_maker_execution.py",
    "tools/country_maker_design.py",
    "tools/templates/world.html",
    "tools/static/world.js",
    "tools/static/world.css",
    "tools/experimental_stack.py",
    "tools/hub_control.py",
    "tools/diplomacy_engine.py",
    "tools/agreement_manager.py",
    "tools/contextual_consent.py",
    "tools/action_registry.py",
    "tools/ollama_provider.py",
    "tools/structured_intents.py",
    "tools/hoi4_country_names.py",
    # Imported behavior modules must be fingerprinted independently: editing
    # one of these must invalidate a Flask process even if the engine facade
    # itself was not touched.
    "tools/diplomatic_doctrine.py",
    "tools/diplomatic_planner.py",
    "tools/diplomatic_phrases.py",
    "tools/phrase_destinations.py",
    "tools/grounded_expression.py",
    "tools/negotiation_progress.py",
    "tools/natural_bilingual.py",
    "data/diplomatic_fragments/natural_bilingual.tsv",
    "tools/diplomatic_memory.py",
    "tools/historical_people.py",
    "tools/historical_behavior.py",
    "data/history/behavior_evidence.json",
    "tools/historical_conferences.py",
    "tools/native_leadership.py",
    "tools/delegation_deliberation.py",
    "tools/conference_phrases.py",
    "tools/diplomatic_catalog.py",
    "tools/phrase_usage_memory.py",
    "tools/mechanic_expression.py",
    "data/diplomatic_fragments/mechanic_bindings.json",
    "data/diplomatic_fragments/mechanic_ten_locales.tsv",
    "data/diplomatic_fragments/parallel_en_ru.tsv",
    "data/diplomatic_fragments/parallel_other_locales.tsv",
    "data/diplomatic_fragments/expanded_ten_locales.tsv",
    "data/history/people.json",
    "data/history/behavior_profiles.json",
    "tools/strategic_interests.py",
    "tools/diplomatic_language.py",
    "tools/language_detection.py",
    "tools/voices_adapter.py",
    "tools/voices_history/context.py",
    "tools/voices_history/strategist.py",
    "tools/v6_autonomy.py",
    "tools/autonomy_game_transport.py",
    "tools/autonomous_negotiation.py",
    "tools/autonomous_term_plan.py",
    "tools/treaty_lifecycle.py",
    "tools/treaty_monitor.py",
    "tools/treaty_decision_package.py",
    "tools/treaty_ai_execution.py",
    "tools/execution_markers.py",
    "tools/initiative_compromise.py",
    "tools/player_native_actions.py",
    "tools/treaty_exit_queue.py",
    "tools/player_diplomacy.py",
    "tools/hoi4_windows_adapter.py",
)


def compute_runtime_source_revision() -> str:
    """Return a deterministic digest for source that affects live diplomacy.

    The Flask process computes this once during import. A later disk edit will
    therefore produce a different value from the one exposed by the old process.
    """
    digest = hashlib.sha256()
    for relative_path in _RUNTIME_COMPONENTS:
        path = _PROJECT_ROOT / relative_path
        content = path.read_bytes()
        digest.update(relative_path.encode("utf-8"))
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return digest.hexdigest()[:16]
