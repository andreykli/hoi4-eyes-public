"""In-game controls for EYES notifications."""

ACK_TEXT = {
    'english': 'It seems this will shift the balance of power.',
    'russian': 'Похоже, это изменит баланс сил',
    'french': "Il semble que cela va modifier l'équilibre des puissances",
}


def control_files():
    return {'common/decisions/eyes_experimental_notifications.txt': '''eyes_autonomy_observer_category = {
 eyes_exp_disable_news = { visible = { NOT = { has_global_flag = eyes_news_disabled } } cost = 0 ai_will_do = { factor = 0 } complete_effect = { set_global_flag = eyes_news_disabled } }
 eyes_exp_enable_news = { visible = { has_global_flag = eyes_news_disabled } cost = 0 ai_will_do = { factor = 0 } complete_effect = { clr_global_flag = eyes_news_disabled } }
 eyes_exp_disable_personal = { visible = { NOT = { has_country_flag = eyes_personal_notifications_disabled } } cost = 0 ai_will_do = { factor = 0 } complete_effect = { set_country_flag = eyes_personal_notifications_disabled } }
 eyes_exp_enable_personal = { visible = { has_country_flag = eyes_personal_notifications_disabled } cost = 0 ai_will_do = { factor = 0 } complete_effect = { clr_country_flag = eyes_personal_notifications_disabled } }
}
''', 'localisation/english/eyes_experimental_notifications_l_english.yml': '''\ufeffl_english:
 eyes_exp_disable_news:0 "EYES: mute international news"
 eyes_exp_enable_news:0 "EYES: enable international news"
 eyes_exp_disable_personal:0 "EYES: mute personal message popups"
 eyes_exp_enable_personal:0 "EYES: enable preloaded personal message popups"
'''}
