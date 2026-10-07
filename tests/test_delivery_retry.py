# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Durable, bounded retry for failed webhook deliveries (the R3 residual).

The per-subscriber ledger (see `test_subscriber_delivery.py`) records *who* still
owes a delivery. This file covers what happens *after* a failure: a failed row is
retried with the same spacing as the sweep, the attempt counter is persisted so
the budget survives a restart, and a destination that never recovers stops at the
budget instead of looping forever.

The same contract is asserted for all three persistence modes; SQLite stands in
for the SQL dialects (PostgreSQL/GaussDB/MySQL share this code path).
"""

import httpx
import pytest

from agent_registry.broadcast.dispatcher import WebhookDispatcher
from agent_registry.broadcast.events import EventType, build_event
from agent_registry.broadcast.outbox import FileOutbox, MemoryOutbox, SqlOutbox
from agent_registry.broadcast.subscriptions import MemorySubscriptionStore, Subscription
from agent_registry.persistence.sqlite_storage import SQLiteStorage

MAX_ATTEMPTS = 3


def _event(name="a1", organization="org1"):
    return build_event(EventType.AGENT_REGISTERED,
                       {"name": name, "organization": organization}, 0)


@pytest.fixture(params=["memory", "file", "sqlite"])
def outbox(request, tmp_path):
    if request.param == "memory":
        yield MemoryOutbox()
    elif request.param == "file":
        yield FileOutbox(str(tmp_path / "events.jsonl"))
    else:
        storage = SQLiteStorage.init({"sqlite.path": str(tmp_path / "registry.db")})
        try:
            yield SqlOutbox(storage)
        finally:
            storage.close()


# ---------- the contract, identical across persistence modes ----------

def test_attempts_are_counted_per_delivery(outbox):
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a", "sub-b"])

    assert outbox.delivery_attempts(event.event_id, "sub-a") == 0

    outbox.mark_delivery("sub-a", event.event_id, "delivery_failed")

    assert outbox.delivery_attempts(event.event_id, "sub-a") == 1
    # The other subscriber's budget is its own.
    assert outbox.delivery_attempts(event.event_id, "sub-b") == 0


def test_failed_delivery_is_retryable_until_the_budget_runs_out(outbox):
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a"])

    for attempt in range(MAX_ATTEMPTS):
        outbox.mark_delivery("sub-a", event.event_id, "delivery_failed")
        assert outbox.delivery_attempts(event.event_id, "sub-a") == attempt + 1
        retryable = [e.event_id for e in outbox.list_retryable_for("sub-a", MAX_ATTEMPTS)]
        if attempt + 1 < MAX_ATTEMPTS:
            assert retryable == [event.event_id]
            assert outbox.retry_delivery("sub-a", event.event_id, MAX_ATTEMPTS) is True
            assert outbox.delivery_status(event.event_id, "sub-a") == "pending"
        else:
            # Budget exhausted: terminal failure, reported through /changes.
            assert retryable == []
            assert outbox.retry_delivery("sub-a", event.event_id, MAX_ATTEMPTS) is False

    assert outbox.delivery_status(event.event_id, "sub-a") == "delivery_failed"
    assert outbox.list_after(0, 10)[0].status == "delivery_failed"


def test_an_operator_can_force_a_retry_without_a_budget(outbox):
    """An explicit retry (no budget) still works after the sweep gave up."""
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a"])
    for _ in range(MAX_ATTEMPTS):
        outbox.mark_delivery("sub-a", event.event_id, "delivery_failed")

    assert outbox.list_retryable_for("sub-a", MAX_ATTEMPTS) == []
    assert outbox.retry_delivery("sub-a", event.event_id) is True

    assert outbox.delivery_status(event.event_id, "sub-a") == "pending"
    assert outbox.list_after(0, 10)[0].status == "pending"


def test_retrying_keeps_the_attempt_count_and_requeues_the_event(outbox):
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a"])
    outbox.mark_delivery("sub-a", event.event_id, "delivery_failed")

    assert outbox.retry_delivery("sub-a", event.event_id, MAX_ATTEMPTS) is True

    # The row is pending again (so a restart recovers it) without losing the fact
    # that one attempt was already spent.
    assert outbox.delivery_status(event.event_id, "sub-a") == "pending"
    assert outbox.delivery_attempts(event.event_id, "sub-a") == 1
    assert [e.event_id for e in outbox.list_pending_for("sub-a")] == [event.event_id]
    assert [e.event_id for e in outbox.list_pending()] == [event.event_id]


def test_retry_refuses_a_delivery_that_did_not_fail(outbox):
    """Only failed rows are requeued; a dispatched delivery stays done."""
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a"])
    outbox.mark_delivery("sub-a", event.event_id, "dispatched")

    assert outbox.retry_delivery("sub-a", event.event_id, MAX_ATTEMPTS) is False
    assert outbox.retry_delivery("sub-unknown", event.event_id, MAX_ATTEMPTS) is False

    assert outbox.delivery_status(event.event_id, "sub-a") == "dispatched"
    assert outbox.list_retryable_for("sub-a", MAX_ATTEMPTS) == []
    assert outbox.list_after(0, 10)[0].status == "dispatched"


def test_event_with_any_pending_subscriber_is_pending_again(outbox):
    """One subscriber's failure must not hide another's still-owed delivery."""
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a", "sub-b"])
    outbox.mark_delivery("sub-a", event.event_id, "dispatched")
    outbox.mark_delivery("sub-b", event.event_id, "delivery_failed")

    outbox.retry_delivery("sub-b", event.event_id, MAX_ATTEMPTS)

    assert outbox.list_after(0, 10)[0].status == "pending"
    assert [e.event_id for e in outbox.list_pending()] == [event.event_id]
    assert outbox.list_pending_for("sub-a") == []


def test_file_mode_keeps_the_attempt_count_across_a_restart(tmp_path):
    path = str(tmp_path / "events.jsonl")
    outbox = FileOutbox(path)
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a"])
    outbox.mark_delivery("sub-a", event.event_id, "delivery_failed")
    outbox.mark_delivery("sub-a", event.event_id, "delivery_failed")

    restarted = FileOutbox(path)

    assert restarted.delivery_attempts(event.event_id, "sub-a") == 2
    assert restarted.delivery_status(event.event_id, "sub-a") == "delivery_failed"
    # Still inside the budget, so a restarted process still retries it.
    assert [e.event_id for e in restarted.list_retryable_for("sub-a", MAX_ATTEMPTS)] \
        == [event.event_id]
    assert [e.event_id for e in restarted.list_retryable_for("sub-a", 2)] == []


def test_sql_mode_keeps_the_attempt_count_across_a_restart(tmp_path):
    path = str(tmp_path / "registry.db")
    storage = SQLiteStorage.init({"sqlite.path": path})
    try:
        outbox = SqlOutbox(storage)
        event = outbox.append(_event())
        outbox.ensure_deliveries(event, ["sub-a"])
        outbox.mark_delivery("sub-a", event.event_id, "delivery_failed")
    finally:
        storage.close()

    storage2 = SQLiteStorage.init({"sqlite.path": path})
    try:
        reopened = SqlOutbox(storage2)
        assert reopened.delivery_attempts(event.event_id, "sub-a") == 1
        assert [e.event_id for e in reopened.list_retryable_for("sub-a", MAX_ATTEMPTS)] \
            == [event.event_id]
        assert reopened.retry_delivery("sub-a", event.event_id) is True
        assert reopened.delivery_status(event.event_id, "sub-a") == "pending"
        assert [e.event_id for e in reopened.list_pending()] == [event.event_id]
    finally:
        storage2.close()


def test_older_delivery_lines_without_attempts_still_load(tmp_path):
    """The ledger file predates the counter: a missing `attempts` means zero."""
    path = str(tmp_path / "events.jsonl")
    outbox = FileOutbox(path)
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a"])
    outbox.mark_delivery("sub-a", event.event_id, "delivery_failed")

    legacy = (tmp_path / "events.jsonl.deliveries")
    legacy.write_text(
        '{"event_id": "%s", "subscription_id": "sub-a", "status": "delivery_failed"}\n'
        % event.event_id, encoding="utf-8")

    reloaded = FileOutbox(path)
    assert reloaded.delivery_attempts(event.event_id, "sub-a") == 0
    assert [e.event_id for e in reloaded.list_retryable_for("sub-a", MAX_ATTEMPTS)] \
        == [event.event_id]


# ---------- dispatcher ----------

def _allow_hosts(monkeypatch, hosts):
    monkeypatch.setattr('agent_registry.broadcast.callback_policy.get_conf',
                        lambda: {'broadcast.callback.allowlist': ",".join(hosts)})


class _FlakyEndpoint:
    """Fails the first `failures` deliveries, then accepts."""

    def __init__(self, failures: int):
        self.failures = failures
        self.calls = 0

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls += 1
        return httpx.Response(500 if self.calls <= self.failures else 200)


def _dispatcher(store, outbox, client, **overrides):
    kwargs = dict(debounce_window=0.01, max_events_per_second=1000.0, max_retries=0,
                  backoff_base=0.01, backoff_max=0.02, webhook_timeout=1.0,
                  delivery_max_attempts=MAX_ATTEMPTS, retry_interval=3600.0)
    kwargs.update(overrides)
    return WebhookDispatcher(store, outbox, client=client, **kwargs)


@pytest.mark.asyncio
async def test_failed_delivery_is_delivered_by_a_later_sweep(monkeypatch):
    _allow_hosts(monkeypatch, ["a.example.com"])
    endpoint = _FlakyEndpoint(failures=1)

    store = MemorySubscriptionStore()
    sub = store.create(Subscription("", "https://a.example.com/hook"))
    outbox = MemoryOutbox()
    client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    dispatcher = _dispatcher(store, outbox, client)
    dispatcher.add_subscription(sub)

    event = outbox.append(_event())
    dispatcher.submit(event)
    dispatcher._flush_buffers()
    await dispatcher._process_batch(sub.subscription_id,
                                    dispatcher._workers[sub.subscription_id].queue.get_nowait())
    assert outbox.delivery_status(event.event_id, sub.subscription_id) == "delivery_failed"

    assert dispatcher.retry_failed_deliveries() == 1
    await dispatcher._process_batch(sub.subscription_id,
                                    dispatcher._workers[sub.subscription_id].queue.get_nowait())

    assert outbox.delivery_status(event.event_id, sub.subscription_id) == "dispatched"
    assert outbox.delivery_attempts(event.event_id, sub.subscription_id) == 2
    assert outbox.list_after(0, 10)[0].status == "dispatched"
    await dispatcher.stop()


@pytest.mark.asyncio
async def test_destination_that_never_recovers_stops_at_the_budget(monkeypatch):
    _allow_hosts(monkeypatch, ["a.example.com"])
    endpoint = _FlakyEndpoint(failures=10)

    store = MemorySubscriptionStore()
    sub = store.create(Subscription("", "https://a.example.com/hook"))
    outbox = MemoryOutbox()
    client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    dispatcher = _dispatcher(store, outbox, client)
    dispatcher.add_subscription(sub)

    event = outbox.append(_event())
    dispatcher.submit(event)
    dispatcher._flush_buffers()
    await dispatcher._process_batch(sub.subscription_id,
                                    dispatcher._workers[sub.subscription_id].queue.get_nowait())

    sweeps = 0
    while dispatcher.retry_failed_deliveries():
        sweeps += 1
        await dispatcher._process_batch(sub.subscription_id,
                                        dispatcher._workers[sub.subscription_id].queue.get_nowait())
        assert sweeps <= MAX_ATTEMPTS, "retry budget did not stop the sweeps"

    assert sweeps == MAX_ATTEMPTS - 1
    assert outbox.delivery_attempts(event.event_id, sub.subscription_id) == MAX_ATTEMPTS
    assert outbox.delivery_status(event.event_id, sub.subscription_id) == "delivery_failed"
    # Terminal: neither the sweep nor recovery resurrects it, and /changes reports it.
    assert outbox.list_retryable_for(sub.subscription_id, MAX_ATTEMPTS) == []
    assert outbox.list_pending() == []
    assert outbox.list_after(0, 10)[0].status == "delivery_failed"
    await dispatcher.stop()


@pytest.mark.asyncio
async def test_restart_requeues_a_failed_delivery(monkeypatch, tmp_path):
    """The point of the durable counter: a crash does not lose the retry."""
    _allow_hosts(monkeypatch, ["a.example.com"])
    path = str(tmp_path / "registry.db")
    storage = SQLiteStorage.init({"sqlite.path": path})
    store = MemorySubscriptionStore()
    sub = store.create(Subscription("", "https://a.example.com/hook"))
    try:
        outbox = SqlOutbox(storage)
        event = outbox.append(_event())
        outbox.ensure_deliveries(event, [sub.subscription_id])
        outbox.mark_delivery(sub.subscription_id, event.event_id, "delivery_failed")
    finally:
        storage.close()

    # A fresh process: new backend, new outbox, new dispatcher, same delivery row.
    storage2 = SQLiteStorage.init({"sqlite.path": path})
    try:
        outbox2 = SqlOutbox(storage2)
        endpoint = _FlakyEndpoint(failures=0)
        client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
        dispatcher = _dispatcher(store, outbox2, client)
        dispatcher.add_subscription(sub)

        assert outbox2.delivery_status(event.event_id, sub.subscription_id) == "delivery_failed"
        assert dispatcher.retry_failed_deliveries() == 1

        await dispatcher._process_batch(sub.subscription_id,
                                        dispatcher._workers[sub.subscription_id].queue.get_nowait())
        assert outbox2.delivery_status(event.event_id, sub.subscription_id) == "dispatched"
        await dispatcher.stop()
    finally:
        storage2.close()


@pytest.mark.asyncio
async def test_start_sweeps_failures_without_waiting_for_the_interval(monkeypatch, tmp_path):
    """`start()` retries immediately; the interval only spaces later sweeps."""
    _allow_hosts(monkeypatch, ["a.example.com"])
    path = str(tmp_path / "events.jsonl")
    store = MemorySubscriptionStore()
    sub = store.create(Subscription("", "https://a.example.com/hook"))

    outbox = FileOutbox(path)
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, [sub.subscription_id])
    outbox.mark_delivery(sub.subscription_id, event.event_id, "delivery_failed")

    restarted = FileOutbox(path)
    endpoint = _FlakyEndpoint(failures=0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    dispatcher = _dispatcher(store, restarted, client, retry_interval=3600.0)

    dispatcher.start()
    try:
        # Queued during start() (recover() would not touch a failed row).
        batch = dispatcher._workers[sub.subscription_id].queue.get_nowait()
        await dispatcher._process_batch(sub.subscription_id, batch)
        assert restarted.delivery_status(event.event_id, sub.subscription_id) == "dispatched"
        assert restarted.delivery_attempts(event.event_id, sub.subscription_id) == 2
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_crash_after_a_retry_reset_is_still_recovered(monkeypatch, tmp_path):
    """The row is pending between the retry and the attempt: a crash must not strand it.

    `_sync_event_status` normally never writes `pending` (it is the default state),
    so the retry has to force it — otherwise `recover()` would skip the event and
    the delivery would be lost until the subscriber reconciles by hand.
    """
    _allow_hosts(monkeypatch, ["a.example.com"])
    path = str(tmp_path / "events.jsonl")
    store = MemorySubscriptionStore()
    sub = store.create(Subscription("", "https://a.example.com/hook"))

    outbox = FileOutbox(path)
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, [sub.subscription_id])
    outbox.mark_delivery(sub.subscription_id, event.event_id, "delivery_failed")

    # A sweep requeues it, then the process dies before the attempt happens.
    assert outbox.retry_delivery(sub.subscription_id, event.event_id, MAX_ATTEMPTS) is True
    assert outbox.list_after(0, 10)[0].status == "pending"

    restarted = FileOutbox(path)
    assert [e.event_id for e in restarted.list_pending()] == [event.event_id]

    endpoint = _FlakyEndpoint(failures=0)
    client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    dispatcher = _dispatcher(store, restarted, client, retry_interval=3600.0)
    dispatcher.start()
    try:
        batch = dispatcher._workers[sub.subscription_id].queue.get_nowait()
        await dispatcher._process_batch(sub.subscription_id, batch)
        assert restarted.delivery_status(event.event_id, sub.subscription_id) == "dispatched"
        # The attempt spent before the crash is still counted.
        assert restarted.delivery_attempts(event.event_id, sub.subscription_id) == 2
    finally:
        await dispatcher.stop()


@pytest.mark.asyncio
async def test_retry_loop_requeues_on_its_own_interval(monkeypatch):
    """End-to-end: the periodic sweep, not a manual call, drives the retry."""
    import asyncio
    _allow_hosts(monkeypatch, ["a.example.com"])
    endpoint = _FlakyEndpoint(failures=1)

    store = MemorySubscriptionStore()
    sub = store.create(Subscription("", "https://a.example.com/hook"))
    outbox = MemoryOutbox()
    client = httpx.AsyncClient(transport=httpx.MockTransport(endpoint))
    dispatcher = _dispatcher(store, outbox, client, retry_interval=0.05)
    dispatcher.add_subscription(sub)

    event = outbox.append(_event())
    dispatcher.submit(event)
    dispatcher._flush_buffers()
    await dispatcher._process_batch(sub.subscription_id,
                                    dispatcher._workers[sub.subscription_id].queue.get_nowait())
    assert outbox.delivery_status(event.event_id, sub.subscription_id) == "delivery_failed"

    dispatcher._retry_task = asyncio.get_running_loop().create_task(dispatcher._retry_loop())
    try:
        for _ in range(200):
            if outbox.delivery_status(event.event_id, sub.subscription_id) == "dispatched":
                break
            await asyncio.sleep(0.01)
    finally:
        await dispatcher.stop()

    assert outbox.delivery_status(event.event_id, sub.subscription_id) == "dispatched"
    assert outbox.delivery_attempts(event.event_id, sub.subscription_id) == 2
