# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Configuration ownership, legacy precedence and secret-safe diagnostics."""
from pathlib import Path
import re
from unittest.mock import Mock

import pytest

from common.util import app_config

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    for key in list(app_config.os.environ):
        if key.startswith('REGISTRY_'):
            monkeypatch.delenv(key)
    conf = tmp_path / 'etc/conf'
    conf.mkdir(parents=True)
    monkeypatch.setattr(app_config, 'get_root_path', lambda: str(tmp_path))
    return conf


def read(path):
    values = {}
    app_config.load_configs(str(path), values)
    return values


def test_public_templates_have_disjoint_keys():
    deployment = read(REPO / 'etc/conf/server.conf.example')
    policies = read(REPO / 'etc/conf/server.properties')
    assert not deployment.keys() & policies.keys()
    assert {'heartbeat.enabled', 'broadcast.enabled', 'integration.token.enabled',
            'integration.oauth2.introspection_uri', 'integration.oauth2.client_secret',
            'integration.token.ca_file', 'ssl_keyfile'} <= deployment.keys()
    assert {'flowcontrol.ratelimit.jwk', 'flowcontrol.parallelism.jwk',
            'heartbeat.interval', 'broadcast.webhook.timeout',
            'integration.oauth2.cache_seconds', 'integration.token.allowed_scopes',
            'integration.auth.scope_role.registry.read'} <= policies.keys()
    assert not any(key.endswith(('.enabled', '_enabled', '.ca_file', '.endpoint',
                                 '.client_secret')) for key in policies)


def test_policy_values_are_loaded_and_environment_is_last(runtime, monkeypatch):
    (runtime / 'server.conf').write_text('PORT=5000\nheartbeat.enabled=true\n', encoding='utf-8')
    (runtime / 'server.properties').write_text('heartbeat.interval=30\n', encoding='utf-8')
    monkeypatch.setenv('REGISTRY_PORT', '6000')
    monkeypatch.setenv('REGISTRY_HEARTBEAT_INTERVAL', '45')
    actual = app_config.get_conf()
    assert actual['port'] == '6000'
    assert actual['heartbeat.enabled'] == 'true'
    assert actual['heartbeat.interval'] == '45'


def test_legacy_cross_file_duplicates_warn_without_secret_values(runtime, monkeypatch):
    (runtime / 'server.conf').write_text('Integration.Secret=old-sensitive-value\n', encoding='utf-8')
    (runtime / 'server.properties').write_text('integration.secret=new-sensitive-value\n', encoding='utf-8')
    warning = Mock()
    monkeypatch.setattr(app_config.logger, 'warning', warning)
    assert app_config.get_conf()['integration.secret'] == 'new-sensitive-value'
    warning.assert_called_once()
    diagnostic = str(warning.call_args)
    assert 'integration.secret' in diagnostic
    assert 'server.properties' in diagnostic and 'server.conf' in diagnostic
    assert 'sensitive-value' not in diagnostic


def test_same_file_duplicates_warn_case_insensitively(runtime, monkeypatch):
    path = runtime / 'server.conf'
    path.write_text('KEY=old-private\nkey=new-private\n', encoding='utf-8')
    warning = Mock()
    monkeypatch.setattr(app_config.logger, 'warning', warning)
    assert read(path) == {'key': 'new-private'}
    warning.assert_called_once()
    assert 'private' not in str(warning.call_args)


def test_public_defaults_survive_partition(runtime):
    (runtime / 'server.conf').write_bytes((REPO / 'etc/conf/server.conf.example').read_bytes())
    (runtime / 'server.properties').write_bytes((REPO / 'etc/conf/server.properties').read_bytes())
    actual = app_config.get_conf()
    expected = {'flowcontrol.ratelimit.jwk': '10', 'flowcontrol.parallelism.jwk': '1',
                'heartbeat.enabled': 'false', 'heartbeat.interval': '30',
                'broadcast.enabled': 'false', 'broadcast.webhook.timeout': '10',
                'integration.enabled': 'false', 'integration.oauth2.cache_seconds': '0',
                'integration.token.timeout_seconds': '3'}
    assert {key: actual[key] for key in expected} == expected


def test_synthetic_network_fixture_obeys_file_boundary(tmp_path):
    from integration_oauth_smoke import override_config
    conf = tmp_path / 'etc/conf'
    conf.mkdir(parents=True)
    (conf / 'server.conf').write_text('heartbeat.interval=legacy\nheartbeat.enabled=false\n', encoding='utf-8')
    (conf / 'server.properties').write_text('heartbeat.interval=30\n', encoding='utf-8')
    override_config(tmp_path, {'heartbeat.interval': '2', 'heartbeat.enabled': 'true'})
    assert read(conf / 'server.conf') == {'heartbeat.enabled': 'true'}
    assert read(conf / 'server.properties') == {'heartbeat.interval': '2'}


def test_initialization_preserves_policy_file(runtime, monkeypatch):
    from agent_registry import init
    monkeypatch.setattr(init, 'get_root_path', lambda: str(runtime.parent.parent))
    policies = runtime / 'server.properties'
    original = b'# Operator policy\nheartbeat.interval=45\n'
    policies.write_bytes(original)
    init.InitCommand().save_config_to_file({'port': '6000', 'heartbeat.enabled': 'true'})
    assert policies.read_bytes() == original
    assert read(runtime / 'server.conf') == {'port': '6000', 'heartbeat.enabled': 'true'}


@pytest.mark.parametrize('document', [
    'docs/en/Registry Center Integration Guide.md',
    'docs/zh/注册中心系统集成指南.md',
])
def test_integration_examples_respect_public_policy_ownership(document):
    policies = read(REPO / 'etc/conf/server.properties')
    text = (REPO / document).read_text(encoding='utf-8')
    examples = re.findall(r'`etc/conf/(server.conf|server.properties)`:\n\n'
                          r'```properties\n(.*?)\n```', text, re.DOTALL)
    assert len(examples) == 6
    for filename, body in examples:
        keys = {line.split('=', 1)[0] for line in body.splitlines() if '=' in line}
        assert all((key in policies) == (filename == 'server.properties') for key in keys)


def test_audit_sink_keys_are_public_and_environment_overridable(runtime, monkeypatch):
    """A shipped key must appear in the public template, or env overrides miss it."""
    template = read(REPO / 'etc/conf/persistence.conf.example')
    assert {'audit.mysql.enabled', 'audit.mysql.host', 'audit.mysql.password',
            'audit.mysql.batch_size'} <= template.keys()
    (runtime / 'persistence.conf').write_bytes(
        (REPO / 'etc/conf/persistence.conf.example').read_bytes())
    assert app_config.get_persistence_conf()['audit.mysql.enabled'] == 'false'
    monkeypatch.setenv('REGISTRY_AUDIT_MYSQL_ENABLED', 'true')
    assert app_config.get_persistence_conf()['audit.mysql.enabled'] == 'true'
