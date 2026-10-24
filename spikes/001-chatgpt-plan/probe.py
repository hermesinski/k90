"""Standalone ChatGPT-plan smoke test. Never reads k90 data or API keys."""

import json
import re
import time

API_ORIGIN = 'https://api.openai.com'


class ProbeError(Exception):
    """An intentionally content-free diagnostic safe for a test report."""

    diagnostics: dict[str, int | bool]


def read_stream(stream):
    data = []
    total = 0
    deadline = time.monotonic() + 180
    for raw in stream:
        total += len(raw)
        if total > 2_000_000 or time.monotonic() > deadline:
            raise ProbeError('stream_limit')
        line = raw.decode('utf-8').rstrip('\r\n')
        if line.startswith('data:'):
            data.append(line[5:].lstrip())
        elif not line and data:
            try:
                event = json.loads('\n'.join(data))
            except (ValueError, TypeError):
                raise ProbeError('invalid_stream') from None
            data = []
            if not isinstance(event, dict):
                raise ProbeError('invalid_stream')
            kind = event.get('type')
            if kind in ('response.failed', 'response.incomplete', 'error'):
                detail = event.get('response') or event
                error = detail.get('error', {}) if isinstance(detail, dict) else {}
                code = error.get('code', '') if isinstance(error, dict) else ''
                safe = code if isinstance(code, str) and re.fullmatch(r'[a-z_]{1,100}', code) else 'response_failed'
                raise ProbeError(safe)
            if kind == 'response.completed':
                response = event.get('response')
                if not isinstance(response, dict) or response.get('status') != 'completed':
                    raise ProbeError('invalid_completion')
                return response
    raise ProbeError('stream_not_completed')


def validate_callback(path, expected_state, selected_client=None):
    import hmac
    from urllib.parse import parse_qs, urlsplit
    parsed = urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.path != '/auth/callback':
        raise ProbeError('invalid_callback_path')
    params = parse_qs(parsed.query, keep_blank_values=True)
    if any(len(v) != 1 for v in params.values()):
        raise ProbeError('duplicate_callback_parameter')
    state = params.get('state', [''])[0]
    if not state or not hmac.compare_digest(state, expected_state):
        raise ProbeError('invalid_callback_state')
    if 'error' in params:
        raise ProbeError('authorization_denied')
    code = params.get('code', [''])[0]
    client = params.get('client_id', [selected_client or ''])[0]
    if not code or not client or client == 'dynamic_agent_client':
        raise ProbeError('incomplete_registration')
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,256}', client):
        raise ProbeError('invalid_client_id')
    if selected_client and client != selected_client:
        raise ProbeError('client_id_mismatch')
    return code, client


def validate_identity(tokens, client_id, nonce, jwks):
    import hmac
    import jwt
    try:
        if not {'chatgpt.tokens.use.direct', 'resource.invoke'} <= set(tokens.get('scope', '').split()):
            raise ProbeError('plan_permission_missing')
        if tokens.get('token_type', '').lower() != 'bearer' or not tokens.get('access_token'):
            raise ProbeError('invalid_token_response')
        token = tokens['id_token']
        header = jwt.get_unverified_header(token)
        if header.get('alg') != 'RS256':
            raise ProbeError('invalid_id_token')
        matches = [jwk for jwk in jwks['keys'] if jwk.get('kid') == header.get('kid')]
        if len(matches) != 1:
            raise ProbeError('id_token_key_not_found')
        key = jwt.PyJWK.from_dict(matches[0], algorithm='RS256').key
        claims = jwt.decode(token, key, algorithms=['RS256'], audience=client_id,
                            issuer='https://auth.openai.com', leeway=30,
                            options={'require': ['exp', 'iat', 'sub', 'iss', 'aud', 'nonce']})
        if not isinstance(claims['nonce'], str) or not hmac.compare_digest(claims['nonce'], nonce):
            raise ProbeError('invalid_nonce')
        if not isinstance(claims['sub'], str) or not claims['sub']:
            raise ProbeError('invalid_subject')
        return claims['sub']
    except ProbeError:
        raise
    except Exception:
        raise ProbeError('invalid_id_token') from None


def response_text(response):
    return ''.join(part.get('text', '') for item in response.get('output', [])
                   if item.get('type') == 'message'
                   for part in item.get('content', []) if part.get('type') == 'output_text').strip()


def fixture_png():
    import struct
    import zlib
    def chunk(kind, data):
        return struct.pack('>I', len(data)) + kind + data + struct.pack('>I', zlib.crc32(kind + data) & 0xffffffff)
    pixels = bytearray()
    for y in range(96):
        pixels.append(0)
        for x in range(160):
            color = (255, 255, 255)
            if (x - 40) ** 2 + (y - 48) ** 2 < 25 ** 2:
                color = (255, 0, 0)
            if 100 <= x < 145 and 25 <= y < 70:
                color = (0, 0, 255)
            pixels.extend(color)
    return b'\x89PNG\r\n\x1a\n' + chunk(b'IHDR', struct.pack('>IIBBBBB', 160, 96, 8, 2, 0, 0, 0)) + chunk(b'IDAT', zlib.compress(bytes(pixels))) + chunk(b'IEND', b'')


def run_checks(model, ask):
    import base64
    first = ask(build_payload(model, [{'role': 'user', 'content': 'Reply exactly K90_SUBSCRIPTION_OK and nothing else.'}]))
    if response_text(first) != 'K90_SUBSCRIPTION_OK':
        # Report only counts and booleans, never model content or response IDs.
        text = response_text(first)
        items = first.get('output', [])
        messages = [item for item in items if item.get('type') == 'message']
        parts = [part for item in messages for part in item.get('content', [])]
        error = ProbeError('text_output_mismatch')
        error.diagnostics = {
            'output_items': len(items),
            'message_items': len(messages),
            'reasoning_items': sum(item.get('type') == 'reasoning' for item in items),
            'output_text_items': sum(part.get('type') == 'output_text' for part in parts),
            'refusal_items': sum(part.get('type') == 'refusal' for part in parts),
            'other_content_items': sum(part.get('type') not in ('output_text', 'refusal') for part in parts),
            'extracted_text_characters': len(text),
            'expected_marker_present': 'K90_SUBSCRIPTION_OK' in text,
            'exact_match_ignoring_case': text.casefold() == 'k90_subscription_ok',
            'top_level_output_text_present': isinstance(first.get('output_text'), str),
        }
        raise error
    print('TEXT: PASS', flush=True)
    tools = [{'type': 'namespace', 'name': 'k90_test', 'description': 'Read-only synthetic test data, not a real patient.', 'tools': [
        {'type': 'function', 'name': 'get_demo_meals', 'description': 'Read two fictional meals for an integration test.',
         'strict': True, 'parameters': {'type': 'object', 'properties': {}, 'required': [], 'additionalProperties': False}}
    ]}]
    messages = [{'role': 'user', 'content': 'Call get_demo_meals exactly once. Only after its result, reply with just a JSON object containing total_kcal, the sum of the returned calories. Do not invent meals.'}]
    called = ask(build_payload(model, messages, tools))
    calls = [item for item in called.get('output', []) if item.get('type') == 'function_call']
    if len(calls) != 1:
        raise ProbeError('expected_one_tool_call')
    call = calls[0]
    if call.get('namespace') not in (None, 'k90_test') or call.get('name') not in ('get_demo_meals', 'k90_test.get_demo_meals'):
        raise ProbeError('unexpected_tool')
    if json.loads(call.get('arguments', 'null')) != {} or not call.get('call_id'):
        raise ProbeError('invalid_tool_arguments')
    meals = [{'name': 'Synthetic meal A', 'kcal': 120}, {'name': 'Synthetic meal B', 'kcal': 230}]
    messages = messages + called['output'] + [{'type': 'function_call_output', 'call_id': call['call_id'], 'output': json.dumps({'fictional': True, 'meals': meals})}]
    result = ask(build_payload(model, messages, tools))
    if any(item.get('type') == 'function_call' for item in result.get('output', [])):
        raise ProbeError('unexpected_extra_tool_call')
    if json.loads(response_text(result)) != {'total_kcal': sum(meal['kcal'] for meal in meals)}:
        raise ProbeError('tool_result_mismatch')
    print('TOOL: PASS', flush=True)
    image = 'data:image/png;base64,' + base64.b64encode(fixture_png()).decode()
    seen = ask(build_payload(model, [{'role': 'user', 'content': [
        {'type': 'input_text', 'text': 'Identify the two shapes in this synthetic image. Reply only with a JSON object: left and right. Values must be lowercase English color and shape, e.g. green triangle. No markdown.'},
        {'type': 'input_image', 'image_url': image}
    ]}]))
    if json.loads(response_text(seen)) != {'left': 'red circle', 'right': 'blue square'}:
        raise ProbeError('image_output_mismatch')
    print('IMAGE: PASS', flush=True)
    return {'text': 'PASS', 'tool': 'PASS', 'image': 'PASS'}


def request_json(url, data=None, token=None, form=False, stream=False):
    import http.client
    from urllib.parse import urlencode, urlsplit
    allowed = {
        'https://auth.openai.com/.well-known/openid-configuration',
        'https://auth.openai.com/.well-known/jwks.json',
        'https://auth.openai.com/api/accounts/oauth/token',
        'https://auth.openai.com/api/accounts/oauth/revoke',
        API_ORIGIN + '/v1/models', API_ORIGIN + '/v1/responses',
    }
    if url not in allowed:
        raise ProbeError('endpoint_not_allowed')
    if url.startswith(API_ORIGIN) and not token:
        raise ProbeError('oauth_token_required')
    parsed = urlsplit(url)
    headers = {'Accept': 'text/event-stream' if stream else 'application/json', 'User-Agent': 'k90-subscription-probe/0.1'}
    if token:
        headers['Authorization'] = 'Bearer ' + token
    body = None
    if data is not None:
        body = (urlencode(data) if form else json.dumps(data)).encode()
        headers['Content-Type'] = 'application/x-www-form-urlencoded' if form else 'application/json'
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=180 if stream else 30)
    try:
        connection.request('POST' if body is not None else 'GET', parsed.path, body=body, headers=headers)
        response = connection.getresponse()
        if response.status != 200:
            # No redirects or raw response bodies: neither can leak credentials.
            raise ProbeError('http_' + str(response.status))
        if stream:
            return read_stream(response)
        raw = response.read(2_000_001)
        if len(raw) > 2_000_000:
            raise ProbeError('response_too_large')
        return json.loads(raw) if raw else {}
    finally:
        connection.close()


def revoke(tokens, client_id, request=None):
    request = request or request_json
    refresh = tokens.get('refresh_token')
    if not refresh:
        return False
    request('https://auth.openai.com/api/accounts/oauth/revoke', data={
        'client_id': client_id, 'token': refresh, 'token_type_hint': 'refresh_token'}, form=True)
    return True


def save_private(path, value):
    import os
    import secrets
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_name(path.name + '.' + secrets.token_hex(8) + '.new')
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'w') as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def authorize(state_dir, open_browser=None, request=None):
    import base64
    import hashlib
    import secrets
    import threading
    import uuid
    import webbrowser
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from urllib.parse import urlencode
    request = request or request_json
    open_browser = open_browser or webbrowser.open
    metadata = request('https://auth.openai.com/.well-known/openid-configuration')
    expected = {'issuer': 'https://auth.openai.com',
                'authorization_endpoint': 'https://auth.openai.com/api/accounts/authorize',
                'token_endpoint': 'https://auth.openai.com/api/accounts/oauth/token',
                'jwks_uri': 'https://auth.openai.com/.well-known/jwks.json'}
    if any(metadata.get(key) != value for key, value in expected.items()):
        raise ProbeError('unexpected_oidc_configuration')
    registration_path = state_dir / 'registration.json'
    registration = json.loads(registration_path.read_text()) if registration_path.exists() else {}
    selected = registration.get('client_id')
    host_id = registration.get('host_id') or 'urn:uuid:' + str(uuid.uuid4())
    save_private(registration_path, {**registration, 'host_id': host_id})
    verifier, state, nonce = secrets.token_urlsafe(48), secrets.token_urlsafe(32), secrets.token_urlsafe(32)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    finished = threading.Event()
    outcome = {}

    class Callback(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass  # Default HTTP logs would expose the authorization code.

        def do_GET(self):
            try:
                if len(self.path) > 8192 or self.headers.get('Host') != f'127.0.0.1:{server.server_port}':
                    raise ProbeError('invalid_callback_host')
                result = validate_callback(self.path, state, selected)
                outcome['result'] = result
                self.send_response(200)
                body = b'Login received. You can close this tab and return to the terminal.'
            except ProbeError as exc:
                self.send_response(400)
                body = b'Callback rejected. Return to the terminal.'
                if str(exc) == 'authorization_denied':
                    outcome['error'] = 'authorization_denied'
            self.send_header('Content-Type', 'text/plain; charset=utf-8')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            finally:
                if outcome:
                    finished.set()

    with ThreadingHTTPServer(('127.0.0.1', 0), Callback) as server:
        server.daemon_threads = True
        redirect = f'http://127.0.0.1:{server.server_port}/auth/callback'
        params = {'client_id': selected or 'dynamic_agent_client', 'ext_agent_host_id': host_id,
                  'response_type': 'code', 'redirect_uri': redirect,
                  'scope': 'openid profile email offline_access resource.invoke chatgpt.tokens.use.direct',
                  'resource': API_ORIGIN + '/v1', 'state': state, 'nonce': nonce,
                  'code_challenge_method': 'S256', 'code_challenge': challenge}
        if not selected:
            params['agent_name_hint'] = 'k90 Subscription Test'
        url = expected['authorization_endpoint'] + '?' + urlencode(params)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            print('Continue with ChatGPT: otwieram przeglądarkę na tym komputerze.', flush=True)
            if open_browser(url) is False:
                print('Otwórz ten link lokalnie w przeglądarce (nie wklejaj adresu zwrotnego do czatu):\n' + url, flush=True)
            if not finished.wait(600):
                raise ProbeError('login_timeout')
        finally:
            server.shutdown()
            thread.join(timeout=5)
    if 'error' in outcome:
        raise ProbeError(outcome['error'])
    code, client_id = outcome['result']
    # The issued client ID is metadata, not a token; preserve it before exchange.
    save_private(registration_path, {**registration, 'host_id': host_id, 'client_id': client_id})
    tokens = request(expected['token_endpoint'], data={'grant_type': 'authorization_code',
                     'client_id': client_id, 'code': code, 'code_verifier': verifier,
                     'redirect_uri': redirect, 'resource': API_ORIGIN + '/v1'}, form=True)
    try:
        subject = validate_identity(tokens, client_id, nonce, request(expected['jwks_uri']))
        if registration.get('subject') and subject != registration['subject']:
            raise ProbeError('account_mismatch')
        save_private(registration_path, {'host_id': host_id, 'client_id': client_id, 'subject': subject})
        return tokens, client_id
    except BaseException as exc:
        # Tokens exist but cannot reach main(); carry only a boolean cleanup status.
        setattr(exc, 'renewable_session_revoked', False)
        try:
            setattr(exc, 'renewable_session_revoked', revoke(tokens, client_id, request=request) is True)
        except BaseException:
            pass  # Cleanup failure/cancellation must not replace the original error.
        raise


def build_payload(model, messages, tools=None):
    body = {
        'model': model,
        'input': messages,
        'store': False,
        'stream': True,
        'include': ['reasoning.encrypted_content'],
        'instructions': 'This is a synthetic integration test. Follow the requested output format and answer briefly.',
    }
    if tools:
        body['tools'] = tools
    return body


def main(argv=None):
    import argparse
    from pathlib import Path
    parser = argparse.ArgumentParser(description='Standalone ChatGPT subscription test; no k90 data or paid API fallback.')
    parser.add_argument('--preflight', action='store_true', help='Check public OAuth metadata only; no sign-in or inference.')
    parser.add_argument('--model', help='Exact slug from your account model catalog.')

    args = parser.parse_args(argv)
    if args.preflight:
        metadata = request_json('https://auth.openai.com/.well-known/openid-configuration')
        keys = request_json('https://auth.openai.com/.well-known/jwks.json')
        if metadata.get('issuer') != 'https://auth.openai.com' or not keys.get('keys'):
            raise ProbeError('preflight_failed')
        print('PREFLIGHT: public OAuth metadata and signing keys reachable. No account or inference tested.')
        return 0
    state_dir = Path.home() / '.config' / 'k90-subscription-probe'
    tokens, client_id = {}, None
    report: dict[str, object] = {'verdict': 'PARTIAL', 'tests': 'not_completed'}
    code = 1
    try:
        tokens, client_id = authorize(state_dir)
        print('LOGIN: verified identity and ChatGPT plan permission; tokens stay in memory.', flush=True)
        catalog = request_json(API_ORIGIN + '/v1/models', token=tokens['access_token'])
        models = [m for m in catalog.get('models', []) if m.get('visibility') == 'list'
                  and isinstance(m.get('slug'), str) and re.fullmatch(r'[A-Za-z0-9._:/-]{1,160}', m['slug'])]
        if not models:
            raise ProbeError('no_visible_models')
        print('Dostępne modele:', ', '.join(m['slug'] for m in models), flush=True)
        chosen = args.model
        if not chosen:
            default = next((m['slug'] for m in models if m['slug'] == 'gpt-6.1-sol'), models[0]['slug'])
            chosen = input('Model [' + default + ']: ').strip() or default
        if chosen not in {m['slug'] for m in models}:
            raise ProbeError('model_not_in_account_catalog')
        report['model'] = chosen
        print('Przed testem w https://chatgpt.com/settings/usage wyłącz dla k90 Subscription Test korzystanie z dodatkowych kredytów. Tester nie potrafi zweryfikować tego ustawienia.')
        if input('Kredyty są wyłączone; zgadzam się na maksymalnie 4 żądania w ramach planu [t/N]: ').strip().lower() != 't':
            raise ProbeError('cancelled')
        report['credits_setting_user_confirmed'] = True
        # The OAuth bearer is passed explicitly; no environment API key is read.
        result = run_checks(chosen, lambda payload: request_json(API_ORIGIN + '/v1/responses',
                            data=payload, token=tokens['access_token'], stream=True))
        report.update({'verdict': 'VALIDATED', 'tests': result})
        code = 0
    except KeyboardInterrupt as exc:
        report['error'] = 'cancelled'
        if hasattr(exc, 'renewable_session_revoked'):
            report['renewable_session_revoked'] = getattr(exc, 'renewable_session_revoked') is True
    except Exception as exc:
        # Never print third-party exceptions: they can contain URLs or tokens.
        report['error'] = str(exc) if isinstance(exc, ProbeError) and re.fullmatch(r'[a-z0-9_]{1,100}', str(exc)) else type(exc).__name__
        if isinstance(exc, ProbeError) and hasattr(exc, 'diagnostics'):
            report['diagnostics'] = exc.diagnostics
        if hasattr(exc, 'renewable_session_revoked'):
            report['renewable_session_revoked'] = getattr(exc, 'renewable_session_revoked') is True
    finally:
        if tokens and client_id:
            try:
                report['renewable_session_revoked'] = revoke(tokens, client_id)
            except BaseException:
                report['renewable_session_revoked'] = False
            tokens.clear()
        if report.get('renewable_session_revoked') is False:
            print('UWAGA: nie potwierdzono unieważnienia sesji. Odłącz k90 Subscription Test w ustawieniach ChatGPT.', flush=True)
        save_private(state_dir / 'result.json', report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    print('Raport bez poświadczeń:', state_dir / 'result.json')
    return code


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('Anulowano.')
        raise SystemExit(130)
    except Exception as exc:
        print('Przerwano bez wypisywania danych błędu:', type(exc).__name__)
        raise SystemExit(1)
