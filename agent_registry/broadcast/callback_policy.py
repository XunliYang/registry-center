# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Operator-authorized webhook destinations, checked at creation and delivery."""

from urllib.parse import urlsplit
from common.util.app_config import get_conf


def validate_callback_destination(url: str, config=None) -> None:
    config = get_conf() if config is None else config
    parsed = urlsplit(url)
    if parsed.scheme not in ('http', 'https') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('callback_url must be an HTTP(S) URL without embedded credentials')
    # Access .port to reject malformed/non-numeric/out-of-range ports.
    if parsed.port is not None and parsed.port not in range(1, 65536):
        raise ValueError('Invalid callback port')
    if parsed.scheme == 'http' and str(config.get('broadcast.allow.http.callbacks', 'false')).lower() != 'true':
        raise ValueError('HTTP callbacks are disabled; use HTTPS')
    hosts = {host.strip().lower().rstrip('.') for host in
             str(config.get('broadcast.callback.allowlist', '')).split(',') if host.strip()}
    if not hosts:
        raise ValueError('broadcast.callback.allowlist must authorize webhook destinations')
    if parsed.hostname.lower().rstrip('.') not in hosts:
        raise ValueError('Callback host is not authorized by broadcast.callback.allowlist')
