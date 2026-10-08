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
Certificate authentication integration tests (task 2.3).

Generates a self-signed CA, server certificate, and client certificates
(valid / expired / revoked + CRL) with the cryptography library, runs the
integration listener with mTLS required, and connects over real TLS.
"""

import socket
import datetime
import threading

import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

from agent_registry.integration.listener import ThirdPartyAccessServer


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _gen_key():
    # 3072-bit: matches the certificate strength rule enforced by the
    # pre-check tool and the startup CertValidator
    return rsa.generate_private_key(public_exponent=65537, key_size=3072)


def _name(cn: str):
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _write_key(path, key):
    path.write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL,
        serialization.NoEncryption()))


def _write_cert(path, cert):
    path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def _make_cert(cn, key, issuer_cert, issuer_key, not_after, is_ca=False,
               not_before=None):
    builder = (x509.CertificateBuilder()
               .subject_name(_name(cn))
               .issuer_name(issuer_cert.subject if issuer_cert else _name(cn))
               .public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(not_before or (datetime.datetime.now(datetime.timezone.utc)
                                                - datetime.timedelta(days=1)))
               .not_valid_after(not_after)
               .add_extension(x509.BasicConstraints(ca=is_ca, path_length=None), critical=True)
               .add_extension(x509.KeyUsage(
                   digital_signature=not is_ca,
                   content_commitment=False,
                   key_encipherment=not is_ca,
                   data_encipherment=False,
                   key_agreement=False,
                   key_cert_sign=is_ca,
                   crl_sign=is_ca,
                   encipher_only=False,
                   decipher_only=False,
               ), critical=True)
               .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()),
                              critical=False))
    if issuer_cert is not None and issuer_key is not None:
        builder = builder.add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
    return builder.sign(issuer_key or key, hashes.SHA256())


def _load_cert(path):
    return x509.load_pem_x509_certificate(path.read_bytes())


def ip(addr):
    import ipaddress
    return ipaddress.IPv4Address(addr)


class _StubConfObj:
    def __init__(self, server_cert, server_key, ca_certs, crl_file, crl_serials=()):
        self.ssl_certfile = str(server_cert)
        self.ssl_keyfile = str(server_key)
        self.ssl_keyfile_password = ""
        self.ssl_ca_certs = str(ca_certs)
        self.ssl_crl_file = str(crl_file) if crl_file else ""
        self.crl_serials = crl_serials

    def get_crl_list(self):
        return self.crl_serials


@pytest.fixture(scope="module")
def pki(tmp_path_factory):
    """Generate CA, server cert, and three client certs (valid/expired/revoked) + CRL."""
    root = tmp_path_factory.mktemp("pki")

    ca_key, ca_cert = _gen_key(), None
    ca_cert = _make_cert("Test CA", ca_key, None, None,
                         datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30),
                         is_ca=True)
    _write_cert(root / "ca.cer", ca_cert)

    server_key = _gen_key()
    server_cert = (x509.CertificateBuilder()
                   .subject_name(_name("127.0.0.1"))
                   .issuer_name(ca_cert.subject)
                   .public_key(server_key.public_key())
                   .serial_number(x509.random_serial_number())
                   .not_valid_before(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
                   .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
                   .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
                   .add_extension(x509.KeyUsage(
                       digital_signature=True,
                       content_commitment=False,
                       key_encipherment=True,
                       data_encipherment=False,
                       key_agreement=False,
                       key_cert_sign=False,
                       crl_sign=False,
                       encipher_only=False,
                       decipher_only=False,
                   ), critical=True)
                   .add_extension(x509.SubjectKeyIdentifier.from_public_key(server_key.public_key()),
                                  critical=False)
                   .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()),
                                  critical=False)
                   .add_extension(x509.SubjectAlternativeName([x509.IPAddress(ip("127.0.0.1"))]),
                                  critical=False)
                   .sign(ca_key, hashes.SHA256()))
    _write_key(root / "server.key", server_key)
    _write_cert(root / "server.cer", server_cert)

    # valid client cert, CN mapped in credentials
    valid_key = _gen_key()
    valid_cert = _make_cert("partner_valid_cn", valid_key, ca_cert, ca_key,
                            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
    _write_key(root / "client_valid.key", valid_key)
    _write_cert(root / "client_valid.cer", valid_cert)

    # unmapped client cert (valid chain, CN not in credentials)
    unmapped_key = _gen_key()
    unmapped_cert = _make_cert("unmapped_cn", unmapped_key, ca_cert, ca_key,
                               datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
    _write_key(root / "client_unmapped.key", unmapped_key)
    _write_cert(root / "client_unmapped.cer", unmapped_cert)

    # expired client cert
    expired_key = _gen_key()
    expired_cert = _make_cert("expired_cn", expired_key, ca_cert, ca_key,
                              datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=5),
                              not_before=datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=10))
    _write_key(root / "client_expired.key", expired_key)
    _write_cert(root / "client_expired.cer", expired_cert)

    # revoked client cert + CRL
    revoked_key = _gen_key()
    revoked_cert = _make_cert("revoked_cn", revoked_key, ca_cert, ca_key,
                              datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
    _write_key(root / "client_revoked.key", revoked_key)
    _write_cert(root / "client_revoked.cer", revoked_cert)
    revoked_serial = revoked_cert.serial_number
    crl = (x509.CertificateRevocationListBuilder()
           .issuer_name(ca_cert.subject)
           .last_update(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
           .next_update(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=30))
           .add_revoked_certificate(x509.RevokedCertificateBuilder()
                                    .serial_number(revoked_serial)
                                    .revocation_date(datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(days=1))
                                    .build()))
    crl = crl.sign(ca_key, hashes.SHA256())
    (root / "revocationlist.crl").write_bytes(crl.public_bytes(serialization.Encoding.PEM))

    return {
        "root": root,
        "ca": root / "ca.cer",
        "server_cert": root / "server.cer",
        "server_key": root / "server.key",
        "valid": (root / "client_valid.cer", root / "client_valid.key"),
        "unmapped": (root / "client_unmapped.cer", root / "client_unmapped.key"),
        "expired": (root / "client_expired.cer", root / "client_expired.key"),
        "revoked": (root / "client_revoked.cer", root / "client_revoked.key"),
        "revoked_serial": revoked_serial,
    }


def ip(addr):
    import ipaddress
    return ipaddress.IPv4Address(addr)


@pytest.fixture(scope="module")
def server(pki, tmp_path_factory):
    """Run the integration listener with mTLS required and CRL checking."""
    work = tmp_path_factory.mktemp("run")
    cred_file = work / "credentials.conf"
    cred_file.write_text(
        "credential.p1.cn=partner_valid_cn\n"
        "credential.p1.role=partner_service\n"
        "credential.p1.owner=partner_owner\n",
        encoding="utf-8")

    # trust.cer must contain the CA; conf_obj crl hooks use the CRL file
    crl_serial_hex = hex(pki["revoked_serial"])
    stub = _StubConfObj(pki["server_cert"], pki["server_key"], pki["ca"],
                        pki["root"] / "revocationlist.crl", crl_serials=[crl_serial_hex])

    port = _free_port()
    server = ThirdPartyAccessServer(
        {"integration.enabled": "true", "integration.ip": "127.0.0.1",
         "integration.port": str(port), "integration.client_cert": "true",
         "integration.credential.file": str(cred_file)},
        conf_obj=stub)
    server.start()
    assert server._server.started, "listener failed to start"

    # The authn handler is a process-wide singleton reading its credential
    # file from server.conf; point it at the test credential file.
    import agent_registry.integration.authn as authn_module
    from common.custom.custom_handle import HandlerRegistry
    from common.custom.interface_type import InterfaceType
    handler = authn_module.ThirdPartyAuthnHandler(
        credential_file=str(cred_file), config={'integration.auth.mode': 'mtls'})
    HandlerRegistry._instances[InterfaceType.INTEGRATION_AUTHENTICATE.value] = handler

    yield server, port
    HandlerRegistry._instances.pop(InterfaceType.INTEGRATION_AUTHENTICATE.value, None)
    server.stop()


def _tls_context(pki, client_cert=None):
    """Prebuilt client TLS context. httpx's cert=(cert, key) tuple silently
    omits the certificate on some versions, so we build the context directly."""
    import ssl as _ssl
    ctx = _ssl.create_default_context(cafile=str(pki["ca"]))
    if client_cert:
        ctx.load_cert_chain(str(client_cert[0]), str(client_cert[1]))
    return ctx


class TestCertificateAuthentication:
    def test_valid_cert_authenticates_with_mapped_role(self, pki, server):
        _, port = server
        with httpx.Client(verify=_tls_context(pki, pki["valid"])) as client:
            resp = client.get(f"https://127.0.0.1:{port}/integration/v1/agent-cards")
        assert resp.status_code == 200

    def test_unmapped_cn_rejected_at_app_layer(self, pki, server):
        _, port = server
        with httpx.Client(verify=_tls_context(pki, pki["unmapped"])) as client:
            resp = client.get(f"https://127.0.0.1:{port}/integration/v1/agent-cards")
        assert resp.status_code == 401

    def test_expired_cert_rejected_at_tls(self, pki, server):
        _, port = server
        with pytest.raises(httpx.HTTPError):
            with httpx.Client(verify=_tls_context(pki, pki["expired"])) as client:
                client.get(f"https://127.0.0.1:{port}/integration/v1/agent-cards")

    def test_revoked_cert_rejected_at_tls(self, pki, server):
        _, port = server
        with pytest.raises(httpx.HTTPError):
            with httpx.Client(verify=_tls_context(pki, pki["revoked"])) as client:
                client.get(f"https://127.0.0.1:{port}/integration/v1/agent-cards")

    def test_no_cert_rejected_at_tls(self, pki, server):
        _, port = server
        with pytest.raises(httpx.HTTPError):
            with httpx.Client(verify=_tls_context(pki)) as client:
                client.get(f"https://127.0.0.1:{port}/integration/v1/agent-cards")
