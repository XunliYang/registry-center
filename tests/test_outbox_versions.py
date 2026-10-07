# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Commit-ordered `registry_version` allocation for the SQL outbox.

Before this, `SqlOutbox.append()` computed `MAX(registry_version) + 1` under a
process-local lock. Two service instances could race on it, and even without a
collision the number was *not* a commit order: instance A could take 5, instance
B take 6, B commit first, and a consumer following `list_after(version)` would
then never see 5. Versions are now allocated from a counter row updated in the
same unit of work as the event insert, so the counter's row lock is held until
commit and version order equals commit order.

SQLite exercises the allocation code end to end (it is the only dialect runnable
without a live server). PostgreSQL/GaussDB/MySQL share that code path but only
differ in the statements issued, which the fake-backend tests below pin down;
the real-DB commit-order guarantee stays unverified in this environment and is
listed as such in the review response.
"""

import threading
import time
from contextlib import contextmanager

import pytest

from agent_registry.broadcast.events import EventType, build_event
from agent_registry.broadcast.outbox import (
    REGISTRY_VERSION_COUNTER, SqlOutbox,
)
from agent_registry.persistence.sqlite_storage import SQLiteStorage


def _event(name="a1", organization="org1"):
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


# ---------- allocation behaviour (SQLite end to end) ----------

def test_versions_are_monotonic_and_unique(outbox):
    versions = [outbox.append(_event(name=f"a{i}")).registry_version for i in range(25)]

    assert versions == list(range(1, 26))


def test_counter_seeds_from_existing_events(sqlite_storage, outbox):
    """An upgrade from a deployment without the counter must not reuse versions."""
    outbox.append(_event())
    outbox.append(_event())
    sqlite_storage._execute_write("DELETE FROM registry_event_counter")

    rebuilt = SqlOutbox(sqlite_storage)

    assert rebuilt.append(_event()).registry_version == 3


def test_rolled_back_append_does_not_consume_a_version(sqlite_storage, outbox):
    with pytest.raises(RuntimeError):
        with sqlite_storage.transaction():
            outbox.append(_event())
            raise RuntimeError("authoritative write failed")

    assert outbox.max_version() == 0
    assert outbox.list_pending() == []
    # The version is reusable: consumers key on event_id, not on the version.
    assert outbox.append(_event()).registry_version == 1


def test_concurrent_appends_stay_unique(outbox):
    versions = []
    lock = threading.Lock()

    def worker(index):
        event = outbox.append(_event(name=f"a{index}"))
        with lock:
            versions.append(event.registry_version)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(40)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert sorted(versions) == list(range(1, 41))


def test_missing_counter_row_fails_loudly(sqlite_storage, outbox):
    """Without the counter there is no commit-order guarantee, so refuse."""
    sqlite_storage._execute_write("DELETE FROM registry_event_counter")

    with pytest.raises(RuntimeError, match="commit-order guarantee"):
        outbox.append(_event())


def test_append_joins_an_outer_unit_of_work(sqlite_storage, outbox):
    """The authoritative-write path wraps append; the version must join its commit."""
    with pytest.raises(RuntimeError):
        with sqlite_storage.transaction():
            event = outbox.append(_event())
            assert event.registry_version == 1
            raise RuntimeError("record write failed")

    assert outbox.max_version() == 0
    assert outbox.append(_event()).registry_version == 1


# ---------- statements issued per dialect (fake backend) ----------

class _RecordingBackend:
    """Minimal SqlStorageBackend stand-in that records the SQL it is given."""

    def __init__(self, dialect, counter_row=(41,), ph="%s"):
        self.dialect = dialect
        self.param_ph = ph
        self.queries = []
        self._counter_row = counter_row

    @contextmanager
    def _serialize(self):
        yield

    @contextmanager
    def transaction(self):
        yield

    def ensure_index(self, ddl_if_not_exists, ddl_plain):
        self.queries.append(ddl_if_not_exists)

    def _execute_write(self, query, params=None):
        self.queries.append(" ".join(query.split()))
        return 1

    def _execute_read_one(self, query, params=None):
        query = " ".join(query.split())
        self.queries.append(query)
        if query.startswith("SELECT version FROM registry_event_counter"):
            return self._counter_row
        if query == "SELECT LAST_INSERT_ID()":
            return self._counter_row
        if query.startswith("SELECT version FROM"):
            return None
        return None

    def sql(self):
        """Counter statements only: the CREATE TABLE issued during init is not one."""
        return [q for q in self.queries
                if "registry_event_counter" in q or "LAST_INSERT_ID" in q
                if not q.startswith("CREATE")]


def test_postgresql_allocates_with_a_locked_counter_row():
    backend = _RecordingBackend("postgresql")

    event = SqlOutbox(backend).append(_event())

    assert event.registry_version == 41
    assert backend.sql() == [
        "SELECT version FROM registry_event_counter WHERE counter_name = %s",
        "UPDATE registry_event_counter SET version = version + 1 WHERE counter_name = %s",
        "SELECT version FROM registry_event_counter WHERE counter_name = %s",
    ]


def test_mysql_allocates_with_last_insert_id():
    """MySQL has no UPDATE ... RETURNING, so it needs the session-local variable."""
    backend = _RecordingBackend("mysql")

    event = SqlOutbox(backend).append(_event())

    assert event.registry_version == 41
    assert backend.sql() == [
        "SELECT version FROM registry_event_counter WHERE counter_name = %s",
        "UPDATE registry_event_counter SET version = LAST_INSERT_ID(version + 1) WHERE counter_name = %s",
        "SELECT LAST_INSERT_ID()",
    ]


def test_sqlite_placeholder_is_used_in_counter_statements(sqlite_storage):
    """The counter SQL must go through the dialect placeholder (%s vs ?)."""
    outbox = SqlOutbox(sqlite_storage)
    row = sqlite_storage._execute_read_one(
        "SELECT version FROM registry_event_counter WHERE counter_name = ?",
        (REGISTRY_VERSION_COUNTER,))

    assert row == (0,)
    assert outbox.append(_event()).registry_version == 1


# ---------- real-database guarantees (skipped when the DB is unavailable) ----------

def _assert_commit_order(storage):
    """A second writer cannot take a version while the first is uncommitted.

    Two outbox instances are required on purpose: `SqlOutbox` has a process-local
    lock, so reusing one instance would prove nothing about the database. These
    backends do not share a single connection either, so the only thing that can
    hold writer B back is the row lock on `registry_event_counter` — which is
    exactly the guarantee `list_after(version)` consumers depend on.
    """
    first = SqlOutbox(storage)
    second = SqlOutbox(storage)
    first.append(_event(name="seed"))

    versions = {}
    first_open = threading.Event()
    release_first = threading.Event()
    second_started = threading.Event()

    def writer(outbox, key, opened=None, wait_for=None):
        if opened is None:
            versions[key] = outbox.append(_event(name=key)).registry_version
            return
        with storage.transaction():
            versions[key] = outbox.append(_event(name=key)).registry_version
            opened.set()
            wait_for.wait(15)

    thread_a = threading.Thread(target=writer, args=(first, "a", first_open, release_first))
    thread_a.start()
    assert first_open.wait(15), "the first writer never opened its transaction"

    def writer_b():
        second_started.set()
        versions["b"] = second.append(_event(name="b")).registry_version

    thread_b = threading.Thread(target=writer_b)
    thread_b.start()
    assert second_started.wait(15)
    time.sleep(1.0)
    assert "b" not in versions, "a second writer took a version before the first committed"

    release_first.set()
    thread_a.join(15)
    thread_b.join(15)

    assert not thread_a.is_alive() and not thread_b.is_alive()
    assert versions["b"] == versions["a"] + 1


def _assert_unique_and_contiguous(storage, writers=4, per_writer=10):
    outboxes = [SqlOutbox(storage) for _ in range(writers)]
    versions = []
    lock = threading.Lock()

    def worker(outbox, index):
        batch = [outbox.append(_event(name=f"w{index}-{i}")).registry_version
                 for i in range(per_writer)]
        with lock:
            versions.extend(batch)

    threads = [threading.Thread(target=worker, args=(outbox, i))
               for i, outbox in enumerate(outboxes)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(60)

    assert not any(thread.is_alive() for thread in threads)
    assert len(versions) == len(set(versions)) == writers * per_writer
    assert sorted(versions) == list(range(min(versions), min(versions) + len(versions)))


def test_postgresql_version_allocation_is_commit_ordered(clean_pg_tables):
    _assert_commit_order(clean_pg_tables)


def test_postgresql_concurrent_versions_are_unique_and_contiguous(clean_pg_tables):
    _assert_unique_and_contiguous(clean_pg_tables)


def test_gaussdb_version_allocation_is_commit_ordered(clean_gauss_tables):
    _assert_commit_order(clean_gauss_tables)


def test_mysql_version_allocation_is_commit_ordered(clean_mysql_tables):
    _assert_commit_order(clean_mysql_tables)


def test_mysql_concurrent_versions_are_unique_and_contiguous(clean_mysql_tables):
    _assert_unique_and_contiguous(clean_mysql_tables)
