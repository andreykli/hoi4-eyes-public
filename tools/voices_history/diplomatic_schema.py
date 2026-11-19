# Copyright (c) 2026 Voices of History — HOI4 AI Mod contributors
# SPDX-License-Identifier: MIT
# Adapted from middleware/safe_actions.py:response_schema at
# 3226478a81edfc376d63502fa9708c48d309b447. Full MIT notice: LICENSE.
# Adaptation: diplomacy-only, no profile/equipment/production/power actions.
# Experimental model proposals only. No execution authority is conferred.


def response_schema():
    """Responses structured output; authorization still belongs to Python."""
    def obj(properties):
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}

    def variant(names, arguments):
        return obj({"name": {"type": "string", "enum": names}, "args": obj(arguments)})

    tag = {"type": "string", "pattern": "^[A-Z][A-Z0-9]{2}$"}
    diplomacy = variant(["improve_relations", "worsen_relations", "non_aggression_pact",
                         "guarantee", "military_access"], {"country_a": tag, "country_b": tag})
    return obj({"message": {"type": "string"},
                "decision": {"type": "string", "enum": ["inform", "negotiate", "refuse", "accept"]},
                "actions": {"type": "array", "maxItems": 2, "items": diplomacy}})
