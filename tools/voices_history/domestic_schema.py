# Copyright (c) 2026 Voices of History — HOI4 AI Mod contributors
# SPDX-License-Identifier: MIT
# Adapted from safe_actions.py:response_schema (nested typed objects). See LICENSE.
"""Classification schema for a domestic policy discussion."""

TOPICS = ("industry", "economic_law", "conscription", "political_reform", "other")


def response_schema():
    def obj(properties):
        return {"type": "object", "properties": properties,
                "required": list(properties), "additionalProperties": False}

    return obj({"proposals": {"type": "array", "maxItems": 3, "items": obj({
        "topic": {"type": "string", "enum": list(TOPICS)},
        "excerpt": {"type": "string", "maxLength": 600},
    })}})
