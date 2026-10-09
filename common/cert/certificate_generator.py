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
import os
import datetime
import ipaddress
import re
from typing import List
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
from cryptography.hazmat.primitives.asymmetric.types import PrivateKeyTypes

from common.cert.password_generator import PasswordGenerator
from common.util.cipher_util import encrypt, DEFAULT_ENCODING
from common.log.audit_logger import audit_logger, LogLevel, OperationResult, OperationName
from loguru import logger


class CertificateGenerator:
    """Certificate generation utility, providing certificate creation, validation, and related functions."""

    CERT_FILE = "server.cer"
    KEY_FILE = "server_key.pem"
    PWD_FILE = "cert_pwd"

    KEY_SIZE = 3072
    VALID_YEARS = 99
    ISSUER = "agent-registry"
    SUBJECT = "agent-registry"

    def __init__(self, key_algorithm: str = 'RSA', *,
                 dns_names: list[str] | None = None, ip_addresses: list[str] | None = None):
        """Use loopback SANs by default; explicit lists replace the complete SAN set."""
        self.key_algorithm = key_algorithm
        self.alg = key_algorithm
        self.password_generator = PasswordGenerator()
        local_defaults = dns_names is None and ip_addresses is None
        self.dns_names = ["localhost"] if local_defaults else list(dns_names or [])
        self.ip_addresses = ["127.0.0.1", "::1"] if local_defaults else list(ip_addresses or [])

    def _server_san(self) -> x509.SubjectAlternativeName:
        names = []
        for value in self.dns_names:
            name = value.rstrip(".").encode("idna").decode("ascii").lower()
            labels = name.split(".")
            if labels[0] == "*":
                labels = labels[1:]
            if not labels or len(name) > 253 or any(
                    not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                    for label in labels):
                raise ValueError("DNS SAN must be a hostname, not a URL or host:port")
            try:
                ipaddress.ip_address(name)
            except ValueError:
                names.append(x509.DNSName(name))
            else:
                raise ValueError("IP addresses must use ip_addresses / --ip, not DNS SAN")
        names.extend(x509.IPAddress(ipaddress.ip_address(value)) for value in self.ip_addresses)
        if not names:
            raise ValueError("A serverAuth certificate requires at least one DNS or IP SAN")
        return x509.SubjectAlternativeName(list(dict.fromkeys(names)))

    def generate_certificates(self, cert_dir: str, cert_usage: List[str]) -> bool:
        """
        Generate self-signed certificate.
        :param cert_dir: Certificate directory path.
        :param cert_usage: List of certificate usage types. Supports: serverAuth for TLS server auth, dataSigning for data signing
        :return: True on success, False on failure. False if certificates already exist in the target directory.
        """
        try:
            if self._check_certificates_exists(cert_dir):
                return False

            if not os.path.exists(cert_dir):
                os.makedirs(cert_dir, mode=0o700)

            private_key = self._generate_key()
            self._save_server_cert(cert_dir, private_key, cert_usage)
            password = self._generate_password()
            self._save_encrypted_key(cert_dir, private_key, password)
            self._save_encrypted_password(cert_dir, password)
            self._set_file_permissions(cert_dir)
            self._audit_log_generation(cert_dir)

            return True
        except Exception as e:
            logger.error(f"Certificate generation failed: {e}")
            return False

    def generate_self_signed_cert(self, cert_dir: str, cert_usage: str, password: str) -> bool:
        """
        Generate self-signed certificate (new API).
        :param cert_dir: Certificate directory path.
        :param cert_usage: Certificate usage type. Supports: serverAuth for TLS server auth, dataSigning for data signing
        :param password: Private key encryption password.
        :return: True on success, False on failure. False if certificates already exist in the target directory.
        """
        try:
            if cert_usage not in ("serverAuth", "dataSigning"):
                raise ValueError("Certificate usage must be serverAuth or dataSigning")
            if cert_usage == "serverAuth":
                self._server_san()  # Validate identities before any write.
            if self._check_self_signed_certificates_exists(cert_dir):
                return False

            if not os.path.exists(cert_dir):
                os.makedirs(cert_dir, mode=0o700)

            if not password:
                raise ValueError("Password cannot be empty")

            private_key = self._generate_key()
            self._save_self_signed_cert(cert_dir, private_key, cert_usage)
            self._save_encrypted_key_with_password(cert_dir, private_key, password)
            self._set_self_signed_file_permissions(cert_dir)

            return True
        except Exception as e:
            logger.error(f"Self-signed certificate generation failed: {e}")
            return False

    def _check_certificates_exists(self, cert_dir: str) -> bool:
        """
        Check if certificate files already exist in the directory.
        :param cert_dir: Certificate directory path.
        :return: True if ANY of the three files exist, False otherwise.
        """
        cert_path = os.path.join(cert_dir, self.CERT_FILE)
        key_path = os.path.join(cert_dir, self.KEY_FILE)
        pwd_path = os.path.join(cert_dir, self.PWD_FILE)

        return os.path.exists(cert_path) or os.path.exists(key_path) or os.path.exists(pwd_path)

    def _check_self_signed_certificates_exists(self, cert_dir: str) -> bool:
        """
        Check if self-signed certificate files already exist in the directory.
        :param cert_dir: Certificate directory path.
        :return: True if either of the two files exists, False otherwise.
        """
        cert_file = f"server_{self.alg}.cer"
        key_file = f"server_key_{self.alg}.pem"

        cert_path = os.path.join(cert_dir, cert_file)
        key_path = os.path.join(cert_dir, key_file)

        return os.path.lexists(cert_path) or os.path.lexists(key_path)

    def _generate_key(self) -> PrivateKeyTypes:
        """
        Generate a signing key with the specified algorithm.
        :return: Private key object.
        """
        if self.key_algorithm.upper() == 'RSA':
            return rsa.generate_private_key(
                public_exponent=65537,
                key_size=self.KEY_SIZE
            )
        else:
            raise ValueError(f"Unsupported key algorithm: {self.key_algorithm}")

    def _save_server_cert(self, cert_dir: str, private_key: PrivateKeyTypes, cert_usage: List[str]) -> None:
        """
        Use the private key to generate a self-signed certificate for the public key.
        :param cert_dir: Certificate directory path.
        :param private_key: Private key object.
        :param cert_usage: List of certificate usage types.
        """
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, self.SUBJECT),
        ])

        builder = x509.CertificateBuilder()
        builder = builder.subject_name(subject)
        builder = builder.issuer_name(issuer)
        builder = builder.not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        builder = builder.not_valid_after(
            datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=self.VALID_YEARS * 365)
        )
        builder = builder.serial_number(x509.random_serial_number())
        builder = builder.public_key(private_key.public_key())

        extended_key_usage = []
        if "serverAuth" in cert_usage:
            extended_key_usage.append(ExtendedKeyUsageOID.SERVER_AUTH)
        if "dataSigning" in cert_usage:
            extended_key_usage.append(ExtendedKeyUsageOID.CODE_SIGNING)

        if extended_key_usage:
            builder = builder.add_extension(
                x509.ExtendedKeyUsage(extended_key_usage),
                critical=False
            )

        builder = builder.add_extension(
            x509.BasicConstraints(ca=False, path_length=None),
            critical=True
        )

        certificate = builder.sign(private_key, hashes.SHA256())

        cert_path = os.path.join(cert_dir, self.CERT_FILE)
        with open(cert_path, "wb") as f:
            f.write(certificate.public_bytes(serialization.Encoding.PEM))

    def _generate_password(self) -> bytes:
        """
        Generate a random password meeting complexity requirements.
        :return: Password bytes.
        """
        password = self.password_generator.generate_password(16)
        return password.encode(DEFAULT_ENCODING)

    def _save_encrypted_key(self, cert_dir: str, private_key: PrivateKeyTypes, password: bytes) -> None:
        """
        Encrypt the plaintext private key and save it.
        :param cert_dir: Certificate directory path.
        :param private_key: Private key object.
        :param password: Encryption password.
        """
        encryption_algorithm = serialization.NoEncryption()

        key_path = os.path.join(cert_dir, self.KEY_FILE)
        with open(key_path, "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=encryption_algorithm
            ))

    def _save_encrypted_password(self, cert_dir: str, password: bytes) -> None:
        """
        Encrypt and save password to file.
        :param cert_dir: Certificate directory path.
        :param password: Plaintext password.
        """
        password_str = password.decode(DEFAULT_ENCODING)
        encrypted_password = encrypt(password_str)

        pwd_path = os.path.join(cert_dir, self.PWD_FILE)
        with open(pwd_path, "w", encoding=DEFAULT_ENCODING) as f:
            f.write(encrypted_password)

    def _save_self_signed_cert(self, cert_dir: str, private_key: PrivateKeyTypes, cert_usage: str) -> None:
        subject = issuer = x509.Name([
            x509.NameAttribute(NameOID.COMMON_NAME, self.SUBJECT),
        ])

        builder = x509.CertificateBuilder()
        builder = builder.subject_name(subject)
        builder = builder.issuer_name(issuer)
        builder = builder.not_valid_before(datetime.datetime.now(datetime.UTC))
        builder = builder.not_valid_after(
            datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=self.VALID_YEARS * 365)
        )
        builder = builder.serial_number(x509.random_serial_number())
        builder = builder.public_key(private_key.public_key())

        digital_signature = False
        content_commitment = False
        key_encipherment = False

        if cert_usage == "serverAuth":
            digital_signature = True
            key_encipherment = True
        elif cert_usage == "dataSigning":
            digital_signature = True
            content_commitment = True

        # In the development-only serverAuth profile, the self-signed cert
        # anchors its own trust so it can issue local client certificates.
        # dataSigning remains a least-privilege leaf certificate.
        acts_as_local_ca = cert_usage == "serverAuth"
        builder = builder.add_extension(
            x509.KeyUsage(
                digital_signature=digital_signature,
                content_commitment=content_commitment,
                key_encipherment=key_encipherment,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=acts_as_local_ca,
                crl_sign=acts_as_local_ca,
                encipher_only=False,
                decipher_only=False
            ),
            critical=True
        )

        if cert_usage == "serverAuth":
            # clientAuth in the EKU lets this self-signed cert act as the issuer
            # of client certificates: OpenSSL's sslclient purpose check rejects
            # a chain whose issuing CA lacks clientAuth.
            builder = builder.add_extension(
                x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH, ExtendedKeyUsageOID.CLIENT_AUTH]),
                critical=False
            )
            builder = builder.add_extension(self._server_san(), critical=False)

        builder = builder.add_extension(
            x509.BasicConstraints(
                ca=acts_as_local_ca,
                path_length=0 if acts_as_local_ca else None,
            ),
            critical=True
        )

        certificate = builder.sign(private_key, hashes.SHA256())

        cert_file = f"server_{self.alg}.cer"
        cert_path = os.path.join(cert_dir, cert_file)
        fd = os.open(cert_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(certificate.public_bytes(serialization.Encoding.PEM))

    def _save_encrypted_key_with_password(self, cert_dir: str, private_key: PrivateKeyTypes, password: str) -> None:
        encryption_algorithm = serialization.BestAvailableEncryption(password.encode())

        key_file = f"server_key_{self.alg}.pem"
        key_path = os.path.join(cert_dir, key_file)
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(private_key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.PKCS8,
                encryption_algorithm=encryption_algorithm
            ))

    def _set_self_signed_file_permissions(self, cert_dir: str) -> None:
        cert_file = f"server_{self.alg}.cer"
        key_file = f"server_key_{self.alg}.pem"

        cert_path = os.path.join(cert_dir, cert_file)
        key_path = os.path.join(cert_dir, key_file)

        for file_path in [cert_path, key_path]:
            if os.path.exists(file_path):
                os.chmod(file_path, 0o600)

    def _set_file_permissions(self, cert_dir: str) -> None:
        """
        Set certificate file permissions to 600.
        :param cert_dir: Certificate directory path.
        """
        cert_path = os.path.join(cert_dir, self.CERT_FILE)
        key_path = os.path.join(cert_dir, self.KEY_FILE)
        pwd_path = os.path.join(cert_dir, self.PWD_FILE)

        for file_path in [cert_path, key_path, pwd_path]:
            if os.path.exists(file_path):
                os.chmod(file_path, 0o600)

    def _audit_log_generation(self, cert_dir: str) -> None:
        """
        Record audit log for certificate generation.
        :param cert_dir: Certificate directory path.
        """
        cert_path = os.path.join(cert_dir, self.CERT_FILE)
        audit_logger.audit({
            "operation_name": OperationName.GENERATE_CERTIFICATE,
            "level": LogLevel.INFO,
            "result": OperationResult.SUCCESS,
            "object_name": "Certificate",
            "details": {
                "certificate_path": cert_path,
                "certificate_usage": "TLS communication certificate"
            }
        })
