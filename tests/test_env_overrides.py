# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""Contract tests for the REGISTRY_* environment override mapping.

Container overrides (Dockerfile/entrypoint/compose) depend on this mapping:
each single underscore becomes a dot, so the canonical spelling of
``owner.validation.mode`` is ``REGISTRY_OWNER_VALIDATION_MODE``.
"""

from common.util.app_config import apply_env_overrides


def test_single_underscore_segments_map_to_dotted_keys(monkeypatch):
    monkeypatch.setenv('REGISTRY_OWNER_VALIDATION_MODE', 'strict')
    conf = {'owner.validation.mode': 'relaxed'}

    apply_env_overrides(conf)

    assert conf['owner.validation.mode'] == 'strict'


def test_dotted_key_with_registry_prefix_is_not_a_typo(monkeypatch):
    """REGISTRY_REGISTRY_SIGN_ENABLED maps to registry.sign.enabled."""
    monkeypatch.setenv('REGISTRY_REGISTRY_SIGN_ENABLED', 'false')
    conf = {'registry.sign.enabled': 'true'}

    apply_env_overrides(conf)

    assert conf['registry.sign.enabled'] == 'false'


def test_legacy_double_underscore_spelling_cannot_override_the_key(monkeypatch):
    """The legacy image spelling lands on a junk key instead of the real one.

    This is the trap the image used to fall into: only the entrypoint ``sed``
    made it work, so a deployment that replaced the entrypoint silently lost the
    setting. The image now uses the canonical name above.
    """
    monkeypatch.setenv('REGISTRY_OWNER__VALIDATION__MODE', 'strict')
    conf = {'owner.validation.mode': 'relaxed'}

    apply_env_overrides(conf)

    assert conf['owner.validation.mode'] == 'relaxed'
    assert conf['owner__validation__mode'] == 'strict'


def test_unmapped_variable_is_kept_under_its_raw_name(monkeypatch):
    monkeypatch.setenv('REGISTRY_NOT_A_CONFIG_KEY', 'value')
    conf = {'owner.validation.mode': 'relaxed'}

    apply_env_overrides(conf)

    assert conf['not_a_config_key'] == 'value'
