"""Build an opt-in read-only HOI4 overlay; never installs or starts the game."""
from pathlib import Path
import argparse


def observer_effect():
    seed = '[?global.eyes_campaign_seed_1|0]-[?global.eyes_campaign_seed_2|0]-[?global.eyes_campaign_seed_3|0]-[?global.eyes_campaign_seed_4|0]'
    seq = '[?ROOT.eyes_autonomy_sequence|0]'
    def log(kind, fields):
        return f'log = "EYES_AUTONOMY_V1|{kind}|{seed}|{seq}|{fields}"'
    header = log('BEGIN', '[?global.num_days|0]|[ROOT.GetTag]|1')
    other = log('BEGIN', '[?global.num_days|0]|[ROOT.GetTag]|0')
    country = log('COUNTRY', '[THIS.GetTag]|[?THIS.eyes_autonomy_is_ai|0]|[?THIS.eyes_autonomy_major|0]')
    pair = log('PAIR', '[PREV.GetTag]|[THIS.GetTag]|[?PREV.opinion@THIS|0]|[?THIS.eyes_autonomy_war|0]|[?THIS.eyes_autonomy_faction|0]|[?THIS.eyes_autonomy_nap|0]|[?global.threat|2]')
    end = log('END', '[?ROOT.eyes_autonomy_rows|0]')
    return '''# Opt-in telemetry only. ROOT is the human observer, never an AI.
eyes_autonomy_observe = {
    eyes_ensure_campaign_identity = yes
    add_to_variable = { eyes_autonomy_sequence = 1 }
    set_variable = { eyes_autonomy_rows = 0 }
    set_variable = { eyes_autonomy_selected_count = 0 }
    every_country = { clr_country_flag = eyes_autonomy_selected }
    set_country_flag = eyes_autonomy_selected
    every_country = {
        limit = { is_ai = yes is_major = yes ROOT = { check_variable = { eyes_autonomy_selected_count < 3 } } }
        set_country_flag = eyes_autonomy_selected
        ROOT = { add_to_variable = { eyes_autonomy_selected_count = 1 } }
    }
    if = { limit = { is_historical_focus_on = yes }
        HEADER
    }
    else = { OTHER }
    every_country = {
        limit = { has_country_flag = eyes_autonomy_selected }
        set_variable = { eyes_autonomy_is_ai = 0 }
        set_variable = { eyes_autonomy_major = 0 }
        if = { limit = { is_ai = yes } set_variable = { eyes_autonomy_is_ai = 1 } }
        if = { limit = { is_major = yes } set_variable = { eyes_autonomy_major = 1 } }
        COUNTRY
        ROOT = { add_to_variable = { eyes_autonomy_rows = 1 } }
    }
    every_country = {
        limit = { has_country_flag = eyes_autonomy_selected }
        every_country = {
            limit = { has_country_flag = eyes_autonomy_selected NOT = { tag = PREV } }
            set_variable = { eyes_autonomy_war = 0 }
            set_variable = { eyes_autonomy_faction = 0 }
            set_variable = { eyes_autonomy_nap = 0 }
            if = { limit = { has_war_with = PREV } set_variable = { eyes_autonomy_war = 1 } }
            if = { limit = { is_in_faction_with = PREV } set_variable = { eyes_autonomy_faction = 1 } }
            if = { limit = { has_non_aggression_pact_with = PREV } set_variable = { eyes_autonomy_nap = 1 } }
            PAIR
            ROOT = { add_to_variable = { eyes_autonomy_rows = 1 } }
        }
    }
    END
}
'''.replace('HEADER', header).replace('OTHER', other).replace('COUNTRY', country).replace('PAIR', pair).replace('END', end)


def build(destination):
    destination = Path(destination).resolve()
    # A build is a separate overlay directory, not a mod installer.
    for relative, content in {
        'common/scripted_effects/eyes_autonomy_observer.txt': observer_effect(),
        'common/on_actions/eyes_autonomy_observer.txt': '''# Disabled by default: requires an explicit human country flag.
on_actions = {
    on_daily = {
        effect = {
            if = {
                limit = { is_ai = no has_country_flag = eyes_autonomy_observer_enabled
                    NOT = { has_country_flag = eyes_autonomy_observer_cooldown } }
                set_country_flag = { flag = eyes_autonomy_observer_cooldown days = 7 }
                eyes_autonomy_observe = yes
            }
        }
    }
}
''',
        'common/decisions/eyes_autonomy_observer.txt': '''# This decision enables telemetry, not treaty approval or execution.
eyes_autonomy_observer_category = {
    eyes_autonomy_enable_observer = {
        icon = generic_political_actions
        visible = { NOT = { has_country_flag = eyes_autonomy_observer_enabled } }
        cost = 0
        complete_effect = { set_country_flag = eyes_autonomy_observer_enabled }
        ai_will_do = { factor = 0 }
    }
    eyes_autonomy_disable_observer = {
        icon = generic_political_actions
        visible = { has_country_flag = eyes_autonomy_observer_enabled }
        cost = 0
        complete_effect = { clr_country_flag = eyes_autonomy_observer_enabled }
        ai_will_do = { factor = 0 }
    }
}
''',
        'common/decisions/categories/eyes_autonomy_observer.txt': '''eyes_autonomy_observer_category = {
    icon = generic_political_actions
    allowed = { is_ai = no }
    visible = { is_ai = no }
}
''',
        'localisation/english/eyes_autonomy_observer_l_english.yml': '''l_english:
 eyes_autonomy_observer_category:0 "EYES: experimental diplomacy observer"
 eyes_autonomy_enable_observer:0 "Enable weekly diplomacy observations"
 eyes_autonomy_enable_observer_desc:0 "Read-only export for EYES Bridge. No treaty is approved or executed. Server autonomy must be enabled separately."
 eyes_autonomy_disable_observer:0 "Disable diplomacy observations"
''',
    }.items():
        path = destination / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding='utf-8-sig' if path.suffix == '.yml' else 'utf-8')
    return destination


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True)
    print(build(parser.parse_args().output))
