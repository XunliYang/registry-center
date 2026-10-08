# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import pytest
from a2a.types import AgentCard
from google.protobuf.json_format import MessageToDict
from unittest.mock import AsyncMock, MagicMock, patch
from fastapi.testclient import TestClient

from agent_registry.broadcast.event_bus import EventBus
from agent_registry.broadcast.events import EventType
from agent_registry.broadcast.events import build_event, public_event
from types import SimpleNamespace
from agent_registry.broadcast.outbox import SqlOutbox
from agent_registry.core import RegistryCore
from agent_registry.errors import SemanticSearchUnavailable
from agent_registry import server


@pytest.fixture
def registry(tmp_path, monkeypatch):
    core = RegistryCore(use_vectordb=False, persistence_mode='sqlite',
                        persistence_conf={'sqlite.path': str(tmp_path / 'registry.db')})
    bus = EventBus(SqlOutbox(core.storage))
    monkeypatch.setattr('agent_registry.core.get_event_bus', lambda: bus)
    core._llm = MagicMock()
    yield core, bus._outbox
    core.close()


def card(name='agent'):
    return AgentCard(name=name, description='test', version='1',
                     provider={'organization': 'org', 'url': 'https://example.com'})


def test_sql_card_update_does_not_publish_pending_agent(registry):
    core, outbox = registry
    core.register_with_status(card(), initial_status='registered', owner='owner')
    changed = card()
    changed.description = 'changed'
    assert core.update('agent', 'org', MessageToDict(changed), owner='owner')
    assert core.get_status('agent', 'org') == 'registered'
    assert outbox.max_version() == 0


def test_pending_to_pending_emits_no_public_update(registry):
    core, outbox = registry
    core.register_with_status(card(), initial_status='registered')
    assert core.update_status('agent', 'org', 'registered')
    assert outbox.max_version() == 0


def test_removed_during_semantic_selection_is_not_returned(registry, monkeypatch):
    core, _ = registry
    core.register(card())
    def select(*args):
        core.deregister('agent', 'org')
        return [('org', 'agent')]
    monkeypatch.setattr(core, '_select_agents_by_llm', select)
    assert core.retrieve_by_task('find', 5, use_vectordb=False) == []


def test_status_read_failure_is_not_treated_as_published(registry, monkeypatch):
    core, _ = registry
    core.register(card())
    monkeypatch.setattr(core.storage, 'find_by_key', MagicMock(side_effect=RuntimeError('database unavailable')))
    with pytest.raises(RuntimeError, match='database unavailable'):
        core._stored_status('agent', 'org')


@pytest.mark.parametrize('operation', ['rename', 'delete'])
def test_tag_lifecycle_updates_references_and_public_events(registry, operation):
    core, outbox = registry
    core.register(card('published'))
    core.register_with_status(card('pending'), initial_status='registered')
    tag = core.create_tag('old')
    core.update_agent_tags('published', 'org', ['old', 'keep'])
    core.update_agent_tags('pending', 'org', ['old'])
    before = outbox.max_version()
    if operation == 'rename':
        assert core.update_tag(tag.tag_id, 'new')
        assert core.get_agent_tags('published', 'org') == ['new', 'keep']
        assert core.get_agent_tags('pending', 'org') == ['new']
    else:
        assert core.delete_tag(tag.tag_id)
        assert core.get_agent_tags('published', 'org') == ['keep']
        assert core.get_agent_tags('pending', 'org') == []
    events = outbox.list_after(before, 10)
    assert len(events) == 1
    assert events[0].event_type == EventType.AGENT_UPDATED
    assert events[0].data['name'] == 'published'
    assert 'old' not in events[0].data['tags']


def test_tag_event_failure_rolls_back_tag_and_references(registry, monkeypatch):
    core, outbox = registry
    core.register(card())
    tag = core.create_tag('old')
    core.update_agent_tags('agent', 'org', ['old'])
    before = outbox.max_version()
    monkeypatch.setattr(outbox, 'append', MagicMock(side_effect=RuntimeError('outbox unavailable')))
    with pytest.raises(RuntimeError, match='outbox unavailable'):
        core.update_tag(tag.tag_id, 'new')
    assert core.get_tag(tag.tag_id).name == 'old'
    assert core.get_agent_tags('agent', 'org') == ['old']
    assert outbox.max_version() == before


def test_model_failure_is_503_not_no_match(registry, monkeypatch):
    core, _ = registry
    core.register(card())
    core._llm.ask_llm.side_effect = RuntimeError('upstream secret must not escape')
    with pytest.raises(SemanticSearchUnavailable):
        core.retrieve_by_task('sensitive task', 5, use_vectordb=False)
    server.app.dependency_overrides[server.get_registry] = lambda: core
    monkeypatch.setattr('agent_registry.registry_instance._registry_instance', core)
    try:
        response = TestClient(server.app).post('/rest/v1/registry-center/agent-cards/semantic-query',
                                               json={'task': 'sensitive task'})
        assert response.status_code == 503
        assert 'upstream secret' not in response.text
    finally:
        server.app.dependency_overrides.clear()


def test_historical_pending_payload_is_quarantined_in_changes(registry, monkeypatch):
    core, outbox = registry
    legacy = outbox.append(build_event(EventType.AGENT_REGISTERED,
        {'name': 'pending', 'agent_card': {'description': 'private pending payload'}}, 0))
    core.register(card('published'))
    monkeypatch.setattr(server, 'get_broadcast_service', lambda: SimpleNamespace(outbox=outbox))
    response = TestClient(server.app).get('/rest/v1/registry-center/changes')
    assert response.status_code == 200
    changes = response.json()['changes']
    assert changes[0]['event_type'] == 'SYNC_REQUIRED'
    assert changes[0]['registry_version'] == legacy.registry_version
    assert changes[0]['data']['requires_snapshot'] is True
    assert 'private pending payload' not in response.text
    assert changes[1]['data']['agent_card']['name'] == 'published'
    assert response.json()['next_since'] == changes[1]['registry_version']


def test_public_removal_is_not_filtered_by_current_status(registry):
    core, outbox = registry
    core.register(card())
    before = outbox.max_version()
    core.update_status('agent', 'org', 'registered')
    event = public_event(outbox.list_after(before, 1)[0])
    assert event.event_type == EventType.AGENT_DEREGISTERED
    assert 'agent_card' not in event.data


def test_legacy_null_status_row_stays_published_on_every_surface(registry, monkeypatch):
    """A row whose status column is NULL is the legacy published card.

    The storage layer already answers `find_all(status='published')` through
    `COALESCE(status, 'published')`, so reporting the raw NULL from `get_status()`
    made a returned card invisible to the server/integration visibility checks.
    """
    core, _ = registry
    core.register(card())
    core.storage._execute_write("UPDATE agent_card SET status = NULL WHERE name = ?", ('agent',))

    assert [c.name for c in core.storage.find_all(status='published')] == ['agent']
    assert core.get_status('agent', 'org') == 'published'
    assert core.get_metadata('agent', 'org')['status'] == 'published'
    assert server._is_discoverable('agent', 'org', core) is True

    record = core.storage.find_by_key('agent', 'org')
    assert record.status is None, 'the raw row must still be NULL for this to be the legacy case'
    handler = MagicMock()
    handler.handle = AsyncMock(return_value=record)
    server.app.dependency_overrides[server.get_registry] = lambda: core
    try:
        with patch('common.custom.custom_handle.HandlerRegistry.get_handler', return_value=handler):
            response = TestClient(server.app).get('/rest/v1/registry-center/agent-cards/org/agent')
    finally:
        server.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert [card['name'] for card in response.json()['agentCards']] == ['agent']


def test_missing_record_is_still_reported_as_missing(registry):
    """Normalizing a NULL status must not turn "absent" into "published"."""
    core, _ = registry
    assert core.get_status('ghost', 'org') is None
    assert core.get_metadata('ghost', 'org')['status'] == 'registered'
    assert server._is_discoverable('ghost', 'org', core) is False


def test_missing_transport_peer_cannot_trust_forwarded_client_address():
    from agent_registry.identity import resolve_caller_identity
    from starlette.requests import Request
    request = Request({'type': 'http', 'headers': [(b'x-ssl-client-dn', b'CN=owner')],
                       'client': ('127.0.0.1', 123), 'tls_direct_peer': None})
    identity = resolve_caller_identity(request, {
        'owner.identity.mode': 'trusted_proxy', 'owner.trusted.proxy.ips': '127.0.0.1'})
    assert not identity.verified
