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

from dataclasses import dataclass, field
from typing import Any, Dict, Optional

@dataclass
class ModelConfig:
    description: str = ""
    model: str = ""
    url: str = ""
    api_key: str = ""
    enable_thinking: bool = False
    verify_ssl: bool = True
    timeout: float = 60.0
    auth: Any = None
    headers: Dict[str, str] = field(default_factory=dict)
    body: Dict[str, Any] = field(default_factory=dict)
    response: Dict[str, str] = field(default_factory=dict)

    @staticmethod
    def from_dict(key: str, raw: dict) -> "ModelConfig":
        return ModelConfig(
            description=raw.get("description", key),
            model=raw.get("model", ""),
            url=raw.get("url", ""),
            api_key=raw.get("api_key", ""),
            enable_thinking=raw.get("enable_thinking", False),
            verify_ssl=raw.get("verify_ssl", True),
            timeout=float(raw.get("timeout", 60.0)),
            auth=raw.get("auth"),
            headers=raw.get("headers", {}),
            body=raw.get("body", {}),
            response=raw.get("response", {}),
        )


class _ModelConfigHolder:
    _instance: Optional[Dict[str, ModelConfig]] = None

    @classmethod
    def get(cls) -> Dict[str, ModelConfig]:
        if cls._instance is None:
            from common.llm.config.model_sources import load_model_configs
            raw_config = load_model_configs()
            cls._instance = {key: ModelConfig.from_dict(key, val) for key, val in raw_config.items()}
        return cls._instance

    @classmethod
    def reset(cls) -> None:
        cls._instance = None


def get_model_config(capability: str) -> Optional[ModelConfig]:
    return _ModelConfigHolder.get().get(capability)
