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
from pydantic import BaseModel, Field, field_validator, model_validator
from typing import Optional, List


class SignatureObject(BaseModel):
    """Signature object model"""
    protected: str = Field(..., description="base64url-encoded protected header")
    signature: str = Field(..., description="base64url-encoded signature value")


class ProtectedHeader(BaseModel):
    """Protected header model (decoded)"""
    alg: str = Field(..., description="Signature algorithm, e.g., ES256, RS256")
    typ: str = Field(default="JOSE", description="Type identifier")
    kid: str = Field(..., description="Key ID")
    jku: Optional[str] = Field(None, description="JWK Set URL")

    @field_validator('alg')
    @classmethod
    def validate_alg(cls, v):
        if v not in ['ES256', 'RS256']:
            raise ValueError('Signature algorithm only supports ES256 or RS256')
        return v


class JWK(BaseModel):
    """JSON Web Key model"""
    kty: str = Field(..., description="Key type, supports EC or RSA only")
    kid: str = Field(..., description="Key ID")
    use: str = Field(default="sig", description="Key usage")
    alg: Optional[str] = Field(None, description="Optional algorithm, e.g., ES256, RS256")
    crv: Optional[str] = Field(None, description="Curve, e.g., P-256")
    x: Optional[str] = Field(None, description="X coordinate (ECDSA)")
    y: Optional[str] = Field(None, description="Y coordinate (ECDSA)")
    n: Optional[str] = Field(None, description="Modulus (RSA)")
    e: Optional[str] = Field(None, description="Exponent (RSA)")

    @field_validator('kty')
    @classmethod
    def validate_kty(cls, v):
        if v not in ['EC', 'RSA']:
            raise ValueError('Key type only supports EC or RSA')
        return v

    @model_validator(mode='after')
    def validate_key_material(self):
        if self.kty == 'RSA':
            if self.alg not in (None, 'RS256') or not self.n or not self.e:
                raise ValueError('RS256 RSA keys require n and e')
        elif self.alg not in (None, 'ES256') or self.crv != 'P-256' or not self.x or not self.y:
            raise ValueError('ES256 EC keys require P-256, x and y')
        return self


class JWKS(BaseModel):
    """JSON Web Key Set model (only contains keys array)"""
    keys: List[JWK] = Field(default_factory=list, description="List of public keys")
