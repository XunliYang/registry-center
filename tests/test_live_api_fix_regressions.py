# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Regression contracts for the six findings from the real-service review.

These focused tests complement, not replace, the socket/TLS acceptance probe.
"""
import asyncio
import json
import threading
from collections import Counter
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from a2a.types import AgentCard
from fastapi import HTTPException
from google.protobuf.json_format import MessageToDict

from agent_registry.broadcast.dispatcher import WebhookDispatcher
from agent_registry.broadcast.event_bus import EventBus
from agent_registry.broadcast.events import EventType, build_event
from agent_registry.broadcast.outbox import FileOutbox, MemoryOutbox, SqlOutbox
from agent_registry.broadcast.subscriptions import MemorySubscriptionStore, Subscription
from agent_registry.core import RegistryCore
from agent_registry.middleware import ConnectionLimitMiddleware
from agent_registry.persistence.sqlite_storage import SQLiteStorage
from agent_registry.request_validation import card_batch, parse_card, semantic_query
from common.custom.custom_handle import RetrieveHandler


def card(name='original', organization='org'):
    return AgentCard(name=name, provider={'organization': organization}, description='diagnostics')


def event(name='a'):
    return build_event(EventType.AGENT_REGISTERED, {'name': name, 'organization': 'org'}, 0)


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


@pytest.mark.parametrize('mode', ['file', 'sqlite'])
@pytest.mark.parametrize('replacement', [card('renamed'), card(organization='changed')])
def test_identity_rejected_before_card_or_event_write(mode, replacement, tmp_path, monkeypatch):
    registry = RegistryCore(use_vectordb=False, persistence_mode=mode,
                            persistence_file=str(tmp_path / 'cards.json'),
                            persistence_conf={'sqlite.path': str(tmp_path / 'registry.db')})
    store = SqlOutbox(registry.storage) if mode == 'sqlite' else MemoryOutbox()
    monkeypatch.setattr('agent_registry.core.get_event_bus', lambda: EventBus(store))
    try:
        assert registry.register(card())
        version = store.max_version()
        payload = MessageToDict(replacement)
        with pytest.raises(ValueError, match='primary key'):
            registry.update('original', 'org', payload)
        # Storage is a second line of defense against callers bypassing core.
        with pytest.raises(ValueError, match='primary key'):
            registry.storage.update('original', 'org', payload)
        assert registry.storage.find_by_key('original', 'org').agent_card == card()
        assert registry.storage.find_by_key(replacement.name, replacement.provider.organization) is None
        assert store.max_version() == version
    finally:
        registry.storage.close()


@pytest.mark.parametrize('cards', [None, '', 'text', {}, [], 1])
def test_invalid_batch_shape_is_client_error(cards):
    with pytest.raises(HTTPException) as error:
        card_batch({'agentCards': cards})
    assert error.value.status_code == 422


@pytest.mark.parametrize('value', [None, [], 'text', 1, {'unknownField': 1}, {'skills': 'wrong'}])
def test_invalid_card_schema_is_client_error(value):
    with pytest.raises(HTTPException) as error:
        parse_card(value)
    assert error.value.status_code == 422


@pytest.mark.parametrize('task', [None, '', '   ', True, 12, {}, [], 'x' * 10001])
def test_semantic_task_contract(task):
    with pytest.raises(HTTPException) as error:
        semantic_query({'task': task})
    assert error.value.status_code == 422


@pytest.mark.parametrize('top_n', [None, True, False, 0, -1, 51, 1.5, '10'])
def test_semantic_count_is_strict_integer(top_n):
    with pytest.raises(HTTPException) as error:
        semantic_query({'task': 'diagnostics'}, top_n)
    assert error.value.status_code == 422


@pytest.mark.parametrize('top_n', [1, 10, 50])
def test_semantic_contract_keeps_valid_values(top_n):
    assert semantic_query({'task': 'diagnostics'}, top_n) == ('diagnostics', top_n)


@pytest.mark.asyncio
@pytest.mark.parametrize('cancel', [False, True])
async def test_connection_quota_covers_body_and_releases_on_cancel(cancel):
    streaming = asyncio.Event()
    finish = asyncio.Event()

    async def app(scope, receive, send):
        await send({'type': 'http.response.start', 'status': 200, 'headers': []})
        await send({'type': 'http.response.body', 'body': b'data: open\n\n', 'more_body': True})
        streaming.set()
        await finish.wait()
        await send({'type': 'http.response.body', 'body': b'', 'more_body': False})

    middleware = ConnectionLimitMiddleware(app, 1)
    scope = {'type': 'http'}
    first = asyncio.create_task(middleware(scope, AsyncMock(), AsyncMock()))
    try:
        await asyncio.wait_for(streaming.wait(), 1)
        assert middleware.active_connections == 1
        send = AsyncMock()
        await middleware(scope, AsyncMock(), send)
        assert send.await_args_list[0].args[0]['status'] == 503
        assert middleware.active_connections == 1
        if cancel:
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
        else:
            finish.set()
            await first
        assert middleware.active_connections == 0
        finish.set()
        send = AsyncMock()
        await middleware(scope, AsyncMock(), send)
        assert send.await_args_list[0].args[0]['status'] == 200
    finally:
        finish.set()
        first.cancel()
        await asyncio.gather(first, return_exceptions=True)


@pytest.mark.asyncio
async def test_non_http_scope_does_not_consume_quota():
    app = AsyncMock()
    middleware = ConnectionLimitMiddleware(app, 0)
    await middleware({'type': 'lifespan'}, AsyncMock(), AsyncMock())
    app.assert_awaited_once()
    assert middleware.active_connections == 0


@pytest.mark.asyncio
async def test_model_io_is_bounded_and_does_not_free_slots_on_cancellation(monkeypatch):
    release = threading.Event()
    eight_started = threading.Event()
    lock = threading.Lock()
    active = total = peak = 0

    def blocking_query(*args):
        nonlocal active, total, peak
        with lock:
            active += 1
            total += 1
            peak = max(peak, active)
            if total == 8:
                eight_started.set()
        try:
            assert release.wait(5)
            return []
        finally:
            with lock:
                active -= 1

    monkeypatch.setattr('common.custom.custom_handle.get_registry',
                        lambda: MagicMock(retrieve_by_task=blocking_query))
    handler = RetrieveHandler()
    tasks = [asyncio.create_task(handler.handle('x', 10)) for _ in range(16)]
    try:
        for _ in range(100):
            if eight_started.is_set():
                break
            await asyncio.sleep(0.01)
        assert eight_started.is_set()  # Loop stayed responsive while 8 threads blocked.
        for task in tasks[:8]:
            task.cancel()
        await asyncio.sleep(0.1)
        assert total == 8  # Await cancellation must NOT start eight additional I/O calls.
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        assert peak <= 8
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)


def test_pending_and_retry_fetches_are_paginated(outbox):
    events = [outbox.append(event(str(i))) for i in range(13)]
    first = outbox.list_pending(limit=4)
    second = outbox.list_pending(limit=4, after_version=first[-1].registry_version)
    assert first == events[:4]
    assert second == events[4:8]
    for item in events:
        outbox.ensure_deliveries(item, ['sub'])
        outbox.mark_delivery('sub', item.event_id, 'delivery_failed')
    assert len(outbox.list_retryable_for('sub', 3, limit=4)) == 4


@pytest.mark.asyncio
async def test_bounded_dispatch_recovers_overflow_without_duplicates(outbox, monkeypatch):
    monkeypatch.setattr('agent_registry.broadcast.callback_policy.get_conf',
                        lambda: {'broadcast.callback.allowlist': 'example.test'})
    subscriptions = MemorySubscriptionStore()
    sub = subscriptions.create(Subscription('', 'https://example.test/hook'))
    release, started = asyncio.Event(), asyncio.Event()
    received = []

    async def callback(request):
        started.set()
        await release.wait()
        received.extend(item['event_id'] for item in json.loads(request.content)['events'])
        return httpx.Response(200)

    async with httpx.AsyncClient(transport=httpx.MockTransport(callback)) as client:
        dispatcher = WebhookDispatcher(subscriptions, outbox, client=client,
                                       debounce_window=0.005, batch_size=3, buffer_limit=5,
                                       max_events_per_second=10000, retry_interval=0)
        bus = EventBus(outbox, dispatch_enabled=True)
        bus.attach_dispatcher(dispatcher)
        # Overflow the small dispatcher cache; the durable store owns all facts.
        events = [bus.publish(EventType.AGENT_REGISTERED,
                              {'name': str(i), 'organization': 'org'}) for i in range(41)]
        dispatcher.start()
        bus.start_consumer()
        try:
            await asyncio.wait_for(started.wait(), 3)
            await asyncio.sleep(0.05)
            assert len(dispatcher._buffers) <= 5
            assert dispatcher._workers[sub.subscription_id].queue.qsize() <= 1
            assert len(dispatcher._scheduled[sub.subscription_id]) <= 6
            assert len(outbox.list_pending_for(sub.subscription_id)) > 6
            release.set()
            for _ in range(1500):
                if len(received) == len(events):
                    break
                await asyncio.sleep(0.01)
            assert Counter(received) == Counter(item.event_id for item in events)
            assert outbox.list_pending() == []
            assert outbox.list_pending_for(sub.subscription_id) == []
        finally:
            release.set()
            await bus.stop_consumer()
            await dispatcher.stop()


def test_wake_queue_overflow_keeps_every_durable_fact():
    outbox = MemoryOutbox()
    bus = EventBus(outbox, dispatch_enabled=True)
    bus.attach_dispatcher(MagicMock())
    for i in range(1031):
        bus.publish(EventType.AGENT_REGISTERED, {'name': str(i), 'organization': 'org'})
    assert bus._queue.qsize() == bus._queue.maxsize == 1024
    assert bus._overflowed
    assert len(outbox.list_pending()) == 1031
