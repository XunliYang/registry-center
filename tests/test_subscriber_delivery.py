# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Per-subscriber delivery state in the outbox (the R3 finding).

The outbox used to keep one `status` per *event* and the dispatcher wrote it once
per batch. With two subscriptions that meant the first to finish decided that the
other was done: a slow subscriber's event was marked `dispatched` (so recovery
skipped it and cleanup could prune it) and a fast subscriber's success could be
overwritten by another's failure. Each (subscription, event) pair now has its own
row, and the event-level status is only the fold of those rows.

The same contract is asserted for all three persistence modes; SQLite stands in
for the SQL dialects (PostgreSQL/GaussDB/MySQL share this code path, only the
connection and query text differ and stay unverified here).
"""

import json

import httpx
import pytest

from agent_registry.broadcast.events import EventType, build_event
from agent_registry.broadcast.dispatcher import WebhookDispatcher
from agent_registry.broadcast.outbox import FileOutbox, MemoryOutbox, SqlOutbox
from agent_registry.broadcast.subscriptions import MemorySubscriptionStore, Subscription
from agent_registry.persistence.sqlite_storage import SQLiteStorage


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

def test_two_subscribers_are_tracked_independently(outbox):
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a", "sub-b"])

    assert outbox.mark_delivery("sub-a", event.event_id, "dispatched") is True

    assert outbox.delivery_status(event.event_id, "sub-a") == "dispatched"
    assert outbox.delivery_status(event.event_id, "sub-b") == "pending"
    # The slow subscriber still owns the event...
    assert [e.event_id for e in outbox.list_pending_for("sub-b")] == [event.event_id]
    assert outbox.list_pending_for("sub-a") == []
    # ...and the event is not considered done just because one subscriber is.
    assert [e.event_id for e in outbox.list_pending()] == [event.event_id]


def test_event_is_terminal_only_when_every_subscriber_is_done(outbox):
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a", "sub-b"])

    outbox.mark_delivery("sub-a", event.event_id, "dispatched")
    outbox.mark_delivery("sub-b", event.event_id, "delivery_failed")

    assert outbox.list_pending() == []
    assert outbox.list_pending_for("sub-a") == []
    assert outbox.list_pending_for("sub-b") == []
    # A failure anywhere keeps the event visible as failed for /changes consumers.
    assert outbox.list_after(0, 10)[0].status == "delivery_failed"


def test_ensure_deliveries_is_idempotent(outbox):
    event = outbox.append(_event())

    outbox.ensure_deliveries(event, ["sub-a"])
    outbox.ensure_deliveries(event, ["sub-a"])

    assert outbox.delivery_status(event.event_id, "sub-a") == "pending"
    assert len(outbox.list_pending_for("sub-a")) == 1


def test_marking_an_unknown_pair_falls_back_to_the_event_status(outbox):
    """Callers that drive the outbox without subscriptions keep old behaviour."""
    event = outbox.append(_event())

    assert outbox.mark_delivery("sub-a", event.event_id, "dispatched") is True

    assert outbox.delivery_status(event.event_id, "sub-a") is None
    assert outbox.list_pending() == []


def test_event_that_matches_no_subscriber_does_not_stay_pending(outbox):
    """A skipped-only event is terminal, otherwise it would never be pruned."""
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, [], ["sub-a"])

    assert outbox.delivery_status(event.event_id, "sub-a") == "skipped"
    assert outbox.list_pending() == []
    assert outbox.list_after(0, 10)[0].status == "abandoned"


def test_cleanup_keeps_events_a_subscriber_still_owes(outbox):
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a", "sub-b"])
    outbox.mark_delivery("sub-a", event.event_id, "dispatched")

    assert outbox.cleanup(-1, active_subscription_ids=["sub-a", "sub-b"]) == 0
    assert [e.event_id for e in outbox.list_pending()] == [event.event_id]

    outbox.mark_delivery("sub-b", event.event_id, "dispatched")
    assert outbox.cleanup(-1, active_subscription_ids=["sub-a", "sub-b"]) == 1
    assert outbox.list_after(0, 10) == []


def test_cleanup_prunes_deliveries_of_vanished_subscriptions(outbox):
    """A subscription deleted while the process was down must not pin events."""
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a", "sub-gone"])
    outbox.mark_delivery("sub-a", event.event_id, "dispatched")

    assert outbox.cleanup(-1, active_subscription_ids=["sub-a"]) == 1
    assert outbox.list_after(0, 10) == []
    assert outbox.delivery_status(event.event_id, "sub-gone") is None


def test_dropping_a_subscription_releases_its_events(outbox):
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a", "sub-b"])
    outbox.mark_delivery("sub-a", event.event_id, "dispatched")

    assert outbox.drop_deliveries("sub-b") == 1

    assert outbox.delivery_status(event.event_id, "sub-b") is None
    assert outbox.list_pending() == []
    assert outbox.list_after(0, 10)[0].status == "dispatched"


def test_file_mode_keeps_the_ledger_across_a_restart(tmp_path):
    path = str(tmp_path / "events.jsonl")
    outbox = FileOutbox(path)
    event = outbox.append(_event())
    outbox.ensure_deliveries(event, ["sub-a", "sub-b"])
    outbox.mark_delivery("sub-a", event.event_id, "dispatched")

    restarted = FileOutbox(path)

    assert restarted.delivery_status(event.event_id, "sub-a") == "dispatched"
    assert restarted.delivery_status(event.event_id, "sub-b") == "pending"
    assert [e.event_id for e in restarted.list_pending_for("sub-b")] == [event.event_id]
    # The next line must be valid JSONL, not a partially written record.
    with open(path + ".deliveries", "r", encoding="utf-8") as f:
        for line in f:
            json.loads(line)


def test_sql_mode_persists_the_ledger_across_a_restart(tmp_path):
    path = str(tmp_path / "registry.db")
    storage = SQLiteStorage.init({"sqlite.path": path})
    try:
        outbox = SqlOutbox(storage)
        event = outbox.append(_event())
        outbox.ensure_deliveries(event, ["sub-a", "sub-b"])
        outbox.mark_delivery("sub-a", event.event_id, "dispatched")
        outbox.mark_delivery("sub-b", event.event_id, "delivery_failed")
    finally:
        storage.close()

    storage2 = SQLiteStorage.init({"sqlite.path": path})
    try:
        reopened = SqlOutbox(storage2)
        assert reopened.delivery_status(event.event_id, "sub-a") == "dispatched"
        assert reopened.delivery_status(event.event_id, "sub-b") == "delivery_failed"
        assert reopened.list_pending() == []
    finally:
        storage2.close()


# ---------- dispatcher ----------

def _allow_hosts(monkeypatch, hosts):
    monkeypatch.setattr('agent_registry.broadcast.callback_policy.get_conf',
                        lambda: {'broadcast.callback.allowlist': ",".join(hosts)})


@pytest.mark.asyncio
async def test_slow_subscriber_survives_a_fast_subscriber(monkeypatch):
    _allow_hosts(monkeypatch, ["a.example.com", "b.example.com"])

    def handler(request: httpx.Request) -> httpx.Response:
        # `b` is down, `a` is healthy: only their own delivery state may change.
        return httpx.Response(200 if request.url.host == "a.example.com" else 500)

    store = MemorySubscriptionStore()
    fast = store.create(Subscription("", "https://a.example.com/hook"))
    slow = store.create(Subscription("", "https://b.example.com/hook"))
    outbox = MemoryOutbox()
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    dispatcher = WebhookDispatcher(store, outbox, client=client, debounce_window=0.01,
                                   max_events_per_second=1000.0, max_retries=0,
                                   backoff_base=0.01, backoff_max=0.02, webhook_timeout=1.0)
    dispatcher.add_subscription(fast)
    dispatcher.add_subscription(slow)

    event = outbox.append(build_event(EventType.AGENT_REGISTERED,
                                      {"name": "a1", "organization": "org1"}, 0))
    dispatcher.submit(event)
    assert outbox.delivery_status(event.event_id, fast.subscription_id) == "pending"

    dispatcher._flush_buffers()
    await dispatcher._process_batch(fast.subscription_id,
                                    dispatcher._workers[fast.subscription_id].queue.get_nowait())
    await dispatcher._process_batch(slow.subscription_id,
                                    dispatcher._workers[slow.subscription_id].queue.get_nowait())

    assert outbox.delivery_status(event.event_id, fast.subscription_id) == "dispatched"
    assert outbox.delivery_status(event.event_id, slow.subscription_id) == "delivery_failed"
    assert outbox.list_pending() == []
    await dispatcher.stop()


@pytest.mark.asyncio
async def test_recover_only_replays_what_each_subscriber_still_owes(monkeypatch):
    _allow_hosts(monkeypatch, ["a.example.com", "b.example.com"])

    store = MemorySubscriptionStore()
    fast = store.create(Subscription("", "https://a.example.com/hook"))
    slow = store.create(Subscription("", "https://b.example.com/hook"))
    outbox = MemoryOutbox()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    dispatcher = WebhookDispatcher(store, outbox, client=client, debounce_window=0.01,
                                   max_events_per_second=1000.0)
    dispatcher.add_subscription(fast)
    dispatcher.add_subscription(slow)

    event = outbox.append(build_event(EventType.AGENT_REGISTERED,
                                      {"name": "a1", "organization": "org1"}, 0))
    dispatcher.submit(event)
    outbox.mark_delivery(fast.subscription_id, event.event_id, "dispatched")

    dispatcher.recover()

    assert dispatcher._workers[fast.subscription_id].queue.empty()
    assert [e.event_id for e in
            dispatcher._workers[slow.subscription_id].queue.get_nowait()] == [event.event_id]
    await dispatcher.stop()


@pytest.mark.asyncio
async def test_removing_a_subscription_drops_its_pending_deliveries(monkeypatch):
    _allow_hosts(monkeypatch, ["a.example.com"])

    store = MemorySubscriptionStore()
    sub = store.create(Subscription("", "https://a.example.com/hook"))
    outbox = MemoryOutbox()
    client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
    dispatcher = WebhookDispatcher(store, outbox, client=client, debounce_window=0.01)
    dispatcher.add_subscription(sub)
    event = outbox.append(build_event(EventType.AGENT_REGISTERED,
                                      {"name": "a1", "organization": "org1"}, 0))
    dispatcher.submit(event)

    dispatcher.remove_subscription(sub.subscription_id)

    assert outbox.delivery_status(event.event_id, sub.subscription_id) is None
    assert outbox.list_pending() == []
    await dispatcher.stop()
