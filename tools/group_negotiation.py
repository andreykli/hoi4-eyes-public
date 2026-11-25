"""Bounded group turn selection; randomness chooses speakers, never their positions."""
import hashlib
import random
import re

MAX_AI_COUNTRIES = 6
MAX_AUTO_REPLIES = 3


def addressed(text, candidates):
    from hoi4_country_names import load_country_name_aliases, load_country_names
    aliases = load_country_name_aliases('en')
    names = load_country_names('en')
    extra = {'SOV': ('СССР', 'Советский Союз', 'USSR', 'Soviet Union'),
             'ENG': ('UK', 'Britain', 'United Kingdom', 'Великобритания'),
             'USA': ('USA', 'US', 'United States', 'США'),
             'FRA': ('France', 'Франция'), 'GER': ('Germany', 'Германия'),
             'ITA': ('Italy', 'Италия'), 'POL': ('Poland', 'Польша')}
    found = []
    # A vocative at the start or immediately before a comma has priority over
    # incidental references to a country later in the proposal.
    for tag in candidates:
        for name in (tag, names.get(tag, tag), *aliases.get(tag, ()), *extra.get(tag, ())):
            pattern = r'(?<!\w)' + re.escape(name) + r'(?!\w)'
            match = re.search(pattern, str(text), re.I)
            if match:
                tail = str(text)[match.end():].lstrip()
                score = 2 if match.start() == 0 or tail.startswith((',', ':', '—')) else 1
                found.append((score, -match.start(), tag))
                break
    return [row[2] for row in sorted(found, reverse=True)]


def choose_speaker(chat, candidates):
    candidates = list(candidates)
    messages = chat.get('messages', [])
    latest = messages[-1] if messages else {}
    targets = addressed(latest.get('text', ''), candidates)
    if targets:
        return targets[0]
    # Fairness is based on the saved transcript, so reopening never resets it.
    previous = [m for m in messages if m.get('kind') == 'country_response'][-18:]
    weights = []
    for tag in candidates:
        last = next((i for i,m in enumerate(reversed(previous)) if m.get('speaker') == tag), 18)
        count = sum(m.get('speaker') == tag for m in previous[-6:])
        context = chat.get('country_contexts', {}).get(tag, {})
        relations = context.get('relations', 0)
        relations = relations if isinstance(relations, (int,float)) and not isinstance(relations,bool) else 0
        interest = 1 + min(abs(relations),100)/200
        topic = str(latest.get('text', '')).casefold()
        for key in ('interests','strategic_interests','claims'):
            values = context.get(key, [])
            if isinstance(values,list) and any(isinstance(v,str) and v.casefold() in topic for v in values):interest += .5
        weights.append((1 + last) * interest / (1 + count*3))
    seed = hashlib.sha256((chat['chat_id']+':'+str(chat.get('round'))+':'+str(len(messages))).encode()).digest()
    return random.Random(seed).choices(candidates, weights=weights, k=1)[0]


def position_context(chat, country):
    """Country-specific statements remain conversation memory, never game facts."""
    own = [m for m in chat.get('messages',[]) if m.get('speaker') == country and m.get('kind') == 'country_response'
           and not m.get('retryable') and m.get('provider_status') not in {'fallback','timeout','error','invalid_json'}][-3:]
    return {'country':country, 'prior_statements':[
        {'text':m.get('text','')[:360], 'decision':m.get('decision'),
         'positions':m.get('dialogue_positions',[])[:4], 'round':m.get('round')}
        for m in own],
        'continuity_rule':'Preserve your own previous positions. Change only when new terms or evidence justify it; explain the reason. Other countries cannot consent on your behalf. Statements do not execute treaties.'}


def english_names():
    from chat_presentation import names
    result = dict(names('en'))
    # Stable diplomatic names for the standard HOI4 participants; tags stay internal.
    result.update({'GER':'Germany','SOV':'Soviet Union','FRA':'France',
                   'ENG':'United Kingdom','ITA':'Italy','POL':'Poland','USA':'United States'})
    return result


def country_options():
    """Exclude unrelated three-letter localization keys from the country picker."""
    from hoi4_country_names import _game_roots
    from unified_mod_setup import documents_directory
    from validate_hoi4_gui import parse_mod_descriptor
    from pathlib import Path
    import json
    labels = english_names()
    roots = list(_game_roots())
    docs = documents_directory()
    try:
        for entry in json.loads((docs/'dlc_load.json').read_text(encoding='utf-8-sig')).get('enabled_mods',[]):
            data = parse_mod_descriptor((docs/entry).read_text(encoding='utf-8-sig'))
            source = data.first('path')
            if isinstance(source,str):roots.append(docs/source.strip('"'))
    except (OSError,ValueError,TypeError):
        pass
    tags = set()
    for root in roots:
        for file in (root/'common/country_tags').glob('*.txt'):
            try:
                tags.update(re.findall(r'^\s*([A-Z0-9]{3})\s*=',file.read_text(encoding='utf-8-sig',errors='replace'),re.M))
            except OSError:
                continue
    return {tag:name for tag,name in labels.items() if len(tag)==3 and
            (tag in tags if tags else name != name.lower())}


def observed_contexts(chat, peers):
    result, times = {}, {}
    for peer in peers:
        if (peer.get('campaign_id') != chat.get('campaign_id')
                or peer.get('player_country') != chat.get('player_country')
                or peer.get('save_scope_id') != chat.get('save_scope_id')):
            continue
        for tag,rows in peer.get('game_context_observations',{}).get('by_country',{}).items():
            for row in rows if isinstance(rows,list) else []:
                if (not isinstance(row,dict) or row.get('campaign_id') != chat['campaign_id']
                        or row.get('country') != tag or not isinstance(row.get('context'),dict)
                        or not str(row.get('source','')).startswith('bridge_parsed_hoi4_game_log_v')):
                    continue
                stamp = str(row.get('observed_at',''))
                if stamp >= times.get(tag,''):
                    times[tag] = stamp
                    result[tag] = {**chat.get('country_contexts',{}).get(tag,{}), **row['context']}
    return result


def unsupported_institution(text, context):
    """A model must not invent postwar institutions in an unobserved campaign."""
    import json
    patterns = (r'\bNATO\b|\bНАТО\b|\bOTAN\b',
                r'European Union|Евросоюз|Union européenne',
                r'United Nations|Организация Объединённых Наций|Nations unies')
    player = context.get('latest_player_country_tag')
    evidence = [context.get('latest_player_message_text',''),context.get('known_game_context',{})]
    evidence += [m.get('text','') for m in context.get('recent_messages',[]) if m.get('speaker')==player]
    source = json.dumps(evidence,ensure_ascii=False)
    return any(re.search(pattern,text,re.I) and not re.search(pattern,source,re.I) for pattern in patterns)


def discussion_year(context):
    facts=context.get('known_game_context',{})
    for value in (facts.get('game_date'),facts.get('date'),context.get('latest_player_message_text','')):
        match=re.search(r'\b(1[89]\d{2}|20\d{2})\b',str(value or ''))
        if match:return match[1]
    return '1936 (unverified default; discuss principles, not current events)'
