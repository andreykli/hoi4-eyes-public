"""Exact reviewed engine profiles plus documentary capabilities, never live proof."""
from hashlib import sha256
import json
from pathlib import Path
import re

SUPPORTED_VERSIONS = ('1.19.5.0', '1.20.0.0')
DEFAULT_VERSION = '1.20.0.0'
REFERENCE_DOCS = {
    '1.19.5.0': {'effect':'2cdc93d26775990468eed167442f7a416b13059fb8fe07d139b54250b79209af',
                 'trigger':'7d27ed77dadaf3a213a4b3e2c624bf9c02dae495317abe8eee833b27c2d1de29'},
    '1.20.0.0': {'effect':'94d55ffb68041abbb634366fbb39c542adfd9f6ba03178084a4c90e490c77029',
                 'trigger':'a18ef0beabe99ecf97b07e8bc38bc731e31716d42e0bb43f9c5ee94f13bf03e0'},
}
REQUIRED = {
    'effect': {
        **dict.fromkeys(('diplomatic_relation','set_capital','transfer_state','create_faction',
                        'dismantle_faction','add_to_faction','remove_from_faction','set_country_flag',
                        'clr_country_flag','add_opinion_modifier','set_autonomy'), 'COUNTRY'),
        **dict.fromkeys(('add_core_of','remove_core_of','add_building_construction'), 'STATE'),
        'log':'any','create_dynamic_country':'any',
    },
    'trigger': {
        **dict.fromkeys(('has_non_aggression_pact_with','has_military_access_to',
                        'is_guaranteed_by','exists','has_country_flag'), 'COUNTRY'),
        **dict.fromkeys(('is_owned_by','is_controlled_by','is_fully_controlled_by'), 'STATE'),
    },
}
PARAMETERS = {'diplomatic_relation':('country','relation','active'),
              'set_capital':('state',),'create_dynamic_country':('original_tag',)}


def supported_version(version):
    if version not in SUPPORTED_VERSIONS:
        raise ValueError('game_version_unsupported')
    return '.'.join(version.split('.')[:3])+'.*'


def inspect_game(game):
    game=Path(game)
    if not (game/'hoi4.exe').is_file():raise ValueError('game_executable_missing')
    version=json.loads((game/'launcher-settings.json').read_text(encoding='utf-8-sig')).get('rawVersion')
    supported_version(version)
    fingerprints={};checked=[]
    for kind,required in REQUIRED.items():
        path=game/f'documentation/{kind}s_documentation.md'
        if not path.is_file():raise ValueError('engine_documentation_missing')
        data=path.read_bytes();fingerprints[kind]=sha256(data).hexdigest()
        if fingerprints[kind]!=REFERENCE_DOCS[version][kind]:
            raise ValueError('engine_documentation_changed_requires_review')
        entries=dict(re.findall(r'^## ([A-Za-z_][\w]*)\s*\n(.*?)(?=^## |\Z)',data.decode('utf-8-sig'),re.M|re.S))
        for name,scope in required.items():
            body=entries.get(name)
            if body is None:raise ValueError('engine_command_missing:'+name)
            match=re.search(r'Supported Scopes:\s*([^\r\n]+)',body)
            scopes=set(re.findall(r'[A-Z_]+|any',match[1])) if match else set()
            if scope not in scopes and 'any' not in scopes:
                raise ValueError('engine_command_scope_changed:'+name)
            for parameter in PARAMETERS.get(name,()) if kind=='effect' else ():
                if not re.search(r'\b'+parameter+r'\s*=',body):
                    raise ValueError('engine_command_parameters_changed:'+name)
            checked.append(kind+':'+name)
    return {'version':version,'supported_version':supported_version(version),
            'documentation_sha256':fingerprints,'commands_checked':checked,
            'evidence':'reviewed installed documentation; native gameplay remains unverified for this run',
            'gameplay_confirmed':False}


def descriptor(name,version,path=None):
    if any(c in name for c in ('"','\n','\r')):raise ValueError('invalid_mod_name')
    text=f'name="{name}"\nsupported_version="{supported_version(version)}"\n'
    if path is not None:
        value=Path(path).resolve().as_posix()
        if any(c in value for c in ('"','\n','\r')):raise ValueError('invalid_mod_path')
        text+=f'path="{value}"\n'
    return text
