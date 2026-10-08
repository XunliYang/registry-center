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


def test_key_with_internal_underscore_is_reachable_under_its_canonical_name(monkeypatch):
    """A key may keep an underscore inside a segment: knowledge_graph.ratelimit.

    Replacing every underscore with a dot cannot spell that key, so before the
    canonical mapping existed the override was stored under
    'knowledge_graph_ratelimit' and the real key kept its file value.
    """
    monkeypatch.setenv('REGISTRY_KNOWLEDGE_GRAPH_RATELIMIT', '7/second')
    conf = {'knowledge_graph.ratelimit': '100/second'}

    apply_env_overrides(conf)

    assert conf['knowledge_graph.ratelimit'] == '7/second'
    assert 'knowledge_graph_ratelimit' not in conf


def test_canonical_mapping_covers_auth_material_and_audit_sink_keys(monkeypatch):
    monkeypatch.setenv('REGISTRY_INTEGRATION_AUTH_STATIC_HMAC_KEY', 'secret')
    monkeypatch.setenv('REGISTRY_AUDIT_MYSQL_BATCH_SIZE', '25')
    conf = {
        'integration.auth.static.hmac_key': '',
        'audit.mysql.batch_size': '50',
    }

    apply_env_overrides(conf)

    assert conf['integration.auth.static.hmac_key'] == 'secret'
    assert conf['audit.mysql.batch_size'] == '25'


def test_dotted_key_wins_when_a_legacy_underscored_key_has_the_same_name(monkeypatch):
    """REGISTRY_FOO_BAR targets 'foo.bar', not the legacy 'foo_bar' key."""
    monkeypatch.setenv('REGISTRY_FOO_BAR', 'value')
    conf = {'foo.bar': 'old', 'foo_bar': 'legacy'}

    apply_env_overrides(conf)

    assert conf['foo.bar'] == 'value'
    assert conf['foo_bar'] == 'legacy'
