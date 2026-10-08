# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Expiry, revocation, bounded optional caching and provider lifecycle regressions."""
import time

import httpx
import jwt
import pytest

import agent_registry.integration.authn as authn
from agent_registry.integration.authn import (
    AuthenticationContext, Credential, IntrospectionBearerProvider,
    JwtBearerProvider, ScopeRoleMapper,
)
from common.util.authenticate_util import AuthFailureReason, AuthenticationError


def payload(**extra):
    result = {'active': True, 'sub': 'partner', 'client_id': 'client',
              'scope': 'registry.read', 'iss': 'https://iam.example', 'aud': 'registry-center',
              'exp': time.time() + 300}
    result.update(extra)
    return result


def provider(client, **extra):
    return IntrospectionBearerProvider('https://iam.example/introspect', 'registry', 'service-secret',
        'https://iam.example', 'registry-center', ScopeRoleMapper({'registry.read': 'partner_service'}),
        client=client, **extra)


@pytest.mark.asyncio
async def test_default_every_request_observes_revocation():
    calls = []
    def endpoint(request):
        calls.append(request)
        return httpx.Response(200, json=payload(active=len(calls) == 1))
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        instance = provider(client)
        await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
        with pytest.raises(AuthenticationError) as exc:
            await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
        assert exc.value.reason == AuthFailureReason.INVALID_TOKEN
        assert len(calls) == 2 and not instance._cache


@pytest.mark.parametrize('expiry', [None, True, False, 'nan', 'inf', float('-inf'), 'bad'])
@pytest.mark.asyncio
async def test_malformed_time_claims_never_enter_cache(expiry):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=payload(exp=expiry)))) as client:
        instance = provider(client, cache_seconds=30)
        with pytest.raises(AuthenticationError):
            await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
        assert not instance._cache


@pytest.mark.asyncio
async def test_missing_expiry_requires_revalidation_even_when_cache_enabled():
    data = payload()
    del data['exp']
    calls = []
    def endpoint(request):
        calls.append(request)
        return httpx.Response(200, json=data)
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        instance = provider(client, cache_seconds=30)
        for _ in range(2):
            await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
        assert len(calls) == 2 and not instance._cache


@pytest.mark.parametrize('invalid', [{'iss': 'wrong'}, {'aud': None}, {'aud': 'wrong'},
                                   {'scope': 'unknown'}, {'sub': '', 'client_id': ''}])
@pytest.mark.asyncio
async def test_only_fully_validated_tokens_can_be_cached(invalid):
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda req: httpx.Response(200, json=payload(**invalid)))) as client:
        instance = provider(client, cache_seconds=30)
        with pytest.raises(AuthenticationError):
            await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
        assert not instance._cache


@pytest.mark.asyncio
async def test_cache_is_bounded_lru_and_empty_fingerprints_do_not_collide():
    calls = []
    def endpoint(request):
        calls.append(request)
        return httpx.Response(200, json=payload())
    async with httpx.AsyncClient(transport=httpx.MockTransport(endpoint)) as client:
        instance = provider(client, cache_seconds=30, cache_max_entries=2)
        for token in ('one', 'two', 'one', 'three', 'two'):
            await instance.authenticate(Credential('bearer', token), AuthenticationContext('ip'))
            assert len(instance._cache) <= 2
        assert len(calls) == 4


@pytest.mark.asyncio
async def test_network_elapsed_time_and_wall_clock_expiry_on_cached_result(monkeypatch):
    class Clock:
        wall = 1000
        ticks = 100
        def time(self): return self.wall
        def monotonic(self): return self.ticks
    clock = Clock()
    monkeypatch.setattr(authn, 'time', clock)
    data = payload(exp=1001)
    async def late(request):
        clock.wall = 1002
        return httpx.Response(200, json=data)
    async with httpx.AsyncClient(transport=httpx.MockTransport(late)) as client:
        instance = provider(client, cache_seconds=30)
        with pytest.raises(AuthenticationError):
            await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
        assert not instance._cache
    clock.wall = 1000
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data))) as client:
        instance = provider(client, cache_seconds=30)
        await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
        clock.wall = 1002  # monotonic TTL unchanged; wall-clock expiry still wins
        with pytest.raises(AuthenticationError):
            await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
        assert not instance._cache


@pytest.mark.asyncio
async def test_owned_transport_closed_borrowed_transport_preserved():
    instance = provider(None)
    await instance.aclose()
    assert instance.client.is_closed
    async with httpx.AsyncClient() as client:
        instance = provider(client)
        await instance.aclose()
        assert not client.is_closed


@pytest.mark.asyncio
async def test_jwks_connection_failure_is_provider_unavailable(monkeypatch):
    instance = JwtBearerProvider('https://iam.example', 'registry-center',
        'https://iam.example/jwks', ['RS256'], ScopeRoleMapper({'registry.read': 'partner_service'}))
    def outage(token):
        raise jwt.PyJWKClientConnectionError('private upstream detail')
    monkeypatch.setattr(instance, '_decode', outage)
    with pytest.raises(AuthenticationError) as exc:
        await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
    assert exc.value.reason == AuthFailureReason.PROVIDER_UNAVAILABLE
    assert 'private' not in str(exc.value)


@pytest.mark.parametrize('kwargs', [{'cache_seconds': -1}, {'cache_seconds': 61},
                                   {'cache_max_entries': 0}, {'timeout': float('nan')},
                                   {'timeout': 0}])
def test_invalid_cache_and_timeout_config_fail_early(kwargs):
    with pytest.raises(ValueError):
        provider(None, **kwargs)


@pytest.mark.parametrize('data', [[], {}, {'active': 'true'}, {'active': 1}])
@pytest.mark.asyncio
async def test_unusable_iam_active_contract_is_not_a_bad_token(data):
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=data))) as client:
        instance = provider(client)
        with pytest.raises(AuthenticationError) as exc:
            await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
        assert exc.value.reason == AuthFailureReason.PROVIDER_UNAVAILABLE


@pytest.mark.asyncio
async def test_cached_claims_discard_nonstandard_echoed_token():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200,
            json=payload(access_token='should-not-be-retained', extra_claim='should-not-be-retained')))) as client:
        instance = provider(client, cache_seconds=30)
        await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
        assert 'should-not-be-retained' not in str(instance._cache)


@pytest.mark.asyncio
async def test_introspection_response_size_is_bounded():
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda req: httpx.Response(200,
            content=b'x' * 65537))) as client:
        instance = provider(client)
        with pytest.raises(AuthenticationError) as exc:
            await instance.authenticate(Credential('bearer', 'opaque'), AuthenticationContext('ip'))
        assert exc.value.reason == AuthFailureReason.PROVIDER_UNAVAILABLE
