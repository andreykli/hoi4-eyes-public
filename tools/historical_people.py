"""Offline, source-labelled people; a historical office is NOT a game mandate."""
import json
from pathlib import Path
from datetime import date
from validate_hoi4_gui import TOKEN_RE
from hoi4_country_tags import COUNTRY_TAG_RE

CATALOG=Path(__file__).resolve().parents[1]/'data/history/people.json'

def load_people(path=CATALOG):
    data=json.loads(Path(path).read_text(encoding='utf-8-sig'))
    result={}
    for row in data['people']:
        if not row.get('sources') or row['id'] in result:raise ValueError('Invalid historical provenance/duplicate')
        born=date.fromisoformat(row['birth']) if row.get('birth') else None
        died=date.fromisoformat(row['death']) if row.get('death') else None
        year=row.get('birth_year')
        if year is not None and (type(year) is not int or not 1000<=year<=2100 or not row.get('birth_year_source')):
            raise ValueError('Invalid sourced birth year')
        if born and died and died<=born:raise ValueError('Invalid life dates')
        result[row['id']]=row
    return result

def eligibility(person,calendar_date,official=False):
    today=date.fromisoformat(calendar_date)
    if person.get('birth') and today<date.fromisoformat(person['birth']):return 'not_born'
    if person.get('death') and today>=date.fromisoformat(person['death']):return 'historical_death_date_reached'
    # A sourced year is a closed birth interval, not a fabricated January 1.
    # Eligibility is conservative: no positive result during the unknown year.
    year=person.get('birth_year')
    bounded_birth=(type(year) is int and bool(person.get('birth_year_source')) and today.year>year)
    if not person.get('birth') and type(year) is int and person.get('birth_year_source') and today.year<year:return 'not_born'
    if (not person.get('birth') and not bounded_birth) or not person.get('death'):return 'life_dates_unverified'
    if official and not any(date.fromisoformat(r['start'])<=today<date.fromisoformat(r['end']) for r in person['offices']):
        return 'historical_office_not_confirmed_at_date'
    return 'eligible'

def representative(country,calendar_date,game_leader_id=None):
    # No inference of the current government from 1936 or the country tag.
    people=load_people()
    matched=next((p for p in people.values() if p['country']==country and game_leader_id in p['game_ids']),None)
    if matched and calendar_date and eligibility(matched,calendar_date)=='eligible':
        return {'person_id':matched['id'],'name':matched['name'],'basis':'observed_game_leader_id_plus_sourced_identity',
                'historical_office_matches_date':eligibility(matched,calendar_date,official=True)=='eligible',
                'historical_quote':False,'game_leader_id':game_leader_id}
    return {'person_id':None,'name':country,'game_leader_id':game_leader_id,
            'basis':'current_representative_unverified','historical_quote':False}

def salutation(person,calendar_date,language):
    roles=[r['role'] for r in person['offices'] if r['start']<=calendar_date<r['end']]
    if 'president' in roles:
        return {'en':'Mr. President','fr':'Monsieur le Président','ru':'Господин президент'}.get(language,person['name'])
    return person['name'] # Unknown titles are not invented from a surname.

def _character_blocks(text):
    # Discovery only: reuse the established tokenizer, but skip arbitrary nested
    # blocks/trait lists. The GUI subset parser deliberately rejects bare lists.
    tokens=[m.group(0) for m in TOKEN_RE.finditer(text)
            if not m.group(0).isspace() and not m.group(0).startswith('#')]
    try:start=tokens.index('characters')
    except ValueError:return []
    if tokens[start+1:start+3]!=['=','{']:raise ValueError('Invalid characters root')
    rows=[];i=start+3
    while i<len(tokens) and tokens[i]!='}':
        if tokens[i+1:i+3]!=['=','{']:
            i+=1;continue
        name=tokens[i].strip('"');begin=i+3;j=begin;depth=1
        while j<len(tokens) and depth:
            if tokens[j]=='{':depth+=1
            elif tokens[j]=='}':depth-=1
            j+=1
        if depth:raise ValueError('Unbalanced character definition')
        rows.append((name,tokens[begin:j-1]));i=j
    return rows

def _first_value(tokens,key):
    for i,token in enumerate(tokens[:-2]):
        if token==key and tokens[i+1]=='=' and tokens[i+2]!='{':return tokens[i+2].strip('"')
    return None

def scan_game_characters(game_dir,tags):
    rows=[]
    for tag in tags:
        if not isinstance(tag,str) or not COUNTRY_TAG_RE.fullmatch(tag):raise ValueError('Invalid country tag')
        path=Path(game_dir)/'common/characters'/f'{tag}.txt'
        if not path.is_file():continue
        for key,tokens in _character_blocks(path.read_text(encoding='utf-8-sig')):
            rows.append({'id':key,'country':tag,'name_key':_first_value(tokens,'name'),
                'game_ideology':_first_value(tokens,'ideology'),
                'source':str(path),'real_person_status':'unverified_game_character',
                'birth':None,'death':None,'office_periods':[],
                'warning':'Game expire values are not historical death dates. Definitions do not prove current office.'})
    return rows
