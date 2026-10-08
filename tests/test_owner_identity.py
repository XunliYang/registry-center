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

"""
R1 regression tests: a caller identity (and therefore an AgentCard owner) must
come from a credential the client cannot choose.

Covers the pure resolver (peer certificate vs forged ``X-SSL-Client-DN``
header, trusted-proxy allowlist) and an end-to-end main-port run over real
mutual TLS, where a forged header must not beat the presented certificate.
"""

import datetime
import ssl
import threading
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
import uvicorn
from a2a.types import AgentCard
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from fastapi.testclient import TestClient
from starlette.requests import Request

from agent_registry import server as server_mod
from agent_registry.identity import (
    CERTIFICATE,
    NONE,
    TRUSTED_PROXY,
    cn_from_peer_cert,
    direct_peer_ip,
    identity_mode,
    install_tls_peer_cert_injection,
    resolve_caller_identity,
    strict_startup_failures,
)
from agent_registry.health import HealthStatus
from agent_registry.persistence.base import AgentRecord
from agent_registry.server import app, get_registry, get_registry_signer, get_signature_validator
from test_integration_cert_auth import _free_port, _gen_key, _make_cert, _write_cert, _write_key

VALID_AGENT_CARD = {
    "name": "TestAgent",
    "provider": {"organization": "TestOrg", "url": "https://test.org"},
    "description": "A test agent for testing",
    "version": "1.0.0",
    "capabilities": {"streaming": False},
    "default_input_modes": ["text/plain"],
    "default_output_modes": ["text/plain"],
    "skills": [{
        "id": "s1", "name": "TestSkill", "description": "Test",
        "tags": [], "input_modes": ["text/plain"], "output_modes": ["text/plain"]
    }]
}

PEER_CERT_A = {"subject": ((("commonName", "agent-a"),), (("organizationName", "TestOrg"),))}
PEER_CERT_B = {"subject": ((("commonName", "agent-b"),),)}

BASE_CONFIG = {
    "owner.identity.mode": CERTIFICATE,
    "owner.trusted.proxy.ips": "",
    "owner.validation.mode": "strict",
    "owner.isolation.enabled": "true",
}


def _request(peer_cert=None, direct_peer=("127.0.0.1", 34567), headers=None):
    raw_headers = [(str(k).lower().encode(), str(v).encode())
                   for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "PUT",
        "scheme": "https",
        "path": "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent",
        "raw_path": b"/",
        "query_string": b"",
        "headers": raw_headers,
        "client": direct_peer,
        "server": ("127.0.0.1", 5000),
        "tls_peer_cert": peer_cert,
        "tls_direct_peer": direct_peer,
    }
    return Request(scope)


class TestPeerCertificateParsing:

    def test_cn_extracted_from_validated_certificate(self):
        assert cn_from_peer_cert(PEER_CERT_A) == "agent-a"

    def test_cn_extracted_from_rfc4514_subject(self):
        assert cn_from_peer_cert("CN=agent-a,OU=test,O=TestOrg") == "agent-a"

    def test_unvalidated_certificate_yields_no_identity(self):
        # ssl.getpeercert() returns {} when the TLS stack did not validate it.
        assert cn_from_peer_cert({}) is None
        assert cn_from_peer_cert(None) is None

    def test_verified_config_survives_successive_installs(self):
        # Idempotent: repeated installation must not stack wrappers.
        install_tls_peer_cert_injection()
        install_tls_peer_cert_injection()
        import uvicorn.protocols.http.h11_impl as h11_impl
        assert getattr(h11_impl.RequestResponseCycle.run_asgi, "_registry_peer_cert_patched", False)


class TestIdentityResolution:

    def test_certificate_mode_uses_verified_certificate(self):
        identity = resolve_caller_identity(_request(peer_cert=PEER_CERT_A), dict(BASE_CONFIG))
        assert (identity.owner, identity.source, identity.verified) == ("agent-a", CERTIFICATE, True)

    def test_certificate_mode_ignores_forged_header(self):
        """The header is not an identity source: no certificate means no owner."""
        request = _request(peer_cert=None, headers={"X-SSL-Client-DN": "CN=agent-a"})
        identity = resolve_caller_identity(request, dict(BASE_CONFIG))
        assert identity.verified is False
        assert identity.owner is None
        assert identity.audit_identity() == ""

    def test_certificate_mode_ignores_mismatched_header(self):
        request = _request(peer_cert=PEER_CERT_B,
                           headers={"X-SSL-Client-DN": "CN=agent-a"})
        identity = resolve_caller_identity(request, dict(BASE_CONFIG))
        assert identity.owner == "agent-b"

    def test_trusted_proxy_mode_accepts_header_from_listed_proxy(self):
        config = dict(BASE_CONFIG, **{"owner.identity.mode": TRUSTED_PROXY,
                                      "owner.trusted.proxy.ips": "127.0.0.1"})
        identity = resolve_caller_identity(
            _request(headers={"X-SSL-Client-DN": "CN=agent-a"}),
            config)
        assert (identity.owner, identity.verified) == ("agent-a", True)

    def test_trusted_proxy_mode_ignores_header_from_unlisted_peer(self):
        config = dict(BASE_CONFIG, **{"owner.identity.mode": TRUSTED_PROXY,
                                      "owner.trusted.proxy.ips": "10.0.0.1"})
        identity = resolve_caller_identity(
            _request(headers={"X-SSL-Client-DN": "CN=agent-a"}), config)
        assert identity.verified is False
        assert "not a trusted proxy" in identity.detail

    def test_trusted_proxy_mode_without_allowlist_is_fail_closed(self):
        config = dict(BASE_CONFIG, **{"owner.identity.mode": TRUSTED_PROXY})
        identity = resolve_caller_identity(
            _request(headers={"X-SSL-Client-DN": "CN=agent-a"}), config)
        assert identity.verified is False

    def test_forwarded_for_cannot_promote_an_untrusted_peer(self):
        """scope['client'] may hold a proxy-supplied X-Forwarded-For value."""
        request = _request(peer_cert=None, direct_peer=("203.0.113.9", 1234),
                           headers={"X-SSL-Client-DN": "CN=agent-a"})
        request.scope["client"] = ("127.0.0.1", 1234)  # forwarded value
        config = dict(BASE_CONFIG, **{"owner.identity.mode": TRUSTED_PROXY,
                                      "owner.trusted.proxy.ips": "127.0.0.1"})
        identity = resolve_caller_identity(request, config)
        assert identity.verified is False
        assert direct_peer_ip(request.scope) == "203.0.113.9"

    def test_none_mode_trusts_nothing(self):
        config = dict(BASE_CONFIG, **{"owner.identity.mode": NONE})
        identity = resolve_caller_identity(_request(peer_cert=PEER_CERT_A), config)
        assert identity.verified is False and identity.owner is None

    def test_unknown_mode_is_fail_closed(self):
        config = dict(BASE_CONFIG, **{"owner.identity.mode": "header"})
        assert identity_mode(config) == NONE
        identity = resolve_caller_identity(_request(peer_cert=PEER_CERT_A), config)
        assert identity.verified is False

    def test_invalid_cn_in_strict_mode_is_rejected(self):
        cert = {"subject": ((("commonName", "bad_cn!"),),)}
        identity = resolve_caller_identity(_request(peer_cert=cert), dict(BASE_CONFIG))
        assert identity.verified is False


def _mock_agent(name="TestAgent", org="TestOrg"):
    return AgentCard(
        name=name, provider={"organization": org, "url": "https://test.org"},
        description="Test", version="1.0.0", capabilities={"streaming": False},
        default_input_modes=[], default_output_modes=[], skills=[])


def _fake_registry(stored_owner):
    registry = MagicMock()
    registry.count.return_value = 0
    registry.get_agents.return_value = {}
    registry.get_status.return_value = "published"
    registry.get_by_key_with_owner.return_value = AgentRecord(
        agent_card=_mock_agent(), owner=stored_owner, status="published")
    return registry


def _tls_client(peer_cert=None, direct_peer=("127.0.0.1", 34567), headers=None):
    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            scope["tls_peer_cert"] = peer_cert
            scope["tls_direct_peer"] = direct_peer
        await app(scope, receive, send)
    return TestClient(wrapped, headers=headers)


@pytest.fixture
def isolated_owner_config(monkeypatch):
    """Owner isolation on, certificate identity, shared mocked registry."""
    monkeypatch.setattr(server_mod, "OWNER_ISOLATION_ENABLED", True)
    monkeypatch.setattr(server_mod, "OWNER_VALIDATION_MODE", "strict")
    monkeypatch.setitem(server_mod.config, "owner.isolation.enabled", "true")
    monkeypatch.setitem(server_mod.config, "owner.identity.mode", CERTIFICATE)
    monkeypatch.setitem(server_mod.config, "owner.trusted.proxy.ips", "")
    monkeypatch.setitem(server_mod.config, "owner.validation.mode", "strict")
    registry = _fake_registry("agent-a")
    validator = MagicMock()
    validator.validate_agent_card_async = AsyncMock(return_value=MagicMock(is_valid=True))
    signer = MagicMock()
    signer.is_enabled.return_value = False
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_signature_validator] = lambda: validator
    app.dependency_overrides[get_registry_signer] = lambda: signer
    yield registry
    app.dependency_overrides.clear()


class TestOwnerAuthorizationOverHTTP:

    def test_owner_with_verified_certificate_may_update(self, isolated_owner_config):
        with patch("common.custom.custom_handle.HandlerRegistry.get_handler",
                   return_value=MagicMock(handle=AsyncMock(return_value=True))):
            response = _tls_client(peer_cert=PEER_CERT_A).put(
                "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent",
                json={"agentCards": [VALID_AGENT_CARD]})
        assert response.status_code == 200

    def test_different_verified_certificate_is_forbidden(self, isolated_owner_config):
        response = _tls_client(peer_cert=PEER_CERT_B).put(
            "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent",
            json={"agentCards": [VALID_AGENT_CARD]})
        assert response.status_code == 403

    def test_forged_header_without_certificate_is_rejected(self, isolated_owner_config):
        """Old behaviour returned 200 here; the header must not grant ownership."""
        response = _tls_client(peer_cert=None,
                               headers={"X-SSL-Client-DN": "CN=agent-a"}).put(
            "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent",
            json={"agentCards": [VALID_AGENT_CARD]})
        assert response.status_code == 401

    def test_delete_with_forged_header_is_rejected(self, isolated_owner_config):
        response = _tls_client(peer_cert=None,
                               headers={"X-SSL-Client-DN": "CN=agent-a"}).delete(
            "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent")
        assert response.status_code == 401

    def test_register_with_forged_header_is_rejected_in_strict_mode(self, isolated_owner_config):
        response = _tls_client(peer_cert=None,
                               headers={"X-SSL-Client-DN": "CN=agent-a"}).post(
            "/rest/v1/registry-center/agent-cards",
            json={"agentCards": [VALID_AGENT_CARD]})
        assert response.status_code == 401

    def test_register_with_verified_certificate_binds_owner(self, isolated_owner_config):
        handler = MagicMock()
        handler.handle = AsyncMock(return_value=True)
        isolated_owner_config.get_by_key_with_owner.return_value = None
        with patch("common.custom.custom_handle.HandlerRegistry.get_handler",
                   return_value=handler):
            response = _tls_client(peer_cert=PEER_CERT_A).post(
                "/rest/v1/registry-center/agent-cards",
                json={"agentCards": [VALID_AGENT_CARD]})
        assert response.status_code == 201
        assert handler.handle.call_args.kwargs.get("owner") == "agent-a"

    def test_register_in_relaxed_mode_binds_no_owner_without_credential(
            self, isolated_owner_config, monkeypatch):
        monkeypatch.setattr(server_mod, "OWNER_VALIDATION_MODE", "relaxed")
        handler = MagicMock()
        handler.handle = AsyncMock(return_value=True)
        isolated_owner_config.get_by_key_with_owner.return_value = None
        with patch("common.custom.custom_handle.HandlerRegistry.get_handler",
                   return_value=handler):
            response = _tls_client(peer_cert=None).post(
                "/rest/v1/registry-center/agent-cards",
                json={"agentCards": [VALID_AGENT_CARD]})
        assert response.status_code == 201
        assert handler.handle.call_args.kwargs.get("owner") is None

    def test_ownerless_card_cannot_be_deleted_without_identity(self, isolated_owner_config):
        isolated_owner_config.get_by_key_with_owner.return_value = AgentRecord(
            agent_card=_mock_agent(), owner=None, status="published")
        with patch("common.custom.custom_handle.HandlerRegistry.get_handler",
                   return_value=MagicMock(handle=AsyncMock(return_value=True))):
            response = _tls_client(peer_cert=None).delete(
                "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent")
        assert response.status_code == 401

    def test_heartbeat_from_same_owner_is_accepted(self, isolated_owner_config, monkeypatch):
        monkeypatch.setattr(server_mod, "get_health_service",
                            lambda: SimpleNamespace(enabled=True, record_heartbeat=lambda n, o: (None, SimpleNamespace(status=HealthStatus.HEALTHY)),
                                                    interval=30, failure_threshold=3, grace_period=10))
        response = _tls_client(peer_cert=PEER_CERT_A).post(
            "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent/heartbeat")
        assert response.status_code == 200

    def test_heartbeat_from_another_owner_is_forbidden(self, isolated_owner_config, monkeypatch):
        """A liveness claim must not be forgeable for someone else's card.

        Health hiding and the offline TTL both key off heartbeats: without this
        check any authenticated caller could keep a dead card looking alive (and
        emit a public health-recovery event for it).
        """
        monkeypatch.setattr(server_mod, "get_health_service",
                            lambda: SimpleNamespace(enabled=True, record_heartbeat=lambda n, o: (None, SimpleNamespace(status=HealthStatus.HEALTHY)),
                                                    interval=30, failure_threshold=3, grace_period=10))
        response = _tls_client(peer_cert=PEER_CERT_B).post(
            "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent/heartbeat")
        assert response.status_code == 403

    def test_heartbeat_without_verified_identity_is_rejected(self, isolated_owner_config, monkeypatch):
        monkeypatch.setattr(server_mod, "get_health_service",
                            lambda: SimpleNamespace(enabled=True, record_heartbeat=lambda n, o: (None, SimpleNamespace(status=HealthStatus.HEALTHY)),
                                                    interval=30, failure_threshold=3, grace_period=10))
        response = _tls_client(peer_cert=None,
                               headers={"X-SSL-Client-DN": "CN=agent-a"}).post(
            "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent/heartbeat")
        assert response.status_code == 401

    def test_ownerless_card_requires_admin_claim(self, isolated_owner_config):
        isolated_owner_config.get_by_key_with_owner.return_value = AgentRecord(
            agent_card=_mock_agent(), owner=None, status="published")
        with patch("common.custom.custom_handle.HandlerRegistry.get_handler",
                   return_value=MagicMock(handle=AsyncMock(return_value=True))):
            response = _tls_client(peer_cert=PEER_CERT_A).delete(
                "/rest/v1/registry-center/agent-cards/TestOrg/TestAgent")
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# End-to-end main port over real mutual TLS
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def main_port_pki(tmp_path_factory):
    root = tmp_path_factory.mktemp("main-port-pki")
    ca_key = _gen_key()
    ca_cert = _make_cert("Main Port Test CA", ca_key, None, None,
                         datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
                         is_ca=True)
    _write_cert(root / "ca.cer", ca_cert)

    server_key = _gen_key()
    server_cert = (x509.CertificateBuilder()
                   .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "127.0.0.1")]))
                   .issuer_name(ca_cert.subject)
                   .public_key(server_key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
                   .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.SubjectAlternativeName([
                       x509.IPAddress(__import__("ipaddress").IPv4Address("127.0.0.1"))]), critical=False)
                   .sign(ca_key, hashes.SHA256()))
    _write_key(root / "server.key", server_key)
    _write_cert(root / "server.cer", server_cert)

    clients = {}
    for cn in ("agent-a", "agent-b"):
        key = _gen_key()
        cert = _make_cert(cn, key, ca_cert, ca_key,
                          datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
        _write_key(root / f"{cn}.key", key)
        _write_cert(root / f"{cn}.cer", cert)
        clients[cn] = (root / f"{cn}.cer", root / f"{cn}.key")
    return {"root": root, "ca": root / "ca.cer",
            "server_cert": root / "server.cer", "server_key": root / "server.key",
            "clients": clients}


def _client_context(pki, client_cert=None):
    context = ssl.create_default_context(cafile=str(pki["ca"]))
    if client_cert:
        context.load_cert_chain(str(client_cert[0]), str(client_cert[1]))
    return context


@pytest.fixture
def main_port_server(main_port_pki, monkeypatch):
    """Run the real main-port app over mutual TLS (startup handlers disabled)."""
    install_tls_peer_cert_injection()
    monkeypatch.setattr(server_mod, "OWNER_ISOLATION_ENABLED", True)
    monkeypatch.setattr(server_mod, "OWNER_VALIDATION_MODE", "strict")
    monkeypatch.setitem(server_mod.config, "owner.isolation.enabled", "true")
    monkeypatch.setitem(server_mod.config, "owner.identity.mode", CERTIFICATE)
    monkeypatch.setitem(server_mod.config, "owner.trusted.proxy.ips", "")
    monkeypatch.setitem(server_mod.config, "owner.validation.mode", "strict")
    registry = _fake_registry("agent-a")
    # FastAPI overrides alone do not inject the default handler chain. Both
    # authorization and mutation must use the same isolated registry instance.
    monkeypatch.setattr('agent_registry.registry_instance._registry_instance', registry)
    app.dependency_overrides[get_registry] = lambda: registry
    app.dependency_overrides[get_signature_validator] = lambda: MagicMock(
        validate_agent_card_async=AsyncMock(return_value=MagicMock(is_valid=True)))
    app.dependency_overrides[get_registry_signer] = lambda: MagicMock(
        is_enabled=MagicMock(return_value=False))

    port = _free_port()
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port, log_level="warning", lifespan="off",
        ssl_certfile=str(main_port_pki["server_cert"]),
        ssl_keyfile=str(main_port_pki["server_key"]),
        ssl_ca_certs=str(main_port_pki["ca"]),
        ssl_cert_reqs=ssl.CERT_REQUIRED,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    assert server.started, "main port TLS server failed to start"
    try:
        yield port, registry
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        app.dependency_overrides.clear()


class TestOwnerAuthorizationOverMutualTLS:

    def test_certificate_owner_may_update(self, main_port_pki, main_port_server):
        port, _ = main_port_server
        with httpx.Client(verify=_client_context(main_port_pki, main_port_pki["clients"]["agent-a"])) as client:
            response = client.put(
                f"https://127.0.0.1:{port}/rest/v1/registry-center/agent-cards/TestOrg/TestAgent",
                json={"agentCards": [VALID_AGENT_CARD]})
        assert response.status_code == 200

    def test_forged_header_cannot_override_presented_certificate(self, main_port_pki, main_port_server):
        """CN=agent-b presented; header claims agent-a; the certificate wins."""
        port, _ = main_port_server
        with httpx.Client(verify=_client_context(main_port_pki, main_port_pki["clients"]["agent-b"])) as client:
            response = client.put(
                f"https://127.0.0.1:{port}/rest/v1/registry-center/agent-cards/TestOrg/TestAgent",
                headers={"X-SSL-Client-DN": "CN=agent-a"},
                json={"agentCards": [VALID_AGENT_CARD]})
        assert response.status_code == 403

    def test_certificate_owner_is_not_confused_by_foreign_header(self, main_port_pki, main_port_server):
        port, _ = main_port_server
        with httpx.Client(verify=_client_context(main_port_pki, main_port_pki["clients"]["agent-a"])) as client:
            response = client.put(
                f"https://127.0.0.1:{port}/rest/v1/registry-center/agent-cards/TestOrg/TestAgent",
                headers={"X-SSL-Client-DN": "CN=agent-b"},
                json={"agentCards": [VALID_AGENT_CARD]})
        assert response.status_code == 200

    def test_write_without_client_certificate_is_rejected_at_tls(self, main_port_pki, main_port_server):
        port, _ = main_port_server
        with pytest.raises(httpx.HTTPError):
            with httpx.Client(verify=_client_context(main_port_pki)) as client:
                client.put(
                    f"https://127.0.0.1:{port}/rest/v1/registry-center/agent-cards/TestOrg/TestAgent",
                    headers={"X-SSL-Client-DN": "CN=agent-a"},
                    json={"agentCards": [VALID_AGENT_CARD]})


class TestStrictStartupIdentity:
    """startup.strict.identity=true promotes fail-closed warnings to a hard stop."""

    BROKEN_CERTIFICATE_CONFIG = {
        'startup.strict.identity': 'true',
        'owner.isolation.enabled': 'true',
        'owner.identity.mode': CERTIFICATE,
        'verify_client': 'false',
    }

    def test_disabled_by_default(self):
        config = dict(self.BROKEN_CERTIFICATE_CONFIG)
        config.pop('startup.strict.identity')

        assert strict_startup_failures(config) == []

    def test_clean_configuration_passes(self):
        config = dict(self.BROKEN_CERTIFICATE_CONFIG, verify_client='true')

        assert strict_startup_failures(config) == []

    def test_http_listener_cannot_supply_certificate_identity(self):
        config = dict(self.BROKEN_CERTIFICATE_CONFIG, verify_client='true', enable_https='false')
        failures = strict_startup_failures(config)
        assert len(failures) == 1
        assert 'enable_https=false' in failures[0]

    def test_http_with_verified_proxy_identity_passes(self):
        config = dict(self.BROKEN_CERTIFICATE_CONFIG, enable_https='false')
        config.update({'owner.identity.mode': TRUSTED_PROXY,
                       'owner.trusted.proxy.ips': '127.0.0.1'})
        assert strict_startup_failures(config) == []

    def test_misconfiguration_is_reported(self):
        failures = strict_startup_failures(self.BROKEN_CERTIFICATE_CONFIG)

        assert len(failures) == 1
        assert 'verify_client=false' in failures[0]

    def test_underscore_key_spelling_is_accepted(self):
        """Env overrides land as startup_strict_identity (REGISTRY_ prefix stripped)."""
        config = {'startup_strict_identity': 'true',
                  'owner.isolation.enabled': 'true',
                  'owner.identity.mode': NONE}

        assert strict_startup_failures(config)

    def test_isolation_disabled_needs_no_identity_source(self):
        config = {'startup.strict.identity': 'true',
                  'owner.isolation.enabled': 'false',
                  'owner.identity.mode': NONE}

        assert strict_startup_failures(config) == []
