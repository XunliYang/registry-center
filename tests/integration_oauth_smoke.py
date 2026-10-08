# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Real start.main + SQLite + verified HTTPS IAM and integration listener.

Run: python tests/integration_oauth_smoke.py
No production config, credentials, network IAM, storage or auth handler mocks.
Temporary runtime data and certificate fixtures stay in the printed directory.
"""
import base64
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import socket
import ssl
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))


def free_port():
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        return sock.getsockname()[1]


def configure(root, main_port, integration_port, iam_port, enabled, iam_secret):
    conf_dir = root / 'etc/conf'
    conf_dir.mkdir(parents=True)
    now = datetime.now(timezone.utc)
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'OAuth smoke localhost')])
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name)
            .public_key(key.public_key()).serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(minutes=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('localhost')]), False)
            .sign(key, hashes.SHA256()))
    (root / 'ca.cer').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    password = secrets.token_urlsafe(24)
    (root / 'server.pem').write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.BestAvailableEncryption(password.encode())))
    (root / 'cert_pwd').write_text(password, encoding='utf-8')
    values = {'ip': '127.0.0.1', 'port': str(main_port), 'enable_https': 'false',
        'verify_client': 'false', 'registry.sign.enabled': 'false', 'signature_validation_enabled': 'false',
        'owner.identity.mode': 'trusted_proxy', 'owner.trusted.proxy.ips': '127.0.0.1',
        'startup.strict.identity': 'true', 'use_vectordb': 'false', 'connection.max': '100',
        'agent_approval_enabled': 'false', 'heartbeat.enabled': 'false', 'broadcast.enabled': 'false',
        'integration.enabled': 'true', 'integration.ip': '127.0.0.1', 'integration.port': str(integration_port),
        'integration.client_cert': 'false', 'integration.auth.mode': 'oauth2_introspection',
        'integration.auth.fingerprint_key': secrets.token_urlsafe(32),
        'integration.oauth2.introspection_uri': f'https://localhost:{iam_port}/introspect',
        'integration.oauth2.client_id': 'registry-validator', 'integration.oauth2.client_secret': iam_secret,
        'integration.oauth2.issuer': f'https://localhost:{iam_port}', 'integration.oauth2.audience': 'registry-center',
        'integration.oauth2.ca_file': str(root / 'ca.cer'), 'integration.oauth2.cache_seconds': '0',
        'integration.oauth2.timeout_seconds': '2', 'integration.ban.threshold': '20',
        'integration.auth.scope_role.registry.read': 'partner_service',
        'integration.auth.scope_role.registry.vendor': 'vendor_agent',
        'integration.token.enabled': str(enabled).lower(),
        'integration.token.endpoint': f'https://localhost:{iam_port}/oauth2/token?api-version=1',
        'integration.token.allowed_scopes': 'registry.read registry.vendor',
        'integration.token.default_scope': 'registry.read', 'integration.token.ca_file': str(root / 'ca.cer'),
        'integration.token.timeout_seconds': '2', 'integration.ratelimit': '1000/second',
        'integration.preratelimit': '1000/second',
        'ssl_certfile': str(root / 'ca.cer'), 'ssl_keyfile': str(root / 'server.pem'),
        'ssl_ca_certs': str(root / 'ca.cer'), 'ssl_keyfile_password': str(root / 'cert_pwd')}
    for name in ('server.conf', 'server.properties'):
        source = name + '.example' if name == 'server.conf' else name
        (conf_dir / name).write_bytes((REPO / 'etc/conf' / source).read_bytes())
    override_config(root, values)
    (conf_dir / 'persistence.conf').write_text(f'persistence.mode=sqlite\nsqlite.path={root / "registry.db"}\n', encoding='utf-8')
    return password


def override_config(root, values):
    """Override only synthetic fixtures, respecting the public file boundary."""
    from common.util.app_config import load_configs
    policies = {}
    load_configs(str(REPO / 'etc/conf/server.properties'), policies)
    for name in ('server.conf', 'server.properties'):
        selected = {k: v for k, v in values.items()
                    if (k in policies) == (name == 'server.properties')}
        path = root / 'etc/conf' / name
        lines = [s for s in path.read_text(encoding='utf-8').splitlines()
                 if s.startswith('#') or '=' not in s
                 or s.split('=', 1)[0].strip().lower() not in values]
        path.write_text('\n'.join(lines) + '\n' +
                        ''.join(f'{k}={v}\n' for k, v in selected.items()), encoding='utf-8')


def run():
    parent = Path(tempfile.mkdtemp(prefix='registry-oauth-smoke-'))
    results = []
    caller_secret, iam_secret = secrets.token_urlsafe(24), secrets.token_urlsafe(24)
    state = {'mode': 'healthy', 'tokens': {}, 'introspection_calls': 0, 'issuance_calls': 0}
    lock = threading.Lock()
    main_port, integration_port, iam_port = free_port(), free_port(), free_port()
    root = parent / 'enabled'
    password = configure(root, main_port, integration_port, iam_port, True, iam_secret)
    issuer = f'https://localhost:{iam_port}'

    class IAM(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, code, data):
            body = json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):
            form = parse_qs(self.rfile.read(int(self.headers.get('Content-Length', 0))).decode())
            with lock:
                if state['mode'] == 'outage':
                    self.send(503, {'error_description': caller_secret})
                    return
                if urlsplit(self.path).path == '/oauth2/token':
                    assert parse_qs(urlsplit(self.path).query) == {'api-version': ['1']}
                    state['issuance_calls'] += 1
                    expected = 'Basic ' + base64.b64encode(f'partner:{caller_secret}'.encode()).decode()
                    if self.headers.get('Authorization') != expected:
                        self.send(401, {'error': 'invalid_client', 'error_description': caller_secret})
                        return
                    token = 'smoke-' + secrets.token_urlsafe(24)
                    scope = form.get('scope', ['registry.read'])[0]
                    state['tokens'][token] = {'active': True, 'sub': 'partner', 'client_id': 'partner',
                        'iss': issuer, 'aud': 'registry-center', 'scope': scope, 'exp': time.time() + 300}
                    self.send(200, {'access_token': token, 'token_type': 'Bearer', 'expires_in': 300,
                                    'scope': scope, 'refresh_token': 'not-forwarded'})
                elif self.path == '/introspect':
                    state['introspection_calls'] += 1
                    expected = 'Basic ' + base64.b64encode(f'registry-validator:{iam_secret}'.encode()).decode()
                    if self.headers.get('Authorization') != expected:
                        self.send(401, {})
                        return
                    self.send(200, state['tokens'].get(form.get('token', [''])[0], {'active': False}))
                else:
                    self.send(404, {})

    iam = ThreadingHTTPServer(('127.0.0.1', iam_port), IAM)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(root / 'ca.cer'), str(root / 'server.pem'), password)
    iam.socket = ctx.wrap_socket(iam.socket, server_side=True)
    iam_thread = threading.Thread(target=iam.serve_forever, daemon=True)
    iam_thread.start()
    env = {k: v for k, v in os.environ.items() if not k.startswith(('REGISTRY_', 'LLM_'))}
    env.update(PYTHONPATH=str(REPO), LLM_CONFIG_FILE=str(root / 'absent-models.yaml'))
    log = (root / 'service.log').open('w', encoding='utf-8')
    proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve', str(root)],
                            cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
    url = f'https://localhost:{integration_port}/integration/v1'

    def expect(name, response, status):
        results.append({'name': name, 'status': response.status_code, 'expected': status})
        assert response.status_code == status, f'{name}: {response.status_code} != {status}'
        return response

    try:
        verify = ssl.create_default_context(cafile=str(root / 'ca.cer'))
        with httpx.Client(verify=verify, trust_env=False, timeout=5) as client:
            deadline = time.monotonic() + 40
            while time.monotonic() < deadline:
                if proc.poll() is not None:
                    raise RuntimeError(f'Registry startup exited; see {root / "service.log"}')
                try:
                    if client.get(url + '/agent-cards').status_code == 401:
                        break
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
            else:
                raise RuntimeError('Registry TLS startup timed out')
            data = {'grant_type': 'client_credentials', 'scope': 'registry.read'}
            acquired = expect('acquire_no_bearer_required', client.post(url + '/oauth2/token',
                data=data, auth=('partner', caller_secret)), 200)
            assert acquired.headers['cache-control'] == 'no-store' and 'refresh_token' not in acquired.json()
            token = acquired.json()['access_token']
            headers = {'Authorization': 'Bearer ' + token}
            expect('read_with_acquired_token', client.get(url + '/agent-cards', headers=headers), 200)
            before = state['introspection_calls']
            expect('read_revalidates_same_token', client.get(url + '/agent-cards', headers=headers), 200)
            assert state['introspection_calls'] == before + 1
            expect('partner_cannot_write', client.post(url + '/agent-cards', headers=headers, json={'agentCards': []}), 403)
            expect('wrong_client_credentials', client.post(url + '/oauth2/token', data=data, auth=('partner', 'wrong')), 401)
            expect('scope_escalation_rejected', client.post(url + '/oauth2/token',
                data=dict(data, scope='registry.admin'), auth=('partner', caller_secret)), 400)
            expect('client_secret_query_rejected', client.post(url + '/oauth2/token', params={'client_secret': caller_secret},
                data=data, auth=('partner', caller_secret)), 400)
            expect('unknown_route_with_query', client.post(
                f'https://localhost:{integration_port}/wrong-token-path',
                params={'client_secret': caller_secret}, data=data), 404)
            expect('proxy_prefix_unknown_route_with_query', client.post(
                f'https://localhost:{integration_port}/proxy/integration/v1/oauth2/token',
                params={'client_secret': caller_secret}, data=data), 404)
            with lock:
                state['tokens'][token]['exp'] = time.time() - 1
            expect('expired_token', client.get(url + '/agent-cards', headers=headers), 401)
            with lock:
                state['tokens'][token]['exp'] = time.time() + 300
                state['tokens'][token]['active'] = False
            expect('revoked_token', client.get(url + '/agent-cards', headers=headers), 401)
            with lock:
                state['tokens'][token]['active'] = True
                state['tokens'][token]['aud'] = 'other-service'
            expect('wrong_audience', client.get(url + '/agent-cards', headers=headers), 401)
            with lock:
                state['tokens'][token]['aud'] = 'registry-center'
                state['mode'] = 'outage'
            for index in range(25):  # exceeds the ban threshold: outage must never ban
                expect(f'introspection_outage_{index}', client.get(url + '/agent-cards', headers=headers), 503)
            expect('acquisition_outage', client.post(url + '/oauth2/token', data=data, auth=('partner', caller_secret)), 503)
            with lock:
                state['mode'] = 'healthy'
            expect('recovery_without_ban', client.get(url + '/agent-cards', headers=headers), 200)
            again = expect('acquisition_not_cached', client.post(url + '/oauth2/token', data=data,
                auth=('partner', caller_secret)), 200)
            assert again.json()['access_token'] != token
            # Exercise durable SQLite writes with the vendor role, not mocked core calls.
            vendor = expect('acquire_vendor_scope', client.post(url + '/oauth2/token',
                data=dict(data, scope='registry.vendor'), auth=('partner', caller_secret)), 200).json()['access_token']
            vendor_headers = {'Authorization': 'Bearer ' + vendor}
            card = {'name': 'OAuthSmokeAgent', 'provider': {'organization': 'SmokeOrg', 'url': 'https://example.org'},
                'description': 'OAuth smoke', 'version': '1.0.0', 'capabilities': {'streaming': False},
                'defaultInputModes': ['text/plain'], 'defaultOutputModes': ['text/plain'], 'skills': []}
            expect('vendor_register_sqlite', client.post(url + '/agent-cards', headers=vendor_headers,
                json={'agentCards': [card]}), 201)
            expect('read_registered_card', client.get(url + '/agent-cards/SmokeOrg/OAuthSmokeAgent', headers=headers), 200)
            expect('vendor_delete_sqlite', client.delete(url + '/agent-cards/SmokeOrg/OAuthSmokeAgent', headers=vendor_headers), 200)
        with httpx.Client(trust_env=False, timeout=5) as main_client:
            expect('token_api_not_exposed_on_main', main_client.post(
                f'http://127.0.0.1:{main_port}/integration/v1/oauth2/token', data=data), 404)
            expect('main_route_with_query_log_redacted', main_client.get(
                f'http://127.0.0.1:{main_port}/rest/v1/registry-center/agent-cards',
                params={'client_secret': caller_secret}), 200)
    finally:
        if proc.poll() is None:
            proc.terminate()
        try:
            proc.wait(timeout=12)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.close()
        iam.shutdown()
        iam.server_close()
        iam_thread.join(timeout=3)
        (parent / 'observations.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
        print('ARTIFACT_ROOT=' + str(parent), flush=True)
    logs = '\n'.join(path.read_text(encoding='utf-8', errors='replace') for path in root.rglob('*.log'))
    for secret in (caller_secret, iam_secret, *state['tokens']):
        assert secret not in logs, 'Credential or token leaked to service/audit logs'
    assert '/wrong-token-path' in logs and '/proxy/integration/v1/oauth2/token' in logs
    assert '/rest/v1/registry-center/agent-cards' in logs, 'Main access path logs must remain enabled'
    # A second real process proves opt-in behavior, not just a mocked singleton.
    disabled = parent / 'disabled'
    disabled_port, disabled_tls_port = free_port(), free_port()
    configure(disabled, disabled_port, disabled_tls_port, iam_port, False, iam_secret)
    env['LLM_CONFIG_FILE'] = str(disabled / 'absent-models.yaml')
    with (disabled / 'service.log').open('w', encoding='utf-8') as disabled_log:
        disabled_proc = subprocess.Popen([sys.executable, str(Path(__file__).resolve()), '--serve', str(disabled)],
            cwd=disabled, env=env, stdout=disabled_log, stderr=subprocess.STDOUT)
        try:
            disabled_context = ssl.create_default_context(cafile=str(disabled / 'ca.cer'))
            with httpx.Client(verify=disabled_context, trust_env=False, timeout=5) as client:
                deadline = time.monotonic() + 40
                while time.monotonic() < deadline:
                    if disabled_proc.poll() is not None:
                        raise RuntimeError('Disabled-profile startup failed')
                    try:
                        response = client.post(f'https://localhost:{disabled_tls_port}/integration/v1/oauth2/token',
                            data=data, auth=('partner', caller_secret))
                        if response.status_code == 404:
                            expect('token_acquisition_disabled', response, 404)
                            break
                    except httpx.HTTPError:
                        pass
                    time.sleep(0.1)
                else:
                    raise RuntimeError('Disabled-profile startup timed out')
        finally:
            if disabled_proc.poll() is None:
                disabled_proc.terminate()
            try:
                disabled_proc.wait(timeout=12)
            except subprocess.TimeoutExpired:
                disabled_proc.kill()
                disabled_proc.wait(timeout=5)
    (parent / 'observations.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print(f'PASS {len(results)} real HTTP assertions; verified TLS, SQLite, revocation and no secret logs')


if __name__ == '__main__':
    if len(sys.argv) == 3 and sys.argv[1] == '--serve':
        from common.util import app_config
        app_config.get_root_path = lambda: sys.argv[2]
        import agent_registry.start as start
        if start.IS_WINDOWS:
            # Test-only endpoint selection, still the real internal service.
            # Linux UDS is already isolated by the child's working directory.
            from agent_registry.internal.tcp_internal_service import TCPInternalService
            start._create_internal_service = lambda config: TCPInternalService(port=0)
        start.main()
    else:
        run()
