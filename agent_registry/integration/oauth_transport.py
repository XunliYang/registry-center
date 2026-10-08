# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Verified, fixed-destination transport for integration IAM adapters."""

import math
import ssl
from pathlib import Path
from urllib.parse import quote_plus, urlsplit

import httpx

from common.util.app_config import get_root_path


def require_https_endpoint(endpoint: str) -> None:
    url = urlsplit(endpoint)
    if (url.scheme != 'https' or not url.hostname or url.username is not None
            or url.password is not None or url.fragment):
        raise ValueError('IAM endpoint must be an HTTPS URL without credentials or fragment')


def require_timeout(timeout: float) -> None:
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError('IAM timeout must be finite and positive')


def tls_context(ca_file: str = '') -> ssl.SSLContext:
    if not ca_file:
        return ssl.create_default_context()
    path = Path(ca_file)
    if not path.is_absolute():
        path = Path(get_root_path()) / path
    return ssl.create_default_context(cafile=str(path))


def iam_client(ca_file: str = '') -> httpx.AsyncClient:
    # Credentials must not reach an environment proxy or redirect destination.
    return httpx.AsyncClient(verify=tls_context(ca_file), trust_env=False,
                             follow_redirects=False,
                             limits=httpx.Limits(max_connections=20,
                                                 max_keepalive_connections=10))


def client_basic_auth(client_id: str, client_secret: str) -> httpx.BasicAuth:
    # OAuth 2.0 requires form encoding each component before HTTP Basic (RFC 6749 §2.3.1).
    return httpx.BasicAuth(quote_plus(client_id), quote_plus(client_secret))
