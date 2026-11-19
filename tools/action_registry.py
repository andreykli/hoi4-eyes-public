"""Registry and conservative policy for HOI4 diplomatic actions.

Support evidence is the Clausewitz script shipped beside this project. EYES
does not claim that a Python proposal is an in-game diplomatic command.
"""

from copy import deepcopy
import re
from typing import Any, Mapping, Optional
from hoi4_country_tags import is_country_tag
from guarded_treaty_effects import EFFECTS, NEW_ACTIONS


_ACTIONS = {
    **{action: {
        "intent": action, "vanilla_effect": EFFECTS[action][0],
        "required_scope": "country-to-country",
        "required_context": ["signed_revision", "bound_campaign"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["exact_participants", "live_precondition", "postcondition", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": True, "live_verified": False,
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    } for action in NEW_ACTIONS},
    "non_aggression_pact": {
        "intent": "non_aggression", "vanilla_effect": "diplomatic_relation: non_aggression_pact",
        "required_scope": "country-to-country", "required_context": [
            "war_status", "same_faction", "existing_non_aggression_pact",
        ],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["not_at_war", "not_same_faction", "no_existing_pact", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": True, "live_verified": False,
        "live_verification_scope": None,
        "eyes_scripted_execution_supported": True,
        "execution_limitation": "The event can create HOI4's native non-aggression relation. HOI4 controls its expiry (vanilla default: 48 months); custom durations and side clauses cannot be enforced by this effect.",
        "manual_confirmation_required": True,
        "reason_if_unsupported": None,
    },
    "guarantee": {
        "intent": "guarantee", "vanilla_effect": "give_guarantee",
        "required_scope": "country-to-country", "required_context": ["war_status", "target_country"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["target_exists", "not_at_war", "no_duplicate_guarantee", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": True, "live_verified": False,
        "execution_scope": "the agreed guarantor guarantees the agreed beneficiary",
        "revalidation": "generated event checks current war, country existence, and existing guarantee",
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
    "embargo": {
        "intent": "embargo", "vanilla_effect": "send_embargo",
        "required_scope": "country-to-third-country", "required_context": ["target_country"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["explicit_target_tag", "target_exists_at_execution",
                          "by_blood_alone_dlc", "no_duplicate_embargo", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": True, "live_verified": False,
        "execution_scope": "the proposing treaty participant embargoes the explicit third-country tag",
        "revalidation": "generated event checks target existence, DLC, and current embargo status",
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
    "lift_embargo": {
        "intent": "lift_embargo", "vanilla_effect": "break_embargo",
        "required_scope": "country-to-third-country", "required_context": ["target_country"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["explicit_target_tag", "target_exists_at_execution",
                          "by_blood_alone_dlc", "embargo_must_exist", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": True, "live_verified": False,
        "execution_scope": "the proposing treaty participant lifts its embargo on the explicit third-country tag",
        "revalidation": "generated event checks target existence, DLC, and current embargo status",
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
    "military_access": {
        "intent": "military_access", "vanilla_effect": "give_military_access",
        "required_scope": "country-to-country", "required_context": ["target_country", "existing_military_access", "war_status"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["no_duplicate_access", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": True, "live_verified": False,
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
    "docking_rights": {
        "intent": "docking_rights", "vanilla_effect": "diplomatic_relation: docking_rights",
        "required_scope": "country-to-country", "required_context": ["target_country", "existing_docking_rights"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["no_duplicate_rights", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": False, "live_verified": False,
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
    "air_base_access": {
        "intent": "air_base_access", "vanilla_effect": "diplomatic_relation: air_base_access",
        "required_scope": "country-to-country", "required_context": ["target_country", "existing_air_base_access"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["no_duplicate_access", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": False, "live_verified": False,
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
    "faction_join": {
        "intent": "faction_join", "vanilla_effect": "add_to_faction",
        "required_scope": "country-to-faction", "required_context": ["faction", "faction_membership", "faction_rules"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["not_already_member", "faction_rules_allow_join", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": True, "live_verified": False,
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
    "faction_leave": {
        "intent": "faction_leave", "vanilla_effect": "remove_from_faction",
        "required_scope": "human_faction_leader_to_other_member",
        "required_context": ["faction", "faction_membership"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["both_at_peace", "exactly_two_faction_members", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": True, "live_verified": False,
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
    "market_access": {
        "intent": "market_access", "vanilla_effect": None,
        "required_scope": "country-to-market", "required_context": ["market_id", "market_rules"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["market_system_mechanism_unverified"],
        "supported_by_hoi4": False, "implemented_in_eyes": False, "live_verified": False,
        "manual_confirmation_required": True,
        "reason_if_unsupported": "No matching market_access diplomatic effect or action was found in the installed game script.",
    },
    "truce": {
        "intent": "truce", "vanilla_effect": "set_truce",
        "required_scope": "country-to-country", "required_context": ["war_status", "recent_war", "existing_truce"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["war_and_truce_mechanism_unverified"],
        "supported_by_hoi4": True, "implemented_in_eyes": False, "live_verified": False,
        "eyes_scripted_execution_supported": False,
        "manual_confirmation_required": True,
        "reason_if_unsupported": "Native set_truce is documented, but EYES has no verified ceasefire workflow for war/faction participants, consent and postconditions.",
    },
    "white_peace": {
        "intent": "white_peace", "vanilla_effect": "white_peace",
        "required_scope": "country-to-country-war", "required_context": ["war_status", "war_participants"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["both_parties_at_war", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": True, "live_verified": False,
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
    "peace_conference": {
        "intent": "peace_conference", "vanilla_effect": "peace-conference game system",
        "required_scope": "war-participants", "required_context": ["war_status", "capitulation_state", "conference_state"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": ["game_controls_conference_eligibility", "player_confirmation"],
        "supported_by_hoi4": True, "implemented_in_eyes": False, "live_verified": False,
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
    "territory_partition": {
        "intent": "territory_partition",
        "vanilla_effect": "transfer_state / transfer_state_to (scripted state-transfer effects)",
        "required_scope": "multi-country-state-allocation",
        "required_context": [
            "target_state_ids", "current_state_owners", "all_participant_approval",
            "war_status", "claims", "allied_interests", "military_balance",
        ],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": [
            "every_target_state_assigned_exactly_once", "all_participants_review_current_version",
            "live_ownership_revalidated", "player_confirmation", "no_automatic_transfer",
        ],
        "supported_by_hoi4": True, "implemented_in_eyes": True, "live_verified": True,
        "live_verification_scope": "one-state GER-FRA transfer and guarded rollback in HOI4 1.19.3.0; full group-to-SQLite flow not live-tested",
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
    "political_annexation": {
        "intent": "political_annexation",
        "vanilla_effect": "annex_country",
        "required_scope": "player-to-consenting-country",
        "required_context": ["war_status", "target_country"],
        "possible_decisions": ["accept", "reject", "counter_offer", "defer"],
        "safety_guards": [
            "target_is_a_chat_signatory", "every_required_signatory_explicitly_accepts",
            "player_approves_exact_agreement_version", "target_and_initiator_are_at_peace",
            "target_exists_and_owns_states_at_execution", "player_confirms_in_hoi4",
            "no_automatic_annexation", "no_automatic_rollback",
        ],
        "supported_by_hoi4": True, "implemented_in_eyes": True, "live_verified": False,
        "evidence": "Installed HOI4 common/decisions/MEX.txt uses annex_country = { target = GUA }.",
        "manual_confirmation_required": True, "reason_if_unsupported": None,
    },
}


class ActionRegistry:
    """Read-only action catalog plus context and execution gate."""

    def __init__(self):
        self._actions = deepcopy(_ACTIONS)

    def get(self, action_id: str) -> Optional[dict]:
        item = self._actions.get(action_id)
        return deepcopy({"action_id": action_id, **item}) if item else None

    def all(self) -> list:
        return [self.get(action_id) for action_id in self._actions]

    def for_intent(self, intent: str) -> Optional[dict]:
        # The engine's legacy "alliance" label is a request to join a faction
        # for registry purposes; it still cannot trigger a game effect.
        if intent == "alliance":
            return self.get("faction_join")
        for action_id, item in self._actions.items():
            if item["intent"] == intent:
                return self.get(action_id)
        return None

    def assess(self, action_id: str, decision: str, context: Mapping[str, Any]) -> dict:
        """Return a proposal status; never perform an action."""
        action = self.get(action_id)
        if action is None:
            return {"action_id": action_id, "status": "BLOCKED", "reason": "unknown_action"}
        if decision == "counter_offer":
            return {"action_id": action_id, "status": "COUNTER_OFFER", "reason": "counter_offer_does_not_execute_original_action"}
        if decision != "accept":
            return {"action_id": action_id, "status": "PROPOSED", "reason": "no_accepted_proposal"}
        if not action["supported_by_hoi4"]:
            return {"action_id": action_id, "status": "BLOCKED", "reason": action["reason_if_unsupported"]}
        if action_id == 'truce' and not action['implemented_in_eyes']:
            return {'action_id': action_id, 'status': 'BLOCKED', 'reason': action['reason_if_unsupported']}
        if action_id == "territory_partition":
            if context.get("agreement_reached") is not True:
                return {"action_id": action_id, "status": "PROPOSED", "reason": "participant_approval_pending"}
            return {
                "action_id": action_id,
                "status": "MANUAL_REQUIRED",
                "reason": "guarded_event_requires_player_confirmation_and_game_result_marker",
            }
        if action_id == "political_annexation":
            target = context.get("target_country")
            if not is_country_tag(target):
                return {"action_id": action_id, "status": "BLOCKED",
                        "reason": "explicit_target_country_required"}
            if context.get("all_required_signatories_accepted") is not True:
                return {"action_id": action_id, "status": "BLOCKED",
                        "reason": "all_required_signatory_acceptance_not_confirmed"}
            at_war = _bool(context.get("war_status", context.get("at_war")))
            if at_war is True:
                return {"action_id": action_id, "status": "BLOCKED",
                        "reason": "annexation_requires_peace_between_signatories"}
            if at_war is None:
                return {"action_id": action_id, "status": "MANUAL_REQUIRED",
                        "reason": "required_context_unknown: war_status"}
            return {"action_id": action_id, "status": "MANUAL_REQUIRED",
                    "reason": "guarded_annex_country_event_requires_player_confirmation_and_v2_marker"}
        if action_id == "non_aggression_pact":
            at_war = _bool(context.get("war_status", context.get("at_war")))
            same_faction = _bool(context.get("same_faction"))
            existing = _bool(context.get("existing_non_aggression_pact"))
            if at_war is True or same_faction is True or existing is True:
                return {"action_id": action_id, "status": "BLOCKED", "reason": "incompatible_current_game_state"}
            unknown = [
                name for name, value in (
                    ("war_status", at_war),
                    ("same_faction", same_faction),
                    ("existing_non_aggression_pact", existing),
                ) if value is None
            ]
            if unknown:
                return {
                    "action_id": action_id,
                    "status": "MANUAL_REQUIRED",
                    "reason": "required_context_unknown: " + ", ".join(unknown),
                }
        if action_id in {"white_peace", "faction_join", "faction_leave", *NEW_ACTIONS}:
            at_war = _bool(context.get("war_status"))
            same_faction = _bool(context.get("same_faction"))
            if action_id == "white_peace" and at_war is False:
                return {"action_id": action_id, "status": "BLOCKED", "reason": "no_observed_war"}
            if (action_id == "faction_join" and same_faction is True
                    or action_id == "faction_leave" and same_faction is False):
                return {"action_id": action_id, "status": "BLOCKED", "reason": "incompatible_faction_membership"}
            return {"action_id": action_id, "status": "MANUAL_REQUIRED",
                    "reason": "guarded_bilateral_event_requires_live_validation"}
        if action_id == "guarantee":
            at_war = _bool(context.get("war_status", context.get("at_war")))
            target = context.get("target_country")
            if at_war is True:
                return {"action_id": action_id, "status": "BLOCKED", "reason": "countries_at_war"}
            if not is_country_tag(target):
                return {"action_id": action_id, "status": "MANUAL_REQUIRED",
                        "reason": "required_context_unknown: target_country"}
            if at_war is None:
                return {"action_id": action_id, "status": "MANUAL_REQUIRED",
                        "reason": "required_context_unknown: war_status"}
        if action_id == "military_access":
            if _bool(context.get("war_status", context.get("at_war"))) is True:
                return {"action_id": action_id, "status": "BLOCKED", "reason": "countries_at_war"}
            target = context.get("target_country")
            if not is_country_tag(target):
                return {"action_id": action_id, "status": "MANUAL_REQUIRED",
                        "reason": "required_context_unknown: target_country"}
            if _bool(context.get("existing_military_access")) is True:
                return {"action_id": action_id, "status": "BLOCKED",
                        "reason": "military_access_already_active"}
        if action_id in {"embargo", "lift_embargo"}:
            target = context.get("target_country")
            if not is_country_tag(target):
                return {"action_id": action_id, "status": "MANUAL_REQUIRED",
                        "reason": "required_context_unknown: target_country_tag"}
            if context.get("bba_enabled") is False:
                return {"action_id": action_id, "status": "BLOCKED",
                        "reason": "embargo_requires_by_blood_alone"}
            already = _bool(context.get("already_embargoed"))
            if action_id == "embargo" and already is True:
                return {"action_id": action_id, "status": "BLOCKED", "reason": "embargo_already_active"}
            if action_id == "lift_embargo" and already is False:
                return {"action_id": action_id, "status": "BLOCKED", "reason": "no_embargo_to_lift"}
            # DLC and relationship state are rechecked by the generated event
            # immediately before it applies the effect. Unknown is not treated
            # as permission by the event: its apply option stays unavailable.
        return {"action_id": action_id, "status": "MANUAL_REQUIRED", "reason": "no_automated_execution_path"}


def _bool(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        value = value.strip().lower()
        if value in {"yes", "true", "1", "at_war", "same_faction"}:
            return True
        if value in {"no", "false", "0", "not_at_war", "different_faction"}:
            return False
    return None


__all__ = ["ActionRegistry"]
