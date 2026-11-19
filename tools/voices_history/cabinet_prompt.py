# Copyright (c) 2026 Voices of History — HOI4 AI Mod contributors
# SPDX-License-Identifier: MIT
# Adapted from middleware/prompts.py:DIFFICULTY_MODES and
# middleware/unified_runtime.py:UnifiedRuntime._unified_system. See LICENSE.
"""Meeting-specific adaptations of Voices role and realism prompts."""
from functools import lru_cache

DIFFICULTY_MODES = {
    "realistic": "Judge proposals by historical plausibility for the era and nation. Keep cause and effect grounded.",
    "plausible": "Allow internally consistent alternate history. Accept unlikely but possible developments with believable causes.",
    "sandbox": "Explore imaginative premises as hypothetical. Keep hypothetical outcomes separate from verified campaign facts.",
}

ROLE_FOCUS = {
    "head": "government priorities and political tradeoffs",
    "minister": "resources, legislation and implementation costs",
    "general": "army readiness, training and supply",
    "marshal": "coordination across armies, reserves and long-term readiness",
    "admiral": "fleet readiness, shipyards and fuel",
    "diplomat": "treaties, foreign reactions and international credibility",
}


@lru_cache(maxsize=32)
def instruction(mode, language):
    return (
        f"Cabinet discussion in {language}. Compact JSON keyed by speaker. "
        "Each reply: s (0 unsure, 1 supports, 2 objects, 3 conditional), t (one sentence, up to 120 characters). "
        "Address the latest argument. Continue reacts to peers; compromise offers a concession on each side. "
        "Role priorities differ. Change stance only for a reason; agreement and uncertainty are valid. "
        "Prior statements are opinions. Historical background is context, not current policy. "
        "Game facts must come from observations. No invented appointments, quotes, votes, commands or results. "
        + DIFFICULTY_MODES[mode]
    )
