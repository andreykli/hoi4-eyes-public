"""Unread metadata on existing chats; reading never grants diplomatic consent."""
from copy import deepcopy


def unread(chat):
    read = set(chat.get('read_message_ids', []))
    return [m for m in chat.get('messages', []) if m.get('kind') == 'initiative_notice'
            and isinstance(m.get('request_id'), str) and m['request_id'] not in read]


def mark_read(chat, message_ids):
    if not isinstance(message_ids, list) or len(message_ids) > 100 or any(not isinstance(x, str) for x in message_ids):
        raise ValueError('bounded message identities required')
    available = {m.get('request_id') for m in chat.get('messages', []) if m.get('kind') == 'initiative_notice'}
    if not set(message_ids) <= available: raise ValueError('message does not belong to this chat')
    chat['read_message_ids'] = sorted(set(chat.get('read_message_ids', [])) | set(message_ids))
    return {'chat_id': chat['chat_id'], 'unread_count': len(unread(chat)), 'diplomatic_consent_changed': False}


def campaign_inbox(engine, campaign):
    result = []
    for chat in engine.chats.values():
        if chat.get('campaign_id') != campaign or chat.get('chat_mode') == 'ai_to_ai': continue
        messages = unread(chat)
        if messages: result.append({'chat_id': chat['chat_id'], 'participants': deepcopy(chat['participants']),
            'unread_count': len(messages), 'message_ids': [m['request_id'] for m in messages]})
    return {'campaign_id': campaign, 'unread_count': sum(c['unread_count'] for c in result), 'chats': result}
