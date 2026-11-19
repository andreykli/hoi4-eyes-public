import os
from urllib.parse import quote
import requests
from dotenv import load_dotenv
from bridge_auth import BRIDGE_TOKEN_HEADER, load_bridge_token

load_dotenv()

class AIProvider:
    def record_native_leader(self, campaign_id, payload):
        return self._chat_api('POST', f'/campaigns/{campaign_id}/leaders/native-observation', payload)

    def conferences(self, chat_id):
        try:
            response = requests.get(f'{self.base_url}/chats/{quote(chat_id, safe="")}/conferences',
                                    headers=self._bridge_headers(), timeout=min(self.timeout, 10))
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            return {'error':'conference_unavailable','detail':str(error)}

    def conference_command(self, chat_id, operation, payload, conference_id=None):
        path = f'/chats/{quote(chat_id, safe="")}/conferences'
        if conference_id:
            path += f'/{quote(conference_id, safe="")}/{operation}'
        result = self._chat_api('POST', path, payload)
        if result.get('error'): return result
        snapshot = self.conferences(chat_id)
        snapshot['command_result'] = result
        return snapshot

    def campaign_inbox(self, campaign_id):
        try:
            response = requests.get(f'{self.base_url}/campaigns/{quote(campaign_id, safe="")}/inbox', timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            return {'error': 'inbox_failed', 'detail': str(error)}

    def mark_chat_read(self, chat_id, message_ids):
        try:
            response = requests.post(f'{self.base_url}/chats/{quote(chat_id, safe="")}/read',
                json={'message_ids': message_ids}, headers=self._bridge_headers(), timeout=self.timeout)
            response.raise_for_status()
            return response.json()
        except (requests.RequestException, ValueError) as error:
            return {'error': 'read_ack_failed', 'detail': str(error)}

    def __init__(self, base_url=None, timeout=None, bridge_auth_token=None):
        # Default to the mock server
        self.base_url = base_url or os.getenv('AI_BRIDGE_URL', 'http://127.0.0.1:5000')
        # An explicit Bridge timeout must win over a stale AI_TIMEOUT value.
        # Other callers may omit it and continue to use the environment setting.
        self.timeout = int(timeout if timeout is not None else os.getenv('AI_TIMEOUT', '165'))
        if self.timeout <= 0:
            raise ValueError('AI timeout must be positive')
        self.bridge_auth_token = load_bridge_token(bridge_auth_token)

    def _bridge_headers(self):
        if self.bridge_auth_token is None:
            return None
        return {BRIDGE_TOKEN_HEADER: self.bridge_auth_token}

    def deliver_autonomy_snapshot(self, campaign_id, snapshot):
        result = self._chat_api('POST', f'/campaigns/{campaign_id}/autonomy/native-snapshot', snapshot)
        if 'error' not in result and (result.get('campaign_id') != campaign_id
                or result.get('sequence') != snapshot.get('sequence') or result.get('recorded') is not True):
            return {'error': 'schema', 'detail': 'autonomy acknowledgement identity mismatch'}
        return result

    def record_treaty_observation(self, observation):
        result = self._chat_api('POST', f'/chats/{observation["chat_id"]}/agreements/{observation["agreement_id"]}/lifecycle/observe', observation)
        if 'error' not in result and (result.get('recorded') is not True or any(
                result.get('lifecycle', {}).get(k) != observation[k]
                for k in ('campaign_id', 'chat_id', 'agreement_id', 'version', 'monitor_id'))):
            return {'error': 'schema', 'detail': 'treaty observation acknowledgement identity mismatch'}
        return result

    def send_request(self, context: dict) -> dict:
        url = f"{self.base_url}/respond"
        try:
            r = requests.post(url, json=context, timeout=self.timeout, headers=self._bridge_headers())
            if 400 <= r.status_code < 500:
                detail = r.text
                try:
                    detail = r.json().get('detail', detail)
                except (ValueError, AttributeError):
                    pass
                return {'error': 'request', 'detail': str(detail)}
            r.raise_for_status()
            try:
                data = r.json()
            except ValueError as e:
                raise ValueError(f'Invalid JSON response: {e}') from e
            # Basic validation
            if not isinstance(data, dict) or 'intent' not in data:
                raise ValueError('Invalid response schema')
            if not isinstance(data.get('response', data.get('message')), str):
                raise ValueError('Response must contain a text response')
            if 'decision' in data and not isinstance(data['decision'], str):
                raise ValueError('Decision must be a string')
            if 'confidence' in data and not isinstance(data['confidence'], (int, float)):
                raise ValueError('Confidence must be numeric')
            if 'reasoning_factors' in data and not isinstance(data['reasoning_factors'], list):
                raise ValueError('Reasoning factors must be a list')
            return data
        except requests.exceptions.RequestException as e:
            return { 'error': 'network', 'detail': str(e) }
        except ValueError as e:
            return { 'error': 'schema', 'detail': str(e) }

    def _chat_api(self, method: str, path: str, payload=None, params=None, timeout=None) -> dict:
        """Call the existing persisted group-chat API without owning chat state."""
        url = f"{self.base_url}{path}"
        try:
            if method == "GET":
                response = requests.get(url, params=params, timeout=self.timeout)
            elif method == "PUT":
                response = requests.put(url, json=payload, timeout=self.timeout, headers=self._bridge_headers())
            else:
                response = requests.post(url, json=payload, timeout=self.timeout if timeout is None else timeout, headers=self._bridge_headers())
            if 400 <= response.status_code < 500:
                detail = response.text
                try:
                    detail = response.json().get("detail", detail)
                except (ValueError, AttributeError):
                    pass
                if response.status_code == 404:
                    return {"error": "not_found", "detail": str(detail), "status_code": 404}
                return {"error": "request", "detail": str(detail)}
            response.raise_for_status()
            data = response.json()
            if not isinstance(data, dict):
                raise ValueError("Chat API response must be a JSON object")
            return data
        except requests.exceptions.RequestException as error:
            return {"error": "network", "detail": str(error)}
        except ValueError as error:
            return {"error": "schema", "detail": str(error)}

    def list_chats(self, campaign_id: str) -> dict:
        result = self._chat_api("GET", "/chats", params={"campaign_id": campaign_id})
        if "error" not in result and not isinstance(result.get("chats"), list):
            return {"error": "schema", "detail": "Chat list response is invalid"}
        return result

    def get_chat(self, chat_id: str) -> dict:
        return self._chat_api("GET", f"/chats/{chat_id}")

    def create_chat(self, campaign_id: str, player_country: str, ai_country: str) -> dict:
        result = self._chat_api("POST", "/chats", {
            "campaign_id": campaign_id,
            "player_country": player_country,
            "ai_countries": [ai_country],
        })
        if "error" not in result and (result.get("campaign_id") != campaign_id
                                       or not isinstance(result.get("chat_id"), str)
                                       or not isinstance(result.get("conversation_id"), str)):
            return {"error": "schema", "detail": "Created chat identity is invalid"}
        return result

    def update_chat_context(self, chat_id: str, country: str, context: dict) -> dict:
        """Persist fresh game facts into the existing campaign chat."""
        result = self._chat_api("PUT", f"/chats/{chat_id}/contexts/{country}", context)
        if "error" not in result and result.get("chat_id") != chat_id:
            return {"error": "schema", "detail": "Updated chat context belongs to a different chat"}
        return result

    def record_game_observation(self, chat_id: str, observation: dict) -> dict:
        """Persist a versioned, source-linked HOI4 observation in the existing chat."""
        result = self._chat_api("POST", f"/chats/{chat_id}/game-observations", observation)
        if "error" not in result and (
            result.get("campaign_id") != observation.get("campaign_id")
            or result.get("request_id") != observation.get("request_id")
            or result.get("country") != observation.get("country")
        ):
            return {"error": "schema", "detail": "Stored game observation identity does not match its source"}
        return result

    def record_state_integration_observation(self, observation: dict) -> dict:
        """Persist a state-level observation parsed from a HOI4 game.log marker."""
        campaign_id = observation.get("campaign_id")
        result = self._chat_api(
            "POST", f"/campaigns/{campaign_id}/state-integration-observations", observation,
        )
        if result.get("error") == "not_found":
            return {
                "error": "not_found", "detail": "Flask does not expose the state integration route.",
                "status_code": result.get("status_code", 404),
            }
        if "error" not in result and (
            result.get("campaign_id") != campaign_id
            or result.get("player_country") != observation.get("player_country")
            or result.get("request_id") != observation.get("request_id")
            or result.get("observation", {}).get("source")
            != "bridge_parsed_hoi4_state_integration_v1"
        ):
            return {"error": "schema", "detail": "Stored state integration observation does not match its source"}
        return result

    def record_state_integration_snapshot(self, snapshot: dict) -> dict:
        """Persist one complete read-only state scan as an idempotent batch."""
        campaign_id = snapshot.get("campaign_id")
        result = self._chat_api(
            "POST", f"/campaigns/{campaign_id}/state-integration-snapshots", snapshot,
        )
        if "error" not in result and (
            result.get("campaign_id") != campaign_id
            or result.get("player_country") != snapshot.get("player_country")
            or result.get("snapshot_seq") != snapshot.get("snapshot_seq")
            or result.get("recorded") is not True
        ):
            return {"error": "schema", "detail": "Stored state integration snapshot does not match its source"}
        if ("error" not in result and snapshot.get("historical_focuses_request_id")
                and (
                    result.get("snapshot", {}).get("game_rules", {}).get("request_id")
                    != snapshot.get("historical_focuses_request_id")
                    or result.get("snapshot", {}).get("game_rules", {}).get("historical_focuses_enabled")
                    != snapshot.get("historical_focuses_enabled")
                    or result.get("snapshot", {}).get("game_rules", {}).get("source")
                    != "authenticated_bridge_hoi4_historical_focuses_v1"
                )):
            return {"error": "schema", "detail": "Stored historical-focus fact does not match its Bridge marker"}
        return result

    def get_campaign_state_integration(self, campaign_id: str, player_country: str) -> dict:
        result = self._chat_api(
            "GET", f"/campaigns/{campaign_id}/state-integration",
            params={"player_country": player_country},
        )
        if "error" not in result and (
            result.get("campaign_id") != campaign_id
            or result.get("player_country") != player_country
            or not isinstance(result.get("states"), list)
        ):
            return {"error": "schema", "detail": "Campaign state integration response is invalid"}
        return result

    def post_chat_message(self, chat_id: str, message: str, request_id: str) -> dict:
        return self._chat_api("POST", f"/chats/{chat_id}/messages", {
            "message": message, "request_id": request_id,
        })

    def advance_chat(self, chat_id: str, request_id: str) -> dict:
        return self._chat_api("POST", f"/chats/{chat_id}/advance", {"request_id": request_id})

    def retry_chat_response(self, chat_id: str, response_request_id: str, attempt_id: str) -> dict:
        encoded_request_id = quote(str(response_request_id), safe="")
        result = self._chat_api(
            "POST", f"/chats/{chat_id}/responses/{encoded_request_id}/retry",
            {"attempt_id": attempt_id},
        )
        if not result.get("error") and (
                result.get("attempt_id") != attempt_id
                or result.get("request_id") != response_request_id
                or not isinstance(result.get("chat"), dict)
                or result["chat"].get("chat_id") != chat_id):
            return {"error": "schema", "detail": "Flask returned a mismatched chat retry acknowledgement."}
        return result

    def send_territory_result(self, result: dict) -> dict:
        """Forward a game-log result marker for durable matching in Flask/SQLite."""
        url = f"{self.base_url}/territory-results"
        try:
            response = requests.post(url, json=result, timeout=self.timeout, headers=self._bridge_headers())
            if response.status_code == 404:
                try:
                    body = response.json()
                except ValueError:
                    return {"error": "network", "detail": "territory result endpoint is not loaded yet"}
                if isinstance(body, dict) and body.get("error") == "not_found":
                    return {"error": "request", "detail": body.get("detail", "unknown territory plan")}
            response.raise_for_status()
            data = response.json()
            if (not isinstance(data, dict) or data.get("recorded") is not True
                    or data.get("plan_id") != result.get("plan_id")
                    or data.get("status") != result.get("status")):
                raise ValueError("Invalid territory result acknowledgement")
            return data
        except requests.exceptions.RequestException as error:
            return {"error": "network", "detail": str(error)}
        except ValueError as error:
            return {"error": "schema", "detail": str(error)}

    def get_pending_agreement_execution(self, request_id: str) -> dict:
        """Resolve a V2 marker against the exact prepared agreement request."""
        from urllib.parse import quote

        url = f"{self.base_url}/agreement-executions/pending/{quote(request_id, safe=':')}"
        try:
            response = requests.get(url, timeout=self.timeout)
            if response.status_code == 404:
                return {"error": "not_found"}
            response.raise_for_status()
            data = response.json()
            if (not isinstance(data, dict) or data.get("execution_request_id") != request_id
                    or not isinstance(data.get("agreement_id"), str)
                    or not isinstance(data.get("chat_id"), str)):
                raise ValueError("Invalid pending agreement response")
            return data
        except requests.exceptions.RequestException as error:
            return {"error": "network", "detail": str(error)}
        except ValueError as error:
            return {"error": "schema", "detail": str(error)}

    def send_agreement_execution_result(self, result: dict) -> dict:
        url = f"{self.base_url}/agreement-executions/result"
        try:
            response = requests.post(url, json=result, timeout=self.timeout, headers=self._bridge_headers())
            if response.status_code == 404:
                return {"error": "not_found"}
            response.raise_for_status()
            data = response.json()
            if (not isinstance(data, dict) or data.get("recorded") is not True
                    or data.get("agreement_id") != result.get("agreement_id")
                    or data.get("request_id") != result.get("request_id")):
                raise ValueError("Invalid agreement result acknowledgement")
            return data
        except requests.exceptions.RequestException as error:
            return {"error": "network", "detail": str(error)}
        except ValueError as error:
            return {"error": "schema", "detail": str(error)}

    def send_diplomatic_refusal_result(self, result: dict) -> dict:
        url = f"{self.base_url}/diplomatic-events/refusal-result"
        try:
            response = requests.post(url, json=result, timeout=self.timeout,
                                     headers=self._bridge_headers())
            if response.status_code == 404:
                return {"error": "not_found"}
            response.raise_for_status()
            data = response.json()
            if (not isinstance(data, dict) or data.get("recorded") is not True
                    or data.get("campaign_id") != result.get("campaign_id")
                    or data.get("chat_id") != result.get("chat_id")
                    or data.get("request_id") != result.get("refusal_request_id")):
                raise ValueError("Invalid diplomatic refusal event acknowledgement")
            return data
        except requests.exceptions.RequestException as error:
            return {"error": "network", "detail": str(error)}
        except ValueError as error:
            return {"error": "schema", "detail": str(error)}
