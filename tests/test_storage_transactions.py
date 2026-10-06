# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit-of-work tests for the SQL storage backend.

Before this, `RegistryCore` committed an AgentCard change and its outbox event
in two independent transactions, so a crash (or a failing event append) could
leave a committed record whose change was never announced to consumers. These
tests pin the guarantee: inside `storage.transaction()` the record and the event
share one commit, and any failure rolls both back.

SQLite is used because it is the only SQL dialect available without a live
server; PostgreSQL/Gauss/MySQL share this exact code path (only connections and
query text differ) but stay unverified in this environment.
"""

import threading
import time

import pytest

from a2a.types import AgentCard

from agent_registry.broadcast.event_bus import EventBus
from agent_registry.broadcast.events import EventType, build_event
from agent_registry.broadcast.outbox import SqlOutbox
from agent_registry.core import RegistryCore
from agent_registry.persistence.file_storage import FileStorage
from agent_registry.persistence.sql_backend import SqlStorageBackend
from agent_registry.persistence.sqlite_storage import SQLiteStorage


def _card(name="a1", organization="org1"):
    return AgentCard(
        name=name,
        description=f"{name} description",
        version="1.0.0",
        provider={"organization": organization, "url": "https://test.com"},
        skills=[],
    )


def _event(name="a1", organization="org1"):
    # Version is assigned by the outbox on append; the envelope only needs data.
    return build_event(EventType.AGENT_REGISTERED,
                       {"name": name, "organization": organization}, 0)


@pytest.fixture
def sqlite_storage(tmp_path):
    storage = SQLiteStorage.init({"sqlite.path": str(tmp_path / "registry.db")})
    yield storage
    storage.close()


@pytest.fixture
def outbox(sqlite_storage):
    return SqlOutbox(sqlite_storage)


# ---------- the unit-of-work primitive ----------

def test_record_and_event_commit_together(sqlite_storage, outbox):
    with sqlite_storage.transaction():
        assert sqlite_storage.create(_card()) is True
        outbox.append(_event())
        # reads inside the unit of work see the uncommitted record
        assert sqlite_storage.find_by_key("a1", "org1") is not None

    assert sqlite_storage.find_by_key("a1", "org1") is not None
    assert outbox.max_version() == 1


def test_failure_rolls_back_both_record_and_event(sqlite_storage, outbox):
    with pytest.raises(RuntimeError):
        with sqlite_storage.transaction():
            sqlite_storage.create(_card())
            outbox.append(_event())
            raise RuntimeError("crash before commit")

    assert sqlite_storage.find_by_key("a1", "org1") is None
    assert outbox.max_version() == 0


def test_nested_transaction_joins_the_outer_unit(sqlite_storage, outbox):
    """An inner transaction must not commit what the outer one rolls back."""
    with pytest.raises(RuntimeError):
        with sqlite_storage.transaction():
            sqlite_storage.create(_card())
            with sqlite_storage.transaction():
                outbox.append(_event())
            raise RuntimeError("outer fails after inner block")

    assert sqlite_storage.find_by_key("a1", "org1") is None
    assert outbox.max_version() == 0


def test_no_unit_of_work_leaks_between_calls(sqlite_storage, outbox):
    with sqlite_storage.transaction():
        sqlite_storage.create(_card())
        outbox.append(_event())

    # A later standalone write must commit on its own again.
    assert sqlite_storage.create(_card(name="a2")) is True
    assert sqlite_storage.find_by_key("a2", "org1") is not None


# ---------- RegistryCore writes adopt the unit of work ----------

def _sqlite_registry(tmp_path):
    return RegistryCore(use_vectordb=False, persistence_mode='sqlite',
                        persistence_conf={"sqlite.path": str(tmp_path / "registry.db")})


class _FailingOutbox(SqlOutbox):
    """A real outbox bound to the same storage whose INSERT always fails."""

    def append(self, event):
        raise RuntimeError("outbox down")


def test_status_change_rolls_back_when_event_append_fails(tmp_path, monkeypatch):
    """The authoritative record must not survive an event that never got persisted."""
    registry = _sqlite_registry(tmp_path)
    try:
        monkeypatch.setattr("agent_registry.core.get_event_bus",
                            lambda: EventBus(SqlOutbox(registry.storage)))
        assert registry.register_with_status(_card(), initial_status='published') is True
        monkeypatch.setattr("agent_registry.core.get_event_bus",
                            lambda: EventBus(_FailingOutbox(registry.storage)))

        with pytest.raises(RuntimeError, match="outbox down"):
            registry.update_status("a1", "org1", 'registered')

        assert registry.get_status("a1", "org1") == 'published'
    finally:
        registry.close()


def test_outer_rollback_suppresses_notification(tmp_path, monkeypatch):
    """A nested _atomic_write must not notify while the outer unit can still fail."""
    registry = _sqlite_registry(tmp_path)
    try:
        bus = _RecordingBus(SqlOutbox(registry.storage))
        monkeypatch.setattr("agent_registry.core.get_event_bus", lambda: bus)

        with pytest.raises(RuntimeError, match="outer rolls back"):
            with registry.storage.transaction():
                assert registry.register_with_status(_card(), initial_status='published') is True
                assert bus.seen == []  # the outer unit may still roll back
                raise RuntimeError("outer rolls back")

        assert bus.seen == []
        assert bus._outbox.max_version() == 0
        assert registry.storage.find_by_key("a1", "org1") is None
    finally:
        registry.close()


def test_nested_commit_restores_event_state_for_next_write(tmp_path, monkeypatch):
    registry = _sqlite_registry(tmp_path)
    try:
        bus = _RecordingBus(SqlOutbox(registry.storage))
        monkeypatch.setattr("agent_registry.core.get_event_bus", lambda: bus)

        with registry.storage.transaction():
            assert registry.register_with_status(_card(), initial_status='published')
            assert bus.seen == []
        assert len(bus.seen) == 1
        assert registry._deferred_events is None
        assert registry._event_must_succeed is False

        assert registry.register_with_status(_card(name="a2"), initial_status='published')
        assert len(bus.seen) == 2
        assert bus._outbox.max_version() == 2
    finally:
        registry.close()


def test_nested_event_failure_rolls_back_and_restores_state(tmp_path, monkeypatch):
    registry = _sqlite_registry(tmp_path)
    try:
        good_bus = _RecordingBus(SqlOutbox(registry.storage))
        monkeypatch.setattr("agent_registry.core.get_event_bus", lambda: good_bus)
        assert registry.register_with_status(_card(), initial_status='published')
        assert good_bus._outbox.max_version() == 1

        failing_bus = EventBus(_FailingOutbox(registry.storage))
        monkeypatch.setattr("agent_registry.core.get_event_bus", lambda: failing_bus)
        with pytest.raises(RuntimeError, match="outbox down"):
            with registry.storage.transaction():
                registry.update_status("a1", "org1", 'registered')

        assert registry.get_status("a1", "org1") == 'published'
        assert good_bus._outbox.max_version() == 1
        assert len(good_bus.seen) == 1
        assert registry._deferred_events is None
        assert registry._event_must_succeed is False
    finally:
        registry.close()


def test_caught_nested_event_failure_still_rolls_back_outer_unit(tmp_path, monkeypatch):
    registry = _sqlite_registry(tmp_path)
    try:
        good_bus = _RecordingBus(SqlOutbox(registry.storage))
        monkeypatch.setattr("agent_registry.core.get_event_bus", lambda: good_bus)
        assert registry.register_with_status(_card(), initial_status='published')

        failing_bus = EventBus(_FailingOutbox(registry.storage))
        monkeypatch.setattr("agent_registry.core.get_event_bus", lambda: failing_bus)
        with pytest.raises(RuntimeError, match="rollback-only"):
            with registry.storage.transaction():
                with pytest.raises(RuntimeError, match="outbox down"):
                    registry.update_status("a1", "org1", 'registered')

        assert registry.get_status("a1", "org1") == 'published'
        assert good_bus._outbox.max_version() == 1
        assert len(good_bus.seen) == 1
        assert registry._deferred_events is None
        assert registry._event_must_succeed is False
    finally:
        registry.close()


def test_nested_deregister_only_cleans_health_after_outer_commit(tmp_path, monkeypatch):
    registry = _sqlite_registry(tmp_path)
    removed = []
    try:
        bus = _RecordingBus(SqlOutbox(registry.storage))
        monkeypatch.setattr("agent_registry.core.get_event_bus", lambda: bus)
        monkeypatch.setattr(registry, "_remove_health_state",
                            lambda name, org: removed.append((name, org)))
        assert registry.register_with_status(_card(), initial_status='published')

        with pytest.raises(RuntimeError, match="outer rollback"):
            with registry.storage.transaction():
                assert registry.deregister("a1", "org1")
                assert removed == []
                raise RuntimeError("outer rollback")
        assert removed == []
        assert registry.storage.find_by_key("a1", "org1") is not None

        with registry.storage.transaction():
            assert registry.deregister("a1", "org1")
            assert removed == []
        assert removed == [("a1", "org1")]
    finally:
        registry.close()


def test_deregister_rollback_keeps_health_state(tmp_path, monkeypatch):
    """Health state is an external side effect: only drop it after the commit."""
    registry = _sqlite_registry(tmp_path)
    removed = []
    try:
        monkeypatch.setattr("agent_registry.core.get_event_bus",
                            lambda: EventBus(SqlOutbox(registry.storage)))
        assert registry.register_with_status(_card(), initial_status='published') is True
        monkeypatch.setattr("agent_registry.core.get_event_bus",
                            lambda: EventBus(_FailingOutbox(registry.storage)))
        monkeypatch.setattr(registry, "_remove_health_state",
                            lambda name, org: removed.append((name, org)))

        with pytest.raises(RuntimeError, match="outbox down"):
            registry.deregister("a1", "org1")

        assert removed == []
        assert registry.storage.find_by_key("a1", "org1") is not None
    finally:
        registry.close()


def test_unbound_outbox_rejects_sql_write(tmp_path, monkeypatch):
    """A miswired SQL outbox must fail before the authoritative write."""
    from agent_registry.broadcast.outbox import FileOutbox

    registry = _sqlite_registry(tmp_path)
    try:
        bus = EventBus(FileOutbox(str(tmp_path / "events.jsonl")))
        monkeypatch.setattr("agent_registry.core.get_event_bus", lambda: bus)

        with pytest.raises(RuntimeError, match="Event outbox is not bound"):
            registry.register_with_status(_card(), initial_status='published')
        assert registry.storage.find_by_key("a1", "org1") is None
    finally:
        registry.close()


def test_preinitialized_broadcast_service_rejects_sql_startup(tmp_path, monkeypatch):
    import agent_registry.broadcast as broadcast
    from agent_registry.broadcast.outbox import MemoryOutbox
    from agent_registry.broadcast.subscriptions import MemorySubscriptionStore

    registry = _sqlite_registry(tmp_path)
    try:
        early = broadcast.BroadcastService(MemoryOutbox(), MemorySubscriptionStore(),
                                           config={"broadcast.enabled": "false"})
        monkeypatch.setattr(broadcast, "_service", early)
        with pytest.raises(RuntimeError, match="not bound"):
            broadcast.initialize_broadcast_service(registry.storage, "sqlite")
        assert broadcast._service is early
    finally:
        registry.close()


def test_status_change_without_event_failure_is_committed(tmp_path, monkeypatch):
    registry = _sqlite_registry(tmp_path)
    try:
        monkeypatch.setattr("agent_registry.core.get_event_bus",
                            lambda: EventBus(SqlOutbox(registry.storage)))
        assert registry.register_with_status(_card(), initial_status='published') is True
        assert registry.update_status("a1", "org1", 'registered') is True
        assert registry.get_status("a1", "org1") == 'registered'
    finally:
        registry.close()


# ---------- non-transactional backends stay explicitly best-effort ----------

def test_file_backend_declares_no_transaction_support(tmp_path):
    storage = FileStorage.init({"file.path": str(tmp_path / "agentcard.json")})
    try:
        assert storage.supports_transactions is False
    finally:
        storage.close()


def test_atomic_write_is_a_noop_on_file_backend(tmp_path, monkeypatch):
    """File mode keeps today's best-effort semantics instead of pretending."""
    monkeypatch.setattr("agent_registry.core.get_root_path", lambda: str(tmp_path))
    registry = RegistryCore(use_vectordb=False, persistence_mode='file', persistence_conf={})
    try:
        assert registry.storage is not None
        assert registry.storage.supports_transactions is False
        with registry._atomic_write() as atomic:
            assert atomic is False
    finally:
        registry.close()


# ---------- dialect semantics: the unit of work must actually exist ----------

def test_mysql_opens_an_explicit_transaction():
    """PooledDB runs with autocommit=True, so the UoW must BEGIN explicitly."""
    from agent_registry.persistence.mysql_storage import MySQLStorage

    assert MySQLStorage._begin_transaction is not SqlStorageBackend._begin_transaction

    calls = []

    class _Conn:
        def begin(self):
            calls.append("begin")

    MySQLStorage._begin_transaction(object.__new__(MySQLStorage), _Conn())
    assert calls == ["begin"]


def test_outbox_version_column_is_unique(sqlite_storage, outbox):
    """Two instances must not be able to record the same event version."""
    outbox.append(_event(name="a1"))
    with pytest.raises(Exception):
        sqlite_storage._execute_write(
            "INSERT INTO registry_events (event_id, registry_version, event_type, payload, "
            "status, retry_count, created_at, dispatched_at) "
            "VALUES ('duplicate', 1, 'agent.registered', '{}', 'pending', 0, 'now', NULL)"
        )


def test_sqlite_outbox_uses_connection_lock_without_a_second_process_lock(sqlite_storage, outbox):
    """The connection lock serializes the complete SQLite unit of work."""
    acquired = []

    class _TracingLock:
        def __init__(self, name, lock):
            self.name = name
            self.lock = lock

        def __enter__(self):
            self.lock.acquire()
            acquired.append(self.name)
            return self

        def __exit__(self, *_):
            self.lock.release()

    sqlite_storage._conn_lock_obj = _TracingLock("connection", threading.RLock())
    outbox._lock = _TracingLock("outbox", threading.Lock())

    outbox.append(_event())

    assert acquired and acquired[0] == 'connection'
    assert 'outbox' not in acquired
    assert outbox.max_version() == 1


def test_shared_connection_writes_are_serialized(sqlite_storage, outbox):
    """Another thread must not commit a unit of work that is still in flight."""
    order = []
    in_tx = threading.Event()

    def hold_transaction():
        with sqlite_storage.transaction():
            sqlite_storage.create(_card(name="a1"))
            in_tx.set()
            time.sleep(0.3)
            outbox.append(_event(name="a1"))
            order.append("tx-end")

    writer = threading.Thread(target=hold_transaction)
    writer.start()
    assert in_tx.wait(5)

    sqlite_storage.create(_card(name="a2"))  # must block until the unit commits
    order.append("standalone")
    writer.join(5)

    assert order == ["tx-end", "standalone"]
    assert outbox.max_version() == 1


# ---------- consumers must only see committed changes ----------

class _RecordingBus(EventBus):
    def __init__(self, outbox):
        super().__init__(outbox)
        self.seen = []
        self.add_listener(self.seen.append)


def test_real_outbox_is_committed_with_the_record(tmp_path, monkeypatch):
    """The event must land in the SQL outbox through the real publish path."""
    registry = _sqlite_registry(tmp_path)
    try:
        bus = _RecordingBus(SqlOutbox(registry.storage))
        monkeypatch.setattr("agent_registry.core.get_event_bus", lambda: bus)

        assert registry.register_with_status(_card(), initial_status='published') is True

        assert registry.storage.find_by_key("a1", "org1") is not None
        assert bus._outbox.max_version() == 1
        assert [e.event_type for e in bus.seen] == [EventType.AGENT_REGISTERED]
    finally:
        registry.close()


def test_rolled_back_unit_never_notifies_consumers(tmp_path, monkeypatch):
    """A change that never committed must not reach queue/SSE listeners."""
    registry = _sqlite_registry(tmp_path)
    try:
        bus = _RecordingBus(SqlOutbox(registry.storage))
        monkeypatch.setattr("agent_registry.core.get_event_bus", lambda: bus)

        with pytest.raises(RuntimeError, match="rolled back after the event"):
            with registry._atomic_write():
                registry.storage.create(_card())
                registry._publish_event(EventType.AGENT_REGISTERED, "a1", "org1")
                raise RuntimeError("rolled back after the event")

        assert bus.seen == []
        assert bus._outbox.max_version() == 0
        assert registry.storage.find_by_key("a1", "org1") is None
    finally:
        registry.close()


def test_deregister_notifies_consumers_after_health_cleanup(tmp_path, monkeypatch):
    """Consumers must be woken only after internal side effects have run.

    The deregister mutation registers a health-state cleanup as a commit
    hook; the notification hook is registered after the mutation body, so a
    subscriber of AGENT_DEREGISTERED observes the event only once the health
    record is already gone."""
    registry = _sqlite_registry(tmp_path)
    try:
        bus = _RecordingBus(SqlOutbox(registry.storage))
        monkeypatch.setattr("agent_registry.core.get_event_bus", lambda: bus)
        order = []
        monkeypatch.setattr(registry, "_remove_health_state",
                            lambda name, org: order.append("health-cleanup"))
        bus.add_listener(lambda event: order.append("notify"))

        assert registry.register_with_status(_card(), initial_status='published') is True
        order.clear()
        assert registry.deregister("a1", "org1") is True

        assert order == ["health-cleanup", "notify"]
    finally:
        registry.close()
