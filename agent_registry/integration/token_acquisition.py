# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Optional token acquisition broker; the target IAM remains the token issuer.

Acquisition and resource authentication are independent extension points. No
client secrets, access tokens or refresh tokens are retained by this module.
"""

import base64
import asyncio
import binascii
import json
import hashlib
import hmac
import math
import re
import secrets
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable, Mapping, Optional
from urllib.parse import parse_qsl, unquote_plus

import httpx
from fastapi import Request

from agent_registry.integration.oauth_transport import (
    client_basic_auth, iam_client, require_https_endpoint, require_timeout,
)
from common.util.app_config import get_conf, resolve_env_vars

_MAX_BODY = 8192
_BODY_TIMEOUT = 10.0
_MAX_IAM_BODY = 65536
_SCOPE = re.compile(r'^[\x21\x23-\x5b\x5d-\x7e]+$')


class TokenAcquisitionError(Exception):
    """Sanitized OAuth error, never an upstream description or response body."""

    def __init__(self, error: str, status_code: int = 400):
        if error not in {'invalid_request', 'invalid_client', 'invalid_scope',
                         'unsupported_grant_type', 'unauthorized_client', 'temporarily_unavailable'}:
            error, status_code = 'temporarily_unavailable', 503
        self.error, self.status_code = error, status_code
        super().__init__(error)


@dataclass(frozen=True)
class ClientCredentials:
    client_id: str
    client_secret: str = field(repr=False)


@dataclass(frozen=True)
class AcquiredToken:
    access_token: str = field(repr=False)
    expires_in: int
    scopes: frozenset[str]

    def response(self) -> dict:
        return {'access_token': self.access_token, 'token_type': 'Bearer',
                'expires_in': self.expires_in, 'scope': ' '.join(sorted(self.scopes))}


def _scopes(value: str) -> frozenset[str]:
    if not isinstance(value, str):
        raise TokenAcquisitionError('invalid_scope')
    parts = value.split(' ') if value else []
    if any(not _SCOPE.fullmatch(s) for s in parts):
        raise TokenAcquisitionError('invalid_scope')
    return frozenset(parts)


def _credentials(request: Request) -> ClientCredentials:
    values = request.headers.getlist('authorization')
    try:
        if len(values) != 1:
            raise ValueError()
        scheme, encoded = values[0].split()
        if scheme.lower() != 'basic' or len(encoded) > _MAX_BODY:
            raise ValueError()
        decoded = base64.b64decode(encoded, validate=True).decode('utf-8')
        client_id, secret = (unquote_plus(s, errors='strict')
                             for s in decoded.split(':', 1))
        if (not client_id or not secret
                or any(ord(c) < 32 or ord(c) == 127 for c in client_id + secret)):
            raise ValueError()
        return ClientCredentials(client_id, secret)
    except (ValueError, UnicodeError, binascii.Error):
        raise TokenAcquisitionError('invalid_client', 401) from None


async def parse_token_request(request: Request) -> tuple[ClientCredentials, Optional[str]]:
    """A bounded OAuth form and client_secret_basic; no credentials in URLs."""
    try:
        async with asyncio.timeout(_BODY_TIMEOUT):
            return await _read_token_request(request)
    except TimeoutError:
        raise TokenAcquisitionError('invalid_request', 408) from None


async def _read_token_request(request: Request) -> tuple[ClientCredentials, Optional[str]]:
    if request.url.query:
        raise TokenAcquisitionError('invalid_request')
    if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'application/x-www-form-urlencoded':
        raise TokenAcquisitionError('invalid_request')
    body = bytearray()
    async for chunk in request.stream():
        body.extend(chunk)
        if len(body) > _MAX_BODY:
            raise TokenAcquisitionError('invalid_request', 413)
    try:
        pairs = parse_qsl(body.decode('utf-8'), keep_blank_values=True,
                          strict_parsing=True, max_num_fields=4, errors='strict')
    except (ValueError, UnicodeError):
        raise TokenAcquisitionError('invalid_request') from None
    data = dict(pairs)
    if len(data) != len(pairs) or set(data) - {'grant_type', 'scope'}:
        raise TokenAcquisitionError('invalid_request')
    if data.get('grant_type') != 'client_credentials':
        raise TokenAcquisitionError('unsupported_grant_type')
    return _credentials(request), data.get('scope')


class TokenAcquisitionProvider(ABC):
    @abstractmethod
    async def acquire(self, credentials: ClientCredentials,
                      scopes: frozenset[str]) -> AcquiredToken:
        """Authenticate THIS caller at IAM and return bounded expiry metadata."""
        raise NotImplementedError

    async def aclose(self) -> None:
        pass


class OAuth2ClientCredentialsProvider(TokenAcquisitionProvider):
    def __init__(self, endpoint: str, timeout: float = 3.0, ca_file: str = '',
                 client: Optional[httpx.AsyncClient] = None):
        require_https_endpoint(endpoint)
        require_timeout(timeout)
        self.endpoint, self.timeout = endpoint, timeout
        self._owns_client = client is None
        self.client = client if client is not None else iam_client(ca_file)

    async def acquire(self, credentials: ClientCredentials,
                      scopes: frozenset[str]) -> AcquiredToken:
        try:
            async with asyncio.timeout(self.timeout):
                return await self._acquire(credentials, scopes)
        except TimeoutError:
            raise TokenAcquisitionError('temporarily_unavailable', 503) from None

    async def _acquire(self, credentials: ClientCredentials,
                      scopes: frozenset[str]) -> AcquiredToken:
        start = time.monotonic()
        try:
            async with self.client.stream(
                    'POST', self.endpoint,
                    data={'grant_type': 'client_credentials', 'scope': ' '.join(sorted(scopes))},
                    auth=client_basic_auth(credentials.client_id, credentials.client_secret),
                    timeout=self.timeout, follow_redirects=False) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    body.extend(chunk)
                    if len(body) > _MAX_IAM_BODY:
                        raise TokenAcquisitionError('temporarily_unavailable', 503)
                if response.status_code == 429:
                    raise TokenAcquisitionError('temporarily_unavailable', 429)
                if response.status_code >= 500 or 300 <= response.status_code < 400:
                    raise TokenAcquisitionError('temporarily_unavailable', 503)
                data = json.loads(body)
                if not isinstance(data, dict):
                    raise ValueError()
                if response.status_code != 200:
                    error = data.get('error')
                    if response.status_code in (400, 401) and error == 'invalid_client':
                        raise TokenAcquisitionError('invalid_client', 401)
                    if response.status_code == 400 and error in ('invalid_scope', 'unauthorized_client'):
                        raise TokenAcquisitionError(error)
                    raise TokenAcquisitionError('temporarily_unavailable', 503)
                token, expires = data.get('access_token'), data.get('expires_in')
                if (not isinstance(token, str) or not token or len(token) > 16384
                        or any(ord(c) <= 32 or ord(c) >= 127 for c in token)
                        or not isinstance(data.get('token_type'), str)
                        or data['token_type'].lower() != 'bearer'
                        or type(expires) is not int or expires <= 0):
                    raise ValueError()
                remaining = math.floor(expires - (time.monotonic() - start))
                if remaining <= 0:
                    raise ValueError()
                try:
                    granted = _scopes(data.get('scope', ' '.join(sorted(scopes))))
                except TokenAcquisitionError:
                    raise ValueError() from None
                if not granted or not granted <= scopes:
                    raise ValueError()
                # Intentionally discard refresh_token and all nonstandard fields.
                return AcquiredToken(token, remaining, granted)
        except TokenAcquisitionError:
            raise
        except Exception:
            raise TokenAcquisitionError('temporarily_unavailable', 503) from None

    async def aclose(self) -> None:
        if self._owns_client:
            await self.client.aclose()


_FACTORIES: dict[str, Callable[[Mapping], TokenAcquisitionProvider]] = {
    'oauth2_client_credentials': lambda conf: OAuth2ClientCredentialsProvider(
        str(conf.get('integration.token.endpoint', '')),
        float(conf.get('integration.token.timeout_seconds', 3)),
        str(conf.get('integration.token.ca_file', ''))),
}


def register_token_acquisition_provider(provider_id: str, factory: Callable) -> None:
    """Register a business adapter separately from Bearer validation providers."""
    if not provider_id or provider_id in _FACTORIES:
        raise ValueError('Token acquisition provider must have a unique nonempty id')
    _FACTORIES[provider_id] = factory


class TokenAcquisitionService:
    def __init__(self, provider: TokenAcquisitionProvider, allowed_scopes: str, default_scope: str = '',
                 timeout: float = 3.0):
        if not isinstance(provider, TokenAcquisitionProvider):
            raise ValueError('Factory must return a TokenAcquisitionProvider')
        self.allowed_scopes = _scopes(allowed_scopes)
        self.default_scopes = _scopes(default_scope)
        if not self.allowed_scopes or not self.default_scopes <= self.allowed_scopes:
            raise ValueError('Token scopes must be explicitly configured within allowed_scopes')
        require_timeout(timeout)
        self.timeout = timeout
        self.provider = provider
        self._budget_key = secrets.token_bytes(32)

    def credential_budget_key(self, credentials: ClientCredentials) -> str:
        """Private, non-reversible bucket; spoofed IDs cannot spend another secret's budget."""
        value = json.dumps([credentials.client_id, credentials.client_secret], ensure_ascii=True).encode()
        return hmac.new(self._budget_key, value, hashlib.sha256).hexdigest()

    async def acquire(self, credentials: ClientCredentials, scope: Optional[str]) -> AcquiredToken:
        requested = _scopes(scope) if scope is not None else self.default_scopes
        if not requested or not requested <= self.allowed_scopes:
            raise TokenAcquisitionError('invalid_scope')
        try:
            async with asyncio.timeout(self.timeout):
                token = await self.provider.acquire(credentials, requested)
        except TokenAcquisitionError:
            raise
        except Exception:
            raise TokenAcquisitionError('temporarily_unavailable', 503) from None
        # The service boundary also enforces the contract for business adapters.
        if (not isinstance(token, AcquiredToken) or not token.scopes
                or not token.scopes <= requested or type(token.expires_in) is not int
                or token.expires_in <= 0 or not isinstance(token.access_token, str)
                or not token.access_token or len(token.access_token) > 16384
                or any(ord(c) <= 32 or ord(c) >= 127 for c in token.access_token)):
            raise TokenAcquisitionError('temporarily_unavailable', 503)
        return token


_service: Optional[TokenAcquisitionService] = None
_configured = False
_ANY_SERVICE = object()


def configure_token_acquisition(config: Mapping) -> Optional[TokenAcquisitionService]:
    """Called before binding the integration listener; fail invalid enabled config early."""
    global _service, _configured
    if _configured and _service is not None:
        raise RuntimeError('Token acquisition is already configured; close before reconfiguring')
    conf = resolve_env_vars(dict(config))
    if str(conf.get('integration.token.enabled', 'false')).lower() == 'true':
        provider_id = str(conf.get('integration.token.provider', 'oauth2_client_credentials'))
        if provider_id not in _FACTORIES:
            raise ValueError('Unknown token acquisition provider')
        # Validate policy before creating a transport resource.
        allowed = str(conf.get('integration.token.allowed_scopes', ''))
        default = str(conf.get('integration.token.default_scope', ''))
        if not _scopes(allowed) or not _scopes(default) <= _scopes(allowed):
            raise ValueError('Token scope configuration is invalid')
        timeout = float(conf.get('integration.token.timeout_seconds', 3))
        require_timeout(timeout)  # Validate before the factory allocates resources.
        _service = TokenAcquisitionService(_FACTORIES[provider_id](conf), allowed, default, timeout)
    _configured = True
    return _service


def get_token_acquisition() -> Optional[TokenAcquisitionService]:
    if not _configured:
        configure_token_acquisition(get_conf())
    return _service


async def close_token_acquisition(expected_service=_ANY_SERVICE) -> None:
    global _service, _configured
    if expected_service is not _ANY_SERVICE and _service is not expected_service:
        return
    try:
        if _service is not None:
            await _service.provider.aclose()
    finally:
        _service, _configured = None, False
