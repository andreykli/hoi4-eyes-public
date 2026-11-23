from flask import Flask, g, make_response, request, jsonify
import logging
import time
import os
import threading
from runtime_revision import compute_runtime_source_revision
from bridge_auth import BRIDGE_TOKEN_HEADER, bridge_auth_error, load_bridge_token
from diplomacy_engine import ChatConflict, DiplomacyEngine
from ollama_provider import OllamaTextProvider
from chat_state_store import ChatStateStore
from language_detection import configured_game_language, normalize_language


RUNTIME_SOURCE_REVISION = compute_runtime_source_revision()


def _configure_provider_diagnostics():
    logger = logging.getLogger("eyes.response_provider")
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
        logger.addHandler(handler)
    return logger


def create_app(engine=None, response_text_provider=None, state_db_path=None, bridge_auth_token=None):
    app = Flask(__name__)
    bridge_token = load_bridge_token(bridge_auth_token)
    app.config["EYES_BRIDGE_AUTH_CONFIGURED"] = bridge_token is not None
    default_chat_language = configured_game_language()
    diagnostic_logger = _configure_provider_diagnostics()
    if engine is not None:
        diplomacy_engine = engine
        selected_provider = getattr(
            getattr(engine, "response_text_provider", None), "diagnostic_name", "custom"
        ) if getattr(engine, "response_text_provider", None) is not None else "template"
    else:
        provider = response_text_provider
        if provider is None and os.getenv("EYES_RESPONSE_PROVIDER", "template").lower() == "ollama":
            provider = OllamaTextProvider.from_env()
        diplomacy_engine = DiplomacyEngine(response_text_provider=provider)
        selected_provider = getattr(provider, "diagnostic_name", "custom") if provider else "template"
    provider_settings = ""
    if selected_provider == "ollama":
        configured_provider = getattr(diplomacy_engine, "response_text_provider", None)
        provider_settings = " model=%s timeout_seconds=%s" % (
            getattr(configured_provider, "model", "unknown"),
            getattr(configured_provider, "timeout", "unknown"),
        )
    diagnostic_logger.info("provider_selected name=%s%s", selected_provider, provider_settings)
    state_store = ChatStateStore(state_db_path) if state_db_path else None
    state_loaded_from_store = False
    chat_state_lock = threading.RLock()

    @app.before_request
    def measure_diplomacy_start():
        if request.path=='/respond' or request.path.endswith('/advance'):
            request.environ['eyes_started']=time.monotonic()

    @app.after_request
    def measure_diplomacy_end(response):
        started=request.environ.get('eyes_started')
        if started is not None:
            diagnostic_logger.info('diplomacy_http status=%d seconds=%.3f bytes=%d',
                response.status_code,time.monotonic()-started,response.calculate_content_length() or 0)
        return response

    @app.before_request
    def guard_developer_assets():
        if request.path in {'/static/live_qa.js','/static/developer_checks.js'}:
            from developer_access import enabled
            if not enabled():
                return jsonify(error='forbidden'),403

    def require_bridge_auth():
        failure = bridge_auth_error(bridge_token, request.headers.get(BRIDGE_TOKEN_HEADER))
        if failure is None:
            return None
        code, status = failure
        detail = (
            "Set EYES_BRIDGE_AUTH_TOKEN or provision runtime/bridge_auth.token before accepting game evidence."
            if code == "bridge_auth_unconfigured" else
            "This endpoint accepts game observations and execution results only from the configured Bridge."
        )
        return jsonify({"error": code, "detail": detail}), status

    @app.route('/health', methods=['GET'])
    def health():
        """Report the running Flask configuration without touching chat state or probing Ollama."""
        provider_instance = getattr(diplomacy_engine, "response_text_provider", None)
        model = getattr(provider_instance, "model", None) if selected_provider == "ollama" else None
        timeout = getattr(provider_instance, "timeout", None)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            timeout = None
        return jsonify({
            "status": "alive",
            "provider": selected_provider,
            "model": model,
            "provider_timeout_seconds": timeout,
            "ollama_probe": "not_performed",
            "state_persistence_enabled": state_store is not None,
            "state_loaded_from_sqlite": state_loaded_from_store,
            "bridge_auth_configured": bridge_token is not None,
            "runtime_source_revision": RUNTIME_SOURCE_REVISION,
            **({"features": {name: endpoint in app.view_functions for name,endpoint in (
                ('atlas','page'),('diplomacy','get_chat'),('cabinet','meetings_page'),('fleet','fleet_page'))}}
               if request.args.get('features') == '1' else {}),
        })

    @app.before_request
    def serialize_chat_mutations():
        mutating_chat_route = (request.path.startswith("/chats")
                               or request.path.startswith("/campaigns/") or request.path in {
            "/territory-results", "/agreement-executions/result",
            "/settings/autonomous-initiatives",
        }) and request.method != "GET"
        g.eyes_chat_state_lock_acquired = False
        if mutating_chat_route:
            # This lock protects the engine's whole-state SQLite snapshot. Never
            # leave another mutation request waiting behind a slow Ollama call
            # without a bound: the caller can retry the same idempotency key.
            acquired = chat_state_lock.acquire(timeout=0.25)
            g.eyes_chat_state_lock_acquired = acquired
            if not acquired:
                response = make_response(jsonify({
                    "error": "server_busy",
                    "detail": "Another diplomatic update is still running. Retry with the same request_id.",
                    "retryable": True,
                }), 503)
                response.headers["Retry-After"] = "1"
                diagnostic_logger.warning(
                    "chat_state_lock status=busy method=%s path=%s", request.method, request.path,
                )
                return response

    @app.teardown_request
    def release_chat_mutation_lock(_error=None):
        if getattr(g, "eyes_chat_state_lock_acquired", False):
            chat_state_lock.release()
            # Flask's test client can invoke teardown once after the request
            # and again when a preserved context is closed. Make release
            # idempotent so a second teardown cannot release an unowned lock.
            g.eyes_chat_state_lock_acquired = False

    if state_store is not None:
        saved_state = state_store.load()
        if saved_state is not None:
            diplomacy_engine.import_chat_state(saved_state)
            state_loaded_from_store = True

        diplomacy_engine._before_expression_persist = lambda: state_store.save(diplomacy_engine.export_chat_state())

        @app.after_request
        def persist_chat_changes(response):
            mutating_chat_route = (request.path.startswith("/chats")
                                   or request.path.startswith("/campaigns/") or request.path in {
                "/territory-results", "/agreement-executions/result",
                "/settings/autonomous-initiatives",
            }) and request.method != "GET"
            diplomatic_response_route = request.path == "/respond" and request.method == "POST"
            if ((mutating_chat_route or diplomatic_response_route)
                    and 200 <= response.status_code < 300):
                if getattr(g, "eyes_state_persisted_in_route", False):
                    return response
                try:
                    state_store.save(diplomacy_engine.export_chat_state())
                except Exception:
                    diagnostic_logger.exception("chat_state_persist status=failed")
                    return make_response(jsonify({
                        "error": "state_persistence",
                        "detail": "The chat change could not be durably saved; retry the same request_id.",
                    }), 503)
                diagnostic_logger.info("chat_state_persist status=saved")
            return response

    @app.route('/respond', methods=['POST'])
    def respond():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400

        # Preserve the old malformed-response switch for provider error tests.
        if data.get('simulate') == 'malformed':
            from developer_access import enabled
            if not enabled():
                return jsonify(error='forbidden',detail='Developer tools are unavailable.'),403
            return "THIS IS NOT JSON", 200, {'Content-Type': 'text/plain'}

        try:
            return jsonify(diplomacy_engine.respond(data))
        except ValueError as error:
            return jsonify({'error': 'request', 'detail': str(error)}), 400

    def chat_result(operation, success_status=200):
        try:
            response = jsonify(operation())
            response.status_code = success_status
            return response
        except KeyError as error:
            response = jsonify({'error': 'not_found', 'detail': str(error).strip("'")})
            response.status_code = 404
            return response
        except ChatConflict as error:
            detail = str(error)
            payload = {'error': 'conflict', 'detail': detail}
            if detail.startswith('independent_policy_requires_review:'):
                from player_diplomacy import preparation_issue
                payload.update(policy_code=detail, detail=preparation_issue(detail))
            response = jsonify(payload)
            response.status_code = 409
            return response
        except ValueError as error:
            response = jsonify({'error': 'request', 'detail': str(error)})
            response.status_code = 400
            return response

    @app.route('/mechanics', methods=['GET'])
    def mechanic_registry():
        from mechanic_expression import status
        return jsonify({'mechanics':[status(a['action_id']) for a in diplomacy_engine.action_registry.all()],
                        'game_effect_applied':False})

    @app.route('/chats', methods=['POST'])
    def create_chat():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.create_chat(
            data.get('campaign_id'), data.get('player_country', ''),
            data.get('ai_countries'), data.get('country_contexts'),
            normalize_language(data.get('response_language')) or default_chat_language,
            normalize_language(data.get('interface_language')) or default_chat_language,
        ), success_status=201)

    @app.route('/campaigns/<campaign_id>/leaders/native-observation', methods=['POST'])
    def native_leader_observation(campaign_id):
        failure = require_bridge_auth()
        if failure is not None:return failure
        from native_leadership import record
        def operation():
            data=request.get_json(silent=True)
            if not isinstance(data,dict):raise ValueError('Leader observation required')
            return record(diplomacy_engine,campaign_id,data)
        return chat_result(operation)

    @app.route('/chats/<chat_id>/conferences', methods=['GET', 'POST'])
    def conferences(chat_id):
        failure = require_bridge_auth()
        if failure is not None:
            return failure
        from historical_conferences import Conferences, observed_clock
        from historical_people import load_people
        def operation():
            service = Conferences(diplomacy_engine)
            chat = diplomacy_engine._chat(chat_id)
            viewer = None if chat.get('chat_mode') == 'ai_to_ai' else chat['player_country']
            if request.method == 'POST':
                if chat.get('chat_mode') == 'ai_to_ai': raise ValueError('Player cannot control a foreign conference')
                data = request.get_json(silent=True)
                if not isinstance(data, dict): raise ValueError('Conference object required')
                return service.view(service.create(chat_id, data), viewer)
            visible = [service.view(c, viewer) for c in service.for_chat(chat_id)]
            try: clock = service.clock(chat)
            except ValueError: clock = None
            return {'chat_id': chat_id, 'conferences': [c for c in visible if c is not None],
                    'observed_clock': {'day': clock['day'], 'date': clock['date']} if clock else None,
                    'historical_people': [p for p in load_people().values() if p['country'] in chat['participants']],
                    'goals': [a['action_id'] for a in diplomacy_engine.action_registry.all()],
                    'game_leader_export_available': bool(service.state.get('observed_leaders',{}).get(chat['campaign_id'])), 'native_intelligence_available': False}
        return chat_result(operation)

    @app.route('/chats/<chat_id>/conferences/<conference_id>/<operation>', methods=['POST'])
    def conference_operation(chat_id, conference_id, operation):
        failure = require_bridge_auth()
        if failure is not None:
            return failure
        from historical_conferences import Conferences
        def perform():
            service = Conferences(diplomacy_engine)
            chat = diplomacy_engine._chat(chat_id)
            if chat.get('chat_mode') == 'ai_to_ai': raise ValueError('Foreign conference controls are not available to the player')
            viewer = chat['player_country']
            if operation == 'invite':
                data = request.get_json(silent=True)
                if not isinstance(data, dict): raise ValueError('Invitation object required')
                return service.invite(chat_id, conference_id, data.get('person_id'))
            if operation == 'report-response':
                data=request.get_json(silent=True) or {}
                return service.respond_report(chat_id,conference_id,data.get('report_id'),viewer,data.get('response'))
            if operation == 'handoff':
                return service.handoff(chat_id,conference_id)
            if operation == 'advance':
                return service.view(service.advance(chat_id, conference_id, respond=True), viewer)
            if operation == 'close':
                return service.view(service.close(chat_id, conference_id), viewer)
            raise ValueError('Unsupported conference operation')
        return chat_result(perform)

    @app.route('/chats/<chat_id>/languages', methods=['PATCH'])
    def update_chat_languages(chat_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.update_chat_languages(
            chat_id, interface_language=data.get('interface_language'),
            dialogue_language=data.get('dialogue_language'),
        ))

    @app.route('/chats', methods=['GET'])
    def list_chats():
        return chat_result(lambda: {'chats': diplomacy_engine.list_chats(request.args.get('campaign_id'))})

    @app.route('/chats/<chat_id>', methods=['GET'])
    def get_chat(chat_id):
        return chat_result(lambda: diplomacy_engine.get_chat(chat_id))

    @app.route('/chats/<chat_id>/game-actions', methods=['GET', 'POST'])
    def game_actions(chat_id):
        from game_actions import proposal, reported_status
        from guarded_treaty_effects import NEW_ACTIONS

        def operation():
            chat = diplomacy_engine.get_chat(chat_id)
            body = request.get_json(silent=True) if request.method == 'POST' else request.args
            if body is None:
                raise ValueError('Expected a structured proposal request.')
            text = proposal(body.get('action'), chat)
            if request.method == 'POST':
                if body.get('preview') != text or not isinstance(body.get('request_id'), str):
                    raise ValueError('Review the current proposal before sending.')
                return diplomacy_engine.send_chat_message(chat_id, text, body['request_id'])
            return {'preview': text, 'request_id': chat['next_request_id'],
                    'history': [{'action': agreement['action_id'], 'version': agreement['current_version'],
                                 'status': reported_status(agreement)}
                                for agreement in chat.get('agreements', {}).values()
                                if agreement['action_id'] in NEW_ACTIONS]}

        return chat_result(operation)

    @app.route('/chats/<chat_id>/memory', methods=['GET'])
    def get_chat_diplomatic_memory(chat_id):
        return chat_result(lambda: diplomacy_engine.get_chat_diplomatic_memory(chat_id))

    @app.route('/chats/<chat_id>/participants', methods=['PATCH'])
    def update_chat_participants(chat_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.update_chat_participants(
            chat_id, data.get('add'), data.get('remove'), data.get('country_contexts'),
        ))

    @app.route('/chats/<chat_id>/contexts/<country>', methods=['PUT'])
    def update_chat_context(chat_id, country):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.update_chat_country_context(chat_id, country, data))

    @app.route('/campaigns/<campaign_id>/autonomy/<operation>', methods=['POST'])
    def autonomy_operation(campaign_id, operation):
        failure = require_bridge_auth()
        if failure is not None:
            return failure
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected autonomy object.'}), 400
        import v6_autonomy
        def perform():
            memory = diplomacy_engine.diplomatic_memory
            if operation == 'snapshot':
                return v6_autonomy.record_snapshot(memory, campaign_id, data)
            if operation == 'native-snapshot':
                from autonomy_game_transport import accept_native_snapshot
                return accept_native_snapshot(diplomacy_engine, campaign_id, data)
            if operation == 'advance':
                from autonomous_negotiation import advance_negotiation
                return advance_negotiation(diplomacy_engine, campaign_id, data.get('proposal_id'))
            if operation == 'tick':
                return v6_autonomy.tick(diplomacy_engine, campaign_id)
            if operation == 'historical-policy':
                return v6_autonomy.policy(memory, campaign_id, data.get('allowlist'))
            if operation == 'open-chat':
                return v6_autonomy.open_chat(diplomacy_engine, campaign_id, data.get('proposal_id'))
            raise ValueError('Unknown autonomy operation')
        return chat_result(perform)

    @app.route('/campaigns/<campaign_id>/autonomy', methods=['GET'])
    def autonomy_status(campaign_id):
        failure = require_bridge_auth()
        if failure is not None:
            return failure
        from v6_autonomy import status
        return chat_result(lambda: status(diplomacy_engine.diplomatic_memory, campaign_id))

    @app.route('/chats/<chat_id>/game-observations', methods=['POST'])
    def record_game_observation(chat_id):
        auth_failure = require_bridge_auth()
        if auth_failure is not None:
            return auth_failure
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a versioned HOI4 game observation object.'}), 400
        def operation():
            observation = diplomacy_engine.record_game_context_observation(
                chat_id, data.get('country'), data.get('campaign_id'), data.get('request_id'),
                data.get('context'), data.get('schema_version', 5),
            )
            return {
                'chat_id': chat_id, 'campaign_id': observation['campaign_id'],
                'country': observation['country'], 'request_id': observation['request_id'],
                'observation': observation,
            }
        return chat_result(operation, success_status=201)

    @app.route('/campaigns/<campaign_id>/state-integration-observations', methods=['POST'])
    def record_state_integration_observation(campaign_id):
        auth_failure = require_bridge_auth()
        if auth_failure is not None:
            return auth_failure
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a state integration observation object.'}), 400
        def operation():
            observation = diplomacy_engine.record_campaign_integration_observation(
                campaign_id, data.get('player_country'), data.get('request_id'), data.get('context'),
            )
            return {
                'campaign_id': observation['campaign_id'],
                'player_country': observation['player_country'],
                'request_id': observation['request_id'], 'observation': observation,
                'recorded': True,
            }
        return chat_result(operation, success_status=201)

    @app.route('/campaigns/<campaign_id>/state-integration-snapshots', methods=['POST'])
    def record_state_integration_snapshot(campaign_id):
        auth_failure = require_bridge_auth()
        if auth_failure is not None:
            return auth_failure
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a complete state integration snapshot object.'}), 400
        def operation():
            snapshot = diplomacy_engine.record_campaign_integration_snapshot(
                campaign_id, data.get('player_country'), data.get('snapshot_seq'),
                data.get('game_day'), data.get('observations'),
                historical_focuses_enabled=data.get('historical_focuses_enabled'),
                historical_focuses_request_id=data.get('historical_focuses_request_id'),
                authenticated_bridge=True,
            )
            return {
                'campaign_id': snapshot['campaign_id'],
                'player_country': snapshot['player_country'],
                'snapshot_id': snapshot['snapshot_id'],
                'snapshot_seq': snapshot['snapshot_seq'],
                'recorded': True, 'snapshot': snapshot,
            }
        return chat_result(operation, success_status=201)

    @app.route('/campaigns/<campaign_id>/state-integration', methods=['GET'])
    def list_campaign_state_integration(campaign_id):
        return chat_result(lambda: diplomacy_engine.list_campaign_state_integration(
            campaign_id, request.args.get('player_country'),
        ))

    @app.route('/chats/<chat_id>/annexation-readiness', methods=['GET'])
    def chat_annexation_readiness(chat_id):
        return chat_result(lambda: diplomacy_engine.chat_annexation_readiness(
            chat_id, request.args.get('target_tag'),
        ))

    @app.route('/chats/<chat_id>/messages', methods=['POST'])
    def post_chat_message(chat_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.send_chat_message(
            chat_id, data.get('message'), data.get('request_id'),
        ), success_status=201)

    @app.route('/chats/<chat_id>/advance', methods=['POST'])
    def advance_chat(chat_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object with request_id.'}), 400
        return chat_result(lambda: diplomacy_engine.advance_chat(chat_id, data.get('request_id')))

    @app.route('/chats/<chat_id>/responses/<path:response_request_id>/retry', methods=['POST'])
    def retry_chat_response(chat_id, response_request_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected an attempt_id JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.retry_chat_response(
            chat_id, response_request_id, data.get('attempt_id'),
        ))

    @app.route('/chats/<chat_id>/close', methods=['POST'])
    def close_chat(chat_id):
        return chat_result(lambda: diplomacy_engine.close_chat(chat_id))

    @app.route('/chats/<chat_id>/annexation-assessments', methods=['GET'])
    def list_chat_annexation_assessments(chat_id):
        return chat_result(lambda: diplomacy_engine.list_chat_political_annexation_assessments(chat_id))

    @app.route('/chats/<chat_id>/annexation-assessments', methods=['POST'])
    def assess_chat_annexation(chat_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected an annexation assessment JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.assess_chat_political_annexation(
            chat_id,
            operation_id=data.get('operation_id'),
            request_id=data.get('request_id'),
            target_tag=data.get('target_tag'),
            approach=data.get('approach', 'voluntary_offer'),
            facts=data.get('facts', {}),
            evidence_sources=data.get('evidence_sources', {}),
        ), success_status=201)

    @app.route('/chats/<chat_id>/computer-annexation-initiatives', methods=['GET'])
    def list_chat_computer_annexation_initiatives(chat_id):
        return chat_result(lambda: diplomacy_engine.list_chat_computer_annexation_initiatives(chat_id))

    @app.route('/settings/autonomous-initiatives', methods=['GET', 'PATCH'])
    def autonomous_initiative_settings():
        if request.method == 'GET':
            return jsonify(diplomacy_engine.get_autonomous_initiative_settings())
        data = request.get_json(silent=True)
        if not isinstance(data, dict) or set(data) != {'enabled'}:
            return jsonify({
                'error': 'request',
                'detail': 'Expected exactly one Boolean field: enabled.',
            }), 400
        return chat_result(lambda: diplomacy_engine.set_autonomous_initiatives_enabled(
            data['enabled'],
        ))

    @app.route('/chats/<chat_id>/computer-annexation-initiatives/evaluate', methods=['POST'])
    def evaluate_chat_computer_annexation_initiative(chat_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a computer-initiative assessment JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.assess_chat_computer_annexation_initiative(
            chat_id,
            request_id=data.get('request_id'),
            target_tag=data.get('target_tag'),
            snapshot=data.get('snapshot', {}),
            snapshot_evidence_sources=data.get('snapshot_evidence_sources', {}),
            facts=data.get('facts', {}),
            evidence_sources=data.get('evidence_sources', {}),
            approach=data.get('approach', 'voluntary_offer'),
        ), success_status=201)

    @app.route('/chats/<chat_id>/agreements', methods=['GET'])
    def list_chat_agreements(chat_id):
        return chat_result(lambda: {"agreements": diplomacy_engine.list_agreements(chat_id)})

    @app.route('/chats/<chat_id>/news', methods=['GET'])
    def list_chat_news(chat_id):
        return chat_result(lambda: diplomacy_engine.list_chat_news(chat_id))

    @app.route('/news', methods=['GET'])
    def list_world_news():
        return chat_result(lambda: diplomacy_engine.list_world_news(request.args.get('campaign_id')))

    @app.route('/chats/<chat_id>/agreements/formalize-latest', methods=['POST'])
    def formalize_latest_chat_acceptance(chat_id):
        return chat_result(lambda: diplomacy_engine.formalize_latest_acceptance(chat_id))

    @app.route('/chats/<chat_id>/agreements/<agreement_id>/recalculate-confirmations', methods=['POST'])
    def recalculate_chat_agreement_confirmations(chat_id, agreement_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected version and exact source_request_ids.'}), 400

        def operation():
            snapshot = diplomacy_engine.export_chat_state()
            result = diplomacy_engine.recalculate_agreement_confirmations(
                chat_id, agreement_id, expected_version=data.get('expected_version'),
                source_request_ids=data.get('source_request_ids'),
            )
            if state_store is not None:
                try:
                    state_store.save(diplomacy_engine.export_chat_state())
                    g.eyes_state_persisted_in_route = True
                except Exception:
                    diplomacy_engine.import_chat_state(snapshot)
                    raise
            return result

        return chat_result(operation)

    @app.route('/chats/<chat_id>/agreements/<agreement_id>', methods=['GET'])
    def get_chat_agreement(chat_id, agreement_id):
        return chat_result(lambda: diplomacy_engine.get_agreement(chat_id, agreement_id))

    @app.get('/chats/<chat_id>/agreements/<agreement_id>/presentation')
    def present_chat_agreement(chat_id, agreement_id):
        from agreement_manager import agreement_display
        from ui_locale import language
        locale = request.args.get('language') or language()
        if locale not in {'ru','en','fr'}:
            return jsonify(error='unsupported_language'),400
        return chat_result(lambda: agreement_display(
            diplomacy_engine.get_agreement(chat_id, agreement_id),locale))

    @app.route('/treaty-mechanisms', methods=['GET'])
    def treaty_mechanisms():
        from treaty_lifecycle import mechanisms
        return jsonify(mechanisms())

    @app.route('/campaigns/<campaign_id>/inbox', methods=['GET'])
    def diplomatic_inbox(campaign_id):
        from diplomatic_inbox import campaign_inbox
        return chat_result(lambda: campaign_inbox(diplomacy_engine, campaign_id))

    @app.route('/chats/<chat_id>/read', methods=['POST'])
    def read_diplomatic_inbox(chat_id):
        failure = require_bridge_auth()
        if failure is not None: return failure
        from diplomatic_inbox import mark_read
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict): return jsonify({'error': 'request'}), 400
        return chat_result(lambda: mark_read(diplomacy_engine._chat(chat_id), data.get('message_ids')))

    @app.route('/chats/<chat_id>/agreements/<agreement_id>/lifecycle', methods=['GET'])
    def treaty_lifecycle_view(chat_id, agreement_id):
        from treaty_lifecycle import view
        return chat_result(lambda: view(diplomacy_engine._agreement(diplomacy_engine._chat(chat_id), agreement_id)))

    @app.route('/chats/<chat_id>/agreements/<agreement_id>/lifecycle/<operation>', methods=['POST'])
    def treaty_lifecycle_operation(chat_id, agreement_id, operation):
        failure = require_bridge_auth()
        if failure is not None: return failure
        data = request.get_json(silent=True)
        if not isinstance(data, dict): return jsonify({'error': 'request'}), 400
        def perform():
            from treaty_lifecycle import register, observe, record_notice
            from treaty_monitor import compile_monitor
            chat = diplomacy_engine._chat(chat_id)
            agreement = diplomacy_engine._agreement(chat, agreement_id)
            if operation == 'register':
                return register(agreement, version=data.get('version'), request_id=data.get('request_id'))
            if operation == 'observe': return observe(agreement, data)
            if operation == 'prepare-monitor': return compile_monitor(agreement)
            if operation == 'prepare-decision':
                from treaty_decision_package import compile_decision
                return compile_decision(agreement, save_token=data.get('save_token'),
                                        not_after_day=data.get('not_after_day'))
            if operation == 'install-decision':
                from treaty_decision_package import compile_decision
                from treaty_package_install import allowed_install_roots, install
                compiled = compile_decision(agreement, save_token=data.get('save_token'),
                                            not_after_day=data.get('not_after_day'))
                return install(compiled, mod_root=data.get('mod_root'),
                               confirm=data.get('confirm') is True,
                               dry_run=data.get('dry_run') is True,
                               allowed_roots=allowed_install_roots())
            if operation == 'prepare-ai-execution':
                from treaty_ai_execution import compile_ai_execution
                from v6_autonomy import status as autonomy_status
                snapshot = autonomy_status(diplomacy_engine.diplomatic_memory, chat['campaign_id']).get('snapshot')
                return compile_ai_execution(agreement, chat, snapshot,
                    save_token=data.get('save_token'), not_after_day=data.get('not_after_day'),
                    confirm_preapproved_execution=data.get('confirm_preapproved_execution'))
            if operation == 'notice':
                observer = (chat.get('autonomous_origin') or {}).get('human_observer', chat['player_country'])
                if data.get('sender') != observer: raise ValueError('a human cannot issue a notice for another country')
                return record_notice(agreement, version=data.get('version'), sender=data.get('sender'),
                    day=data.get('day'), request_id=data.get('request_id'), confirm=data.get('confirm'), scope=data.get('scope'))
            raise ValueError('unknown lifecycle operation')
        return chat_result(perform)

    @app.route('/chats/<chat_id>/agreements/<agreement_id>/approve', methods=['POST'])
    def approve_chat_agreement(chat_id, agreement_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected agreement version, request_id, and confirm.'}), 400
        return chat_result(lambda: diplomacy_engine.approve_agreement(
            chat_id, agreement_id, version=data.get('version'),
            request_id=data.get('request_id'), confirm=data.get('confirm'),
        ))

    @app.route('/chats/<chat_id>/agreements/<agreement_id>/prepare', methods=['POST'])
    def prepare_chat_agreement(chat_id, agreement_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected idempotent request_id.'}), 400
        return chat_result(lambda: diplomacy_engine.prepare_agreement_execution(
            chat_id, agreement_id, request_id=data.get('request_id'),
        ))

    @app.post('/chats/<chat_id>/agreements/<agreement_id>/native-package')
    def prepare_native_package(chat_id, agreement_id):
        failure = require_bridge_auth()
        if failure is not None:
            return failure
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or type(body.get('version')) is not int:
            return jsonify(error='request', detail='An exact agreement version is required.'), 400

        def compile_current():
            from player_native_actions import DELIVERY_ACTIONS, compile_delivery, saved_packet
            from treaty_bundle import compile_bundle, is_pending
            chat = diplomacy_engine._chat(chat_id)
            agreement = diplomacy_engine._agreement(chat, agreement_id)
            if agreement['current_version'] != body['version']:
                raise ChatConflict('Treaty changed. Review the current conditions again.')
            if agreement.get('action_id') not in DELIVERY_ACTIONS:
                raise ValueError('This action has no bilateral native package.')
            if agreement.get('status') != 'manual_required' or agreement.get('execution_result'):
                raise ValueError('This version has no pending native action.')
            diplomacy_engine._revalidate_signed_policy(chat, agreement)
            if (agreement.get('native_delivery') or {}).get('sent'):
                raise ValueError('The event is already open in HOI4. Confirm it there or check the result.')
            cached = saved_packet(agreement)
            signing = body.get('for_signing') is True
            if cached and not signing:
                return cached
            try:
                observation = current_treaty_observation(chat, for_confirmation=signing)
                if cached:
                    from hoi4_campaign_identity import campaign_id_from_save_token
                    delivery = agreement['native_delivery']
                    if (campaign_id_from_save_token(observation['save_token']) != delivery['native_seed_id']
                            or not delivery['first_day'] <= observation['day'] <= delivery['last_day']):
                        raise ValueError('The prepared package is outside its verified save or game-date window.')
                    return cached
            except ValueError as error:
                if is_pending(agreement) or (agreement.get('execution_package') or {}).get('bundle_term_ids'):
                    raise
                from player_native_actions import preview_zip
                return preview_zip(agreement, chat, str(error))
            if is_pending(agreement):
                package, lifecycle, delivery = compile_bundle(diplomacy_engine, chat, agreement, observation)
                agreement.update(execution_package=package, execution_request_id=delivery['packet']['execution_request_id'],
                                 lifecycle=lifecycle, native_delivery=delivery, block_reason=None)
                return delivery['packet']
            packet, lifecycle, delivery = compile_delivery(agreement, chat, observation)
            agreement['lifecycle'] = lifecycle
            agreement['native_delivery'] = delivery
            return packet

        return chat_result(compile_current)

    def current_treaty_observation(chat, *, for_confirmation=False):
        observation, reason = world_observation(for_confirmation=for_confirmation,
                                                expected_campaign=chat.get('campaign_id'),
                                                expected_player=chat.get('player_country'))
        if observation and (observation.get('campaign') != chat.get('campaign_id')
                            or observation.get('player') != chat.get('player_country')):
            reason = 'treaty_branch_does_not_match_native_source'
        elif observation and observation.get('save_token'):
            return observation
        detail = {
            'treaty_branch_pending': 'Bind the pending Atlas export to the agreement campaign.',
            'treaty_branch_does_not_match_native_source': 'Atlas is showing a different campaign or player. Bind the matching export to the agreement campaign.',
            'native_log_changed_or_stale': 'The Atlas export is stale or the save was reloaded. Export again from the matching save.',
            'game_process_changed': 'The matching Atlas export belongs to a different HOI4 process. Export again from this save.',
            'game_process_ambiguous': 'More than one HOI4 process is running. Leave one game open and export again.',
            'game_session_unavailable': 'Open the matching save in HOI4 first.',
        }.get(reason, 'No verified Atlas export for this agreement. Export from the matching save first.')
        raise ValueError(detail)

    @app.route('/domestic-advice', methods=['POST'])
    def domestic_advice():
        failure = require_bridge_auth()
        if failure is not None:
            return failure
        from domestic_advice import classify
        from hoi4_country_tags import COUNTRY_TAG_RE
        import re
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify({'error': 'request'}), 400
        text, campaign, player = (body.get(key) for key in ('text', 'campaign_id', 'player_country'))
        if (not isinstance(text, str) or not 3 <= len(text.strip()) <= 2000
                or len(text) > 2000 or not isinstance(campaign, str)
                or not re.fullmatch(r'[a-f0-9]{32}', campaign)
                or not isinstance(player, str) or not COUNTRY_TAG_RE.fullmatch(player)):
            return jsonify({'error': 'request'}), 400
        scope = {'campaign_id': campaign, 'player_country': player}
        try:
            observation = current_treaty_observation(scope, for_confirmation=True)
            draft = classify(diplomacy_engine.response_text_provider, text, observation)
            refreshed = current_treaty_observation(scope, for_confirmation=True)
            if any(refreshed.get(key) != observation.get(key)
                   for key in ('campaign', 'player', 'pid', 'session', 'day', 'save_token')):
                raise ValueError('Campaign changed while preparing the brief.')
        except ValueError as error:
            return jsonify({'error': 'campaign_unverified', 'detail': str(error)}), 409
        return jsonify(draft)

    @app.post('/chats/<chat_id>/agreements/<agreement_id>/native-confirmation')
    def native_confirmation(chat_id, agreement_id):
        failure = require_bridge_auth()
        if failure is not None:
            return failure
        body = request.get_json(silent=True)
        if not isinstance(body, dict) or type(body.get('version')) is not int:
            return jsonify(error='request', detail='An exact agreement version is required.'), 400

        def command():
            from pathlib import Path
            from player_native_actions import confirmation_command, saved_packet
            chat = diplomacy_engine._chat(chat_id)
            agreement = diplomacy_engine._agreement(chat, agreement_id)
            if agreement['current_version'] != body['version']:
                raise ChatConflict('Treaty changed. Review the current conditions again.')
            if not saved_packet(agreement):
                raise ValueError('Prepare this exact game package first.')
            observation = current_treaty_observation(chat, for_confirmation=True)
            mod = app.config.get('EYES_TREATY_MOD_ROOT') or (
                Path(__file__).resolve().parents[1] / 'prepared/unified_game_mod/ai_assistant')
            return confirmation_command(agreement, chat, observation, mod)

        return chat_result(command)

    @app.post('/chats/<chat_id>/agreements/<agreement_id>/native-sent')
    def record_native_sent(chat_id, agreement_id):
        failure = require_bridge_auth()
        if failure is not None:
            return failure
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error='request'), 400

        def record():
            agreement = diplomacy_engine._agreement(diplomacy_engine._chat(chat_id), agreement_id)
            delivery = agreement.get('native_delivery') or {}
            if (agreement.get('status') != 'manual_required'
                    or delivery.get('version') != agreement['current_version']
                    or not delivery.get('sent_marker') or body.get('marker') != delivery['sent_marker']):
                raise ChatConflict('No matching prepared dispatch.')
            duplicate = delivery.get('sent') is True
            delivery['sent'] = True
            return {'recorded': True, 'duplicate': duplicate}

        return chat_result(record)

    @app.route('/chats/<chat_id>/agreements/<agreement_id>/terms/<term_id>/prepare', methods=['POST'])
    def prepare_chat_agreement_term(chat_id, agreement_id, term_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected idempotent request_id.'}), 400
        return chat_result(lambda: diplomacy_engine.prepare_agreement_term_execution(
            chat_id, agreement_id, term_id=term_id, request_id=data.get('request_id'),
        ))

    @app.route('/chats/<chat_id>/ultimatum-rejections/<path:refusal_request_id>/prepare', methods=['POST'])
    def prepare_political_ultimatum_rejection(chat_id, refusal_request_id):
        # The package was generated with the final refusal and is inert until
        # the player invokes it; no wargoal or relation is applied here.
        return chat_result(lambda: diplomacy_engine.prepare_political_ultimatum_rejection(
            chat_id, refusal_request_id,
        ))

    @app.route('/chats/<chat_id>/diplomatic-refusals/<path:refusal_request_id>/prepare', methods=['POST'])
    def prepare_diplomatic_refusal_event(chat_id, refusal_request_id):
        return chat_result(lambda: diplomacy_engine.prepare_diplomatic_refusal_event(
            chat_id, refusal_request_id,
        ))

    @app.route('/agreement-executions/pending/<path:request_id>', methods=['GET'])
    def pending_agreement_execution(request_id):
        agreement = diplomacy_engine.pending_agreement_execution(request_id)
        if agreement is None:
            return jsonify({'error': 'not_found', 'detail': 'No matching pending agreement execution.'}), 404
        return jsonify({
            'chat_id': agreement['chat_id'], 'agreement_id': agreement['agreement_id'],
            'campaign_id': agreement.get('campaign_id'),
            'participants': agreement['participants'], 'action_id': agreement['action_id'],
            'execution_request_id': agreement['execution_request_id'],
            'execution_package': agreement['execution_package'],
            **({'term_id': agreement['term_id'], 'agreement_version': agreement['agreement_version']}
               if agreement.get('term_id') else {}),
        })

    @app.route('/agreement-executions/result', methods=['POST'])
    def record_agreement_execution_result():
        auth_failure = require_bridge_auth()
        if auth_failure is not None:
            return auth_failure
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected an agreement result object.'}), 400
        try:
            return jsonify(diplomacy_engine.record_agreement_execution_result(
                data.get('chat_id'), data.get('agreement_id'),
                request_id=data.get('request_id'), status=data.get('status'),
                marker=data.get('marker'), reason=data.get('reason'),
            ))
        except KeyError as error:
            return jsonify({'error': 'not_found', 'detail': str(error).strip("'")}), 404
        except ChatConflict as error:
            return jsonify({'error': 'conflict', 'detail': str(error)}), 409
        except ValueError as error:
            return jsonify({'error': 'request', 'detail': str(error)}), 400

    @app.route('/agreements/state-verification', methods=['POST'])
    def record_agreement_state_verification():
        auth_failure = require_bridge_auth()
        if auth_failure is not None:
            return auth_failure
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request',
                            'detail': 'Expected campaign_id, request_id and evidence.'}), 400
        try:
            return jsonify(diplomacy_engine.record_agreement_state_verification(
                campaign_id=data.get('campaign_id'), request_id=data.get('request_id'),
                evidence=data.get('evidence'),
            ))
        except KeyError as error:
            return jsonify({'error': 'not_found', 'detail': str(error).strip("'")}), 404
        except ChatConflict as error:
            return jsonify({'error': 'conflict', 'detail': str(error)}), 409
        except ValueError as error:
            return jsonify({'error': 'request', 'detail': str(error)}), 400

    @app.route('/agreements/treaty-declined', methods=['POST'])
    def record_treaty_declined():
        auth_failure = require_bridge_auth()
        if auth_failure is not None:
            return auth_failure
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request',
                            'detail': 'Expected agreement_id, version, request_id and marker.'}), 400
        try:
            return jsonify(diplomacy_engine.record_treaty_declined(
                agreement_id=data.get('agreement_id'), version=data.get('version'),
                request_id=data.get('request_id'), marker=data.get('marker'),
            ))
        except KeyError as error:
            return jsonify({'error': 'not_found', 'detail': str(error).strip("'")}), 404
        except ChatConflict as error:
            return jsonify({'error': 'conflict', 'detail': str(error)}), 409
        except ValueError as error:
            return jsonify({'error': 'request', 'detail': str(error)}), 400

    @app.route('/diplomacy/third-country-reaction', methods=['POST'])
    def third_country_reaction():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected speaker, parties, action_id and context.'}), 400
        from third_country_reactions import react
        try:
            result = react(speaker=data.get('speaker'), parties=data.get('parties') or [],
                           action_id=data.get('action_id'), context=data.get('context') or {},
                           language=data.get('language') or 'en',
                           key=str(data.get('key') or ''))
        except ValueError as error:
            return jsonify({'error': 'request', 'detail': str(error)}), 400
        return jsonify({'reaction': result})

    @app.route('/chats/<chat_id>/memory-voice', methods=['GET'])
    def chat_memory_voice(chat_id):
        chat = diplomacy_engine._chat(chat_id)
        from hoi4_country_tags import is_country_tag
        country = (request.args.get('country') or chat.get('player_country') or '').upper()
        counterpart = (request.args.get('counterpart') or '').upper()
        if not is_country_tag(country) or not is_country_tag(counterpart) or country == counterpart:
            return jsonify({'error': 'request', 'detail': 'Two distinct country tags are required.'}), 400
        from diplomatic_memory import pair_summary
        from memory_voice import voice
        summary = pair_summary(diplomacy_engine.diplomatic_memory,
                               campaign_id=chat['campaign_id'],
                               observer_tag=country, counterpart_tag=counterpart)
        result = voice(summary=summary, country_a=country, country_b=counterpart,
                       action_id=request.args.get('action_id') or 'non_aggression_pact',
                       language=chat.get('response_language') or 'en',
                       key=f"{chat_id}:{country}:{counterpart}")
        return jsonify({'voice': result, 'summary': summary})

    @app.route('/diplomacy/reply', methods=['POST'])
    def diplomatic_reply():
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request',
                            'detail': 'Expected stance, countries, action and language.'}), 400
        from diplomatic_response import reply
        memory_summary = None
        if (data.get('campaign_id')
                and os.getenv('EYES_DIPLOMATIC_MEMORY', '0').strip().casefold() in {'1', 'true', 'yes'}):
            from diplomatic_memory import pair_summary
            try:
                memory_summary = pair_summary(
                    diplomacy_engine.diplomatic_memory, campaign_id=data['campaign_id'],
                    observer_tag=str(data.get('country_b') or '').upper(),
                    counterpart_tag=str(data.get('country_a') or '').upper())
            except (KeyError, TypeError, ValueError):
                memory_summary = None
        try:
            result = reply(stance=data.get('stance'), language=data.get('language') or 'en',
                           country_a=str(data.get('country_a') or ''),
                           country_b=str(data.get('country_b') or ''),
                           action_id=data.get('action_id') or 'non_aggression_pact',
                           key=str(data.get('key') or ''),
                           tone=data.get('tone'),
                           avoid_ids=data.get('avoid_ids') or (),
                           memory_summary=memory_summary)
        except ValueError as error:
            return jsonify({'error': 'request', 'detail': str(error)}), 400
        return jsonify({'reply': result})

    @app.route('/diplomatic-events/refusal-result', methods=['POST'])
    def record_diplomatic_refusal_event():
        auth_failure = require_bridge_auth()
        if auth_failure is not None:
            return auth_failure
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a refusal event result object.'}), 400
        return chat_result(lambda: diplomacy_engine.record_diplomatic_refusal_event(
                campaign_id=data.get('campaign_id'), chat_id=data.get('chat_id'),
                refusal_request_id=data.get('refusal_request_id'), marker=data.get('marker'),
            ))

    @app.route('/chats/<chat_id>/territory-proposals', methods=['POST'])
    def create_territory_proposal(chat_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.create_territory_proposal(
            chat_id, data.get('target'), data.get('allocations'), data.get('rationale', ''),
        ), success_status=201)

    @app.route('/chats/<chat_id>/territory-proposals/<proposal_id>/evaluate', methods=['POST'])
    def evaluate_territory_vote(chat_id, proposal_id):
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.evaluate_territory_vote(
            chat_id, proposal_id, data.get('country'),
        ))

    @app.route('/chats/<chat_id>/territory-proposals/<proposal_id>/revisions', methods=['POST'])
    def revise_territory_proposal(chat_id, proposal_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.revise_territory_proposal(
            chat_id, proposal_id, data.get('based_on_version'),
            data.get('allocations'), data.get('rationale', ''),
        ))

    @app.route('/chats/<chat_id>/territory-proposals/<proposal_id>/counter-offers/<country>/adopt', methods=['POST'])
    def adopt_territory_counteroffer(chat_id, proposal_id, country):
        data = request.get_json(silent=True) or {}
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.adopt_territory_counteroffer(
            chat_id, proposal_id, country, data.get('based_on_version'),
        ))

    @app.route('/chats/<chat_id>/territory-proposals/<proposal_id>/vote', methods=['POST'])
    def cast_territory_player_vote(chat_id, proposal_id):
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400
        return chat_result(lambda: diplomacy_engine.cast_territory_player_vote(
            chat_id, proposal_id, data.get('decision'),
        ))

    @app.route('/chats/<chat_id>/territory-proposals/<proposal_id>/prepare-execution', methods=['POST'])
    def prepare_territory_execution(chat_id, proposal_id):
        from world_diplomacy import native_geometry_supported
        if not native_geometry_supported():
            return jsonify(error='unverified_atlas_geometry'), 409
        return chat_result(lambda: diplomacy_engine.prepare_territory_execution(chat_id, proposal_id))

    @app.route('/territory-results', methods=['POST'])
    def record_territory_result():
        auth_failure = require_bridge_auth()
        if auth_failure is not None:
            return auth_failure
        data = request.get_json(silent=True)
        if not isinstance(data, dict):
            return jsonify({'error': 'request', 'detail': 'Expected a JSON object.'}), 400
        fields = ('campaign_id', 'proposal_id', 'plan_id', 'status', 'summary')
        if any(not isinstance(data.get(field), str) or not data[field] for field in fields):
            return jsonify({'error': 'request', 'detail': 'Territory result fields are required.'}), 400
        return chat_result(lambda: diplomacy_engine.record_territory_execution_result(
            data['campaign_id'], data['proposal_id'], data['plan_id'],
            data['status'], data['summary'],
        ))

    @app.get('/desktop')
    def desktop_page():
        from flask import render_template
        from game_actions import catalog, presentation, workspace
        from ui_locale import language, tr
        locale = language()
        from developer_access import enabled
        developer_mode=enabled()
        labels=workspace(locale)
        if not developer_mode:
            labels={k:v for k,v in labels.items() if not k.startswith('qa')}
        return render_template('desktop.html',locale=locale,t=tr,actions=presentation(locale),
                               game_catalog=catalog(locale), workspace=labels, developer_mode=developer_mode)

    from world_diplomacy import register as register_world_diplomacy
    world_observation = register_world_diplomacy(app, state_db_path, require_bridge_auth)
    from government_meetings import register as register_meetings
    def meeting_background(scope, participant):
        if participant['role'] != 'head':
            return None
        from datetime import date
        import re
        from historical_behavior import calendar, evidence
        from historical_people import eligibility
        from historical_people import load_people
        memory = diplomacy_engine.diplomatic_memory.get('historical_diplomacy', {})
        row = memory.get('observed_leaders', {}).get(scope['campaign'], {}).get(scope['player'], {})
        if row.get('day') != scope['day'] or not row.get('leader_id'):
            return None
        person = next((p for p in load_people().values() if p['country'] == scope['player']
                       and row['leader_id'] in p['game_ids']), None)
        if not person or person['name'].casefold() != participant['name'].casefold():
            return None
        native_calendar = scope.get('native_calendar')
        if type(native_calendar) is int and 1 <= native_calendar <= date.max.toordinal():
            historical_day = date.fromordinal(native_calendar).isoformat()
        else:
            historical_day = calendar(scope['date'])
        match = re.fullmatch(r'(\d{4})\.(\d{1,2})\.(\d{1,2})',scope['date'])
        if not historical_day and match:
            try:
                historical_day = date(*map(int,match.groups())).isoformat()
            except ValueError:
                return None
        if not historical_day or eligibility(person,historical_day)!='eligible':
            return None
        item = next((f for f in evidence() if f['person']==person['id'] and f['from']<=historical_day),None)
        return {'claim':item['claim'],'source':item['source']} if item else None
    register_meetings(app, state_db_path, world_observation,
                      lambda: diplomacy_engine.response_text_provider, meeting_background,
                      diplomacy_engine.list_world_news)
    return app


if __name__ == '__main__':
    from user_storage import runtime_dir, DATABASE
    state_path = os.getenv(
        "EYES_STATE_DB",
        str(runtime_dir() / DATABASE),
    )
    create_app(state_db_path=state_path).run(host='127.0.0.1', port=5000)
else:
    app = create_app()
