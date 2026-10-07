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
import json
import base64
import asyncio
import copy
from typing import Optional, List, Dict, Any
from loguru import logger

from common.util.app_config import get_conf
from a2a.types import AgentCard
from a2a.utils.signing import create_signature_verifier
from google.protobuf.json_format import MessageToDict

from agent_registry.signature.models import SignatureObject, ProtectedHeader
from agent_registry.signature.jwk_fetcher import JWKFetcher


class ValidationResult:
    """Validation result"""
    def __init__(
        self,
        is_valid: bool,
        error_code: Optional[str] = None,
        error_message: Optional[str] = None,
        details: Optional[Dict[str, Any]] = None
    ):
        self.is_valid = is_valid
        self.error_code = error_code
        self.error_message = error_message
        self.details = details or {}


class AgentCardSignatureValidator:
    """AgentCard signature validator (supports protobuf AgentCard)"""

    def __init__(self, jwk_fetcher: JWKFetcher, signature_validation_enabled: bool = True):
        self.jwk_fetcher = jwk_fetcher
        self._signature_validation_enabled = signature_validation_enabled

    @staticmethod
    def _load_signature_config() -> bool:
        """Deprecated: kept for backward compatibility with tests. Use constructor parameter instead."""
        try:
            config = get_conf()
            enabled = config.get('signature_validation_enabled', 'true')
            return enabled.lower() == 'true'
        except Exception as e:
            logger.error(f"Failed to load signature validation config: {e}")
            return True

    def validate_agent_card(
        self,
        agent_card: AgentCard
    ) -> ValidationResult:
        """
        Validate AgentCard signature.

        Args:
            agent_card: protobuf AgentCard object.

        Returns:
            ValidationResult: Validation result.
        """
        # Synchronous callers retain local-key validation. Network verification
        # on an event loop must use the async API; do not nest asyncio.run().
        result = self._validate_backend(agent_card)
        if result is not None:
            return result
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self._validate_jku(agent_card))
        raise RuntimeError("Use await validate_agent_card_async() on an event loop")

    async def validate_agent_card_async(self, agent_card: AgentCard) -> ValidationResult:
        """Validate any trusted signature, awaiting JKU retrieval when needed."""
        result = self._validate_backend(agent_card)
        return result if result is not None else await self._validate_jku(agent_card)

    def _validate_backend(self, agent_card: AgentCard) -> Optional[ValidationResult]:
        if not self._signature_validation_enabled:
            return ValidationResult(is_valid=True)
        if not self._extract_signatures_from_protobuf(agent_card):
            return ValidationResult(
                is_valid=False, error_code="SIG001",
                error_message="Signatures field is required when signature validation is enabled")
        fetch_key = self.jwk_fetcher.create_backend_key_fetcher(
            agent_card.provider.organization, agent_card.name, agent_card.provider.url)
        for signature in agent_card.signatures:
            header = self._decode_protected(signature.protected)
            if header is None:
                continue
            key = fetch_key(header.kid, "")
            if key is not None and self._verify_one(agent_card, signature, key):
                return ValidationResult(is_valid=True)
        return None

    async def _validate_jku(self, agent_card: AgentCard) -> ValidationResult:
        # A missing/bad first key must not abort checking a valid later one.
        # Cache per validation so repeated signatures cannot multiply fetches.
        keys = {}
        for signature in agent_card.signatures:
            header = self._decode_protected(signature.protected)
            if header is None or not header.jku:
                continue
            key_id = (header.kid, header.jku)
            try:
                if key_id not in keys:
                    keys[key_id] = await self.jwk_fetcher.fetch_jku_key(*key_id)
                key = keys[key_id]
                if key is not None and self._verify_one(agent_card, signature, key):
                    return ValidationResult(is_valid=True)
            except Exception as e:
                logger.warning("JKU signature verification failed: {}", type(e).__name__)
        return ValidationResult(
            is_valid=False, error_code="SIG005",
            error_message="Signature validation failed: no valid signature with a trusted key",
            details={"total_signatures": len(agent_card.signatures)})

    @staticmethod
    def _verify_one(agent_card: AgentCard, signature, key) -> bool:
        # A2A canonicalization excludes signatures. Verify one signature on a
        # copy so malformed earlier headers and missing keys cannot abort its
        # any-valid-signature policy. Keep the submitted card unchanged.
        candidate = copy.deepcopy(agent_card)
        del candidate.signatures[:]
        candidate.signatures.append(signature)
        try:
            create_signature_verifier(lambda kid, jku: key, ['ES256', 'RS256'])(candidate)
            return True
        except Exception as e:
            logger.debug("Signature verification failed: {}", type(e).__name__)
            return False

    @staticmethod
    def _extract_signatures_from_protobuf(agent_card: AgentCard) -> List[SignatureObject]:
        """Extract signatures from protobuf AgentCard"""
        try:
            signatures = agent_card.signatures
            if not signatures:
                return []

            signature_objects = []
            for sig in signatures:
                protected = sig.protected
                signature = sig.signature
                
                if not protected or not signature:
                    logger.warning("Missing required fields in signature")
                    continue

                sig_dict = {
                    "protected": protected,
                    "signature": signature
                }
                if sig.header:
                    sig_dict["header"] = MessageToDict(sig.header)
                
                signature_objects.append(SignatureObject(**sig_dict))

            return signature_objects

        except Exception as e:
            logger.error(f"Failed to extract signatures: {e}")
            return []

    @staticmethod
    def _decode_protected(protected: str) -> Optional[ProtectedHeader]:
        """Decode protected header"""
        try:
            protected += '=' * (-len(protected) % 4)
            decoded_bytes = base64.urlsafe_b64decode(protected)

            protected_json = decoded_bytes.decode('utf-8')
            protected_dict = json.loads(protected_json)

            return ProtectedHeader(**protected_dict)

        except Exception as e:
            logger.error(f"Failed to decode protected header: {e}")
            return None
