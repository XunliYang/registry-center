# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Async JWKS retrieval with the same trust boundary as other IAM requests.

PyJWT handles JWK parsing and signatures, not network access. Cache only the
current bounded key set; an unknown kid refreshes it once, without keeping old
per-key entries indefinitely. A failed refresh never extends cache lifetime.
"""

import asyncio
import json
import time

import httpx
import jwt

from agent_registry.integration.oauth_transport import iam_client, require_https_endpoint, require_timeout


class VerifiedJwkClient:
    def __init__(self, endpoint: str, timeout: float, ca_file: str = '',
                 client: httpx.AsyncClient | None = None):
        require_https_endpoint(endpoint)
        require_timeout(timeout)
        self.endpoint, self.timeout = endpoint, timeout
        self.client = client if client is not None else iam_client(ca_file)
        self._owns_client = client is None
        self._keys = []
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def _refresh(self):
        try:
            async with self.client.stream('GET', self.endpoint, timeout=self.timeout,
                                          follow_redirects=False) as response:
                if response.status_code != 200:
                    raise ValueError('JWKS response unavailable')
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(body) + len(chunk) > 65536:
                        raise ValueError('JWKS response too large')
                    body.extend(chunk)
            data = json.loads(body)
            keys = [key for key in jwt.PyJWKSet.from_dict(data).keys
                    if key.public_key_use in ('sig', None) and key.key_id]
            if not keys:
                raise ValueError('JWKS has no signing keys')
        except Exception:
            # Neither the configured URL, upstream body nor adapter error is exposed.
            raise jwt.PyJWKClientConnectionError('JWKS unavailable') from None
        self._keys = keys
        self._expires_at = time.monotonic() + 300

    async def get_signing_key_from_jwt(self, token: str):
        kid = jwt.get_unverified_header(token).get('kid')
        if not isinstance(kid, str) or not kid:
            raise jwt.PyJWKClientError('JWT signing key missing')
        try:
            # Includes pool waits, lock waits, redirects, headers and complete body.
            async with asyncio.timeout(self.timeout):
                async with self._lock:
                    cached = bool(self._keys) and time.monotonic() < self._expires_at
                    if not cached:
                        await self._refresh()
                    key = next((key for key in self._keys if key.key_id == kid), None)
                    if key is None and cached:
                        await self._refresh()
                        key = next((key for key in self._keys if key.key_id == kid), None)
                    if key is None:
                        raise jwt.PyJWKClientError('JWT signing key unknown')
                    return key
        except TimeoutError:
            raise jwt.PyJWKClientConnectionError('JWKS unavailable') from None

    async def aclose(self):
        if self._owns_client:
            await self.client.aclose()
