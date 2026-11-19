"""Offline multilingual editorial catalog. It cannot decide or edit treaty terms."""
from functools import lru_cache
from pathlib import Path
from mechanic_expression import allowed_categories, bindings
import re,unicodedata,hashlib
ROOT=Path(__file__).resolve().parents[1]
LOCALE_DIRS={'en':'english','ru':'russian','fr':'french','de':'german','es':'spanish','pl':'polish','ja':'japanese','ko':'korean','pt':'braz_por','zh':'simp_chinese'}
OTHER=('fr','de','es','pl','ja','ko','pt','zh')

EXPANSION_CATEGORIES=frozenset({'conference_open','invitation','internal','mandate','evidence','drafting',
 'security','guarantee','trade','alliance','access','territory','peace','ultimatum','compromise',
 'intelligence','confidentiality','mediation','implementation','conclusion','accept','reject','counter_offer'})
DECISION_SUFFIX_CATEGORIES=frozenset({'accept','reject','counter_offer'})
AUTO_NOTICE_CATEGORIES=frozenset({'conference_open','internal','conclusion'})

def language_script_valid(text,language):
 expected={'ru':r'[А-Яа-я]','ja':r'[ぁ-ゖァ-ヺ]','ko':r'[가-힣]','zh':r'[\u4e00-\u9fff]'}.get(language,r'[A-Za-zÀ-ž]')
 if not re.search(expected,text):return False
 if language in {'en','fr','de','es','pl','pt'} and re.search(r'[А-Яа-яぁ-ゖァ-ヺ가-힣\u4e00-\u9fff]',text):return False
 if language=='zh' and re.search(r'[ぁ-ゖァ-ヺ가-힣А-Яа-я]',text):return False
 return True

def read_expansion(path):
 rows=[];ids=set()
 for number,line in enumerate(Path(path).read_text(encoding='utf-8-sig').splitlines(),1):
  if not line.strip() or line.startswith('#'):continue
  fields=line.split('|')
  if len(fields)!=12:raise ValueError(f'Expected ten translations at line {number}')
  key,category,*values=fields
  if not re.fullmatch(r'exp\.[a-z_]+\.[0-9]{2}',key) or key in ids:raise ValueError('Invalid/duplicate expansion ID: '+key)
  if category not in EXPANSION_CATEGORIES | allowed_categories() or any(not v.strip() or v!=v.strip() for v in values):raise ValueError('Invalid fragment category/text: '+key)
  for lang,text in zip(LOCALE_DIRS,values):
   if not language_script_valid(text,lang):raise ValueError('Language script mismatch: '+key+':'+lang)
   if text[-1] not in '.?!。？！':raise ValueError('Missing terminal punctuation: '+key+':'+lang)
   if lang=='es' and text.endswith('?') and not text.startswith('¿'):raise ValueError('Spanish question punctuation: '+key)
   if any(c in text for c in ('\t','\ufffd','{','}')):raise ValueError('Invalid characters: '+key)
  numbers=[re.findall(r'[0-9]+',v) for v in values]
  if any(n!=numbers[0] for n in numbers[1:]):raise ValueError('Numeric translation mismatch: '+key)
  ids.add(key)
  mechanic=next(((action,phase) for action,spec in bindings()['actions'].items()
                 for phase in bindings()['phases'] if category==spec['root']+'_'+phase),None)
  rows.append({'id':key,'category':category,'translations':dict(zip(LOCALE_DIRS,values)),
               'source':'original_editorial_text','historical_quote':False,
               'usage':('guarded_decision_suffix' if mechanic and mechanic[1] in bindings()['automatic_suffix_phases'] else 'contextual_fragment') if mechanic else ('conference_notice' if category in AUTO_NOTICE_CATEGORIES else 'decision_suffix' if category in DECISION_SUFFIX_CATEGORIES else 'contextual_fragment'),
               **({'action_id':mechanic[0],'phase':mechanic[1],'eligibility':['matching_typed_action','matching_saved_stance','not_a_side_clause','canonical_core_preserved']} if mechanic else {})})
 return rows

def read_parallel_translations(path,result):
 seen=set()
 for line in Path(path).read_text(encoding='utf-8-sig').splitlines():
  if not line.strip() or line.startswith('#'):continue
  key,*values=line.split('|')
  if key in seen or key not in result or len(values)!=len(OTHER):raise ValueError('Invalid/duplicate translation ID: '+key)
  for lang,text in zip(OTHER,values):
   if not text or text!=text.strip() or not language_script_valid(text,lang):raise ValueError('Invalid translation language/text: '+key)
   if not text.endswith(('.', '?', '!', '。','？','！')):raise ValueError('Invalid translation punctuation: '+key)
   if re.findall(r'[0-9]+',text)!=re.findall(r'[0-9]+',result[key]['translations']['en']):raise ValueError('Translation numeric mismatch: '+key)
  seen.add(key)
  result[key]['translations'].update(zip(OTHER,values))

@lru_cache(maxsize=1)
def records():
 result={};ordinal={}
 for line in (ROOT/'data/diplomatic_fragments/parallel_en_ru.tsv').read_text(encoding='utf-8-sig').splitlines():
  if not line.strip() or line.startswith('#'):continue
  category,en,ru=line.split('|');ordinal[category]=ordinal.get(category,0)+1
  key=f'{category}.{ordinal[category]}'
  result[key]={'id':key,'category':category,'translations':{'en':en,'ru':ru},'source':'original_editorial_text','historical_quote':False}
 read_parallel_translations(ROOT/'data/diplomatic_fragments/parallel_other_locales.tsv',result)
 for row in (*read_expansion(ROOT/'data/diplomatic_fragments/expanded_ten_locales.tsv'),
             *read_expansion(ROOT/'data/diplomatic_fragments/mechanic_ten_locales.tsv')):
  if row['id'] in result:raise ValueError('Duplicate fragment ID: '+row['id'])
  result[row['id']]=row
 from natural_bilingual import rows as bilingual_rows
 for row in bilingual_rows():
  if row['id'] in result:raise ValueError('Duplicate bilingual speech ID')
  result[row['id']]=row
 from phrase_destinations import destination
 for row in result.values():row['destination']=destination(row)
 return tuple(result.values())

@lru_cache(maxsize=1)
def phrase_index():
    by_category={};by_id={}
    for row in records():
        by_id[row['id']]=row
        for language in row['translations']:
            by_category.setdefault((row['category'],language),[]).append(row)
    return {k:tuple(v) for k,v in by_category.items()},by_id

def texts(category,language):
    return [r['translations'][language] for r in phrase_index()[0].get((category,language),()) if r.get('usage') not in {'contextual_fragment','guarded_decision_suffix','guarded_natural_speech'}]

def position_candidates(core,row,language):
 # Never append clarification questions about facts which are already known.
 category={'accept':'accept','reject':'reject','counter_offer':'counter_offer','discuss':'proposal'}.get(row.get('stance'))
 if not category or language not in {'en','ru'}:return []
 from mechanic_expression import suffixes
 from phrase_destinations import speech_allowed
 tails=[r['translations'][language] for r in phrase_index()[0].get((category,language),()) if speech_allowed(r)]
 return [f'{core} {tail}' for tail in tails if len(f'{core} {tail}')<=600]

def normalise(text):
 return ' '.join(re.findall(r'\w+',unicodedata.normalize('NFKC',text).casefold()))

@lru_cache(maxsize=1)
def _cached_audit():
 counts={lang:0 for lang in LOCALE_DIRS};seen={lang:{} for lang in LOCALE_DIRS};errors=[];warnings=[]
 from difflib import SequenceMatcher
 matchers={lang:{} for lang in LOCALE_DIRS}
 for row in records():
  for lang,text in row['translations'].items():
   counts[lang]+=1;key=normalise(text)
   if key in seen[lang]:errors.append(f'duplicate:{lang}:{row["id"]}:{seen[lang][key]}')
   for other,other_id in seen[lang].items():
    matcher=matchers[lang][other];matcher.set_seq1(key)
    if matcher.real_quick_ratio()>.92 and matcher.quick_ratio()>.92 and matcher.ratio()>.92:warnings.append(f'near_duplicate_review:{lang}:{row["id"]}:{other_id}')
   seen[lang][key]=row['id']
   matchers[lang][key]=SequenceMatcher(None,'',key,autojunk=False)
   minimum=6 if lang in {'zh','ja','ko'} else 12
   if len(text)<minimum or len(text)>600 or any(x in text for x in ('TODO','\ufffd','{{','}}','<placeholder>')):
    errors.append(f'invalid_text:{lang}:{row["id"]}')
   expected={'ru':r'[А-Яа-я]','ja':r'[ぁ-ゖァ-ヺ]','ko':r'[가-힣]','zh':r'[\u4e00-\u9fff]'}.get(lang)
   if expected and not re.search(expected,text):errors.append(f'script_mismatch:{lang}:{row["id"]}')
 # Count authored strings, never Cartesian combinations of openings and closings.
 from diplomatic_phrases import OPENINGS,CLOSINGS
 from conference_phrases import PHRASES
 indexed={lang:list(seen[lang]) for lang in counts}
 for library in (OPENINGS,CLOSINGS,PHRASES):
  for lang,groups in library.items():
   for group,strings in groups.items():
    for text in strings:
     key=normalise(text)
     if key in indexed.setdefault(lang,[]):errors.append(f'legacy_catalog_duplicate:{lang}:{group}')
     for other in indexed[lang]:
      matcher=matchers.setdefault(lang,{}).get(other)
      if matcher is None:
       matcher=SequenceMatcher(None,'',other,autojunk=False);matchers[lang][other]=matcher
      matcher.set_seq1(key)
      if matcher.real_quick_ratio()>.92 and matcher.quick_ratio()>.92 and matcher.ratio()>.92:warnings.append(f'legacy_near_duplicate_review:{lang}:{group}:{text}')
     indexed[lang].append(key)
 category_counts={}
 for row in records():
  bucket=category_counts.setdefault(row['category'],{})
  for lang in row['translations']:bucket[lang]=bucket.get(lang,0)+1
 return {'category_counts':category_counts,'expansion_records':sum(r['id'].startswith('exp.') for r in records()),'unique_indexed_counts':{lang:len(set(strings)) for lang,strings in indexed.items()},'catalog_counts':counts,'errors':errors,'warnings':warnings,
         'grammar_assessment':'automated sanity checks; not a claim of professional native-language certification',
         'historical_quotes':False,'network_required':False}

def translated_fragment(fragment_id,original_language,display_language,mode='translation'):
    if mode not in {'original','translation'}:raise ValueError('Invalid display mode')
    row=phrase_index()[1].get(fragment_id)
    if not row:raise ValueError('Unknown authored fragment')
    original=row['translations'].get(original_language)
    translated=row['translations'].get(display_language)
    if original is None or (mode=='translation' and translated is None):
        return {'available':False,'reason':'verified_translation_missing','historical_quote':False}
    return {'available':True,'original_text':original,'translation':translated,
            'original_language':original_language,'translation_language':display_language,
            'display_text':original if mode=='original' else translated,'historical_quote':False}

def audit():
    from copy import deepcopy
    return deepcopy(_cached_audit())

def select_fragment(category,language,selection_key,recent=()):
    """Explicit contextual lookup, not an automatic diplomatic decision or reply."""
    options=list(phrase_index()[0].get((category,language),()))
    if not options:return None
    recent_set={normalise(t) for t in recent}
    start=int(hashlib.sha256(str(selection_key).encode('utf8')).hexdigest()[:12],16)%len(options)
    ordered=options[start:]+options[:start]
    chosen=next((r for r in ordered if normalise(r['translations'][language]) not in recent_set),None)
    if chosen is None:return None # Exhaustion must not pretend to provide a new formulation.
    return {'fragment_id':chosen['id'],'category':category,'language':language,
            'text':chosen['translations'][language],'text_origin':'authored_template',
            'usage':chosen.get('usage','legacy_fragment'),'historical_quote':False,
            'decision_authority':False}

if __name__=='__main__':
    import argparse,json
    parser=argparse.ArgumentParser(description='Offline diplomatic corpus audit; never calls an LLM.')
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args();result=audit()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf8')
    print(args.output)
    raise SystemExit(bool(result['errors'] or result['warnings']))
