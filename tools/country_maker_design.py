"""Country Maker profiles compiled into native HOI4 character and localisation assets."""
import base64
import binascii
import re
import struct

IDEOLOGIES = {
    'democratic': 'conservatism', 'fascism': 'fascism_ideology',
    'communism': 'marxism', 'neutrality': 'despotism',
}
PORTRAIT_SIZE = (156, 210)


def clean_text(value, limit=120):
    if not isinstance(value, str) or len(value)>limit or re.search(r'["\\\[\]\$§\x00-\x1f]',value):
        raise ValueError('invalid_country_profile_text')
    return value.strip()


def portrait_asset(encoded):
    width,height=PORTRAIT_SIZE
    size=width*height*4
    if not isinstance(encoded,str) or len(encoded)!=4*((size+2)//3):
        raise ValueError('invalid_country_portrait')
    try:
        pixels=base64.b64decode(encoded,validate=True)
    except (ValueError,binascii.Error) as error:
        raise ValueError('invalid_country_portrait') from error
    if len(pixels)!=size:
        raise ValueError('invalid_country_portrait')
    out=bytearray()
    for row in range(height-1,-1,-1):
        for offset in range(row*width*4,(row+1)*width*4,4):
            red,green,blue,_=pixels[offset:offset+4]
            out.extend((blue,green,red,255))
    return struct.pack('<BBBHHBHHHHBB',0,0,2,0,0,0,0,0,width,height,32,0)+out


def compile_design(plan,tag,namespace,country_name):
    color=plan.get('color','#294A69')
    if not isinstance(color,str) or not re.fullmatch(r'#[A-Fa-f0-9]{6}',color):
        raise ValueError('invalid_country_color')
    initial=plan.get('initial_ideology')
    if initial is not None and (not isinstance(initial,str) or initial not in IDEOLOGIES):
        raise ValueError('invalid_initial_ideology')
    values=plan.get('profiles',{})
    if not isinstance(values,dict) or set(values)-IDEOLOGIES.keys():
        raise ValueError('invalid_country_profiles')
    metadata={}; localisation={}; files={}; characters=[]; sprites=[]; effects=[]; checks=[]
    if initial:
        effects.append(f'set_politics = {{ ruling_party = {initial} }}')
        checks.append(f'has_government = {initial}')
    for ideology,subtype in IDEOLOGIES.items():
        fields=values.get(ideology,{})
        if not isinstance(fields,dict):
            raise ValueError('invalid_country_profile')
        given=clean_text(fields.get('given_name',''),80)
        family=clean_text(fields.get('family_name',''),80)
        title=clean_text(fields.get('title',''),80)
        name=clean_text(fields.get('country_name','')) or country_name
        party=clean_text(fields.get('party',''))
        portrait=fields.get('portrait')
        for suffix in ('','_DEF','_ADJ'):
            localisation[f'{tag}_{ideology}{suffix}']=name
        if party:
            party_key=f'{tag}_{ideology}_party'
            localisation[party_key]=localisation[party_key+'_long']=party
            effects.append(f'set_party_name = {{ ideology = {ideology} name = {party_key} long_name = {party_key}_long }}')
        if (title or portrait) and not (given or family):
            raise ValueError('country_portrait_or_title_requires_leader_name')
        character=f'{namespace}_{ideology}'
        asset=None
        if given or family:
            localisation[character]=' '.join(part for part in (title,given,family) if part)
            portrait_script=''
            if portrait:
                asset=f'gfx/leaders/{tag}/{character}.tga'
                files[asset]=portrait_asset(portrait)
                sprite='GFX_'+character
                sprites.append(f'spriteType = {{ name = "{sprite}" texturefile = "{asset}" }}')
                portrait_script=f'portraits = {{ civilian = {{ large = {sprite} }} }}'
            characters.append(f'{character} = {{ name = {character} {portrait_script} '
                              f'country_leader = {{ ideology = {subtype} expire = "9999.1.1.1" id = -1 }} }}')
            effects.extend((f'recruit_character = {character}',f'promote_character = {character}'))
            checks.append(f'has_country_leader = {{ character = {character} ruling_only = no }}')
            checks.append(f'OR = {{ NOT = {{ has_government = {ideology} }} '
                          f'has_country_leader = {{ character = {character} ruling_only = yes }} }}')
        if fields:
            metadata[ideology]=dict(given_name=given,family_name=family,title=title,country_name=name,
                                    party=party,portrait_file=asset)
    if characters:
        files[f'common/characters/{namespace}.txt']='characters = {\n'+'\n'.join(characters)+'\n}\n'
    if sprites:
        files[f'interface/{namespace}_portraits.gfx']='spriteTypes = {\n'+'\n'.join(sprites)+'\n}\n'
    return files,localisation,'\n'.join(effects),' '.join(checks),dict(color=color.upper(),
        initial_ideology=initial,profiles=metadata)
