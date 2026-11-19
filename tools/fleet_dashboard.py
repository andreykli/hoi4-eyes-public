"""Read-only naval observations, reusable templates and manual plans."""
from contextlib import closing
import csv
from functools import lru_cache
import json
from pathlib import Path
import re
import sqlite3
from uuid import uuid4

from flask import jsonify, render_template, request


SHIP_FIELDS = ('ships', 'carrier', 'capital', 'screen', 'submarine', 'fuel_k')
ROLES = ('escort', 'patrol', 'strike', 'submarines', 'repair', 'reserve', 'custom')
SHIP_MATCHES = {'escort': 'screen', 'patrol': 'screen', 'strike': 'capital', 'submarines': 'submarine'}
REGIONAL_ROLES = frozenset(('escort', 'patrol', 'strike', 'submarines'))


def validate_totals(row):
    if not isinstance(row, dict) or set(row) != set(SHIP_FIELDS):
        raise ValueError('invalid_naval_totals')
    if any(type(row[key]) is not int or not 0 <= row[key] <= 100_000
           for key in SHIP_FIELDS[:-1]):
        raise ValueError('invalid_ship_count')
    if any(row[key] > row['ships'] for key in SHIP_FIELDS[1:-1]):
        raise ValueError('naval_group_exceeds_total')
    fuel = row['fuel_k']
    if type(fuel) not in (int, float) or not 0 <= fuel <= 1_000_000_000:
        raise ValueError('invalid_fuel')
    return dict(row)


def parse_totals(values):
    if (len(values) != len(SHIP_FIELDS)
            or any(not re.fullmatch(r'\d{1,6}', v) for v in values[:-1])
            or not re.fullmatch(r'\d{1,10}(?:\.\d{1,3})?', values[-1])):
        raise ValueError('invalid_naval_log_row')
    return validate_totals(dict(zip(SHIP_FIELDS, [*map(int, values[:-1]), float(values[-1])])))


def configured_regions(game, documents, locale):
    from atlas_map_sources import active_map_files
    files = active_map_files(game, documents, strategic_regions=True)
    local = {'en': 'english', 'ru': 'russian', 'fr': 'french'}[locale]
    labels = game / 'localisation' / local / f'strategic_region_names_l_{local}.yml'
    paths = tuple(sorted(path for key, path in files.items() if key.startswith('map/strategicregions/')))
    inputs = (files['map/definition.csv'], *paths)
    signature = tuple((path, path.stat().st_mtime_ns, path.stat().st_size) for path in inputs)
    label_stamp = labels.stat().st_mtime_ns if labels.is_file() else None
    return read_sea_regions(game, signature, labels, label_stamp)


@lru_cache(maxsize=8)
def read_sea_regions(game, signature, labels, label_stamp):
    definition, *_ = signature
    with definition[0].open(encoding='utf-8-sig', errors='replace', newline='') as source:
        province_types = {row[0]: row[4].strip().casefold()
                          for row in csv.reader(source, delimiter=';') if len(row) > 4}
    names = {}
    if label_stamp is not None:
        names = dict(re.findall(r'^\s*(STRATEGICREGION_\d+):\d*\s+"([^"]+)"',
                                labels.read_text(encoding='utf-8-sig'), re.M))
    regions, seen = {}, set()
    for path, _, _ in signature[1:]:
        text = re.sub(r'#.*', '', path.read_text(encoding='utf-8-sig', errors='replace'))
        if not re.match(r'\s*strategic_region\s*=\s*\{', text):
            raise ValueError('invalid_strategic_region')
        identifier = re.findall(r'\bid\s*=\s*([1-9][0-9]{0,8})\b', text)
        provinces = re.findall(r'\bprovinces\s*=\s*\{\s*([0-9\s]+)\}', text)
        if len(identifier) != 1 or identifier[0] in seen or len(provinces) != 1:
            raise ValueError('ambiguous_strategic_region')
        identifier = identifier[0]
        seen.add(identifier)
        ids = provinces[0].split()
        types = {province_types.get(province) for province in ids}
        if None in types or 'sea' not in types:
            continue
        name = names.get(f'STRATEGICREGION_{identifier}') if path.is_relative_to(game) else None
        regions[int(identifier)] = name or re.sub(r'^\d+[- _]*', '', path.stem).strip() or f'#{identifier}'
    return regions


def template_input(body):
    role, name, notes = (body.get(key) for key in ('role', 'name', 'notes'))
    if (role not in ROLES or not isinstance(name, str) or not 1 <= len(name.strip()) <= 80
            or not isinstance(notes, str) or len(notes) > 800):
        raise ValueError('invalid_template')
    return {'role': role, 'name': name.strip(), 'notes': notes}


def build_plan(role, ids, regions, totals):
    if role not in ROLES or not isinstance(ids, list) or len(ids) > 32 or any(
            type(item) is not int or item not in regions for item in ids) or len(ids) != len(set(ids)):
        raise ValueError('invalid_plan')
    if role in REGIONAL_ROLES and not ids:
        raise ValueError('regions_required')
    ship_field = SHIP_MATCHES.get(role)
    fit = 'unknown' if totals is None else 'no_ships' if totals['ships'] == 0 else 'review_in_game'
    if totals and totals['ships'] and ship_field and not totals[ship_field]:
        fit = 'role_group_missing'
    return {'stage': 'draft', 'role': role, 'regions': [{'id': i, 'name': regions[i]} for i in ids],
            'fit': fit, 'delivery': 'unsupported'}


def register(app, db_path, observation, game, documents):
    import ui_locale

    def roster():
        try:
            return configured_regions(game, documents, ui_locale.language()), None
        except (OSError, ValueError, TypeError, IndexError):
            return {}, 'map_unavailable'

    def live():
        row, reason = observation()
        # Only Atlas observations tied to the actual native log can label data live.
        if row is None or not row.get('proof') or not row.get('save_token'):
            return None, reason or 'fresh_native_observation_required'
        return row, None

    @app.get('/fleet')
    def fleet_page():
        return render_template('fleet.html', locale=ui_locale.language(), t=ui_locale.tr, roles=ROLES)

    @app.get('/fleet/state')
    def fleet_state():
        row, reason = live()
        regions, map_issue = roster()
        report = ({key: row[key] for key in ('campaign', 'player', 'day', 'date', 'session')} if row else None)
        totals = row['snapshot'].get('naval') if row else None
        return jsonify(observation=report, totals=totals, fleets=None, task_forces=None,
                       missions=None, repair=None, ports=None, reason=reason, map_issue=map_issue,
                       regions=[{'id': i, 'name': name} for i, name in sorted(regions.items())])

    @app.post('/fleet/plan')
    def fleet_plan():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error='invalid_plan'), 400
        row, _ = live()
        scope = {key: row[key] for key in ('campaign', 'player', 'day', 'session')} if row else None
        if body.get('scope') != scope:
            return jsonify(error='context_changed'), 409
        try:
            regions, _ = roster()
            plan = build_plan(body.get('role'), body.get('regions'), regions,
                              row['snapshot'].get('naval') if row else None)
        except ValueError as error:
            return jsonify(error=str(error)), 400
        return jsonify(**plan, scope=scope)

    @app.route('/fleet/templates', methods=['GET', 'POST', 'DELETE'])
    def fleet_templates():
        if not db_path:
            return jsonify(error='storage_unavailable'), 409
        if request.method == 'GET':
            if not Path(db_path).is_file():
                return jsonify(templates=[])
            uri = Path(db_path).resolve().as_uri() + '?mode=ro'
            with closing(sqlite3.connect(uri, uri=True)) as db:
                if not db.execute("SELECT 1 FROM sqlite_master WHERE name='eyes_fleet_templates'").fetchone():
                    return jsonify(templates=[])
                rows = db.execute('SELECT id,payload FROM eyes_fleet_templates ORDER BY rowid DESC')
                return jsonify(templates=[dict(json.loads(payload), id=i) for i, payload in rows])
        if request.headers.get('Sec-Fetch-Site') not in (None, 'same-origin'):
            return jsonify(error='cross_origin_request'), 403
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error='invalid_template'), 400
        identifier = body.get('id')
        if ((identifier is not None or request.method == 'DELETE')
                and (not isinstance(identifier, str) or not re.fullmatch(r'[a-f0-9]{32}', identifier))):
            return jsonify(error='invalid_template'), 400
        try:
            template = template_input(body) if request.method == 'POST' else None
        except ValueError as error:
            return jsonify(error=str(error)), 400
        with closing(sqlite3.connect(db_path)) as db, db:
            db.execute('CREATE TABLE IF NOT EXISTS eyes_fleet_templates(id TEXT PRIMARY KEY, payload TEXT NOT NULL)')
            if request.method == 'DELETE':
                db.execute('DELETE FROM eyes_fleet_templates WHERE id=?', (identifier,))
                return jsonify(deleted=True)
            if identifier:
                changed = db.execute('UPDATE eyes_fleet_templates SET payload=? WHERE id=?',
                                     (json.dumps(template), identifier)).rowcount
                if not changed:
                    return jsonify(error='invalid_template'), 409
            else:
                identifier = uuid4().hex
                db.execute('INSERT INTO eyes_fleet_templates VALUES (?,?)', (identifier, json.dumps(template)))
        return jsonify(**template, id=identifier)
