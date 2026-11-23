"""Offline subset validator using installed HOI4 docs and pinned MIT CWT data.

Not the CWTools engine: unsupported grammar/scope paths are reported, never
presented as full engine validation. No binaries, game startup or file fixes.
"""
from pathlib import Path
import argparse
import hashlib
import json
import re
from validate_hoi4_gui import parse_pdx, Block, PdxParseError
from third_party.modmaker_balance import _brace_balance
from third_party.live_tools_hints import hint_for

RULES = Path(__file__).parent / 'third_party/cwtools_hoi4/Config'


def documentation_index(path):
    text = Path(path).read_text(encoding='utf-8-sig')
    result = {}
    for name, body in re.findall(r'^## ([a-zA-Z_][\w]*)\s*\n(.*?)(?=^## |\Z)', text, re.M | re.S):
        match = re.search(r'Supported Scopes:\s*([^\r\n]+)', body)
        result[name] = {'scopes': set(re.findall(r'[A-Z_]+|any', match.group(1))) if match else {'any'}}
    return result


def cwt_index(root=RULES):
    """Extract declared literal aliases, without interpreting the CWT language."""
    result = {'effect': set(), 'trigger': set()}
    for path in Path(root).glob('*.cwt'):
        for kind, name in re.findall(r'alias\[(effect|trigger):([a-zA-Z_][\w]*)\]', path.read_text(encoding='utf-8-sig')):
            result[kind].add(name)
    return result


def diplomatic_relation_enum(root=RULES):
    text = (Path(root) / 'shared_enums.cwt').read_text(encoding='utf-8-sig')
    text = re.sub(r'#[^\n]*', '', text)
    match = re.search(r'enum\[diplomatic_relations\]\s*=\s*\{([^}]+)\}', text)
    return set(re.findall(r'[a-z_]\w*', match[1])) if match else set()


def log_diagnostics(path):
    """Read only a bounded error-log tail. Hints are advice, never fixes."""
    with Path(path).open('rb') as stream:
        stream.seek(0, 2);stream.seek(max(0, stream.tell() - 262144))
        lines = stream.read().decode('utf-8-sig', errors='replace').splitlines()
    return [hint for line in lines if (hint := hint_for(line)) is not None]


def audit(mod_root, game_dir, parent_mods=()):
    root, game = Path(mod_root), Path(game_dir)
    docs = {kind: documentation_index(game / f'documentation/{kind}s_documentation.md')
            for kind in ('effect', 'trigger')}
    cwt = cwt_index()
    relation_types = diplomatic_relation_enum()
    findings, trees, declared, native_scripted = [], {}, set(), set()
    def emit(level, code, file, detail):
        findings.append({'severity': level, 'code': code, 'file': str(file), 'detail': detail})
    for folder in ('scripted_effects', 'scripted_triggers'):
        for path in (game / 'common' / folder).glob('*.txt'):
            native_scripted.update(re.findall(r'^([A-Za-z_]\w*)\s*=\s*\{', path.read_text(encoding='utf-8-sig')))
    paths = sorted({*root.rglob('*.txt'), *root.rglob('*.gui'), *root.rglob('*.gfx')})
    for path in paths:
        if '.eyes_install' in path.relative_to(root).parts:
            continue
        try:
            text = path.read_text(encoding='utf-8-sig')
            depth, negative = _brace_balance(text)
            if depth or negative:
                emit('error', 'brace_balance', path, f'depth={depth}, first_negative_line={negative}')
                continue
            if path.parent.name=='countries' and path.parent.parent.name=='common':
                # The generic GUI parser accepts assignments, not vanilla RGB lists.
                colour = re.compile(r'(?m)^(\s*color\s*=\s*)\{\s*(\d+)\s+(\d+)\s+(\d+)\s*\}')
                for match in colour.finditer(text):
                    if any(int(value)>255 for value in match.groups()[1:]):
                        emit('error','country_color',path,'RGB components must be in 0–255')
                text=colour.sub(r'\1verified_rgb_list',text)
            if path.parent.name=='states' and path.parent.parent.name=='history':
                # The generic assignment parser does not represent engine numeric lists.
                text=re.sub(r'\b(provinces|victory_points)\s*=\s*\{\s*[0-9]+(?:\s+[0-9]+)*\s*\}',
                            r'\1 = verified_numeric_list',re.sub(r'#[^\n]*','',text))
            trees[path] = parse_pdx(text)
        except (UnicodeError, PdxParseError) as error:
            emit('error', 'syntax', path, str(error))
    definitions = {}
    for path, tree in trees.items():
        if path.parent.name in {'scripted_effects', 'scripted_triggers'}:
            for key, value in tree.entries:
                if isinstance(value, Block):
                    identity = (path.parent.name, key)
                    if identity in definitions: emit('error', 'duplicate_scripted_definition', path, f'{key} also in {definitions[identity]}')
                    definitions[identity] = str(path)
            declared.update(k for k, v in tree.entries if isinstance(v, Block))
    scripted = declared | native_scripted
    for parent in parent_mods:
        for folder in ('scripted_effects', 'scripted_triggers'):
            for path in (Path(parent) / 'common' / folder).glob('*.txt'):
                scripted.update(k for k, v in parse_pdx(path.read_text(encoding='utf-8-sig')).entries if isinstance(v, Block))
    scope_blocks = {'ROOT', 'FROM', 'PREV', 'THIS', 'owner', 'controller', 'overlord', 'capital_scope'}
    containers = {'if', 'else', 'else_if', 'hidden_effect', 'random', 'random_list', 'effect',
                  'AND', 'OR', 'NOT', 'NOR', 'NAND', 'limit', 'trigger', 'visible', 'available',
                  'allowed', 'enable', 'cancel_trigger', 'remove_trigger', 'activation', 'original_tag'}
    def walk(block, kind, scope, path, weighted=False):
        for key, value in block.entries:
            if key.startswith('@') or key in {'tooltip', 'count', 'random_select_amount', 'display_individual_scopes'}:
                continue
            if key in containers:
                if isinstance(value, Block):
                    next_kind = 'trigger' if key in {'limit', 'trigger', 'visible', 'available', 'allowed', 'enable', 'AND', 'OR', 'NOT', 'NOR', 'NAND', 'cancel_trigger', 'remove_trigger'} else kind
                    walk(value, next_kind, scope, path, weighted=key == 'random_list')
                continue
            if (key in scope_blocks or re.fullmatch(r'[A-Z][A-Z0-9]{2}', key)
                    or key.startswith(('event_target:', 'global_event_target:'))):
                if isinstance(value, Block):
                    walk(value, kind, 'STATE' if key == 'capital_scope' else ('COUNTRY' if key in {'owner', 'controller', 'overlord'} or re.fullmatch(r'[A-Z][A-Z0-9]{2}', key) else 'unknown'), path)
                continue
            if key.isdigit() and isinstance(value, Block):
                walk(value, kind, scope if weighted else 'STATE', path);continue
            entry = docs[kind].get(key)
            if key not in scripted and not entry:
                # CWT may know a newer command than this installed game. It
                # cannot silently establish compatibility with the local build.
                emit('warning' if key in cwt[kind] else 'error',
                     'cwt_only_command' if key in cwt[kind] else 'unknown_' + kind, path, key)
            if entry and scope != 'unknown' and 'any' not in entry['scopes'] and scope not in entry['scopes']:
                emit('error', 'scope', path, f'{key}: {scope}, supported={sorted(entry["scopes"])}')
            if kind == 'effect' and key == 'diplomatic_relation':
                if not isinstance(value, Block):
                    emit('error', 'relation_shape', path, 'diplomatic_relation requires a block')
                else:
                    if value.first('country') is None or value.first('relation') not in relation_types:
                        emit('error', 'relation_parameters', path, 'country and known CWT diplomatic relation required')
                    if value.first('active') not in (None, 'yes', 'no'):
                        emit('error', 'relation_parameters', path, 'active must be yes/no')
            if isinstance(value, Block) and key.startswith(('every_', 'random_', 'any_', 'all_')):
                child = 'STATE' if 'state' in key else 'COUNTRY' if 'country' in key else 'unknown'
                walk(value, kind, child, path)
    event_ids = {}
    for path, tree in trees.items():
        folder = path.parent.name
        if folder == 'scripted_effects':
            for _, body in tree.entries:
                if isinstance(body, Block): walk(body, 'effect', 'unknown', path)
        elif folder == 'scripted_triggers':
            for _, body in tree.entries:
                if isinstance(body, Block): walk(body, 'trigger', 'unknown', path)
        elif folder == 'map_modes':
            outer = tree.first('scripted_map_modes')
            if isinstance(outer, Block):
                for _, mode in outer.entries:
                    if not isinstance(mode, Block): continue
                    for layer_name in ('top', 'bottom'):
                        layer = mode.first(layer_name)
                        if isinstance(layer, Block) and isinstance(layer.first('color'), Block):
                            walk(layer.first('color'), 'trigger', 'COUNTRY', path)
        elif folder == 'scripted_diplomatic_actions':
            outer = tree.first('scripted_diplomatic_actions')
            if isinstance(outer, Block):
                for _, action in outer.entries:
                    if not isinstance(action, Block): continue
                    for field, scope in (('allowed', 'NONE'), ('visible', 'COUNTRY'), ('selectable', 'COUNTRY')):
                        if isinstance(action.first(field), Block):
                            walk(action.first(field), 'trigger', scope, path)
                    if isinstance(action.first('complete_effect'), Block):
                        walk(action.first('complete_effect'), 'effect', 'COUNTRY', path)
        elif folder == 'events':
            for key, body in tree.entries:
                if key not in {'country_event', 'news_event', 'state_event'} or not isinstance(body, Block): continue
                eid = body.first('id')
                if eid in event_ids: emit('error', 'duplicate_event', path, str(eid))
                event_ids[eid] = path
                scope = 'STATE' if key == 'state_event' else 'COUNTRY'
                for field, value in body.entries:
                    if field in {'immediate', 'trigger', 'option'} and isinstance(value, Block):
                        if field == 'option':
                            value = Block([(k, v) for k, v in value.entries if k not in {'name', 'ai_chance', 'highlight', 'default', 'original_tag'}])
                        walk(value, 'trigger' if field == 'trigger' else 'effect', scope, path)
        elif folder in {'decisions', 'categories'}:
            # Decisions are a native execution entry point too. Syntax-only
            # checking missed invalid effects hidden in complete_effect.
            for _, category in tree.entries:
                if not isinstance(category, Block): continue
                bodies = [category] if folder == 'categories' else [
                    v for _, v in category.entries if isinstance(v, Block)]
                for body in bodies:
                    for field, value in body.entries:
                        if not isinstance(value, Block): continue
                        if field in {'visible', 'available', 'allowed', 'remove_trigger', 'cancel_trigger'}:
                            walk(value, 'trigger', 'COUNTRY', path)
                        elif field in {'complete_effect', 'remove_effect', 'cancel_effect', 'timeout_effect'}:
                            walk(value, 'effect', 'COUNTRY', path)
        elif folder == 'on_actions':
            for _, outer in tree.entries:
                if isinstance(outer, Block):
                    for _, action in outer.entries:
                        if isinstance(action, Block):
                            effect = action.first('effect')
                            if isinstance(effect, Block): walk(effect, 'effect', 'unknown', path)
        else:
            emit('info', 'syntax_only', path, 'Semantic rules for this file category not interpreted by EYES subset validator')
    for path in root.rglob('*.yml'):
        if '.eyes_install' in path.relative_to(root).parts:
            continue
        data = path.read_bytes()
        if not data.startswith(b'\xef\xbb\xbf'):
            emit('error', 'localisation_bom', path, 'HOI4 localisation requires UTF-8 BOM')
        try: data.decode('utf-8-sig')
        except UnicodeError as error: emit('error', 'encoding', path, str(error))
    # Scripted diplomatic action titles are implicit engine lookups; checking
    # only explicit references missed EYES_RECOGNIZE_TITLE in real gameplay.
    for path, tree in trees.items():
        if path.parent.name!='scripted_diplomatic_actions':continue
        outer=tree.first('scripted_diplomatic_actions')
        if not isinstance(outer,Block):continue
        for name, action in outer.entries:
            if not name.startswith('eyes_') or not isinstance(action,Block):continue
            required={name.upper()+'_TITLE',name.upper()+'_DESC'}
            if isinstance(action.first('send_description'),str):required.add(action.first('send_description'))
            for locale in ('english','russian','french'):
                keys=set()
                for loc in (root/'localisation'/locale).glob('*.yml'):
                    keys.update(re.findall(r'^\s*([A-Za-z0-9_.-]+):',loc.read_text(encoding='utf-8-sig'),re.M))
                for key in sorted(required-keys):emit('error','missing_diplomatic_localisation',path,locale+':'+key)
    missing = {k: sorted(v - set(docs[k])) for k, v in cwt.items()}
    return {'scope': 'EYES subset static audit; NOT CWTools execution or HOI4 validation',
        'files_parsed': len(trees), 'findings': findings,
        'errors': sum(x['severity'] == 'error' for x in findings),
        'warnings': sum(x['severity'] == 'warning' for x in findings),
        'documentation_sha256': {k: hashlib.sha256((game / f'documentation/{k}s_documentation.md').read_bytes()).hexdigest() for k in docs},
        'cwt_aliases': {k: len(v) for k, v in cwt.items()}, 'cwt_not_in_installed_documentation': missing,
        'cwtools_engine_executed': False}


if __name__ == '__main__':
    p = argparse.ArgumentParser();p.add_argument('--mod', required=True);p.add_argument('--game-dir', required=True);p.add_argument('--output', required=True)
    p.add_argument('--parent-mod', action='append', default=[])
    p.add_argument('--error-log')
    args = p.parse_args();result = audit(args.mod, args.game_dir, args.parent_mod)
    if args.error_log: result['game_log_hints'] = log_diagnostics(args.error_log)
    Path(args.output).parent.mkdir(parents=True, exist_ok=True)
    Path(args.output).write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    print(json.dumps({k: result[k] for k in ('files_parsed', 'errors', 'warnings')}))
    raise SystemExit(bool(result['errors']))
