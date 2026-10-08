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

"""Tests for the JWK host allowlist (CWE-863 signer-controlled jku)."""

import pytest
import httpx
from unittest.mock import MagicMock

from agent_registry.signature.jwk_fetcher import JWKFetcher
from agent_registry.signature.models import JWKS


def _jwks_response_body() -> dict:
    return {
        "keys": [
            {
                "kty": "EC",
                "kid": "test-key",
                "use": "sig",
                "alg": "ES256",
                "crv": "P-256",
                "x": "f83OJ3D2xF1Bg8vub9tLe1gHMzV76e8Tus9uPHvRVEU",
                "y": "x_FEzRu9m36HLN_tue659LNpXW6pCyStikYjKIWI5a0",
            }
        ]
    }


@pytest.mark.asyncio
async def test_fetch_fails_closed_when_no_allowlist_configured():
    """No allowlist => jku path disabled, no HTTP request is made."""
    fetcher = JWKFetcher(jwk_allowlist="")
    fetcher.session.stream = MagicMock()

    result = await fetcher.fetch_jwks("https://attacker.example.com/keys")

    assert result is None
    fetcher.session.stream.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_rejects_host_not_in_allowlist():
    """Host outside the allowlist is rejected before any HTTP request."""
    fetcher = JWKFetcher(jwk_allowlist="keys.example.com")
    fetcher.session.stream = MagicMock()

    result = await fetcher.fetch_jwks("https://attacker.example.com/keys")

    assert result is None
    fetcher.session.stream.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_rejects_non_https_even_when_host_allowed():
    """HTTPS-only rule still applies after the allowlist check."""
    fetcher = JWKFetcher(jwk_allowlist="keys.example.com")
    fetcher.session.stream = MagicMock()

    result = await fetcher.fetch_jwks("http://keys.example.com/keys")

    assert result is None
    fetcher.session.stream.assert_not_called()


@pytest.mark.asyncio
async def test_fetch_allows_host_in_allowlist():
    """Allowed host => request proceeds and JWKS is parsed."""
    fetcher = JWKFetcher(jwk_allowlist="keys.example.com")

    requests = []
    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=_jwks_response_body())
    await fetcher.session.aclose()
    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        fetcher.session = client
        result = await fetcher.fetch_jwks("https://keys.example.com/jwks.json")

    assert isinstance(result, JWKS)
    assert len(result.keys) == 1
    assert result.keys[0].kid == "test-key"
    assert str(requests[0].url) == 'https://keys.example.com/jwks.json'


@pytest.mark.asyncio
async def test_fetch_allowlist_matches_host_only_ignoring_port_and_case():
    """Port and case are ignored; the match is on the hostname alone."""
    fetcher = JWKFetcher(jwk_allowlist="Keys.Example.COM")

    await fetcher.session.aclose()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, json=_jwks_response_body()))) as client:
        fetcher.session = client
        result = await fetcher.fetch_jwks("https://keys.example.com:8443/jwks.json")

    assert isinstance(result, JWKS)


@pytest.mark.asyncio
async def test_fetch_rejects_malformed_jku():
    """A jku with no hostname is rejected."""
    fetcher = JWKFetcher(jwk_allowlist="keys.example.com")
    fetcher.session.stream = MagicMock()

    result = await fetcher.fetch_jwks("https:///missing-host")

    assert result is None
    fetcher.session.stream.assert_not_called()


def test_parse_allowlist_normalizes_whitespace_and_case():
    fetcher = JWKFetcher(jwk_allowlist=" keys.example.com , Keys2.Example.org , ")

    assert fetcher.jwk_allowlist == {"keys.example.com", "keys2.example.org"}


@pytest.mark.asyncio
async def test_jwks_size_limit_without_content_length():
    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b' ' * 600_000
            yield b' ' * 600_000
    fetcher = JWKFetcher(jwk_allowlist='keys.example.com')
    await fetcher.session.aclose()
    async with httpx.AsyncClient(transport=httpx.MockTransport(
            lambda request: httpx.Response(200, stream=Chunks()))) as client:
        fetcher.session = client
        assert await fetcher.fetch_jwks('https://keys.example.com/jwks') is None


@pytest.mark.asyncio
async def test_jku_redirect_is_not_followed():
    requested = []
    def redirect(request):
        requested.append(str(request.url))
        return httpx.Response(302, headers={'Location': 'https://internal.example/keys'})
    fetcher = JWKFetcher(jwk_allowlist='keys.example.com')
    await fetcher.session.aclose()
    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        fetcher.session = client
        assert await fetcher.fetch_jwks('https://keys.example.com/jwks') is None
    assert requested == ['https://keys.example.com/jwks']


def _fetch_with_body(body):
    async def _run():
        fetcher = JWKFetcher(jwk_allowlist='keys.example.com')
        await fetcher.session.aclose()
        async with httpx.AsyncClient(transport=httpx.MockTransport(
                lambda request: httpx.Response(200, json=body))) as client:
            fetcher.session = client
            return await fetcher.fetch_jwks('https://keys.example.com/jwks')
    return _run()


@pytest.mark.asyncio
async def test_mixed_key_set_keeps_the_usable_signing_key():
    """Third-party sets mix key types: unusable entries must not void the set."""
    usable = _jwks_response_body()["keys"][0]
    rsa = {"kty": "RSA", "kid": "rsa-key", "use": "sig", "alg": "RS256",
           "n": "0vx7agoebGcQSuuPiLJXZptN9nndrQmbXEps2aiAFbWhM78LhWx4"
                "cbbfAAtVT86zwu1RK7aPFFxuhDR1L6tSoc_BJECPebWKRXjBZCiFV4n3oknjhMstn"
                "64tZ-68Kd4L1VaEPSHn1vKzLQKoHkQ",
           "e": "AQAB"}
    unsupported = {"kty": "EC", "kid": "p384", "use": "sig", "alg": "ES384",
                   "crv": "P-384", "x": "AAA", "y": "BBB"}
    encryption = {"kty": "RSA", "kid": "enc", "use": "enc",
                  "n": rsa["n"], "e": rsa["e"]}
    result = await _fetch_with_body(
        {"keys": [unsupported, encryption, usable, {"kty": "oct", "kid": "sym", "k": "AAA"}]})

    assert isinstance(result, JWKS)
    assert [key.kid for key in result.keys] == ["test-key"]


@pytest.mark.asyncio
async def test_key_set_without_usable_keys_fails_closed():
    result = await _fetch_with_body({"keys": [
        {"kty": "EC", "kid": "p384", "use": "sig", "alg": "ES384",
         "crv": "P-384", "x": "AAA", "y": "BBB"},
        {"kty": "RSA", "kid": "enc", "use": "enc", "n": "AAA", "e": "AQAB"},
    ]})
    assert result is None


@pytest.mark.asyncio
async def test_payload_that_is_not_a_key_set_fails_closed():
    assert await _fetch_with_body({"error": "not found"}) is None
    assert await _fetch_with_body(["not", "a", "set"]) is None
