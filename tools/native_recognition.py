"""Campaign-scoped legal recognition set and observed history."""
from hashlib import sha256
import json
import re

TAG=re.compile(r'[A-Z][A-Z0-9]{2}')
STATUSES=('recognized','not_recognized')


def validate(value, player, countries):
    if not isinstance(value,dict) or len(value)>4096 or any(
            not isinstance(tag,str) or not TAG.fullmatch(tag) or tag==player or tag not in countries
            or state not in STATUSES for tag,state in value.items()):
        raise ValueError('invalid_recognition_snapshot')


def record(connection,campaign,snapshot):
    positions=snapshot.get('recognition')
    if not positions:
        return
    day=snapshot['day']
    observer=snapshot['player']
    latest={}
    for (payload,) in connection.execute(
            "SELECT payload FROM eyes_world_records WHERE campaign=? AND kind='recognition' ORDER BY rowid",
            (campaign,)):
        row=json.loads(payload)
        if (row.get('source')=='hoi4_native_legal_recognition' and row.get('observer')==observer
                and row.get('day',day)<=day):
            latest[row['subject']]=row
    for target,legal in positions.items():
        subject=f'country:{target}'
        prior=latest.get(subject)
        if prior and prior['legal']==legal:
            continue
        predecessor=prior['identity'] if prior else ''
        identifier=sha256(f'{campaign}|{observer}|{subject}|{day}|{legal}|{predecessor}'.encode()).hexdigest()
        payload={'source':'hoi4_native_legal_recognition','campaign':campaign,'day':day,
                 'game_date':snapshot['date'],'observer':observer,'subject':subject,'legal':legal,
                 'de_facto':None,'protest':None,'identity':identifier}
        connection.execute('INSERT OR IGNORE INTO eyes_world_records VALUES(?,?,?,?,?)',
                           (identifier,campaign,'recognition',snapshot['date'],json.dumps(payload)))


def scripts():
    def has(axis):
        return f'is_in_array = {{ array = eyes_{axis} value = PREV }}'

    files={}
    actions=[]
    for name,status,previous,known in (
            ('eyes_recognize','recognized','not_recognized',False),
            ('eyes_recognize_again','recognized','not_recognized',True),
            ('eyes_withdraw_recognition','not_recognized','recognized',True)):
        state_guard=f'NOT = {{ {has(status)} }} ' + (has(previous) if known else f'NOT = {{ {has(previous)} }}')
        guard=f'exists = yes NOT = {{ tag = ROOT }} ROOT = {{ is_ai = no {state_guard} }}'
        actions.append(f''' {name} = {{
  allowed = {{ always = yes }}
  visible = {{ {guard} }}
  selectable = {{ {guard} }}
  requires_acceptance = no
  cost = 0
  ai_desire = {{ base = -1000 }}
  send_description = {name}_confirm
  complete_effect = {{
   if = {{ limit = {{ {guard} }}
    ROOT = {{
     if = {{ limit = {{ {has(previous)} }} remove_from_array = {{ array = eyes_{previous} value = PREV }} }}
     add_to_array = {{ array = eyes_{status} value = PREV }}
     if = {{ limit = {{ {has(status)} NOT = {{ {has(previous)} }} }}
      log = "EYES_RECOGNITION_CHANGED_V1|[ROOT.GetTag]|[PREV.GetTag]|{status}"
      force_update_map_mode = {{ limit = {{ is_ai = no }} mapmode = eyes_recognition }}
      if = {{
       limit = {{ has_country_flag = eyes_autonomy_observer_enabled NOT = {{ has_country_flag = eyes_world_snapshot_busy }} }}
       set_country_flag = eyes_world_snapshot_busy
       eyes_export_world_states = yes
       clr_country_flag = eyes_world_snapshot_busy
      }}
     }}
    }}
   }}
  }}
 }}
''')
    files['common/scripted_diplomatic_actions/eyes_recognition.txt']='scripted_diplomatic_actions = {\n'+''.join(actions)+'}\n'
    files['common/map_modes/eyes_recognition.txt']='''scripted_map_modes = {
 eyes_recognition = {
  top = {
   type = country
   color = {
    set_temp_variable = { alpha = 0.65 }
    set_temp_variable = { red = 0.34 } set_temp_variable = { green = 0.38 } set_temp_variable = { blue = 0.42 }
    if = {
     limit = { is_in_array = { array = eyes_recognized value = FROM } }
     set_temp_variable = { red = 0.22 } set_temp_variable = { green = 0.68 } set_temp_variable = { blue = 0.40 }
    }
    if = {
     limit = { is_in_array = { array = eyes_not_recognized value = FROM } }
     set_temp_variable = { red = 0.68 } set_temp_variable = { green = 0.26 } set_temp_variable = { blue = 0.24 }
    }
    always = yes
   }
  }
  bottom = { type = none }
  far_text = country
  near_text = country
  update_daily = yes
 }
}
'''
    return files


def export_script():
    base='EYES_WORLD_V1|RECOGNITION|[?global.eyes_campaign_seed_1|0]-[?global.eyes_campaign_seed_2|0]-[?global.eyes_campaign_seed_3|0]-[?global.eyes_campaign_seed_4|0]|[?ROOT.eyes_world_sequence|0]'
    return f'''  if = {{
   limit = {{ NOT = {{ tag = ROOT }} ROOT = {{ is_in_array = {{ array = eyes_recognized value = PREV }} }} }}
   log = "{base}|[THIS.GetTag]|recognized"
  }}
  else_if = {{
   limit = {{ NOT = {{ tag = ROOT }} ROOT = {{ is_in_array = {{ array = eyes_not_recognized value = PREV }} }} }}
   log = "{base}|[THIS.GetTag]|not_recognized"
  }}
'''
