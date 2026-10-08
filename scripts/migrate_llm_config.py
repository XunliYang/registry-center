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

"""Move non-secret env-only LLM settings into a local YAML model file."""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path

import yaml
from dotenv import dotenv_values

from common.llm.config.model_sources import EnvironmentSettingsSource, load_model_configs

_MODEL_FIELDS = {
    "PROVIDER": "provider", "DESCRIPTION": "description", "MODEL": "model",
    "URL": "url", "TIMEOUT": "timeout", "VERIFY_SSL": "verify_ssl",
    "ENABLE_THINKING": "enable_thinking",
}
_AUTH_FIELDS = (
    "APP_KEY", "APP_SECRET", "AUTHORIZATION", "API_CODE", "API_VERSION",
    "SCENARIO_CODE", "SCENARIO_VERSION", "ABILITY_CODE", "TEST_FLAG",
)
_BOOLEAN = {
    "true": True, "1": True, "yes": True, "on": True,
    "false": False, "0": False, "no": False, "off": False,
}
_ENV_LINE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=")


def _write_atomic(path: Path, content: str, *, private: bool = False) -> None:
    """Replace path atomically; private restricts the file to its owner."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".model-migration-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
        if os.name != "nt":
            mode = 0o600 if private else (
                stat.S_IMODE(path.stat().st_mode) if path.exists() else 0o644
            )
            os.chmod(name, mode)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def migrate(dotenv_path: Path, models_path: Path) -> list[str]:
    """Validate, create YAML, then remove only migrated non-secret env keys."""
    if not dotenv_path.is_file():
        raise FileNotFoundError(f"Environment file not found: {dotenv_path}")
    values = dotenv_values(dotenv_path)
    names = values.get("LLM_CAPABILITIES")
    if names:
        capabilities = [name.strip().lower() for name in names.split(",") if name.strip()]
    else:
        capabilities = sorted({
            match.group(1).lower() for key in values
            if (match := re.fullmatch(r"LLM_([A-Z][A-Z0-9_]*)_MODEL", key))
        })
    if not capabilities:
        raise ValueError("No LLM model settings found in .env")
    models: dict[str, dict] = {}
    migrated = {"LLM_CAPABILITIES"}
    for capability in capabilities:
        prefix = f"LLM_{capability.upper()}_"
        config: dict = {}
        for old, new in _MODEL_FIELDS.items():
            key = prefix + old
            value = values.get(key)
            if key in values:
                migrated.add(key)
            if value is None or not value.strip():
                continue
            if old in {"VERIFY_SSL", "ENABLE_THINKING"}:
                if value.lower() not in _BOOLEAN:
                    raise ValueError(
                        f"{key} must be a boolean (true/false/1/0/yes/no/on/off)"
                    )
                config[new] = _BOOLEAN[value.lower()]
            elif old == "TIMEOUT":
                config[new] = float(value)
            else:
                config[new] = value
        if not config.get("model") or not config.get("url"):
            raise ValueError(f"{capability}: model and url are required")
        api_key = prefix + "API_KEY"
        if values.get(api_key):
            config["api_key_env"] = api_key
        auth: dict[str, str] = {}
        for field in _AUTH_FIELDS:
            key = prefix + "AUTH_" + field
            value = values.get(key)
            if field not in {"APP_KEY", "APP_SECRET", "AUTHORIZATION"} and key in values:
                migrated.add(key)
            if value is None or not value.strip():
                continue
            auth[field.lower() + ("_env" if field in {"APP_KEY", "APP_SECRET", "AUTHORIZATION"} else "")] = (
                key if field in {"APP_KEY", "APP_SECRET", "AUTHORIZATION"} else value
            )
        if auth:
            config["auth"] = auth
        models[capability] = config
    candidate = yaml.safe_dump({"models": models}, sort_keys=False, allow_unicode=True)
    if models_path.is_file() and models_path.read_text(encoding="utf-8") != candidate:
        raise ValueError(f"Existing model file differs: {models_path}")
    # Validate all contracts and secret references before changing either file.
    created = not models_path.is_file()
    if created:
        _write_atomic(models_path, candidate)
    try:
        load_model_configs(EnvironmentSettingsSource(dotenv_path), models_path)
    except Exception:
        if created and models_path.read_text(encoding="utf-8") == candidate:
            models_path.unlink()
        raise
    original = dotenv_path.read_text(encoding="utf-8")
    remaining = "".join(
        line for line in original.splitlines(keepends=True)
        if not ((match := _ENV_LINE.match(line)) and match.group(1) in migrated)
    )
    if remaining != original:
        _write_atomic(dotenv_path, remaining, private=True)
    return capabilities


if __name__ == "__main__":
    root = Path(__file__).resolve().parents[1]
    names = migrate(root / ".env", root / "etc" / "config" / "models.yaml")
    print(f"Migrated {len(names)} model capabilities to local models.yaml (values hidden)")
