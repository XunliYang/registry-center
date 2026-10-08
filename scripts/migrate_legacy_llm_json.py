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

"""One-time conversion of legacy JSON to YAML definitions and env secrets."""

from __future__ import annotations

import json
import os
import tempfile
from io import StringIO
from pathlib import Path
from typing import Any

import yaml
from dotenv import dotenv_values

from common.llm.config.model_sources import build_profile, load_model_configs
from scripts.migrate_llm_config import _write_atomic

_SCALARS = ("description", "model", "url", "enable_thinking", "verify_ssl", "timeout")
_AUTH_SECRETS = {"app_key", "app_secret", "authorization"}
_AUTH_FIELDS = {
    "app_key", "app_secret", "authorization", "api_code", "api_version",
    "scenario_code", "scenario_version", "ability_code", "test_flag",
}


def _quoted(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n") + '"'


def migrate(source_path: Path, dotenv_path: Path, models_path: Path) -> list[str]:
    raw = json.loads(source_path.read_text(encoding="utf-8"))
    if not isinstance(raw, dict) or not raw:
        raise ValueError("Legacy model JSON must contain capability mappings")
    models: dict[str, dict[str, Any]] = {}
    secrets: dict[str, str] = {}
    for capability, old in raw.items():
        if not isinstance(capability, str) or not isinstance(old, dict):
            raise ValueError("Invalid legacy model capability")
        auth = old.get("auth") or {}
        if not isinstance(auth, dict):
            raise ValueError(f"{capability}: unsupported legacy auth")
        provider = auth.get("type") or "openai_compatible"
        if provider not in {"openai", "openai_compatible", "aoc_signed"}:
            raise ValueError(f"{capability}: unsupported provider {provider}")
        if set(auth) - (_AUTH_FIELDS | {"type"}):
            raise ValueError(f"{capability}: custom auth requires a profile")
        get = lambda field: auth.get(field)
        profile = build_profile(provider, capability, get)
        for field in ("body", "response", "headers"):
            if old.get(field, {}) != profile[field]:
                raise ValueError(f"{capability}: custom {field} template requires a profile")
        config = {"provider": provider}
        for field in _SCALARS:
            if field in old and old[field] is not None:
                config[field] = old[field]
        if not config.get("model") or not config.get("url"):
            raise ValueError(f"{capability}: model and url are required")
        if old.get("api_key"):
            key = f"LLM_{capability.upper()}_API_KEY"
            config["api_key_env"] = key
            secrets[key] = str(old["api_key"])
        if auth:
            config_auth = {}
            for field, value in auth.items():
                if field == "type" or value is None or value == "":
                    continue
                if field in _AUTH_SECRETS:
                    key = f"LLM_{capability.upper()}_AUTH_{field.upper()}"
                    config_auth[field + "_env"] = key
                    secrets[key] = str(value)
                else:
                    config_auth[field] = str(value)
            if config_auth:
                config["auth"] = config_auth
        models[capability] = config
    candidate = yaml.safe_dump({"models": models}, sort_keys=False, allow_unicode=True)
    if models_path.exists() and models_path.read_text(encoding="utf-8") != candidate:
        raise ValueError(f"Existing model file differs: {models_path}")
    existing = dotenv_values(dotenv_path) if dotenv_path.exists() else {}
    conflicts = sorted(key for key, value in secrets.items() if key in existing and existing[key] != value)
    if conflicts:
        raise ValueError("Existing .env values differ for: " + ", ".join(conflicts))
    original = dotenv_path.read_text(encoding="utf-8") if dotenv_path.exists() else ""
    extra = "".join(f"{key}={_quoted(value)}\n" for key, value in secrets.items() if key not in existing)
    combined = original + ("\n" if original and not original.endswith("\n") and extra else "") + extra
    parsed = dotenv_values(stream=StringIO(combined))
    if any(parsed.get(key) != value for key, value in secrets.items()):
        raise ValueError("Could not safely encode legacy secrets in .env")
    models_path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".model-validation-", dir=models_path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(candidate)

        class CandidateSecrets:
            def get(self, name: str) -> str | None:
                return parsed.get(name)

        load_model_configs(CandidateSecrets(), Path(temporary))
    finally:
        os.unlink(temporary)
    if combined != original:
        _write_atomic(dotenv_path, combined, private=True)
    if not models_path.exists():
        _write_atomic(models_path, candidate)
    return sorted(models)


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    names = migrate(
        root / "common" / "config" / "llm_config.json",
        root / ".env",
        root / "etc" / "config" / "models.yaml",
    )
    print(f"Migrated {len(names)} legacy model capabilities (values hidden)")
