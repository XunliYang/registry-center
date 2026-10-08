# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

import time

import httpx
import pytest
from starlette.datastructures import Headers

from agent_registry.integration.authn import (
    AuthenticationContext, AuthenticationProvider, AuthenticationProviderRegistry,
    BearerTokenExtractor, Credential, IntrospectionBearerProvider,
    JwtBearerProvider, MtlsProvider, ScopeRoleMapper, StaticBearerProvider,
    ThirdPartyAuthnHandler, token_fingerprint,
    register_authentication_provider,
)
from agent_registry.integration.credentials import CredentialEntry
from common.util.authenticate_util import (
    AUTH_METHOD_OAUTH2_INTROSPECTION, AUTH_METHOD_OAUTH2_JWT,
    AUTH_METHOD_STATIC_BEARER, AuthFailureReason, AuthenticationError,
    CallerRole, CallerType, Principal,
)


class Request:
    def __init__(self, headers=(), scope=None):
        self.headers = Headers(raw=[(k.lower().encode(), v.encode()) for k, v in headers])
        self.scope = scope or {}


def test_principal_standard_identity_fields_and_legacy_compatibility():
    legacy = Principal(client_ip='127.0.0.1')
    assert legacy.caller_type == CallerType.INTERNAL
    p = Principal(client_ip='1.2.3.4', identity='svc', subject='svc', issuer='issuer',
                  client_id='client', scopes={'registry.read'}, tenant='tenant',
                  credential_id='issuer:client')
    assert p.subject == 'svc'
    assert p.scopes == frozenset({'registry.read'})
    assert p.credential_id == 'issuer:client'


def test_bearer_extractor_accepts_standard_header_case_insensitively():
    credential = BearerTokenExtractor('fingerprint-key').extract(
        Request([('Authorization', 'bEaReR opaque-token')]))
    assert credential.value == 'opaque-token'
    assert credential.fingerprint != credential.value


@pytest.mark.parametrize('headers', [
    [('Authorization', 'Basic abc')],
    [('Authorization', 'Bearer')],
    [('Authorization', 'Bearer one'), ('Authorization', 'Bearer two')],
])
def test_bearer_extractor_rejects_malformed_or_duplicate_headers(headers):
    with pytest.raises(AuthenticationError) as exc:
        BearerTokenExtractor('key').extract(Request(headers))
    assert exc.value.reason == AuthFailureReason.INVALID_TOKEN_FORMAT
    assert 'one' not in exc.value.detail


@pytest.mark.asyncio
async def test_static_bearer_uses_hmac_digest():
    entry = CredentialEntry('operations', 'operations-service', CallerRole.NMS_OSS,
                            'operations-service', token_fingerprint('secret-token', 'key'))
    provider = StaticBearerProvider({'operations': entry}, 'key')
    principal = await provider.authenticate(Credential('bearer', 'secret-token'),
                                            AuthenticationContext('1.2.3.4'))
    assert principal.auth_method == AUTH_METHOD_STATIC_BEARER
    assert principal.credential_id == 'operations'
    with pytest.raises(AuthenticationError):
        await provider.authenticate(Credential('bearer', 'wrong'), AuthenticationContext('1.2.3.4'))


@pytest.mark.asyncio
async def test_custom_provider_registry_contract():
    class Custom(AuthenticationProvider):
        provider_id = 'custom'
        credential_kind = 'custom'

        async def authenticate(self, credential, context):
            return Principal(context.client_ip, identity='custom-subject', subject='custom-subject')

    registry = AuthenticationProviderRegistry()
    registry.register(Custom())
    result = await registry.get('custom').authenticate(Credential('custom'), AuthenticationContext('ip'))
    assert result.subject == 'custom-subject'

    class Extractor:
        def extract(self, request):
            return Credential('custom', request.headers.get('X-Custom', '')) \
                if request.headers.get('X-Custom') else None

    register_authentication_provider(Custom(), Extractor())
    handler = ThirdPartyAuthnHandler(config={'integration.auth.mode': 'custom'})
    result = await handler.handle('ip', Request([('X-Custom', 'value')]))
    assert result.subject == 'custom-subject'


def test_business_sample_is_not_core_registered():
    from samples.custom_auth_provider import BusinessAuthenticationProvider, BusinessHeaderExtractor
    credential = BusinessHeaderExtractor().extract(
        Request([('X-Business-Credential', 'private-value')]))
    assert credential.kind == 'business'
    assert BusinessAuthenticationProvider.provider_id == 'business_custom'


def test_scope_mapper_is_explicit_and_rejects_conflicting_roles():
    mapper = ScopeRoleMapper({'registry.read': 'partner_service', 'registry.admin': 'nms_oss'})
    assert mapper.role_for({'unknown'}) is None
    assert mapper.role_for({'registry.read'}) == CallerRole.PARTNER_SERVICE
    with pytest.raises(AuthenticationError):
        mapper.role_for({'registry.read', 'registry.admin'})


@pytest.mark.asyncio
async def test_jwt_provider_builds_principal_only_from_verified_claims(monkeypatch):
    mapper = ScopeRoleMapper({'registry.read': 'partner_service'})
    provider = JwtBearerProvider('https://issuer.example', 'registry-center',
                                 'https://issuer.example/jwks', ['RS256'], mapper)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(provider, '_decode', AsyncMock(return_value={
        'sub': 'partner', 'client_id': 'partner-client',
        'scope': 'registry.read', 'iss': 'https://issuer.example',
        'aud': 'registry-center', 'exp': int(time.time()) + 60,
    }))
    p = await provider.authenticate(Credential('bearer', 'jwt'), AuthenticationContext('ip'))
    assert p.auth_method == AUTH_METHOD_OAUTH2_JWT
    assert p.role == CallerRole.PARTNER_SERVICE


@pytest.mark.asyncio
async def test_introspection_is_fail_closed_and_cached():
    calls = 0

    async def endpoint(request):
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={
            'active': True, 'sub': 'analytics', 'client_id': 'analytics-client',
            'scope': 'registry.audit', 'iss': 'https://issuer.example',
            'aud': 'registry-center', 'exp': int(time.time()) + 60,
        })

    client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    provider = IntrospectionBearerProvider(
        'https://issuer.example/introspect', 'client', 'secret',
        'https://issuer.example', 'registry-center',
        ScopeRoleMapper({'registry.audit': 'analytics_tool'}), client=client, cache_seconds=30)
    credential = Credential('bearer', 'opaque', fingerprint='fingerprint')
    p = await provider.authenticate(credential, AuthenticationContext('ip'))
    assert p.auth_method == AUTH_METHOD_OAUTH2_INTROSPECTION
    await provider.authenticate(credential, AuthenticationContext('ip'))
    assert calls == 1
    await client.aclose()


@pytest.mark.asyncio
async def test_mtls_uses_scope_certificate_not_headers():
    entry = CredentialEntry('cert-partner', 'partner', CallerRole.PARTNER_SERVICE,
                            'partner', cn='trusted-cn')
    provider = MtlsProvider({'trusted-cn': entry})
    credential = Credential('mtls', peer_certificate={
        'subject': ((('commonName', 'trusted-cn'),),)
    })
    p = await provider.authenticate(credential, AuthenticationContext('ip'))
    assert p.subject == 'partner'


@pytest.mark.asyncio
async def test_handler_static_bearer_end_to_end(tmp_path):
    digest = token_fingerprint('token', 'static-key')
    path = tmp_path / 'credentials.conf'
    path.write_text(
        f'credential.svc.identity=service\ncredential.svc.token_hash={digest}\n'
        'credential.svc.role=nms_oss\n', encoding='utf-8')
    handler = ThirdPartyAuthnHandler(str(path), {
        'integration.auth.mode': 'static_bearer',
        'integration.auth.fingerprint_key': 'fingerprint-key',
        'integration.auth.static.hmac_key': 'static-key',
    })
    p = await handler.handle('ip', Request([('Authorization', 'Bearer token')]))
    assert p.subject == 'service'
