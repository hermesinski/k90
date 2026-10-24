import importlib.util
from pathlib import Path
import io
import json
import unittest

HERE = Path(__file__).resolve().parent


def load_probe():
    spec = importlib.util.spec_from_file_location('k90_subscription_probe', HERE / 'probe.py')
    if not (HERE / 'probe.py').exists():
        raise AssertionError('subscription probe has not been implemented')
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PayloadChecks(unittest.TestCase):
    def test_subscription_payload_requires_stateless_stream(self):
        probe = load_probe()
        self.assertIsNotNone(probe, 'subscription probe has not been implemented')
        payload = probe.build_payload('demo-model', [{'role': 'user', 'content': 'demo'}])
        self.assertIs(payload['store'], False)
        self.assertIs(payload['stream'], True)
        self.assertNotIn('max_output_tokens', payload)
        self.assertEqual(probe.API_ORIGIN, 'https://api.openai.com')
        self.assertEqual(payload['model'], 'demo-model')


class StreamChecks(unittest.TestCase):
    def test_only_completed_response_is_success(self):
        probe = load_probe()
        self.assertTrue(hasattr(probe, 'read_stream'), 'SSE validation is not implemented')
        response = {'status': 'completed', 'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'OK'}]}]}
        event = {'type': 'response.completed', 'response': response}
        wire = ('data: ' + json.dumps(event) + '\n\n').encode()
        self.assertEqual(probe.read_stream(io.BytesIO(wire)), response)
        with self.assertRaises(probe.ProbeError):
            probe.read_stream(io.BytesIO(b'data: {"type":"response.output_text.delta","delta":"looks OK"}\n\n'))
        for kind in ['response.failed', 'response.incomplete', 'error']:
            bad = ('data: ' + json.dumps({'type': kind, 'response': {'error': {'code': 'subscription_sharing_usage_limit_exceeded'}}}) + '\n\n').encode()
            with self.assertRaises(probe.ProbeError):
                probe.read_stream(io.BytesIO(bad))


class StreamAccumulationChecks(unittest.TestCase):
    def test_text_from_deltas_survives_empty_completed_output(self):
        probe = load_probe()
        events = [
            {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': 'K90_SUBSCRIPTION_'},
            {'type': 'response.output_text.delta', 'output_index': 0, 'content_index': 0, 'delta': 'OK'},
            {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}},
        ]
        wire = ''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
        self.assertEqual(probe.response_text(probe.read_stream(io.BytesIO(wire))), 'K90_SUBSCRIPTION_OK')
        truncated = ''.join('data: '+json.dumps(e)+'\n\n' for e in events[:-1]).encode()
        with self.assertRaises(probe.ProbeError):
            probe.read_stream(io.BytesIO(truncated))


    def test_completed_item_snapshots_preserve_tools_reasoning_and_do_not_duplicate_text(self):
        probe = load_probe()
        reasoning = {'type': 'reasoning', 'id': 'synthetic-reasoning', 'encrypted_content': 'synthetic-encrypted'}
        call = {'type': 'function_call', 'namespace': 'k90_test', 'name': 'get_demo_meals',
                'call_id': 'synthetic-call', 'arguments': '{}'}
        message = {'type': 'message', 'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'OK'}]}
        events = [
            {'type': 'response.output_text.delta', 'output_index': 2, 'content_index': 0, 'delta': 'OK'},
            {'type': 'response.output_item.done', 'output_index': 2, 'item': message},
            {'type': 'response.output_item.done', 'output_index': 0, 'item': reasoning},
            {'type': 'response.output_item.done', 'output_index': 1, 'item': call},
            {'type': 'response.completed', 'response': {'status': 'completed', 'output': []}},
        ]
        wire = ''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
        result = probe.read_stream(io.BytesIO(wire))
        self.assertEqual(result['output'], [reasoning, call, message])
        self.assertEqual(probe.response_text(result), 'OK')
        full = {'status': 'completed', 'output': [message]}
        events[-1]['response'] = full
        wire = ''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
        self.assertEqual(probe.read_stream(io.BytesIO(wire)), full)


class CallbackChecks(unittest.TestCase):
    def test_callback_validates_path_state_duplicates_and_client(self):
        probe = load_probe()
        self.assertTrue(hasattr(probe, 'validate_callback'), 'OAuth callback validation is not implemented')
        good = '/auth/callback?state=state1&code=demo-code&client_id=oaiapp_demo'
        self.assertEqual(probe.validate_callback(good, 'state1'), ('demo-code', 'oaiapp_demo'))
        for path in [good.replace('state1', 'wrong'), good + '&state=state1', good.replace('/auth/callback', '/other'), good.replace('demo-code', ''), good.replace('oaiapp_demo', 'dynamic_agent_client')]:
            with self.assertRaises(probe.ProbeError):
                probe.validate_callback(path, 'state1')
        with self.assertRaises(probe.ProbeError):
            probe.validate_callback(good, 'state1', 'oaiapp_other')
        with self.assertRaises(probe.ProbeError):
            probe.validate_callback('/auth/callback?state=state1&error=access_denied', 'state1')


class IdentityChecks(unittest.TestCase):
    def test_signed_identity_nonce_audience_and_grant_are_required(self):
        probe = load_probe()
        self.assertTrue(hasattr(probe, 'validate_identity'), 'OIDC validation is not implemented')
        import jwt
        import time
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        from jwt.algorithms import RSAAlgorithm
        jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        jwk['kid'] = 'demo'
        claims = {'iss': 'https://auth.openai.com', 'sub': 'synthetic-user', 'aud': 'oaiapp_demo', 'iat': int(time.time()), 'exp': int(time.time()) + 300, 'nonce': 'nonce1'}
        signed = jwt.encode(claims, key, algorithm='RS256', headers={'kid': 'demo'})
        tokens = {'id_token': signed, 'access_token': 'synthetic-not-a-live-token', 'token_type': 'Bearer', 'scope': 'resource.invoke chatgpt.tokens.use.direct'}
        self.assertEqual(probe.validate_identity(tokens, 'oaiapp_demo', 'nonce1', {'keys': [jwk]}), 'synthetic-user')
        for client, nonce in [('other', 'nonce1'), ('oaiapp_demo', 'wrong')]:
            with self.assertRaises(probe.ProbeError):
                probe.validate_identity(tokens, client, nonce, {'keys': [jwk]})
        with self.assertRaises(probe.ProbeError):
            probe.validate_identity({**tokens, 'scope': 'openid'}, 'oaiapp_demo', 'nonce1', {'keys': [jwk]})


class SmokeChecks(unittest.TestCase):
    def test_three_checks_use_only_synthetic_inputs_and_one_local_tool(self):
        probe = load_probe()
        self.assertTrue(hasattr(probe, 'run_checks'), 'smoke checks are not implemented')
        requests = []
        answers = [
            {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'K90_SUBSCRIPTION_OK'}]}]},
            {'output': [{'type': 'function_call', 'namespace': 'k90_test', 'name': 'get_demo_meals', 'call_id': 'demo-call', 'arguments': '{}'}]},
            {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"total_kcal":350}'}]}]},
            {'output': [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"left":"red circle","right":"blue square"}'}]}]},
        ]
        def ask(payload):
            requests.append(payload)
            return answers.pop(0)
        self.assertEqual(probe.run_checks('demo-model', ask), {'text': 'PASS', 'tool': 'PASS', 'image': 'PASS'})
        self.assertEqual(len(requests), 4)
        self.assertIn('function_call_output', json.dumps(requests[2]))
        self.assertIn('data:image/png;base64,', json.dumps(requests[3]))
        self.assertEqual(requests[1]['tools'][0]['type'], 'namespace')


    def test_all_smoke_checks_through_sparse_terminal_streams(self):
        probe = load_probe()
        answers = [
            [{'type': 'message', 'content': [{'type': 'output_text', 'text': 'K90_SUBSCRIPTION_OK'}]}],
            [{'type': 'function_call', 'namespace': 'k90_test', 'name': 'get_demo_meals', 'call_id': 'synthetic', 'arguments': '{}'}],
            [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"total_kcal":350}'}]}],
            [{'type': 'message', 'content': [{'type': 'output_text', 'text': '{"left":"red circle","right":"blue square"}'}]}],
        ]
        def ask(payload):
            items = answers.pop(0)
            events = [{'type': 'response.output_item.done', 'output_index': i, 'item': item} for i, item in enumerate(items)]
            events.append({'type': 'response.completed', 'response': {'status': 'completed', 'output': []}})
            wire = ''.join('data: '+json.dumps(e)+'\n\n' for e in events).encode()
            return probe.read_stream(io.BytesIO(wire))
        self.assertEqual(probe.run_checks('demo-model', ask), {'text': 'PASS', 'tool': 'PASS', 'image': 'PASS'})
        self.assertEqual(answers, [])


class DiagnosticChecks(unittest.TestCase):
    def test_text_mismatch_reports_structure_without_model_text_or_credentials(self):
        import tempfile
        from contextlib import redirect_stdout
        from unittest.mock import patch
        probe = load_probe()
        secret_text = 'synthetic-private@example.invalid K90_SUBSCRIPTION_OK.'
        first = {'output': [{'type': 'reasoning'}, {'type': 'message', 'content': [
            {'type': 'output_text', 'text': secret_text}]}]}
        output = io.StringIO()
        def request(url, **kwargs):
            if url.endswith('/v1/models'):
                return {'models': [{'visibility': 'list', 'slug': 'demo-model'}]}
            return first
        with tempfile.TemporaryDirectory() as tmp, \
                patch('pathlib.Path.home', return_value=Path(tmp)), \
                patch.object(probe, 'authorize', return_value=({'access_token': 'synthetic-secret'}, 'oaiapp_demo')), \
                patch.object(probe, 'request_json', side_effect=request), \
                patch.object(probe, 'revoke', return_value=True), \
                patch('builtins.input', side_effect=['demo-model', 't']), redirect_stdout(output):
            self.assertEqual(probe.main([]), 1)
            saved = (Path(tmp)/'.config/k90-subscription-probe/result.json').read_text()
        report = json.loads(saved)
        self.assertEqual(report['error'], 'text_output_mismatch')
        self.assertEqual(report['diagnostics']['output_items'], 2)
        self.assertEqual(report['diagnostics']['message_items'], 1)
        self.assertEqual(report['diagnostics']['output_text_items'], 1)
        self.assertEqual(report['diagnostics']['extracted_text_characters'], len(secret_text))
        self.assertIs(report['diagnostics']['expected_marker_present'], True)
        self.assertIs(report['diagnostics']['exact_match_ignoring_case'], False)
        self.assertIs(report['renewable_session_revoked'], True)
        self.assertNotIn(secret_text, saved + output.getvalue())
        self.assertNotIn('synthetic-secret', saved + output.getvalue())
        self.assertTrue(all(type(v) in (bool, int) for v in report['diagnostics'].values()))


class AuthorizationChecks(unittest.TestCase):
    def test_real_loopback_callback_and_signature_checks_keep_tokens_out_of_files(self):
        probe = load_probe()
        self.assertTrue(hasattr(probe, 'authorize'), 'interactive authorization is not implemented')
        import tempfile
        import time
        from urllib.parse import urlsplit, parse_qs, urlencode
        from urllib.request import urlopen
        import jwt
        from cryptography.hazmat.primitives.asymmetric import rsa
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        from jwt.algorithms import RSAAlgorithm
        jwk = RSAAlgorithm.to_jwk(key.public_key(), as_dict=True)
        jwk['kid'] = 'demo'
        pending = {}
        def browser(url):
            q = parse_qs(urlsplit(url).query)
            self.assertEqual(q['code_challenge_method'], ['S256'])
            self.assertIn('chatgpt.tokens.use.direct', q['scope'][0].split())
            self.assertEqual(urlsplit(q['redirect_uri'][0]).hostname, '127.0.0.1')
            pending.update({k: v[0] for k,v in q.items()})
            callback = q['redirect_uri'][0] + '?' + urlencode({'state': q['state'][0], 'code': 'demo-code', 'client_id': 'oaiapp_demo'})
            with urlopen(callback, timeout=5) as result:
                self.assertEqual(result.status, 200)
        def request(url, **kwargs):
            if url.endswith('/.well-known/openid-configuration'):
                return {'issuer': 'https://auth.openai.com', 'authorization_endpoint': 'https://auth.openai.com/api/accounts/authorize', 'token_endpoint': 'https://auth.openai.com/api/accounts/oauth/token', 'jwks_uri': 'https://auth.openai.com/.well-known/jwks.json'}
            if url.endswith('/.well-known/jwks.json'):
                return {'keys': [jwk]}
            if url.endswith('/oauth/token'):
                data = kwargs['data']
                self.assertEqual(data['client_id'], 'oaiapp_demo')
                self.assertEqual(data['redirect_uri'], pending['redirect_uri'])
                claims = {'iss': 'https://auth.openai.com', 'sub': 'synthetic-user', 'aud': 'oaiapp_demo', 'iat': int(time.time()), 'exp': int(time.time()) + 300, 'nonce': pending['nonce']}
                return {'access_token': 'synthetic-access-token-never-real', 'refresh_token': 'synthetic-refresh-token-never-real', 'token_type': 'Bearer', 'scope': 'resource.invoke chatgpt.tokens.use.direct', 'id_token': jwt.encode(claims, key, algorithm='RS256', headers={'kid':'demo'})}
            self.fail('Unexpected endpoint')
        with tempfile.TemporaryDirectory() as tmp:
            tokens, client = probe.authorize(Path(tmp), open_browser=browser, request=request)
            self.assertEqual(client, 'oaiapp_demo')
            self.assertTrue(tokens['access_token'].startswith('synthetic-'))
            saved = ''.join(p.read_text() for p in Path(tmp).glob('*.json'))
            self.assertNotIn('synthetic-access-token', saved)
            self.assertNotIn('synthetic-refresh-token', saved)
            self.assertNotIn('id_token', saved)
            self.assertEqual((Path(tmp)/'registration.json').stat().st_mode & 0o777, 0o600)


class AuthorizationCleanupChecks(unittest.TestCase):
    def check_failed_authorization(self, interrupted, revoke_failure=False):
        import tempfile
        from contextlib import redirect_stdout
        from unittest.mock import patch
        from urllib.parse import parse_qs, urlencode, urlsplit
        from urllib.request import urlopen

        probe = load_probe()
        original = KeyboardInterrupt() if interrupted else probe.ProbeError('invalid_id_token')
        tokens = {'access_token': 'synthetic-cleanup-access-secret',
                  'refresh_token': 'synthetic-cleanup-refresh-secret',
                  'id_token': 'synthetic-cleanup-id-secret'}
        secrets = tuple(tokens.values())
        revocations = []
        caught = []
        authorize = probe.authorize

        def browser(url):
            query = parse_qs(urlsplit(url).query)
            callback = query['redirect_uri'][0] + '?' + urlencode({
                'state': query['state'][0], 'code': 'synthetic-code', 'client_id': 'oaiapp_demo'})
            with urlopen(callback, timeout=5) as response:
                self.assertEqual(response.status, 200)
            return True

        def request(url, **kwargs):
            if url.endswith('/.well-known/openid-configuration'):
                return {'issuer': 'https://auth.openai.com',
                        'authorization_endpoint': 'https://auth.openai.com/api/accounts/authorize',
                        'token_endpoint': 'https://auth.openai.com/api/accounts/oauth/token',
                        'jwks_uri': 'https://auth.openai.com/.well-known/jwks.json'}
            if url.endswith('/oauth/token'):
                return tokens
            if url.endswith('/.well-known/jwks.json'):
                if interrupted:
                    raise original
                return {'keys': []}
            if url.endswith('/oauth/revoke'):
                revocations.append(kwargs)
                if revoke_failure:
                    raise ValueError(secrets[1])
                return {}
            self.fail('Unexpected endpoint: no real OAuth or inference allowed')

        def authorize_failure(state_dir):
            try:
                return authorize(state_dir, open_browser=browser, request=request)
            except BaseException as exc:
                caught.append(exc)
                raise

        output = io.StringIO()
        with tempfile.TemporaryDirectory(dir=HERE) as tmp, \
                patch('pathlib.Path.home', return_value=Path(tmp)), \
                patch('http.client.HTTPSConnection', side_effect=AssertionError('external network forbidden')), \
                patch.object(probe, 'authorize', side_effect=authorize_failure), \
                patch.object(probe, 'validate_identity', side_effect=original), \
                redirect_stdout(output):
            self.assertEqual(probe.main([]), 1)
            saved = (Path(tmp) / '.config/k90-subscription-probe/result.json').read_text()
            report = json.loads(saved)
            all_saved = ''.join(path.read_text() for path in Path(tmp).rglob('*.json'))

        self.assertEqual(len(revocations), 1, 'issued refresh token must be revoked after failure/cancellation')
        self.assertEqual(revocations[0]['data'], {
            'client_id': 'oaiapp_demo', 'token': secrets[1], 'token_type_hint': 'refresh_token'})
        self.assertEqual(caught, [original], 'cleanup must preserve the original exception object')
        self.assertIs(getattr(caught[0], 'renewable_session_revoked', None), not revoke_failure)
        self.assertIs(report['renewable_session_revoked'], not revoke_failure)
        self.assertEqual(report['error'], 'cancelled' if interrupted else 'invalid_id_token')
        self.assertEqual('UWAGA:' in output.getvalue(), revoke_failure)
        for secret in secrets:
            self.assertNotIn(secret, all_saved + output.getvalue() + str(caught[0]))
        for field in ('access_token', 'refresh_token', 'id_token'):
            self.assertNotIn(field, report)

    def test_jwks_keyboard_interrupt_revokes_and_reports_without_returned_tokens(self):
        self.check_failed_authorization(interrupted=True)

    def test_validation_failure_and_failed_revocation_preserve_error_and_warn(self):
        self.check_failed_authorization(interrupted=False, revoke_failure=True)


    def test_interrupt_during_final_revocation_still_warns_and_saves_report(self):
        import tempfile
        from contextlib import redirect_stdout
        from unittest.mock import patch
        probe = load_probe()
        output = io.StringIO()
        with tempfile.TemporaryDirectory() as tmp, \
                patch('pathlib.Path.home', return_value=Path(tmp)), \
                patch.object(probe, 'authorize', return_value=({'access_token': 'synthetic', 'refresh_token': 'synthetic-refresh'}, 'oaiapp_demo')), \
                patch.object(probe, 'request_json', return_value={'models': [{'visibility': 'list', 'slug': 'demo-model'}]}), \
                patch.object(probe, 'run_checks', return_value={'text': 'PASS', 'tool': 'PASS', 'image': 'PASS'}), \
                patch('builtins.input', side_effect=['demo-model', 't']), \
                patch.object(probe, 'revoke', side_effect=KeyboardInterrupt), \
                redirect_stdout(output):
            probe.main([])
            report = json.loads((Path(tmp)/'.config/k90-subscription-probe/result.json').read_text())
        self.assertIs(report['renewable_session_revoked'], False)
        self.assertIn('UWAGA:', output.getvalue())


class TransportChecks(unittest.TestCase):
    def test_no_key_fallback_and_no_credential_redirects(self):
        probe = load_probe()
        self.assertTrue(hasattr(probe, 'request_json'), 'restricted transport is not implemented')
        from unittest.mock import Mock, patch
        import os
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'synthetic-paid-api-key'}):
            with self.assertRaises(probe.ProbeError):
                probe.request_json('https://api.openai.com/v1/models')
        with self.assertRaises(probe.ProbeError):
            probe.request_json('https://example.com/v1/models', token='synthetic-oauth-token')
        connection = Mock()
        response = Mock()
        response.status = 302
        response.read.return_value = b''
        connection.getresponse.return_value = response
        with patch('http.client.HTTPSConnection', return_value=connection) as factory:
            with self.assertRaises(probe.ProbeError):
                probe.request_json('https://api.openai.com/v1/models', token='synthetic-oauth-token')
            self.assertEqual(factory.call_count, 1)
            self.assertEqual(connection.request.call_args.args[:2], ('GET', '/v1/models'))
        self.assertTrue(connection.close.called)


class CliChecks(unittest.TestCase):
    def test_help_does_not_start_sign_in(self):
        import subprocess
        import sys
        result = subprocess.run([sys.executable, str(HERE/'probe.py'), '--help'], capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0)
        self.assertIn('--preflight', result.stdout)
        self.assertIn('--model', result.stdout)


if __name__ == '__main__':
    unittest.main()
