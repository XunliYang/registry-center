# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for RC-AUTH-01 through RC-AUTH-07.

All credentials and certificates are synthetic, services bind loopback,
and external IAM is never contacted. PASS anchors corrected behavior.
"""
import asyncio
import base64
import io
import json
import logging
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ssl
import threading
import time
from types import SimpleNamespace

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from limits import parse, storage, strategies

import agent_registry.integration.app as app
import agent_registry.integration.authn as authn
import agent_registry.integration.token_acquisition as acquisition
from agent_registry.integration.authn import AuthenticationContext, Credential, ScopeRoleMapper
from common.util.authenticate_util import AuthenticationError, AuthFailureReason
from agent_registry.integration.ban import BanTracker
from agent_registry.integration.listener import ThirdPartyAccessServer
from agent_registry.integration.jwks import VerifiedJwkClient
from common.custom.custom_handle import HandlerRegistry
from common.custom.interface_type import InterfaceType
from common.util.authenticate_util import CallerRole, CallerType, Principal
from tests.integration_oauth_smoke import configure, free_port


class FakeIssuer(acquisition.TokenAcquisitionProvider):
    def __init__(self):
        self.calls = 0
        self.closed = False

    async def acquire(self, credentials, scopes):
        self.calls += 1
        return acquisition.AcquiredToken('review-synthetic-token', 300, scopes)

    async def aclose(self):
        self.closed = True


@pytest.fixture
def isolated_app(monkeypatch):
    monkeypatch.setattr(app, '_ban_tracker', BanTracker(threshold=3))
    monkeypatch.setattr(app, '_tp_prerate_item', parse('10000/second'))
    monkeypatch.setattr(app, '_tp_rate_item', parse('10000/second'))
    monkeypatch.setattr(app, '_tp_limiter', strategies.MovingWindowRateLimiter(storage.MemoryStorage()))
    monkeypatch.setattr(app, '_tp_prerate_limiter', strategies.MovingWindowRateLimiter(storage.MemoryStorage()))
    async def noop(*args, **kwargs):
        pass
    monkeypatch.setattr(app, 'audit_integration', noop)
    monkeypatch.setattr(app, 'audit_integration_failure', noop)
    monkeypatch.setattr(HandlerRegistry, '_instances', {})
    from tests.fakes.integration import FakeRegistry
    monkeypatch.setattr(app, 'get_registry_dependency', lambda: FakeRegistry())
    return TestClient(app.integration_app, raise_server_exceptions=False)


@pytest.mark.parametrize('failure', [httpx.ReadTimeout('synthetic IAM timeout'),
                                   httpx.ConnectError('synthetic IAM outage'),
                                   RuntimeError('synthetic private adapter fault'), None])
def test_custom_auth_network_outage_does_not_ban(monkeypatch, isolated_app, tmp_path, failure):
    class BusinessAuth(authn.AuthenticationProvider):
        provider_id = 'review_custom_iam'
        credential_kind = 'bearer'
        healthy = False
        async def authenticate(self, credential, context):
            if not self.healthy:
                if failure is not None:
                    raise failure
                return None
            return Principal(client_ip=context.client_ip, identity='review-client',
                caller_type=CallerType.INTEGRATION, role=CallerRole.PARTNER_SERVICE)
    provider = BusinessAuth()
    monkeypatch.setattr(authn, '_CUSTOM_PROVIDERS', {provider.provider_id: provider})
    monkeypatch.setattr(authn, '_CUSTOM_EXTRACTORS', {provider.provider_id: authn.BearerTokenExtractor('review-key')})
    handler = authn.ThirdPartyAuthnHandler(config={'integration.auth.mode': provider.provider_id,
        'integration.credential.file': str(tmp_path / 'no-credentials.conf')})
    monkeypatch.setitem(HandlerRegistry._instances, InterfaceType.INTEGRATION_AUTHENTICATE.value, handler)
    statuses = [isolated_app.get('/integration/v1/agent-cards',
        headers={'Authorization': 'Bearer review-token'}).status_code for _ in range(3)]
    provider.healthy = True
    recovered = isolated_app.get('/integration/v1/agent-cards',
        headers={'Authorization': 'Bearer review-token'}).status_code
    assert statuses == [503, 503, 503] and recovered == 200
    assert not app._ban_tracker._failures


@pytest.mark.asyncio
async def test_jwks_https_redirect_cannot_fetch_http_key(monkeypatch, tmp_path):
    for name in ('HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY', 'http_proxy', 'https_proxy', 'all_proxy'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('NO_PROXY', 'localhost,127.0.0.1')
    tls_port, http_port = free_port(), free_port()
    password = configure(tmp_path / 'pki', free_port(), free_port(), tls_port, False, 'review-service-secret')
    signing_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    jwk = jwt.algorithms.RSAAlgorithm.to_jwk(signing_key.public_key(), as_dict=True)
    jwk.update(kid='http-provided-key', use='sig', alg='RS256')
    calls = {'https': 0, 'http': 0}
    state = {'redirect': True, 'keys': [jwk]}
    class Redirect(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            calls['https'] += 1
            if not state['redirect']:
                body = json.dumps({'keys': state['keys']}).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            self.send_response(302)
            self.send_header('Location', f'http://127.0.0.1:{http_port}/jwks')
            self.send_header('Content-Length', '0')
            self.end_headers()
    class Keys(BaseHTTPRequestHandler):
        def log_message(self, *_): pass
        def do_GET(self):
            calls['http'] += 1
            body = json.dumps({'keys': [jwk]}).encode()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Content-Length', str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    tls_server = ThreadingHTTPServer(('127.0.0.1', tls_port), Redirect)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(tmp_path / 'pki/ca.cer'), str(tmp_path / 'pki/server.pem'), password)
    tls_server.socket = ctx.wrap_socket(tls_server.socket, server_side=True)
    plain_server = ThreadingHTTPServer(('127.0.0.1', http_port), Keys)
    threads = [threading.Thread(target=server.serve_forever, daemon=True) for server in (tls_server, plain_server)]
    for thread in threads: thread.start()
    try:
        provider = authn.JwtBearerProvider('https://review-issuer.example', 'registry-center',
            f'https://localhost:{tls_port}/jwks', ['RS256'], ScopeRoleMapper({'registry.admin': 'nms_oss'}),
            ca_file=str(tmp_path / 'pki/ca.cer'))
        token = jwt.encode({'iss': 'https://review-issuer.example', 'aud': 'registry-center',
                           'sub': 'review-subject', 'scope': 'registry.admin', 'exp': int(time.time()) + 60},
                           signing_key, algorithm='RS256', headers={'kid': 'http-provided-key'})
        with pytest.raises(AuthenticationError) as error:
            await provider.authenticate(Credential('bearer', token), AuthenticationContext('loopback'))
        assert error.value.reason == AuthFailureReason.PROVIDER_UNAVAILABLE
        assert calls == {'https': 1, 'http': 0}
        # A healthy trusted key set authenticates, caches, and rotates on kid miss.
        state['redirect'] = False
        assert (await provider.authenticate(Credential('bearer', token),
                AuthenticationContext('loopback'))).role == CallerRole.NMS_OSS
        await provider.authenticate(Credential('bearer', token), AuthenticationContext('loopback'))
        assert calls['https'] == 2
        new_key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
        new_jwk = jwt.algorithms.RSAAlgorithm.to_jwk(new_key.public_key(), as_dict=True)
        new_jwk.update(kid='rotated-key', use='sig', alg='RS256')
        state['keys'] = [new_jwk]
        rotated = jwt.encode({'iss': 'https://review-issuer.example', 'aud': 'registry-center',
            'sub': 'review-subject', 'scope': 'registry.admin', 'exp': int(time.time()) + 60},
            new_key, algorithm='RS256', headers={'kid': 'rotated-key'})
        await provider.authenticate(Credential('bearer', rotated), AuthenticationContext('loopback'))
        assert calls['https'] == 3
        with pytest.raises(AuthenticationError) as removed:
            await provider.authenticate(Credential('bearer', token), AuthenticationContext('loopback'))
        assert removed.value.reason == AuthFailureReason.INVALID_TOKEN
        state['redirect'] = True
        provider._jwks._expires_at = 0
        with pytest.raises(AuthenticationError) as unavailable:
            await provider.authenticate(Credential('bearer', rotated), AuthenticationContext('loopback'))
        assert unavailable.value.reason == AuthFailureReason.PROVIDER_UNAVAILABLE
        assert calls['http'] == 0
        await provider.aclose()
    finally:
        for server in (tls_server, plain_server):
            server.shutdown()
            server.server_close()
        for thread in threads: thread.join(timeout=3)


def test_client_budget_limits_iam_issuance_before_call(monkeypatch, isolated_app):
    provider = FakeIssuer()
    monkeypatch.setattr(acquisition, '_service', acquisition.TokenAcquisitionService(provider, 'registry.read'))
    monkeypatch.setattr(acquisition, '_configured', True)
    monkeypatch.setattr(app, '_tp_rate_item', parse('1/day'))
    basic = 'Basic ' + base64.b64encode(b'review-client:review-secret').decode()
    statuses = [isolated_app.post('/integration/v1/oauth2/token', headers={'Authorization': basic},
                                 data={'grant_type': 'client_credentials', 'scope': 'registry.read'}).status_code
                for _ in range(3)]
    assert statuses == [200, 429, 429] and provider.calls == 1


def test_spoofed_id_cannot_spend_authenticated_client_budget(monkeypatch, isolated_app):
    class VerifyingIssuer(FakeIssuer):
        async def acquire(self, credentials, scopes):
            if credentials.client_secret != 'valid-secret':
                self.calls += 1
                raise acquisition.TokenAcquisitionError('invalid_client', 401)
            return await super().acquire(credentials, scopes)
    provider = VerifyingIssuer()
    monkeypatch.setattr(acquisition, '_service', acquisition.TokenAcquisitionService(provider, 'registry.read'))
    monkeypatch.setattr(acquisition, '_configured', True)
    monkeypatch.setattr(app, '_tp_rate_item', parse('1/day'))
    def acquire(secret):
        basic = 'Basic ' + base64.b64encode(f'review-client:{secret}'.encode()).decode()
        return isolated_app.post('/integration/v1/oauth2/token', headers={'Authorization': basic},
                                 data={'grant_type': 'client_credentials', 'scope': 'registry.read'})
    assert acquire('wrong-secret').status_code == 401
    assert acquire('wrong-secret').status_code == 429
    assert acquire('valid-secret').status_code == 200
    assert provider.calls == 2
    # Changing an untrusted secret cannot cause issuance after identity exhaustion.
    assert acquire('another-secret').status_code == 429
    assert provider.calls == 2


@pytest.mark.asyncio
async def test_custom_issuance_obeys_configured_deadline_and_cancels(monkeypatch):
    class Slow(FakeIssuer):
        cancelled = False
        async def acquire(self, credentials, scopes):
            try:
                await asyncio.sleep(0.08)
                return await super().acquire(credentials, scopes)
            except asyncio.CancelledError:
                self.cancelled = True
                raise
    monkeypatch.setattr(acquisition, '_service', None)
    monkeypatch.setattr(acquisition, '_configured', False)
    monkeypatch.setattr(acquisition, '_FACTORIES', {'review_slow': lambda conf: Slow()})
    acquisition.configure_token_acquisition({'integration.token.enabled': 'true',
        'integration.token.provider': 'review_slow', 'integration.token.allowed_scopes': 'registry.read',
        'integration.token.timeout_seconds': '0.01'})
    start = time.monotonic()
    service = acquisition.get_token_acquisition()
    try:
        with pytest.raises(acquisition.TokenAcquisitionError) as error:
            await service.acquire(acquisition.ClientCredentials('id', 'review-secret'), 'registry.read')
        assert error.value.status_code == 503
        assert time.monotonic() - start < 0.07
        assert service.provider.cancelled
    finally:
        await acquisition.close_token_acquisition()
    assert service.provider.closed


@pytest.mark.asyncio
async def test_configured_fixed_endpoint_query_is_retained():
    calls = []
    async def endpoint(request):
        calls.append(request)
        return httpx.Response(200, json={'access_token': 'synthetic-token', 'token_type': 'Bearer', 'expires_in': 300})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        provider = acquisition.OAuth2ClientCredentialsProvider('https://review-iam.example/token?api-version=1', client=client)
        await provider.acquire(acquisition.ClientCredentials('id', 'review-secret'), frozenset({'registry.read'}))
        assert str(calls[0].url) == 'https://review-iam.example/token?api-version=1'
        assert calls[0].headers['authorization'].startswith('Basic ')
        assert b'client_secret' not in calls[0].content


def test_unmatched_integration_listener_url_redacts_queries():
    logger = logging.getLogger('uvicorn.access')
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    old_level, old_disabled = logger.level, logger.disabled
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.disabled = False
    try:
        logger.info('%s - "%s %s HTTP/%s" %d', 'loopback', 'POST',
                    '/wrong-token-path?client_secret=REVIEW_CANARY', '1.1', 404)
        assert 'REVIEW_CANARY' not in stream.getvalue()
        assert '/wrong-token-path' in stream.getvalue()
    finally:
        logger.removeHandler(handler)
        handler.close()
        logger.setLevel(old_level)
        logger.disabled = old_disabled


def test_tls_startup_failure_allows_corrected_retry_and_stop_start(monkeypatch, tmp_path):
    pki = tmp_path / 'pki'
    configure(pki, free_port(), free_port(), free_port(), False, 'review-secret')
    issuer = FakeIssuer()
    monkeypatch.setattr(acquisition, '_service', None)
    monkeypatch.setattr(acquisition, '_configured', False)
    monkeypatch.setattr(acquisition, '_FACTORIES', {'review_issuer': lambda conf: issuer})
    monkeypatch.setattr(HandlerRegistry, '_instances', {})
    conf_obj = SimpleNamespace(ssl_certfile=str(pki / 'missing.cer'), ssl_keyfile=str(pki / 'server.pem'),
                              ssl_keyfile_password=str(pki / 'cert_pwd'), ssl_ca_certs=str(pki / 'ca.cer'),
                              get_crl_list=lambda: [], ssl_crl_file='')
    server = ThirdPartyAccessServer({'integration.enabled': 'true', 'integration.port': free_port(),
        'integration.auth.mode': 'static_bearer', 'integration.auth.fingerprint_key': 'review-key',
        'integration.auth.static.hmac_key': 'review-key', 'integration.credential.file': str(pki / 'none.conf'),
        'integration.token.enabled': 'true', 'integration.token.provider': 'review_issuer',
        'integration.token.allowed_scopes': 'registry.read'}, conf_obj)
    try:
        with pytest.raises(FileNotFoundError):
            server.start()
        assert acquisition._service is None and not acquisition._configured
        assert server._server is None and server._thread is None
        server.stop()
        conf_obj.ssl_certfile = str(pki / 'ca.cer')
        for _ in range(2):
            issuer.closed = False
            server.start()
            assert server._server.started
            assert not issuer.closed
            server.stop()
            assert issuer.closed and acquisition._service is None and not acquisition._configured
            assert not HandlerRegistry._instances
        # Failure after allocation must also roll back, in the same event loop.
        import socket
        with socket.socket() as occupied:
            occupied.bind(('127.0.0.1', server.port))
            occupied.listen()
            issuer.closed = False
            with pytest.raises(RuntimeError, match='startup failed'):
                server.start()
            assert issuer.closed and acquisition._service is None and not acquisition._configured
            assert not HandlerRegistry._instances and server._thread is None
        server.start()
        assert server._server.started
        server.stop()
    finally:
        server.stop()
        asyncio.run(acquisition.close_token_acquisition())


@pytest.mark.asyncio
@pytest.mark.parametrize('fault', ['too_large', 'slow', 'malformed', 'redirect'])
async def test_jwks_response_and_duration_are_bounded(fault):
    async def endpoint(request):
        if fault == 'slow':
            await asyncio.sleep(0.08)
        if fault == 'too_large':
            return httpx.Response(200, content=b'x' * 65537)
        if fault == 'redirect':
            return httpx.Response(302, headers={'location': 'http://untrusted.example/jwks'})
        return httpx.Response(200, json={})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        instance = VerifiedJwkClient('https://review-iam.example/jwks', 0.01, client=client)
        token = jwt.encode({}, 'review-synthetic-secret', algorithm='HS256', headers={'kid': 'review-key'})
        with pytest.raises(jwt.PyJWKClientConnectionError):
            await instance.get_signing_key_from_jwt(token)
        assert not instance._keys and instance._expires_at == 0
        await instance.aclose()
        assert not client.is_closed


@pytest.mark.asyncio
async def test_jwks_owned_transport_closed_and_no_environment_proxy(monkeypatch):
    monkeypatch.setenv('HTTPS_PROXY', 'http://untrusted.example:8080')
    instance = VerifiedJwkClient('https://review-iam.example/jwks', 1)
    assert not instance.client.trust_env and not instance.client.follow_redirects
    await instance.aclose()
    assert instance.client.is_closed


@pytest.mark.asyncio
async def test_old_listener_cleanup_cannot_dispose_replacement_resources(monkeypatch):
    old_issuer, new_issuer = FakeIssuer(), FakeIssuer()
    old_service = acquisition.TokenAcquisitionService(old_issuer, 'registry.read')
    new_service = acquisition.TokenAcquisitionService(new_issuer, 'registry.read')
    class Handler:
        closed = False
        async def aclose(self):
            self.closed = True
    old_handler, new_handler = Handler(), Handler()
    monkeypatch.setattr(acquisition, '_service', old_service)
    monkeypatch.setattr(acquisition, '_configured', True)
    monkeypatch.setattr(HandlerRegistry, '_instances', {
        InterfaceType.INTEGRATION_AUTHENTICATE.value: old_handler})
    async with app.integration_lifespan(app.integration_app):
        acquisition._service = new_service
        HandlerRegistry._instances[InterfaceType.INTEGRATION_AUTHENTICATE.value] = new_handler
    await app.close_integration_resources(expected_handler=old_handler, expected_service=old_service)
    assert acquisition._service is new_service and acquisition._configured
    assert HandlerRegistry._instances[InterfaceType.INTEGRATION_AUTHENTICATE.value] is new_handler
    assert not new_handler.closed and not new_issuer.closed
    await app.close_integration_resources()
    assert new_handler.closed and new_issuer.closed


def test_startup_policy_error_closes_unpublished_auth_transport_on_own_loop(monkeypatch, tmp_path):
    pki = tmp_path / 'pki'
    configure(pki, free_port(), free_port(), free_port(), False, 'review-secret')
    monkeypatch.setattr(acquisition, '_service', None)
    monkeypatch.setattr(acquisition, '_configured', False)
    monkeypatch.setattr(HandlerRegistry, '_instances', {})
    handlers = []
    class TrackingHandler(authn.ThirdPartyAuthnHandler):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.created_loop = asyncio.get_running_loop()
            handlers.append(self)
        async def aclose(self):
            assert asyncio.get_running_loop() is self.created_loop
            await super().aclose()
    monkeypatch.setattr(authn, 'ThirdPartyAuthnHandler', TrackingHandler)
    conf = SimpleNamespace(ssl_certfile=str(pki / 'ca.cer'), ssl_keyfile=str(pki / 'server.pem'),
        ssl_keyfile_password=str(pki / 'cert_pwd'), ssl_ca_certs=str(pki / 'ca.cer'),
        ssl_crl_file='', get_crl_list=lambda: [])
    server = ThirdPartyAccessServer({'integration.enabled': 'true', 'integration.port': free_port(),
        'integration.auth.mode': 'oauth2_jwt', 'integration.auth.fingerprint_key': 'review-key',
        'integration.oauth2.issuer': 'https://review-iam.example',
        'integration.oauth2.audience': 'registry-center',
        'integration.oauth2.jwks_uri': 'https://review-iam.example/jwks',
        'integration.token.enabled': 'true', 'integration.token.allowed_scopes': ''}, conf)
    with pytest.raises(RuntimeError, match='startup failed'):
        server.start()
    assert len(handlers) == 1
    assert handlers[0].registry.get('oauth2_jwt')._jwks.client.is_closed
    assert not HandlerRegistry._instances and acquisition._service is None
    assert server._server is None and server._thread is None
