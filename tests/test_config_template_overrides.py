# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Public declarations must work with sparse, non-container deployment files."""
from pathlib import Path

import pytest

from common.util import app_config

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    for name in tuple(app_config.os.environ):
        if name.startswith('REGISTRY_'):
            monkeypatch.delenv(name)
    directory = tmp_path / 'etc/conf'
    directory.mkdir(parents=True)
    for name in ('server.conf.example', 'persistence.conf.example'):
        (directory / name).write_bytes((REPO / 'etc/conf' / name).read_bytes())
    (directory / 'server.conf').write_text('IP=127.0.0.1\nenable_https=true\n', encoding='utf-8')
    (directory / 'server.properties').write_text('heartbeat.interval=30\n', encoding='utf-8')
    (directory / 'persistence.conf').write_text('persistence.mode=file\n', encoding='utf-8')
    monkeypatch.setattr(app_config, 'get_root_path', lambda: str(tmp_path))
    return directory


@pytest.mark.parametrize('key,value', [
    ('integration.enabled', 'true'),
    ('integration.auth.mode', 'static_bearer'),
    ('integration.auth.static.hmac_key', 'synthetic-test-key'),
    ('integration.token.enabled', 'true'),
    ('startup.strict.identity', 'true'),
    ('startup.strict.storage', 'true'),
])
def test_missing_server_key_maps_to_its_real_consumer_name(runtime, monkeypatch, key, value):
    original = (runtime / 'server.conf').read_bytes()
    monkeypatch.setenv(app_config.canonical_env_name(key), value)
    conf = app_config.get_conf()
    assert conf[key] == value
    assert key.replace('.', '_') not in conf
    assert (runtime / 'server.conf').read_bytes() == original


def test_public_templates_are_declarations_not_implicit_defaults(runtime):
    assert app_config.get_conf() == {
        'ip': '127.0.0.1', 'enable_https': 'true', 'heartbeat.interval': '30',
    }
    assert app_config.get_persistence_conf() == {'persistence.mode': 'file'}


def test_commented_public_key_is_still_environment_overridable(runtime, monkeypatch):
    (runtime / 'server.conf').write_text('# integration.enabled=false\n', encoding='utf-8')
    monkeypatch.setenv('REGISTRY_INTEGRATION_ENABLED', 'true')
    assert app_config.get_conf()['integration.enabled'] == 'true'


def test_missing_audit_key_is_overridable_without_importing_other_sink_defaults(runtime, monkeypatch):
    original = (runtime / 'persistence.conf').read_bytes()
    monkeypatch.setenv('REGISTRY_AUDIT_MYSQL_BATCH_SIZE', '25')
    assert app_config.get_persistence_conf() == {
        'persistence.mode': 'file', 'audit.mysql.batch_size': '25',
    }
    assert (runtime / 'persistence.conf').read_bytes() == original


@pytest.mark.parametrize('template', ['server.conf.example', 'persistence.conf.example'])
def test_every_public_declaration_maps_even_when_missing_from_config(runtime, monkeypatch, template):
    declared = {}
    app_config.load_configs(str(runtime / template), declared)
    for key in declared:
        with monkeypatch.context() as case:
            case.setenv(app_config.canonical_env_name(key), 'synthetic-override')
            # Only key mapping is under test; typed consumption belongs to the
            # startup/auth/storage tests. No real credentials or DB connection.
            loader = (app_config.get_conf if template == 'server.conf.example'
                      else app_config.get_persistence_conf)
            conf = loader()
            assert conf[key] == 'synthetic-override'
            if '.' in key:
                assert key.replace('.', '_') not in conf


def test_declared_dotted_key_wins_over_a_loaded_legacy_alias(monkeypatch):
    monkeypatch.setenv('REGISTRY_FOO_BAR', 'new')
    conf = {'foo_bar': 'legacy'}
    app_config.apply_env_overrides(conf, ['foo.bar'])
    assert conf == {'foo.bar': 'new', 'foo_bar': 'legacy'}


def test_custom_loaded_keys_and_unknown_raw_names_remain_supported(runtime, monkeypatch):
    (runtime / 'server.conf').write_text('plugin.extra_key=old\n', encoding='utf-8')
    monkeypatch.setenv('REGISTRY_PLUGIN_EXTRA_KEY', 'new')
    monkeypatch.setenv('REGISTRY_CUSTOM_UNDECLARED', 'raw')
    conf = app_config.get_conf()
    assert conf['plugin.extra_key'] == 'new'
    assert conf['custom_undeclared'] == 'raw'


def test_minimal_installation_without_templates_preserves_legacy_loading(runtime, monkeypatch):
    (runtime / 'server.conf.example').unlink()
    (runtime / 'persistence.conf.example').unlink()
    monkeypatch.setenv('REGISTRY_ENABLE_HTTPS', 'false')
    monkeypatch.setenv('REGISTRY_PERSISTENCE_MODE', 'sqlite')
    assert app_config.get_conf()['enable_https'] == 'false'
    assert app_config.get_persistence_conf()['persistence.mode'] == 'sqlite'


@pytest.mark.parametrize('value', ['true', 'false'])
def test_documented_listener_override_is_a_literal_not_an_unresolved_placeholder(runtime, monkeypatch, value):
    monkeypatch.setenv('REGISTRY_ENABLE_HTTPS', value)
    assert app_config.get_conf()['enable_https'] == value


def test_persistence_placeholder_then_registry_override_precedence(runtime, monkeypatch):
    (runtime / 'persistence.conf').write_text(
        'persistence.mode=file\naudit.mysql.batch_size=${SYNTHETIC_BATCH:50}\n', encoding='utf-8')
    monkeypatch.setenv('SYNTHETIC_BATCH', '70')
    monkeypatch.setenv('REGISTRY_AUDIT_MYSQL_BATCH_SIZE', '25')
    assert app_config.get_persistence_conf()['audit.mysql.batch_size'] == '25'
