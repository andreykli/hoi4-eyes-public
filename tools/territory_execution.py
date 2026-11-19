"""Generate a fail-closed, player-confirmed HOI4 event for an agreed partition.

The generated script is an artifact only. It must be reviewed and installed in
the active mod by the user; this module never writes mod files or executes HOI4.
"""

import hashlib
import json
import re
from hoi4_country_tags import COUNTRY_TAG_RE


TAG_RE = COUNTRY_TAG_RE
UUID_RE = re.compile(r"^[0-9a-f]{32}$")


def _valid_plan(chat, proposal, assessment):
    if proposal.get("state") != "AGREED_PENDING_GAME_VALIDATION":
        raise ValueError("territory proposal has not been unanimously approved")
    if assessment.get("status") != "MANUAL_REQUIRED" or not assessment.get("transfers"):
        raise ValueError("territory proposal has no guarded transfer plan")
    campaign_id = chat.get("campaign_id")
    proposal_id = proposal.get("proposal_id")
    if not isinstance(campaign_id, str) or not UUID_RE.fullmatch(campaign_id):
        raise ValueError("campaign_id must be a lowercase UUID hex string")
    if not isinstance(proposal_id, str) or not UUID_RE.fullmatch(proposal_id):
        raise ValueError("proposal_id must be a lowercase UUID hex string")

    participants = chat.get("participants")
    if (not isinstance(participants, list) or not 2 <= len(participants) <= 6
            or len(set(participants)) != len(participants)
            or any(not isinstance(tag, str) or not TAG_RE.fullmatch(tag) for tag in participants)):
        raise ValueError("chat participant tags are invalid")
    if chat.get("player_country") not in participants:
        raise ValueError("player country must be a chat participant")
    version = proposal.get("versions", [])[-1]
    if version.get("participants") != participants or any(
        version.get("votes", {}).get(tag, {}).get("decision") != "accept"
        for tag in participants
    ):
        raise ValueError("all participants must accept the same proposal version")

    state_ids = version.get("target", {}).get("state_ids")
    if (not isinstance(state_ids, list) or not state_ids
            or any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in state_ids)
            or len(set(state_ids)) != len(state_ids)):
        raise ValueError("target state IDs must be unique positive integers")
    allocations = version.get("allocations")
    if not isinstance(allocations, dict) or set(allocations) != set(participants):
        raise ValueError("allocations must cover exactly the current participants")
    assigned = [state_id for tag in participants for state_id in allocations[tag]]
    if sorted(assigned) != sorted(state_ids) or len(assigned) != len(set(assigned)):
        raise ValueError("each target state must be assigned exactly once")

    transfers = assessment["transfers"]
    current_owners = assessment.get("reported_current_owners")
    if not isinstance(current_owners, dict) or set(current_owners) != {str(value) for value in state_ids}:
        raise ValueError("the plan must include an agreed current-owner snapshot for every target state")
    if any(owner not in participants for owner in current_owners.values()):
        raise ValueError("every reported current owner must be a chat participant")
    for item in transfers:
        state_id = item.get("state_id")
        old_owner = item.get("expected_current_owner")
        new_owner = item.get("new_owner")
        if (state_id not in state_ids or old_owner not in participants or new_owner not in participants
                or old_owner == new_owner or allocations.get(new_owner, []).count(state_id) != 1
                or current_owners.get(str(state_id)) != old_owner):
            raise ValueError("transfer plan does not match approved allocation")
    expected_moved = {
        state_id for state_id in state_ids
        if current_owners[str(state_id)] != next(tag for tag in participants if state_id in allocations[tag])
    }
    if (len(transfers) != len(expected_moved)
            or {item["state_id"] for item in transfers} != expected_moved):
        # A no-op state must not be added as a transfer, and each moved state must be present.
        raise ValueError("transfer list is incomplete or includes a no-op state")
    return participants, state_ids, transfers, current_owners


def build_territory_event_package(chat, proposal, assessment):
    """Return event/localisation text guarded by live owner and war checks."""
    participants, state_ids, transfers, current_owners = _valid_plan(chat, proposal, assessment)
    allocations = proposal["versions"][-1]["allocations"]
    binding = {
        "campaign_id": chat["campaign_id"],
        "chat_id": chat["chat_id"],
        "proposal_id": proposal["proposal_id"],
        "version": proposal["current_version"],
        "participants": participants,
        "state_ids": sorted(state_ids),
        "reported_current_owners": {str(key): current_owners[str(key)] for key in sorted(state_ids)},
        "transfers": sorted(transfers, key=lambda item: item["state_id"]),
    }
    plan_hash = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()[:12]
    namespace = f"eyes_tp_{plan_hash}"
    flag = f"eyes_tp_{plan_hash}_applied"
    attempt_flag = f"eyes_tp_{plan_hash}_apply_attempted"
    rollback_flag = f"eyes_tp_{plan_hash}_rolled_back"
    rollback_attempt_flag = f"eyes_tp_{plan_hash}_rollback_attempted"
    expected_final_owners = {
        str(state_id): next(tag for tag in participants if state_id in allocations[tag])
        for state_id in state_ids
    }

    def eligibility_lines(indent="\t\t\t"):
        lines = [f"{indent}{tag} = {{ exists = yes }}" for tag in participants]
        for index, left in enumerate(participants):
            for right in participants[index + 1:]:
                lines.extend([
                    f"{indent}NOT = {{ {left} = {{ has_war_with = {right} }} }}",
                    f"{indent}NOT = {{ {right} = {{ has_war_with = {left} }} }}",
                ])
        for state_id in sorted(state_ids):
            expected_owner = current_owners[str(state_id)]
            lines.extend([
                f"{indent}{state_id} = {{ is_owned_by = {expected_owner} }}",
                f"{indent}{state_id} = {{ is_fully_controlled_by = {expected_owner} }}",
            ])
        return lines

    apply_effects = [
        f"\t\t\t{item['state_id']} = {{ transfer_state_to = {item['new_owner']} if = {{ limit = {{ is_owned_by = {item['new_owner']} }} set_variable = {{ var = eyes_owner_since_day value = global.num_days }} }} }}"
        for item in sorted(transfers, key=lambda value: value["state_id"])
    ]
    state_summary = ", ".join(
        f"{item['state_id']}: {item['expected_current_owner']} -> {item['new_owner']}"
        for item in sorted(transfers, key=lambda value: value["state_id"])
    )
    news = f"{namespace}.3"
    human_scope = f"tag = {chat['player_country']} is_ai = no"
    news_title = " / ".join(participants)
    news_desc = {
        "english": f"The following changes of sovereignty took effect: {state_summary}.",
        "russian": f"Подтверждена смена владельцев областей: {state_summary}.",
    }
    apply_event = [
        f"add_namespace = {namespace}",
        "",
        "country_event = {",
        f"\tid = {namespace}.1",
        f"\ttitle = {namespace}.1.t",
        f"\tdesc = {namespace}.1.d",
        "\tpicture = GFX_report_event_generic",
        "\tis_triggered_only = yes",
        f"\ttrigger = {{ {human_scope} }}",
        "\toption = {",
        f"\t\tname = {namespace}.1.a",
        "\t\ttrigger = {",
        f"\t\t\tNOT = {{ has_country_flag = {flag} }}",
        f"\t\t\tNOT = {{ has_country_flag = {attempt_flag} }}",
        "\t\t}",
        "\t\tif = {",
        "\t\t\tlimit = {",
        f"\t\t\t\t{human_scope}",
        f"\t\t\t\tNOT = {{ has_country_flag = {flag} }}",
        *eligibility_lines("\t\t\t\t"),
        "\t\t\t}",
        f"\t\t\tset_country_flag = {attempt_flag}",
        *apply_effects,
        "\t\t\tif = {",
        "\t\t\t\tlimit = {",
        *[f"\t\t\t\t\t{state_id} = {{ is_owned_by = {owner} is_fully_controlled_by = {owner} }}"
          for state_id, owner in sorted((int(key), value)
                                        for key, value in expected_final_owners.items())],
        "\t\t\t\t}",
        f"\t\t\t\tset_country_flag = {flag}",
        f"\t\t\t\tlog = \"EYES_HOI4_TERRITORY_RESULT_V1|{chat['campaign_id']}|{proposal['proposal_id']}|{plan_hash}|applied|{state_summary}\"",
        f"\t\t\t\tif = {{ limit = {{ NOT = {{ has_global_flag = eyes_news_disabled }} }} news_event = {{ id = {news} }} }}",
        "\t\t\t}",
        "\t\t\telse = {",
        f"\t\t\t\tlog = \"EYES_HOI4_TERRITORY_RESULT_V1|{chat['campaign_id']}|{proposal['proposal_id']}|{plan_hash}|failed|post_apply_owner_check_failed|{state_summary}\"",
        "\t\t\t}",
        "\t\t}",
        "\t\telse = {",
        f"\t\t\tset_country_flag = {attempt_flag}",
        f"\t\t\tlog = \"EYES_HOI4_TERRITORY_RESULT_V1|{chat['campaign_id']}|{proposal['proposal_id']}|{plan_hash}|failed|pre_apply_guard_failed|{state_summary}\"",
        "\t\t}",
        "\t}",
        "\toption = { name = " + namespace + ".1.b }",
        "}",
    ]
    rollback_guards = [guard
                       for index, left in enumerate(participants)
                       for right in participants[index + 1:]
                       for guard in (
                           f"\t\t\tNOT = {{ {left} = {{ has_war_with = {right} }} }}",
                           f"\t\t\tNOT = {{ {right} = {{ has_war_with = {left} }} }}",
                       )]
    rollback_guards += [f"\t\t\t{state_id} = {{ is_owned_by = {owner} }}"
                        for state_id, owner in sorted((int(key), value)
                                                     for key, value in expected_final_owners.items())]
    rollback_guards += [f"\t\t\t{state_id} = {{ is_fully_controlled_by = {owner} }}"
                        for state_id, owner in sorted((int(key), value)
                                                     for key, value in expected_final_owners.items())]
    rollback_effects = [f"\t\t\t{item['state_id']} = {{ transfer_state_to = {item['expected_current_owner']} if = {{ limit = {{ is_owned_by = {item['expected_current_owner']} }} set_variable = {{ var = eyes_owner_since_day value = global.num_days }} }} }}"
                        for item in sorted(transfers, key=lambda value: value["state_id"])]
    rollback_summary = ", ".join(
        f"{item['state_id']}: {item['new_owner']} -> {item['expected_current_owner']}"
        for item in sorted(transfers, key=lambda value: value["state_id"])
    )
    rollback_event = [
        "",
        "country_event = {",
        f"\tid = {namespace}.2",
        f"\ttitle = {namespace}.2.t",
        f"\tdesc = {namespace}.2.d",
        "\tpicture = GFX_report_event_generic",
        "\tis_triggered_only = yes",
        f"\ttrigger = {{ {human_scope} }}",
        "\toption = {",
        f"\t\tname = {namespace}.2.a",
        "\t\ttrigger = {",
        f"\t\t\thas_country_flag = {flag}",
        f"\t\t\tNOT = {{ has_country_flag = {rollback_flag} }}",
        f"\t\t\tNOT = {{ has_country_flag = {rollback_attempt_flag} }}",
        "\t\t}",
        "\t\tif = {",
        "\t\t\tlimit = {",
        f"\t\t\t\t{human_scope}",
        f"\t\t\t\thas_country_flag = {flag}",
        f"\t\t\t\tNOT = {{ has_country_flag = {rollback_flag} }}",
        f"\t\t\t\tNOT = {{ has_country_flag = {rollback_attempt_flag} }}",
        *[line.replace("\t\t\t", "\t\t\t\t", 1) for line in rollback_guards],
        "\t\t\t}",
        f"\t\t\tset_country_flag = {rollback_attempt_flag}",
        *rollback_effects,
        "\t\t\tif = {",
        "\t\t\t\tlimit = {",
        *[f"\t\t\t\t\t{item['state_id']} = {{ is_owned_by = {item['expected_current_owner']} is_fully_controlled_by = {item['expected_current_owner']} }}"
          for item in sorted(transfers, key=lambda value: value["state_id"])],
        "\t\t\t\t}",
        f"\t\t\t\tset_country_flag = {rollback_flag}",
        f"\t\t\t\tlog = \"EYES_HOI4_TERRITORY_RESULT_V1|{chat['campaign_id']}|{proposal['proposal_id']}|{plan_hash}|rolled_back|{rollback_summary}\"",
        "\t\t\t}",
        "\t\t\telse = {",
        f"\t\t\t\tlog = \"EYES_HOI4_TERRITORY_RESULT_V1|{chat['campaign_id']}|{proposal['proposal_id']}|{plan_hash}|rollback_failed|post_rollback_owner_check_failed|{rollback_summary}\"",
        "\t\t\t}",
        "\t\t}",
        "\t\telse = {",
        f"\t\t\tset_country_flag = {rollback_attempt_flag}",
        f"\t\t\tlog = \"EYES_HOI4_TERRITORY_RESULT_V1|{chat['campaign_id']}|{proposal['proposal_id']}|{plan_hash}|rollback_failed|pre_rollback_guard_failed|{rollback_summary}\"",
        "\t\t}",
        "\t}",
        "\toption = { name = " + namespace + ".2.b }",
        "}",
    ]
    event = "\n".join(apply_event + rollback_event) + f"""

news_event = {{
    id = {news}
    title = {news}.t
    desc = {news}.d
    picture = GFX_news_event_generic_sign_treaty1
    major = yes
    is_triggered_only = yes
    option = {{ name = {news}.a if = {{ limit = {{ is_ai = no }} log = "EYES_HOI4_NEWS_ACK_V1|{news}" }} }}
}}
"""
    localization = "\ufeff" + "\n".join([
        "l_english:",
        f" {namespace}.1.t:0 \"Territorial Agreement — Confirmation Required\"",
        f" {namespace}.1.d:0 \"Confirm the approved transfer only if every state still has its expected owner and no participants are at war. Proposed changes: {state_summary}.\"",
        f" {namespace}.1.a:0 \"Apply these transfers\"",
        f" {namespace}.1.b:0 \"Close without changing territory\"",
        f" {namespace}.2.t:0 \"Rollback Territorial Test — Confirmation Required\"",
        f" {namespace}.2.d:0 \"Return the test state to its original owner only if it is still held by the new owner and participants are not at war. Proposed rollback: {rollback_summary}.\"",
        f" {namespace}.2.a:0 \"Return states to their original owners\"",
        f" {namespace}.2.b:0 \"Close without changing territory\"",
        f' {news}.t:0 "Territorial agreement: {news_title}"',
        f' {news}.d:0 "{news_desc["english"]}"',
        f' {news}.a:0 "Noted"',
        "",
    ])
    return {
        "status": "MANUAL_REQUIRED",
        "action_id": "territory_partition",
        "plan_id": plan_hash,
        "event_id": f"{namespace}.1",
        "event_file_name": f"eyes_territory_{plan_hash}.txt",
        "news_localisation_ru": (
            f'l_russian:\n {news}.t:0 "Территориальное соглашение: {news_title}"\n'
            f' {news}.d:0 "{news_desc["russian"]}"\n {news}.a:0 "Принято к сведению"\n'
        ),
        "localisation_file_name": f"eyes_territory_{plan_hash}_l_english.yml",
        "open_event_command": f"event {namespace}.1 {chat['player_country']}",
        "rollback_event_id": f"{namespace}.2",
        "rollback_event_command": f"event {namespace}.2 {chat['player_country']}",
        "manual_confirmation_required": True,
        "active_mod_install_required": True,
        "live_verified": False,
        "execution_command": None,
        "result_marker_prefix": f"EYES_HOI4_TERRITORY_RESULT_V1|{chat['campaign_id']}|{proposal['proposal_id']}|{plan_hash}|",
        "event_file": event,
        "localisation_file": localization,
        "transfers": binding["transfers"],
        "reported_current_owners": binding["reported_current_owners"],
        "expected_final_owners": expected_final_owners,
    }
