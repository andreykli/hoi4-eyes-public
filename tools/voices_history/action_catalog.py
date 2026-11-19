# Copyright (c) 2026 Voices of History — HOI4 AI Mod contributors
# SPDX-License-Identifier: MIT
# Original CATALOG from middleware/safe_actions.py at 3226478a81edfc376d63502fa9708c48d309b447.
# Experimental comparison only: never imported by the production diplomacy engine.
# Full permission notice and disclaimer: ./LICENSE

CATALOG = """Return ONE JSON object: {"message":"in-character reply", "decision":"inform|negotiate|refuse|accept", "actions":[]}.
Use only these typed actions, with numeric values and exact synchronized country tags:
- improve_relations / worsen_relations {country_a, country_b}: small 30-day opinion signal.
- non_aggression_pact / guarantee / military_access {country_a, country_b}: country_a acts.
  Military access means country_a obtains access TO country_b; it requires country_b's consent.
- send_equipment {sender,receiver,equipment_type,quantity}: 1-500 equipment, donor stockpile required.
- ai_priority {target,priority:hold|infantry|mobile}: AI country's own production role emphasis for 14 days.
- add_political_power {target,amount}: player's explicit Office order only, -5 to +5.
Actions need decision=accept and a concrete authorized request. Never act for third countries.
Your acceptance of a proposal is distinct from execution. Describe actions as proposed/queued;
only a separate engine confirmation can establish success. Refusal/negotiation/information has
actions=[]. 'Take no actions', hypothetical and text-only requests always have actions=[].
No arbitrary commands, effects, wars, annexations, free factories, unearned resource grants,
elections, leader changes or custom-overhaul systems. Unsupported requests stay narrative.
"""

