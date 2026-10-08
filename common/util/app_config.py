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

# app_config.py — application-level configuration loading
import configparser
import os
import re
from typing import Dict, Any, Iterable

from loguru import logger


def get_root_path() -> str:
    """
    get the root path of the component
    Returns:
        the root path
    """
    current_script_path = os.path.abspath(__file__)
    script_dir = os.path.dirname(current_script_path)
    project_root = os.path.dirname(os.path.dirname(script_dir))
    return project_root


def get_conf() -> Dict[str, Any]:
    """
    Load all server configurations.
    server.conf holds feature switches and deployment/access settings;
    server.properties holds operating parameters and business policies.
    Preserve legacy file precedence; diagnose duplicate keys without logging values.
    REGISTRY_* environment overrides are applied last. The public template
    declares key names for overrides even when an older deployment file omits
    them; its example values are never loaded as runtime defaults.
    Returns:
        A dictionary containing all configurations.
    """
    config = {}
    root_path = get_root_path()
    base_config_path = os.path.join(root_path, "etc", "conf", "server.conf")
    safe_config_path = os.path.join(root_path, "etc", "conf", "server.properties")
    load_configs(base_config_path, config)
    policies = {}
    load_configs(safe_config_path, policies)
    duplicates = config.keys() & policies.keys()
    if duplicates:
        logger.warning(
            "Duplicate server configuration keys across server.conf and "
            "server.properties: {}. Keep each key in one file; "
            "server.properties takes precedence.", ", ".join(sorted(duplicates))
        )
    config.update(policies)
    apply_env_overrides(config, _declared_keys(base_config_path + '.example'))
    return config


def load_configs(conf_path, config):
    if not os.path.exists(conf_path):
        logger.error(f"Error: The configuration file {conf_path} does not exist.")
        return
    with open(conf_path, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith('#'):
                continue

            # Processing Comments
            if '#' in line:
                line = line[:line.index('#')].strip()

            if '=' in line:
                key, value = line.split('=', 1)
                key = key.strip()
                value = value.strip()

                key = key.lower()
                if key in config:
                    logger.warning(
                        "Duplicate configuration key in {}: {}. "
                        "Keep one definition; the last definition takes precedence.",
                        os.path.basename(conf_path), key,
                    )
                config[key] = value


def load_conf_as_dict(conf_file: str) -> dict:
    config = configparser.ConfigParser()
    try:
        with open(conf_file, 'r', encoding='utf-8') as f:
            config.read_string('[DEFAULT]\n' + f.read())
            return dict(config['DEFAULT'])
    except Exception as e:
        logger.error(f"load config failed, {e}")
        return {}


def canonical_env_name(key: str) -> str:
    """
    Return the REGISTRY_* spelling that maps back to ``key``.

    Every key is reachable under this name, including keys that keep an
    underscore inside a segment such as ``knowledge_graph.ratelimit``.
    """
    return "REGISTRY_" + key.upper().replace(".", "_")


def _declared_keys(template_path: str) -> Iterable[str]:
    """Read override key names, not defaults, from a shipped public template.

    Minimal/custom installations without a template retain legacy behaviour.
    Never rewrite the operator's configuration to add missing declarations.
    """
    declared = {}
    if os.path.isfile(template_path):
        load_configs(template_path, declared)
    return declared.keys()


def apply_env_overrides(conf: Dict[str, Any], known_keys: Iterable[str] = ()) -> None:
    """
    Override config values with REGISTRY_* environment variables.

    Env var REGISTRY_FOO_BAR overrides config key 'foo.bar' or 'foobar', and
    every key is also reachable under its canonical spelling (see
    canonical_env_name), so a key like 'integration.auth.static.hmac_key'
    cannot be silently missed. ``known_keys`` supplies declarations from the
    public template so missing file entries remain reachable without adopting
    example defaults. Existing deployment/plugin keys remain reachable too.
    A name that matches no key is stored under its raw lowercase form, which
    handlers that read such names expect.
    """
    env_prefix = "REGISTRY_"
    canonical = {}
    # Dotted keys are registered first: when one name is the canonical spelling
    # of both 'foo.bar' and a legacy 'foo_bar' key, the dotted key wins.
    keys = dict.fromkeys((*conf, *known_keys))
    for key in keys:
        if '.' in key:
            canonical.setdefault(canonical_env_name(key), key)
    for key in keys:
        canonical.setdefault(canonical_env_name(key), key)
    for env_key, env_value in os.environ.items():
        if not env_key.startswith(env_prefix):
            continue
        target = canonical.get(env_key)
        if target is not None:
            conf[target] = env_value
            continue
        raw_key = env_key[len(env_prefix):].lower()
        config_key = raw_key.replace("_", ".")
        if config_key in conf:
            conf[config_key] = env_value
            continue
        config_key_no_dot = raw_key.strip("_")
        if config_key_no_dot in conf:
            conf[config_key_no_dot] = env_value
            continue
        conf[raw_key] = env_value


def _resolve_env_vars(conf: dict) -> dict:
    """
    Resolve environment variables in config values.
    Format: ${ENV_VAR:default_value}
    """
    resolved = {}
    for key, value in conf.items():
        if isinstance(value, str):
            pattern = r'\$\{([^}:]+)(?:([^}]*))?\}'
            matches = re.findall(pattern, value)
            for env_var, default in matches:
                env_value = os.environ.get(env_var, default.lstrip(':') if default else '')
                value = value.replace(f'${{{env_var}{default}}}', env_value)
        resolved[key] = value
    return resolved


def resolve_env_vars(conf: dict) -> dict:
    """Public wrapper: resolve ${ENV_VAR:default} placeholders in a conf dict."""
    return _resolve_env_vars(conf)


def get_persistence_conf() -> dict:
    """
    Read persistence configuration file with environment variable substitution.
    Decrypt database password if present.
    """
    root_path = get_root_path()
    persistence_conf_path = os.path.join(root_path, "etc", "conf", "persistence.conf")
    conf = load_conf_as_dict(persistence_conf_path)
    conf = _resolve_env_vars(conf)
    apply_env_overrides(conf, _declared_keys(persistence_conf_path + '.example'))
    if 'postgresql.password' in conf and conf['postgresql.password']:
        from common.util.cipher_util import decrypt
        decrypted = decrypt(conf['postgresql.password'])
        conf['postgresql.password'] = decrypted.decode('utf-8') if isinstance(decrypted, bytes) else decrypted
    if 'gauss.password' in conf and conf['gauss.password']:
        from common.util.cipher_util import decrypt
        decrypted = decrypt(conf['gauss.password'])
        conf['gauss.password'] = decrypted.decode('utf-8') if isinstance(decrypted, bytes) else decrypted
    if 'mysql.password' in conf and conf['mysql.password']:
        from common.util.cipher_util import decrypt
        decrypted = decrypt(conf['mysql.password'])
        conf['mysql.password'] = decrypted.decode('utf-8') if isinstance(decrypted, bytes) else decrypted
    return conf
