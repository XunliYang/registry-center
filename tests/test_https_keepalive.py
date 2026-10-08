# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Production listeners must not immediately close reusable HTTP connections."""
from types import SimpleNamespace

import pytest
import uvicorn


@pytest.fixture(autouse=True)
def preserve_logging_and_proxy_environment(monkeypatch):
    # Config construction must not reconfigure other tests' process-wide logs.
    monkeypatch.setattr(uvicorn.Config, 'configure_logging', lambda self: None)
    monkeypatch.setenv('FORWARDED_ALLOW_IPS', '')


def test_main_https_uses_finite_positive_idle_keepalive(monkeypatch):
    import agent_registry.start as start

    captured = []

    class InspectServer:
        def __init__(self, config):
            captured.append(config)

        def run(self):
            pass

    monkeypatch.setattr(start.uvicorn, 'Server', InspectServer)
    monkeypatch.setattr(start, 'load_cert_password', lambda path: b'')
    conf = SimpleNamespace(ssl_certfile='synthetic.cer', ssl_keyfile='synthetic.pem',
                           ssl_keyfile_password='', ssl_ca_certs='synthetic-ca.cer', verify_client=2)
    start.CustomUvicornServer({'ip': '127.0.0.1', 'port': '0', 'forwarded_allow_ips': '',
                              'tls.cipher': 'TLS_AES_256_GCM_SHA384'}, conf).run()
    assert len(captured) == 1
    assert captured[0].timeout_keep_alive == uvicorn.Config('unused:app').timeout_keep_alive
    assert captured[0].timeout_keep_alive > 0


@pytest.mark.parametrize('require_cert', [False, True])
def test_integration_https_uses_same_idle_keepalive(monkeypatch, require_cert):
    from agent_registry.integration import listener

    captured = []

    class StopBeforeTLS(Exception):
        pass

    def inspect_config(config):
        captured.append(config)
        raise StopBeforeTLS

    # Inspect the real configuration before certificates/providers are allocated.
    monkeypatch.setattr(listener.uvicorn.Config, 'load', inspect_config)
    monkeypatch.setattr(listener, 'load_cert_password', lambda path: b'')
    conf = SimpleNamespace(ssl_certfile='synthetic.cer', ssl_keyfile='synthetic.pem',
                           ssl_keyfile_password='', ssl_ca_certs='synthetic-ca.cer')
    server = listener.ThirdPartyAccessServer({'integration.enabled': 'true',
        'integration.port': '0', 'integration.client_cert': str(require_cert).lower()}, conf)
    with pytest.raises(StopBeforeTLS):
        server.start()
    assert len(captured) == 1
    assert captured[0].timeout_keep_alive == uvicorn.Config('unused:app').timeout_keep_alive
    assert captured[0].timeout_keep_alive > 0
    assert server._thread is None and server._server is None
