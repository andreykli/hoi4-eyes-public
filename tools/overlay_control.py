"""Hub-to-existing-overlay adapter. Does not create chats or consent."""
from ui_locale import tr
import re

def handle(overlay,bridge,body):
 if (getattr(overlay,'_chat_busy',False) or getattr(overlay,'_hub_inflight',0)
     or not overlay._chat_results.empty()):raise RuntimeError(tr('Bridge is busy. Wait for the request to finish.', 'Bridge занят запросом; дождитесь завершения.'))
 if body['operation']=='shutdown':
  overlay._hub_closing=True
  overlay.root.after(100,overlay.root.quit)
  return {'status':'shutdown_requested'}
 selection=getattr(overlay,'_hub_game_selection',None) or {}
 state=overlay._window_state_provider()
 if not state or state.get('running') is not True or state.get('game_state')=='main_menu':
  raise RuntimeError(tr('Load a campaign and choose a country in HOI4 to open the chat.', 'Игра не подтвердила активное окно. История доступна в главном меню EYES; для переписки выберите страну в игре.'))
 if (body.get('campaign_id')!=selection.get('campaign_id')
     or body.get('player_country')!=selection.get('player_country')
     or not re.fullmatch('[0-9a-f]{32}',str(body.get('chat_id','')))):
  raise RuntimeError(tr('Choose a country in the current HOI4 campaign.', 'Кампания не совпадает с последним игровым выбором. Нажмите EYES и выберите страну в HOI4.'))
 def get_existing():
  chat=bridge.provider.get_chat(body['chat_id'])
  if (chat.get('campaign_id')!=selection['campaign_id'] or chat.get('player_country')!=selection['player_country']
      or selection.get('selected_country') not in chat.get('participants',[])):
   raise RuntimeError(tr('The chat belongs to a different game selection.', 'Чат не соответствует текущему игровому выбору.'))
  return chat
 overlay._chat_view_generation+=1;overlay._bridge=bridge;overlay._chat_open=True
 overlay._hub_open_panel=body.get('panel','chat') if body.get('panel','chat') in {'chat','conference'} else 'chat'
 overlay.chat_id=None;overlay.active_chat=None;overlay._set_chat_mode(False)
 overlay.summary.config(text=tr('Opening existing chat…', 'Открываем существующие переговоры…'))
 overlay._sync_game_window_visibility()
 overlay._start_chat_worker('open',get_existing,generation=overlay._chat_view_generation)
 return {'status':'open_requested','chat_id':body['chat_id']}
