# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""OAuth acquisition contract, safe errors, extension and resource independence."""
import asyncio
import base64
from urllib.parse import parse_qs, quote_plus

import httpx
import pytest
from fastapi.testclient import TestClient
from starlette.requests import Request
from limits import parse

import agent_registry.integration.app as app
import agent_registry.integration.token_acquisition as acquisition
from agent_registry.integration.ban import BanTracker
from agent_registry.integration.token_acquisition import (
    AcquiredToken, ClientCredentials, OAuth2ClientCredentialsProvider,
    TokenAcquisitionError, TokenAcquisitionProvider, TokenAcquisitionService,
)
from common.custom.custom_handle import HandlerRegistry
from common.custom.interface_type import InterfaceType
from common.util.authenticate_util import AuthFailureReason, AuthenticationError, CallerRole
from tests.fakes.integration import FakeRegistry, StubAuthnHandler, make_third_party_principal
import agent_registry.registry_instance as registry_instance

PATH = '/integration/v1/oauth2/token'
FORM = {'grant_type': 'client_credentials', 'scope': 'registry.read'}


def basic(client_id='partner', secret='caller-secret'):
    value = f'{quote_plus(client_id)}:{quote_plus(secret)}'.encode()
    return {'Authorization': 'Basic ' + base64.b64encode(value).decode()}


class FakeProvider(TokenAcquisitionProvider):
    def __init__(self):
        self.calls = []
        self.error = None
        self.closed = False

    async def acquire(self, credentials, scopes):
        self.calls.append((credentials, scopes))
        if self.error:
            raise self.error
        return AcquiredToken('fixture-access-token', 300, scopes)

    async def aclose(self):
        self.closed = True


@pytest.fixture
def token_client(monkeypatch):
    provider = FakeProvider()
    service = TokenAcquisitionService(provider, 'registry.read registry.vendor', 'registry.read')
    monkeypatch.setattr(acquisition, '_service', service)
    monkeypatch.setattr(acquisition, '_configured', True)
    tracker = BanTracker(threshold=3)
    monkeypatch.setattr(app, '_ban_tracker', tracker)
    monkeypatch.setattr(app, '_tp_prerate_item', parse('100000/second'))
    monkeypatch.setattr(app, '_tp_rate_item', parse('100000/second'))
    audit = []

    async def record(*args, **kwargs):
        audit.append(args)

    monkeypatch.setattr(app, 'audit_integration', record)
    monkeypatch.setattr(app, 'audit_integration_failure', record)
    yield TestClient(app.integration_app), provider, tracker, audit


def test_acquisition_uses_callers_credentials_not_bearer_auth(token_client, monkeypatch):
    client, provider, _, audit = token_client

    def forbidden(*args):
        raise AssertionError('Resource Bearer authentication must not run on acquisition')

    monkeypatch.setattr(HandlerRegistry, 'get_handler', forbidden)
    response = client.post(PATH, data=FORM, headers=basic('client:one', 'secret:+ é'))
    assert response.status_code == 200
    assert response.json() == {'access_token': 'fixture-access-token', 'token_type': 'Bearer',
                               'expires_in': 300, 'scope': 'registry.read'}
    assert response.headers['cache-control'] == 'no-store'
    assert response.headers['pragma'] == 'no-cache'
    assert provider.calls[0][0] == ClientCredentials('client:one', 'secret:+ é')
    assert audit[0][1].identity == 'client:one'
    assert 'fixture-access-token' not in str(audit)
    assert 'secret:+' not in str(audit)


def test_default_scope_and_no_acquisition_cache(token_client):
    client, provider, _, _ = token_client
    for _ in range(2):
        assert client.post(PATH, data={'grant_type': 'client_credentials'}, headers=basic()).status_code == 200
    assert len(provider.calls) == 2
    assert provider.calls[0][1] == frozenset({'registry.read'})


def test_endpoint_is_disabled_by_default(token_client, monkeypatch):
    monkeypatch.setattr(acquisition, '_service', None)
    response = token_client[0].post(PATH, data=FORM, headers=basic())
    assert response.status_code == 404
    assert response.headers['cache-control'] == 'no-store'
    assert not token_client[1].calls


@pytest.mark.parametrize('body,error,status', [
    ('grant_type=password', 'unsupported_grant_type', 400),
    ('grant_type=client_credentials&scope=registry.admin', 'invalid_scope', 400),
    ('grant_type=client_credentials&scope=', 'invalid_scope', 400),
    ('grant_type=client_credentials&scope=registry.read&scope=registry.vendor', 'invalid_request', 400),
    ('grant_type=client_credentials&client_secret=body-secret', 'invalid_request', 400),
    ('grant_type=client_credentials&endpoint=https://evil.example', 'invalid_request', 400),
    ('a' * 8193, 'invalid_request', 413),
])
def test_rejects_unsafe_forms_without_calling_iam(token_client, body, error, status):
    headers = dict(basic(), **{'Content-Type': 'application/x-www-form-urlencoded'})
    response = token_client[0].post(PATH, content=body, headers=headers)
    assert response.status_code == status
    assert response.json() == {'error': error}
    assert not token_client[1].calls


@pytest.mark.parametrize('headers', [{}, {'Authorization': 'Bearer old-token'},
                                     {'Authorization': 'Basic !!!'},
                                     {'Authorization': 'Basic ' + base64.b64encode(b'partner:').decode()},
                                     [('Authorization', basic()['Authorization'])] * 2])
def test_basic_credentials_required_and_failure_identity_untrusted(token_client, headers):
    client, provider, _, audit = token_client
    response = client.post(PATH, data=FORM, headers=headers)
    assert response.status_code == 401
    assert response.json() == {'error': 'invalid_client'}
    assert response.headers['www-authenticate'].startswith('Basic ')
    assert audit[-1][1].identity == ''
    assert not provider.calls


def test_query_credentials_and_json_rejected(token_client):
    client = token_client[0]
    assert client.post(PATH + '?client_secret=unsafe', data=FORM, headers=basic()).status_code == 400
    assert client.post(PATH, json=FORM, headers=basic()).status_code == 400


@pytest.mark.parametrize('error,status', [('invalid_client', 401), ('invalid_scope', 400),
                                        ('temporarily_unavailable', 503)])
def test_upstream_failure_mapping_and_bans(token_client, error, status):
    client, provider, tracker, audit = token_client
    provider.error = TokenAcquisitionError(error, status)
    for _ in range(3):
        response = client.post(PATH, data=FORM, headers=basic())
        assert response.status_code == status
        assert response.headers['cache-control'] == 'no-store'
    assert tracker.is_banned('ip:testclient') is (error == 'invalid_client')
    assert audit[-1][1].identity == ''
    if error != 'invalid_client':
        provider.error = None
        assert client.post(PATH, data=FORM, headers=basic()).status_code == 200


def test_adapter_internal_error_is_sanitized_not_banned(token_client):
    client, provider, tracker, audit = token_client
    provider.error = RuntimeError('caller-secret fixture-access-token')
    response = client.post(PATH, data=FORM, headers=basic())
    assert response.status_code == 503
    assert 'caller-secret' not in response.text + str(audit)
    assert not tracker._failures


def test_acquisition_rate_limit_and_lifespan_cleanup(token_client, monkeypatch):
    client, provider, _, _ = token_client
    monkeypatch.setattr(app, '_tp_prerate_item', parse('1/day'))
    assert client.post(PATH, data=FORM, headers=basic()).status_code in (200, 429)
    assert client.post(PATH, data=FORM, headers=basic()).status_code == 429
    # Isolate teardown from resource auth handlers owned by other fixtures.
    monkeypatch.setattr(HandlerRegistry, '_instances', {})
    with TestClient(app.integration_app):
        pass
    assert provider.closed
    assert acquisition._service is None and not acquisition._configured


@pytest.mark.parametrize('endpoint', ['http://iam.example/token', 'https://user:secret@iam.example/token',
                                      'https://iam.example/token#x', 'https://'])
def test_transport_requires_fixed_https_endpoint(endpoint):
    with pytest.raises(ValueError):
        OAuth2ClientCredentialsProvider(endpoint)


@pytest.mark.asyncio
async def test_standard_iam_contract_and_response_allowlist():
    async def endpoint(request):
        assert parse_qs(request.content.decode()) == {'grant_type': ['client_credentials'], 'scope': ['registry.read']}
        decoded = base64.b64decode(request.headers['authorization'].split()[1]).decode()
        assert decoded == f'{quote_plus("client:é")}:{quote_plus("secret+:")}'
        return httpx.Response(200, json={'access_token': 'opaque-token', 'token_type': 'bearer',
                                        'expires_in': 300, 'scope': 'registry.read',
                                        'refresh_token': 'never-forward', 'private_claim': 'never-forward'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        provider = OAuth2ClientCredentialsProvider('https://iam.example/token', client=client)
        result = await provider.acquire(ClientCredentials('client:é', 'secret+:'), frozenset({'registry.read'}))
        assert 299 <= result.expires_in <= 300
        assert set(result.response()) == {'access_token', 'token_type', 'expires_in', 'scope'}
        assert result.access_token == 'opaque-token' and result.scopes == frozenset({'registry.read'})
        await provider.aclose()
        assert not client.is_closed  # borrowed transport remains caller-owned


@pytest.mark.parametrize('status,payload,expected', [
    (400, {'error': 'invalid_client', 'error_description': 'sensitive'}, 401),
    (401, {'error': 'invalid_client'}, 401),
    (400, {'error': 'invalid_scope'}, 400),
    (429, {}, 429), (500, {}, 503), (302, {}, 503),
    (200, {'access_token': 'token', 'token_type': 'Bearer'}, 503),
    (200, {'access_token': 'token', 'token_type': 'Bearer', 'expires_in': True}, 503),
    (200, {'access_token': 'token', 'token_type': 'Bearer', 'expires_in': -1}, 503),
    (200, {'access_token': 'token', 'token_type': 'Bearer', 'expires_in': 300, 'scope': 'registry.admin'}, 503),
    (200, {'access_token': 'token', 'token_type': 'Bearer', 'expires_in': 300, 'scope': '\tbad'}, 503),
    (200, {'access_token': 'token\nsecret', 'token_type': 'Bearer', 'expires_in': 300}, 503),
    (200, {'access_token': 'token', 'token_type': 'MAC', 'expires_in': 300}, 503),
])
@pytest.mark.asyncio
async def test_iam_errors_are_sanitized(status, payload, expected):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(status, json=payload))) as client:
        provider = OAuth2ClientCredentialsProvider('https://iam.example/token', client=client)
        with pytest.raises(TokenAcquisitionError) as exc:
            await provider.acquire(ClientCredentials('id', 'secret'), frozenset({'registry.read'}))
        assert exc.value.status_code == expected
        assert 'sensitive' not in str(exc.value)


@pytest.mark.asyncio
async def test_bounded_iam_response_and_total_timeout():
    async def large(request):
        return httpx.Response(200, content=b'x' * 65537)
    async def slow(request):
        await asyncio.sleep(0.1)
        return httpx.Response(200, json={})
    for endpoint in (large, slow):
        async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
            provider = OAuth2ClientCredentialsProvider('https://iam.example/token', timeout=0.02, client=client)
            with pytest.raises(TokenAcquisitionError) as exc:
                await provider.acquire(ClientCredentials('id', 'secret'), frozenset({'registry.read'}))
            assert exc.value.status_code == 503


def test_config_env_resolution_and_independent_business_factory(monkeypatch):
    provider = FakeProvider()
    monkeypatch.setattr(acquisition, '_FACTORIES', dict(acquisition._FACTORIES))
    monkeypatch.setattr(acquisition, '_configured', False)
    monkeypatch.setattr(acquisition, '_service', None)
    monkeypatch.setenv('TOKEN_TEST_SCOPE', 'registry.read')
    acquisition.register_token_acquisition_provider('business_iam', lambda config: provider)
    acquisition.configure_token_acquisition({'integration.token.enabled': 'true',
        'integration.token.provider': 'business_iam', 'integration.token.allowed_scopes': '${TOKEN_TEST_SCOPE}'})
    assert acquisition.get_token_acquisition().provider is provider
    assert acquisition.get_token_acquisition().allowed_scopes == frozenset({'registry.read'})


def test_resource_iam_outage_is_503_and_does_not_ban(token_client, monkeypatch):
    client, _, tracker, _ = token_client
    handler = StubAuthnHandler()
    handler.error = AuthenticationError(AuthFailureReason.PROVIDER_UNAVAILABLE, 'secret IAM detail')
    monkeypatch.setitem(HandlerRegistry._instances, InterfaceType.INTEGRATION_AUTHENTICATE.value, handler)
    monkeypatch.setattr(registry_instance, '_registry_instance', FakeRegistry())
    for _ in range(5):
        response = client.get('/integration/v1/agent-cards', headers={'Authorization': 'Bearer fixture-access-token'})
        assert response.status_code == 503 and 'secret IAM detail' not in response.text
    assert not tracker._failures and not tracker._banned_until
    handler.error = None
    handler.principal = make_third_party_principal(CallerRole.PARTNER_SERVICE)
    assert client.get('/integration/v1/agent-cards').status_code == 200


def test_success_clears_the_actual_failure_fingerprint_and_challenge(token_client, monkeypatch):
    client, _, tracker, _ = token_client
    handler = StubAuthnHandler()
    handler.credential_hint = lambda request: 'safe-fingerprint'
    handler.error = AuthenticationError(AuthFailureReason.INVALID_TOKEN, 'must-not-expose-secret')
    monkeypatch.setitem(HandlerRegistry._instances, InterfaceType.INTEGRATION_AUTHENTICATE.value, handler)
    monkeypatch.setattr(registry_instance, '_registry_instance', FakeRegistry())
    response = client.get('/integration/v1/agent-cards')
    assert response.status_code == 401
    assert response.headers['www-authenticate'] == 'Bearer error="invalid_token"'
    assert 'must-not-expose-secret' not in response.text
    assert tracker._failures['token:safe-fingerprint'] == 1
    handler.error = None
    handler.principal = make_third_party_principal(CallerRole.PARTNER_SERVICE)
    assert client.get('/integration/v1/agent-cards').status_code == 200
    assert 'token:safe-fingerprint' not in tracker._failures
    response = client.post('/integration/v1/agent-cards', json={})
    assert response.status_code == 403
    assert response.headers['www-authenticate'] == 'Bearer error="insufficient_scope"'


def test_disabled_and_invalid_scope_config_do_not_create_http_clients(monkeypatch):
    monkeypatch.setattr(acquisition, '_configured', False)
    monkeypatch.setattr(acquisition, '_service', None)
    def forbidden(config):
        raise AssertionError('Must not create a transport')
    monkeypatch.setattr(acquisition, '_FACTORIES', {'oauth2_client_credentials': forbidden})
    acquisition.configure_token_acquisition({'integration.token.enabled': 'false'})
    assert acquisition.get_token_acquisition() is None
    with pytest.raises(ValueError):
        acquisition.configure_token_acquisition({'integration.token.enabled': 'true',
            'integration.token.allowed_scopes': 'registry.read', 'integration.token.default_scope': 'registry.admin'})


@pytest.mark.parametrize('token', [AcquiredToken('token', 300, frozenset({'registry.admin'})),
                                  AcquiredToken('token', 0, frozenset({'registry.read'})),
                                  AcquiredToken('bad\nheader', 300, frozenset({'registry.read'})),
                                  AcquiredToken('token', 300, frozenset())])
@pytest.mark.asyncio
async def test_business_adapter_cannot_bypass_service_policy(token):
    class BadProvider(FakeProvider):
        async def acquire(self, credentials, scopes):
            return token
    service = TokenAcquisitionService(BadProvider(), 'registry.read', 'registry.read')
    with pytest.raises(TokenAcquisitionError) as exc:
        await service.acquire(ClientCredentials('id', 'secret'), None)
    assert exc.value.status_code == 503


@pytest.mark.asyncio
async def test_slow_token_request_body_has_a_deadline(monkeypatch):
    monkeypatch.setattr(acquisition, '_BODY_TIMEOUT', 0.01)
    async def receive():
        await asyncio.sleep(0.1)
        return {'type': 'http.request', 'body': b'grant_type=client_credentials'}
    request = Request({'type': 'http', 'scheme': 'https', 'method': 'POST', 'path': PATH,
                       'query_string': b'', 'server': ('localhost', 443),
                       'headers': [(b'content-type', b'application/x-www-form-urlencoded')]}, receive=receive)
    with pytest.raises(TokenAcquisitionError) as exc:
        await acquisition.parse_token_request(request)
    assert exc.value.status_code == 408
