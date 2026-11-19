"""Read-only recovery of existing Bridge aliases, never identity rewriting."""
from dataclasses import dataclass
from pathlib import Path
import json
import re
from user_storage import runtime_dir
from hoi4_campaign_identity import campaign_id_from_save_token


@dataclass(frozen=True)
class VerifiedAtlasBinding:
    """Internal evidence from a current native export and its persisted session."""
    campaign_id: str
    save_token: str


def matches_for_native_package(campaign, token, atlas_binding=None):
    if atlas_binding is None:
        return matches(campaign, token)
    campaign_id_from_save_token(token)
    return (isinstance(atlas_binding, VerifiedAtlasBinding)
            and atlas_binding.campaign_id == campaign and atlas_binding.save_token == token)


def matches(campaign, token, registry=None):
    if campaign_id_from_save_token(token) == campaign: return True
    registry = Path(registry) if registry else runtime_dir() / 'response.json.campaign-identities.json'
    if not registry.exists(): return False
    data = json.loads(registry.read_text(encoding='utf-8'))
    identities = {r.get('campaign_id') for r in data.get('campaigns', []) if r.get('source_campaign_token') == token}
    return identities == {campaign}

def matches_bridge_observation(campaign, token, registry=None):
    """The authenticated Bridge supplies its exact resolved alias for this export."""
    if campaign_id_from_save_token(token) == campaign:
        return True
    path = Path(registry) if registry else runtime_dir() / 'response.json.campaign-identities.json'
    if not path.is_file():
        return False
    data = json.loads(path.read_text(encoding='utf-8'))
    return any(row.get('source_campaign_token') == token and row.get('campaign_id') == campaign
               for row in data.get('campaigns', []))


def share_observed_seed(first, second):
    """Only an explicit Atlas binding may use aliases of the same native seed."""
    path=runtime_dir()/'response.json.campaign-identities.json'
    if not path.is_file():
        return False
    data=json.loads(path.read_text(encoding='utf-8'))
    groups={}
    for row in data.get('campaigns', []):
        token=row.get('source_campaign_token')
        groups.setdefault(token,{campaign_id_from_save_token(token)}).add(row.get('campaign_id'))
    return any(first in ids and second in ids for ids in groups.values())


def recover(campaign, text, registry=None):
    tokens = []
    for line in text.splitlines():
        if 'EYES_HOI4_COUNTRY_SELECTED_V3|' in line:
            tokens.append(line.split('EYES_HOI4_COUNTRY_SELECTED_V3|',1)[1].split('|')[2].strip())
        elif 'EYES_HOI4_PAIR_CONTEXT_V' in line:
            tokens.append(line.split('EYES_HOI4_PAIR_CONTEXT_',1)[1].split('|')[3])
        elif re.search(r'EYES_AUTONOMY_V[12]\|BEGIN\|', line):
            tokens.append(re.split(r'EYES_AUTONOMY_V[12]\|BEGIN\|',line)[1].split('|')[0])
    valid = []
    for token in tokens:
        try:
            if matches(campaign, token, registry): valid.append(token)
        except ValueError: continue
    if len(set(valid)) != 1: raise ValueError('No unique real campaign seed; load the SAME save and select France once. Do not renegotiate.')
    token = valid[-1];days=[]
    for line in text.splitlines():
        if 'EYES_HOI4_PAIR_CONTEXT_V' in line:
            row=line.split('EYES_HOI4_PAIR_CONTEXT_',1)[1].split('|')
            if len(row)>5 and row[3]==token and row[5].isdigit(): days.append(int(row[5]))
    if not days: raise ValueError('Real absolute game day missing; select the country again in the SAME save')
    return token,max(days)
