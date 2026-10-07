# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

"""State-combination and crash-boundary regressions for durable delivery."""

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from agent_registry import server
from agent_registry.broadcast.dispatcher import WebhookDispatcher
from agent_registry.broadcast.event_bus import EventBus
from agent_registry.broadcast.events import EventType, build_event
from agent_registry.broadcast.outbox import FileOutbox, MemoryOutbox, SqlOutbox
from agent_registry.broadcast.subscriptions import MemorySubscriptionStore, Subscription
from agent_registry.persistence.sql_backend import SqlStorageBackend
from agent_registry.persistence.sqlite_storage import SQLiteStorage


def event(kind=EventType.AGENT_UPDATED):
    return build_event(kind, {'name': 'same', 'organization': 'org',
                             'discovery_public': True}, 0)


@pytest.fixture(params=['memory', 'file', 'sqlite'])
def outbox(request, tmp_path):
    if request.param == 'memory':
        yield MemoryOutbox()
    elif request.param == 'file':
        yield FileOutbox(str(tmp_path / 'events.jsonl'))
    else:
        storage = SQLiteStorage.init({'sqlite.path': str(tmp_path / 'registry.db')})
        try:
            yield SqlOutbox(storage)
        finally:
            storage.close()


@pytest.fixture
def callbacks(monkeypatch):
    monkeypatch.setattr('agent_registry.broadcast.callback_policy.get_conf',
                        lambda: {'broadcast.callback.allowlist': 'a.example,b.example'})


@pytest.mark.asyncio
@pytest.mark.parametrize('kinds', [
    (EventType.AGENT_REGISTERED, EventType.AGENT_UPDATED),
    (EventType.AGENT_REGISTERED, EventType.AGENT_DEREGISTERED),
    (EventType.AGENT_DEREGISTERED, EventType.AGENT_REGISTERED),
])
async def test_repeated_agent_changes_have_no_orphan_delivery(outbox, callbacks, kinds):
    store = MemorySubscriptionStore()
    fast = store.create(Subscription('', 'https://a.example/hook'))
    slow = store.create(Subscription('', 'https://b.example/hook'))
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200 if request.url.host == 'a.example' else 500)

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        dispatcher = WebhookDispatcher(store, outbox, client=client, max_retries=0)
        dispatcher.add_subscription(fast)
        dispatcher.add_subscription(slow)
        changes = [outbox.append(event(kind)) for kind in kinds]
        try:
            for change in changes:
                dispatcher.submit(change)
            dispatcher.submit(changes[-1])  # Duplicate intake of one ID is harmless.
            dispatcher._flush_buffers()
            for sub in (fast, slow):
                batch = dispatcher._workers[sub.subscription_id].queue.get_nowait()
                assert [e.event_id for e in batch] == [e.event_id for e in changes]
                await dispatcher._process_batch(sub.subscription_id, batch)
            assert len(requests) == 2
            assert outbox.list_pending_for(fast.subscription_id) == []
            assert outbox.list_pending_for(slow.subscription_id) == []
            for change in changes:
                assert outbox.delivery_status(change.event_id, fast.subscription_id) == 'dispatched'
                assert outbox.delivery_status(change.event_id, slow.subscription_id) == 'delivery_failed'
            assert outbox.list_retryable_for(fast.subscription_id, 5) == []
            assert [e.event_id for e in outbox.list_retryable_for(slow.subscription_id, 5)] == [
                e.event_id for e in changes]
        finally:
            await dispatcher.stop()


def test_last_vanished_subscriber_can_be_cleaned(outbox):
    change = outbox.append(event())
    outbox.ensure_deliveries(change, ['gone'])
    assert outbox.cleanup(-1, active_subscription_ids=[]) == 1
    assert outbox.list_after(0, 10) == []


@pytest.mark.parametrize('operation', ['retry', 'mark', 'ensure', 'drop', 'cleanup'])
def test_sql_delivery_write_failure_rolls_back_the_whole_operation(tmp_path, monkeypatch, operation):
    path = str(tmp_path / 'registry.db')
    storage = SQLiteStorage.init({'sqlite.path': path})
    box = SqlOutbox(storage)
    change = box.append(event())
    if operation != 'ensure':
        box.ensure_deliveries(change, ['s'])
    if operation == 'retry':
        box.mark_delivery('s', change.event_id, 'delivery_failed')
    elif operation == 'cleanup':
        box.mark_delivery('s', change.event_id, 'dispatched')
    before_status = box.list_after(0, 1)[0].status
    before_delivery = box.delivery_status(change.event_id, 's')
    before_attempts = box.delivery_attempts(change.event_id, 's')

    def crash(*args, **kwargs):
        raise RuntimeError('injected failure between durable writes')

    with monkeypatch.context() as patch:
        if operation == 'cleanup':
            original = storage._execute_write

            def failing_write(sql, params=None):
                if sql.startswith('DELETE FROM registry_events'):
                    crash()
                return original(sql, params)

            patch.setattr(storage, '_execute_write', failing_write)
        else:
            patch.setattr(box, '_set_event_status' if operation == 'drop'
                          else '_sync_event_status', crash)
        try:
            with pytest.raises(RuntimeError, match='between durable writes'):
                if operation == 'retry':
                    box.retry_delivery('s', change.event_id, 5)
                elif operation == 'mark':
                    box.mark_delivery('s', change.event_id, 'dispatched')
                elif operation == 'ensure':
                    box.ensure_deliveries(change, ['s'])
                elif operation == 'drop':
                    box.drop_deliveries('s')
                else:
                    box.cleanup(-1, active_subscription_ids=['s'])
        finally:
            storage.close()

    reopened = SQLiteStorage.init({'sqlite.path': path})
    try:
        persisted = SqlOutbox(reopened)
        assert persisted.list_after(0, 1)[0].status == before_status
        assert persisted.delivery_status(change.event_id, 's') == before_delivery
        assert persisted.delivery_attempts(change.event_id, 's') == before_attempts
        if operation == 'retry':
            assert [e.event_id for e in persisted.list_retryable_for('s', 5)] == [change.event_id]
    finally:
        reopened.close()


@pytest.mark.asyncio
async def test_recovery_uses_pending_rows_despite_stale_event_aggregate(outbox, callbacks):
    store = MemorySubscriptionStore()
    sub = store.create(Subscription('', 'https://a.example/hook'))
    change = outbox.append(event())
    outbox.ensure_deliveries(change, [sub.subscription_id])
    # Model an older process dying after persisting the ledger but not the aggregate.
    outbox._set_event_status(change.event_id, 'delivery_failed')
    assert outbox.list_pending() == []
    assert outbox.delivery_status(change.event_id, sub.subscription_id) == 'pending'
    if isinstance(outbox, FileOutbox):
        outbox = FileOutbox(str(outbox._path))
    elif isinstance(outbox, SqlOutbox):
        outbox = SqlOutbox(outbox._backend)
    async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200))) as client:
        dispatcher = WebhookDispatcher(store, outbox, client=client, max_retries=0)
        dispatcher.add_subscription(sub)
        try:
            dispatcher.recover()
            batch = dispatcher._workers[sub.subscription_id].queue.get_nowait()
            assert [e.event_id for e in batch] == [change.event_id]
            await dispatcher._process_batch(sub.subscription_id, batch)
            assert outbox.delivery_status(change.event_id, sub.subscription_id) == 'dispatched'
        finally:
            await dispatcher.stop()


@pytest.mark.parametrize('old_session_id', [0, 73])
def test_mysql_missing_counter_rejects_stale_session_id(old_session_id):
    box = SqlOutbox.__new__(SqlOutbox)
    box._backend = SimpleNamespace(dialect='mysql', param_ph='%s',
        _execute_write=MagicMock(return_value=0),
        _execute_read_one=MagicMock(return_value=(old_session_id,)))
    with pytest.raises(RuntimeError, match='commit-order guarantee'):
        box._allocate_version()
    box._backend._execute_read_one.assert_not_called()


class _RowLockBackend(SqlStorageBackend):
    """Model a DB row lock, using the actual backend's nested UoW implementation."""
    dialect = 'postgresql'

    @classmethod
    def init(cls, config=None):
        return cls()

    def close(self):
        pass

    def __init__(self):
        self.row_lock = threading.Lock()
        self.b_waiting = threading.Event()
        self.version = 0
        self.commits = []

    def _acquire_conn(self):
        backend = self

        class Connection:
            owns_row = False

            def commit(self):
                backend.commits.append(threading.current_thread().name)
                self.rollback()

            def rollback(self):
                if self.owns_row:
                    self.owns_row = False
                    backend.row_lock.release()

        return Connection()

    def _execute_write(self, sql, params=None):
        conn = self._tx_conn()
        assert conn is not None
        if sql.startswith('UPDATE registry_event_counter'):
            if not conn.owns_row:
                if threading.current_thread().name == 'B':
                    self.b_waiting.set()
                if not self.row_lock.acquire(timeout=2):
                    raise TimeoutError('database row-lock timeout')
                conn.owns_row = True
            self.version += 1
        return 1

    def _execute_read_one(self, sql, params=None):
        assert self._tx_conn() is not None
        return (self.version,)


def test_shared_outbox_allows_outer_uow_to_append_twice_while_another_writer_waits():
    backend = _RowLockBackend()
    box = SqlOutbox.__new__(SqlOutbox)
    box._backend = backend
    box._lock = threading.Lock()  # Old implementation used this extra process lock.
    first_open = threading.Event()
    errors, versions = [], {}

    def first():
        try:
            with backend.transaction():
                versions['a1'] = box.append(event()).registry_version
                first_open.set()
                assert backend.b_waiting.wait(1)
                versions['a2'] = box.append(event()).registry_version
        except Exception as exc:
            errors.append(exc)

    def second():
        try:
            assert first_open.wait(1)
            versions['b'] = box.append(event()).registry_version
        except Exception as exc:
            errors.append(exc)

    threads = [threading.Thread(target=first, name='A'), threading.Thread(target=second, name='B')]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(4)
    assert not any(thread.is_alive() for thread in threads)
    assert errors == []
    assert versions == {'a1': 1, 'a2': 2, 'b': 3}
    assert backend.commits == ['A', 'B']


@pytest.mark.asyncio
@pytest.mark.parametrize('hide', [False, True])
@pytest.mark.parametrize('health_status', ['healthy', 'suspect', 'offline'])
async def test_health_monitor_stream_keeps_published_unhealthy_notifications(monkeypatch, hide, health_status):
    health = SimpleNamespace(enabled=True, hide_unhealthy_results=hide,
                             status_of=lambda *args: health_status)
    registry = SimpleNamespace(get_status=lambda *args: 'published',
                               require_authoritative_store=lambda *args: None)
    bus = EventBus(MemoryOutbox())
    monkeypatch.setattr(server, 'get_health_service', lambda: health)
    monkeypatch.setattr(server, 'get_broadcast_service', lambda: SimpleNamespace(event_bus=bus))
    monkeypatch.setattr(server.HandlerRegistry, 'get_handler',
                        lambda _: SimpleNamespace(handle=AsyncMock(return_value=None)))
    request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'),
                              is_disconnected=AsyncMock(return_value=False))
    response = await server.stream_agent_health(request, registry=registry)
    try:
        assert 'connected' in await anext(response.body_iterator)
        bus.publish(EventType.AGENT_HEALTH_CHANGED, {'name': 'same', 'organization': 'org',
                                                     'health_status': health_status})
        frame = await asyncio.wait_for(anext(response.body_iterator), 1)
        assert 'event: health_changed' in frame
        assert health_status in frame
        assert server._is_public_agent('same', 'org', registry)
        assert server._is_discoverable('same', 'org', registry) == (
            not hide or health_status == 'healthy')
    finally:
        await response.body_iterator.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize('hide', [False, True])
async def test_health_stream_does_not_publish_pending_agent(monkeypatch, hide):
    health = SimpleNamespace(enabled=True, hide_unhealthy_results=hide, status_of=lambda *args: 'healthy')
    registry = SimpleNamespace(get_status=lambda name, org: 'registered' if name == 'pending' else 'published',
                               require_authoritative_store=lambda *args: None)
    bus = EventBus(MemoryOutbox())
    monkeypatch.setattr(server, 'get_health_service', lambda: health)
    monkeypatch.setattr(server, 'get_broadcast_service', lambda: SimpleNamespace(event_bus=bus))
    monkeypatch.setattr(server.HandlerRegistry, 'get_handler',
                        lambda _: SimpleNamespace(handle=AsyncMock(return_value=None)))
    request = SimpleNamespace(client=SimpleNamespace(host='127.0.0.1'),
                              is_disconnected=AsyncMock(return_value=False))
    response = await server.stream_agent_health(request, registry=registry)
    try:
        await anext(response.body_iterator)
        bus.publish(EventType.AGENT_HEALTH_CHANGED, {'name': 'pending', 'organization': 'org'})
        bus.publish(EventType.AGENT_HEALTH_CHANGED, {'name': 'published', 'organization': 'org'})
        frame = await asyncio.wait_for(anext(response.body_iterator), 1)
        assert 'published' in frame and 'pending' not in frame
    finally:
        await response.body_iterator.aclose()
