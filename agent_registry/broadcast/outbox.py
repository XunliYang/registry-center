# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""
Event outbox: events are persisted before dispatch so a registry restart
never loses broadcast data.

Event status lifecycle: pending -> dispatched | delivery_failed | degraded.
"degraded" marks events skipped because a subscriber hit its rate limit and
received a SYNC_REQUIRED summary instead; they remain queryable via /changes.
"""

import json
import os
import threading
from functools import wraps
from itertools import islice
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from loguru import logger

from agent_registry.broadcast.events import RegistryEvent, utc_now_iso


def _sql_delivery_transaction(method):
    """Commit delivery mutations and their event aggregate as one unit of work."""
    @wraps(method)
    def transactional(self, *args, **kwargs):
        with self._backend.transaction():
            return method(self, *args, **kwargs)
    return transactional

# Single-row table holding the highest allocated registry_version. It is updated
# in the same unit of work as the event insert so versions become visible in
# commit order (see SqlOutbox._allocate_version).
REGISTRY_VERSION_COUNTER = "registry_version"

# Per-subscriber delivery states. `pending` means "this subscriber still needs
# the event"; the others are terminal for that subscriber and only kept for audit
# and for `/changes` display.
DELIVERY_PENDING = "pending"
DELIVERY_DISPATCHED = "dispatched"
DELIVERY_DEGRADED = "degraded"
DELIVERY_FAILED = "delivery_failed"
# Recorded for a subscriber the event did not match, and for rows pruned together
# with a deleted event: it keeps "we have fanned this event out" observable, which
# is what separates "nobody owes it" from "not recorded yet".
DELIVERY_SKIPPED = "skipped"
DELIVERY_PRUNED = "pruned"
# Event-level state for an event no subscriber received (no match, or the only
# subscriber was removed). Terminal: recovery ignores it and cleanup may prune it.
DELIVERY_ABANDONED = "abandoned"
DELIVERY_TERMINAL_OK = (DELIVERY_DISPATCHED, DELIVERY_DEGRADED)


def aggregate_delivery_status(statuses: dict) -> Optional[str]:
    """Fold per-subscriber delivery states into the event-level status.

    The event-level status stays the contract of `/changes`, but it must no longer
    decide whether a *subscriber* still needs the event: an event is pending while
    at least one subscriber has a pending delivery, and only then may the outbox
    stop recovering (and eventually prune) it.
    """
    if not statuses:
        return None
    if any(status == DELIVERY_PENDING for status in statuses.values()):
        return DELIVERY_PENDING
    if any(status == DELIVERY_FAILED for status in statuses.values()):
        return DELIVERY_FAILED
    if any(status in DELIVERY_TERMINAL_OK for status in statuses.values()):
        return DELIVERY_DISPATCHED
    return DELIVERY_ABANDONED


class _DeliveryLedger:
    """In-process per-subscriber delivery state for the memory and file modes.

    SQL mode keeps this table in the database instead (see `SqlOutbox`); the
    aggregation rule is shared so all persistence modes agree on what "pending"
    means.
    """

    def __init__(self, persist=None):
        self._lock = threading.Lock()
        self._by_event: dict = {}
        self._attempts: dict = {}
        self._persist = persist

    def ensure(self, event_id: str, subscription_ids, status: str = DELIVERY_PENDING) -> None:
        with self._lock:
            subscriptions = self._by_event.setdefault(event_id, {})
            for subscription_id in subscription_ids:
                if subscription_id not in subscriptions:
                    subscriptions[subscription_id] = status
                    self._write(event_id, subscription_id, status)

    def load(self, entries) -> None:
        """Rebuild the ledger from persisted delivery entries.

        Entries are `(event_id, subscription_id, status)` plus an optional
        `attempts` (older files predate the counter, so it defaults to 0).
        """
        with self._lock:
            for entry in entries:
                event_id, subscription_id, status = entry[0], entry[1], entry[2]
                attempts = int(entry[3]) if len(entry) > 3 else 0
                self._by_event.setdefault(event_id, {})[subscription_id] = status
                if attempts:
                    self._attempts.setdefault(event_id, {})[subscription_id] = attempts

    def status(self, event_id: str, subscription_id: str) -> Optional[str]:
        with self._lock:
            return self._by_event.get(event_id, {}).get(subscription_id)

    def statuses(self, event_id: str) -> dict:
        with self._lock:
            return dict(self._by_event.get(event_id, {}))

    def attempts(self, event_id: str, subscription_id: str) -> int:
        with self._lock:
            return int(self._attempts.get(event_id, {}).get(subscription_id, 0))

    def mark(self, event_id: str, subscription_id: str, status: str) -> bool:
        with self._lock:
            subscriptions = self._by_event.get(event_id)
            if not subscriptions or subscription_id not in subscriptions:
                return False
            # A mark is the outcome of one delivery attempt (dispatched / failed /
            # degraded): the counter bounds the durable retries that follow.
            attempts = int(self._attempts.setdefault(event_id, {}).get(subscription_id, 0)) + 1
            self._attempts[event_id][subscription_id] = attempts
            subscriptions[subscription_id] = status
            self._write(event_id, subscription_id, status, attempts)
            return True

    def mark_all(self, event_id: str, status: str) -> None:
        with self._lock:
            subscriptions = self._by_event.get(event_id)
            if not subscriptions:
                return
            for subscription_id in list(subscriptions):
                subscriptions[subscription_id] = status
                self._write(event_id, subscription_id, status,
                            int(self._attempts.get(event_id, {}).get(subscription_id, 0)))

    def retryable_event_ids(self, subscription_id: str, max_attempts: int) -> List[str]:
        """Failed deliveries of this subscriber that still have retry budget."""
        with self._lock:
            retryable = []
            for event_id, subscriptions in self._by_event.items():
                if subscriptions.get(subscription_id) != DELIVERY_FAILED:
                    continue
                attempts = int(self._attempts.get(event_id, {}).get(subscription_id, 0))
                if attempts < max_attempts:
                    retryable.append(event_id)
            return retryable

    def reset(self, event_id: str, subscription_id: str,
              max_attempts: Optional[int] = None) -> bool:
        """Put a failed delivery back in the queue without losing its attempt count.

        `max_attempts` is the retry budget: a row that already spent it is left
        failed, so "requeue the failures" cannot turn into an endless loop.
        """
        with self._lock:
            subscriptions = self._by_event.get(event_id)
            if not subscriptions or subscription_id not in subscriptions:
                return False
            if subscriptions[subscription_id] != DELIVERY_FAILED:
                return False
            attempts = int(self._attempts.get(event_id, {}).get(subscription_id, 0))
            if max_attempts is not None and attempts >= int(max_attempts):
                return False
            subscriptions[subscription_id] = DELIVERY_PENDING
            self._write(event_id, subscription_id, DELIVERY_PENDING, attempts)
            return True

    def pending_event_ids(self, subscription_id: str) -> set:
        with self._lock:
            return {event_id for event_id, subscriptions in self._by_event.items()
                    if subscriptions.get(subscription_id) == DELIVERY_PENDING}

    def drop_subscription(self, subscription_id: str) -> List[str]:
        affected = []
        with self._lock:
            for event_id, subscriptions in self._by_event.items():
                if subscriptions.pop(subscription_id, None) is not None:
                    affected.append(event_id)
                self._attempts.get(event_id, {}).pop(subscription_id, None)
        return affected

    def known_subscriptions(self) -> set:
        with self._lock:
            return {subscription_id for subscriptions in self._by_event.values()
                    for subscription_id in subscriptions}

    def drop_event(self, event_id: str) -> None:
        with self._lock:
            subscriptions = self._by_event.pop(event_id, None) or {}
            self._attempts.pop(event_id, None)
            for subscription_id in subscriptions:
                self._write(event_id, subscription_id, DELIVERY_PRUNED)

    def _write(self, event_id: str, subscription_id: str, status: str,
               attempts: int = 0) -> None:
        if self._persist is not None:
            self._persist(event_id, subscription_id, status, attempts)


class OutboxStore(ABC):
    @abstractmethod
    def append(self, event: RegistryEvent) -> RegistryEvent:
        """Assign the next registry_version, persist, and return the event."""
        ...

    def shares_backend(self, backend) -> bool:
        """Whether this outbox persists through the given storage backend.

        The authoritative-write path requires its outbox to share the
        backend's transaction; a formal method (instead of sniffing private
        attributes) keeps that contract checkable as implementations evolve.
        """
        return getattr(self, "_backend", None) is backend

    @abstractmethod
    def mark_status(self, event_id: str, status: str,
                    dispatched_at: Optional[str] = None) -> bool:
        ...

    @abstractmethod
    def list_pending(self, limit: Optional[int] = None, after_version: int = 0) -> List[RegistryEvent]:
        ...

    @abstractmethod
    def list_after(self, version: int, limit: int) -> List[RegistryEvent]:
        """Events with registry_version > version, ascending."""
        ...

    @abstractmethod
    def max_version(self) -> int:
        ...

    @abstractmethod
    def cleanup(self, retention_days: int, active_subscription_ids=None) -> int:
        """Remove events nothing is waiting for, older than the retention window."""
        ...

    @abstractmethod
    def close(self):
        ...

    # ---- per-subscriber delivery ledger (see the R3 finding) ----

    def _ledger(self) -> "_DeliveryLedger":
        ledger = getattr(self, "_delivery_ledger", None)
        if ledger is None:
            ledger = _DeliveryLedger()
            self._delivery_ledger = ledger
        return ledger

    def _all_events(self) -> List[RegistryEvent]:
        """Events this outbox holds, for ledger-backed list/sync operations."""
        return []

    def ensure_deliveries(self, event: RegistryEvent, subscription_ids,
                          skipped_subscription_ids=()) -> None:
        """Record a delivery row per subscription; idempotent per pair.

        Subscribers the event did not match get a terminal `skipped` row so that
        "has this event been fanned out?" stays observable; existing rows are never
        overwritten, so the first decision for a pair wins.
        """
        ledger = self._ledger()
        ledger.ensure(event.event_id, [str(s) for s in subscription_ids], DELIVERY_PENDING)
        ledger.ensure(event.event_id, [str(s) for s in skipped_subscription_ids],
                      DELIVERY_SKIPPED)
        # Fanning out to nobody (or only to non-matching subscribers) makes the
        # event terminal right away; with a pending row it stays pending.
        self._sync_event_status(event.event_id)

    def delivery_status(self, event_id: str, subscription_id: str) -> Optional[str]:
        return self._ledger().status(event_id, subscription_id)

    def delivery_attempts(self, event_id: str, subscription_id: str) -> int:
        """How many delivery attempts this pair has already consumed."""
        return self._ledger().attempts(event_id, subscription_id)

    def list_retryable_for(self, subscription_id: str,
                           max_attempts: int, limit: Optional[int] = None) -> List[RegistryEvent]:
        """Failed deliveries of this subscriber that still have retry budget.

        Durable retry is bounded by the persisted attempt counter, so a restart
        (which resets nothing else) picks the same set back up.
        """
        retryable = set(self._ledger().retryable_event_ids(subscription_id, max_attempts))
        events = [e for e in self._all_events() if e.event_id in retryable]
        events.sort(key=lambda e: e.registry_version)
        return events[:limit] if limit else events

    def retry_delivery(self, subscription_id: str, event_id: str,
                       max_attempts: Optional[int] = None) -> bool:
        """Requeue one failed delivery, keeping its attempt count.

        The event-level status is folded back to `pending` (even though `pending`
        is otherwise never written) so that `list_pending()` — and therefore
        recovery on the next restart — sees the delivery again. Pass
        `max_attempts` to enforce the retry budget at the row level.
        """
        if not self._ledger().reset(event_id, subscription_id, max_attempts):
            return False
        self._sync_event_status(event_id, force=True)
        return True

    def list_pending_for(self, subscription_id: str,
                         limit: Optional[int] = None) -> List[RegistryEvent]:
        """Events this subscriber still has to receive, ascending by version."""
        pending = self._ledger().pending_event_ids(subscription_id)
        events = [e for e in self._all_events() if e.event_id in pending]
        events.sort(key=lambda e: e.registry_version)
        return events[:limit] if limit else events

    def mark_delivery(self, subscription_id: str, event_id: str, status: str,
                      dispatched_at: Optional[str] = None) -> bool:
        """Mark one subscriber's delivery and fold the result into the event."""
        if not self._ledger().mark(event_id, subscription_id, status):
            # No ledger row for this pair (event predates the subscription, or the
            # caller drives the outbox without subscriptions): keep the event-level
            # contract instead of silently dropping the mark.
            return self.mark_status(event_id, status, dispatched_at)
        self._sync_event_status(event_id, dispatched_at)
        return True

    def drop_deliveries(self, subscription_id: str) -> int:
        """Forget a deleted subscription's deliveries and re-fold their events."""
        affected = self._ledger().drop_subscription(subscription_id)
        for event_id in affected:
            if not self._ledger().statuses(event_id):
                # Nobody owes this event any more; leaving it "pending" would
                # recover and keep it forever.
                self._set_event_status(event_id, DELIVERY_ABANDONED)
            else:
                self._sync_event_status(event_id)
        return len(affected)

    def _prune_deliveries(self, active_subscription_ids) -> int:
        """Drop deliveries of subscriptions that no longer exist.

        A subscription can be deleted while this process is down; its pending rows
        would otherwise keep events "pending" forever, so recovery/cleanup prunes
        them before deciding what to keep.
        """
        if active_subscription_ids is None:
            return 0
        known = {str(s) for s in active_subscription_ids}
        removed = 0
        for subscription_id in self._ledger().known_subscriptions():
            if subscription_id not in known:
                removed += self.drop_deliveries(subscription_id)
        return removed

    def _sync_event_status(self, event_id: str,
                           dispatched_at: Optional[str] = None,
                           force: bool = False) -> None:
        """Fold the per-subscriber rows into the event-level status.

        Uses `_set_event_status` (not `mark_status`) on purpose: overwriting the
        individual rows here would erase which subscriber actually failed.

        `force` writes the aggregate even when it is `pending`: a retried delivery
        has to be visible to `list_pending()` again, otherwise a crash between the
        retry and the next attempt would leave it stranded (recovery only looks at
        events that are still pending).
        """
        aggregate = aggregate_delivery_status(self._ledger().statuses(event_id))
        if aggregate is None or (aggregate == DELIVERY_PENDING and not force):
            return
        for event in self._all_events():
            if event.event_id == event_id:
                self._set_event_status(event_id, aggregate, dispatched_at)
                return

    def _set_event_status(self, event_id: str, status: str,
                          dispatched_at: Optional[str] = None) -> bool:
        """Write the event-level status only, leaving per-subscriber rows alone."""
        raise NotImplementedError

    def _mark_all_deliveries(self, event_id: str, status: str) -> None:
        """Keep the ledger consistent when a caller sets the event status directly."""
        self._ledger().mark_all(event_id, status)


def _iso_minus_days(days: int) -> str:
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    return cutoff.isoformat()


class MemoryOutbox(OutboxStore):
    """In-memory outbox. Used for vectordb persistence mode and tests."""

    def __init__(self):
        self._events: List[RegistryEvent] = []
        self._lock = threading.Lock()

    def append(self, event: RegistryEvent) -> RegistryEvent:
        with self._lock:
            # Memory mode is single-process by construction. Only the SQL outbox
            # has to be multi-instance safe (see SqlOutbox._allocate_version).
            event.registry_version = self.max_version() + 1
            event.status = "pending"  # type: ignore[attr-defined]
            self._events.append(event)
            return event

    def _set_event_status(self, event_id: str, status: str,
                          dispatched_at: Optional[str] = None) -> bool:
        with self._lock:
            for event in self._events:
                if event.event_id == event_id:
                    event.status = status  # type: ignore[attr-defined]
                    event.dispatched_at = dispatched_at or utc_now_iso()  # type: ignore[attr-defined]
                    return True
            return False

    def mark_status(self, event_id: str, status: str,
                    dispatched_at: Optional[str] = None) -> bool:
        if not self._set_event_status(event_id, status, dispatched_at):
            return False
        # A direct status write overrides every subscriber's view.
        self._mark_all_deliveries(event_id, status)
        return True

    def _all_events(self) -> List[RegistryEvent]:
        return list(self._events)

    def list_pending(self, limit: Optional[int] = None, after_version: int = 0) -> List[RegistryEvent]:
        with self._lock:
            pending = (e for e in self._events if e.registry_version > after_version
                       and getattr(e, 'status', 'pending') == 'pending')
            return list(islice(pending, limit)) if limit else list(pending)

    def list_after(self, version: int, limit: int) -> List[RegistryEvent]:
        with self._lock:
            matched = [e for e in self._events if e.registry_version > version]
            return matched[:limit]

    def max_version(self) -> int:
        return max((e.registry_version for e in self._events), default=0)

    def cleanup(self, retention_days: int, active_subscription_ids=None) -> int:
        cutoff = _iso_minus_days(retention_days)
        self._prune_deliveries(active_subscription_ids)
        with self._lock:
            kept, removed = [], 0
            for event in self._events:
                status = getattr(event, "status", "pending")
                if status != "pending" and event.timestamp < cutoff:
                    removed += 1
                else:
                    kept.append(event)
            self._events = kept
            return removed

    def close(self):
        pass


class SqlOutbox(OutboxStore):
    """
    SQL-backed outbox delegating connection management to the registry's main
    storage backend (works for PostgreSQL, GaussDB, and SQLite alike).
    """

    def __init__(self, backend):
        self._backend = backend
        self._ensure_table()

    @property
    def _ph(self):
        return getattr(self._backend, "param_ph", "%s")

    def _ensure_table(self):
        ddl = """
            CREATE TABLE IF NOT EXISTS registry_events (
                event_id         VARCHAR(64) PRIMARY KEY,
                registry_version BIGINT      NOT NULL,
                event_type       VARCHAR(32) NOT NULL,
                payload          TEXT        NOT NULL,
                status           VARCHAR(16) NOT NULL DEFAULT 'pending',
                retry_count      INT         NOT NULL DEFAULT 0,
                created_at       VARCHAR(64) NOT NULL,
                dispatched_at    VARCHAR(64)
            )
        """
        self._backend._execute_write(ddl)
        self._ensure_version_counter()
        self._backend.ensure_index(
            "CREATE INDEX IF NOT EXISTS idx_registry_events_version "
            "ON registry_events(registry_version)",
            "CREATE INDEX idx_registry_events_version ON registry_events(registry_version)"
        )
        # Kept as a belt-and-braces check: `_allocate_version()` allocates from the
        # transaction-scoped counter row, so a duplicate here means someone
        # inserted into registry_events outside the outbox. Without it
        # `list_after(version)` would silently skip one of the two events.
        try:
            self._backend.ensure_index(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_registry_events_version_unique "
                "ON registry_events(registry_version)",
                "CREATE UNIQUE INDEX idx_registry_events_version_unique "
                "ON registry_events(registry_version)"
            )
        except Exception as e:
            # Fail loud: without this constraint two instances can record the same
            # version and `list_after()` would skip one of them forever. Starting
            # anyway would mean serving version-cursor consumers (/changes, future
            # projections) without the ordering guarantee they rely on.
            raise RuntimeError(
                f"Could not enforce a unique registry_version on registry_events ({e}). "
                "Repair duplicates first: SELECT registry_version, COUNT(*) FROM "
                "registry_events GROUP BY registry_version HAVING COUNT(*) > 1"
            ) from e
        self._backend.ensure_index(
            "CREATE INDEX IF NOT EXISTS idx_registry_events_status "
            "ON registry_events(status)",
            "CREATE INDEX idx_registry_events_status ON registry_events(status)"
        )
        logger.info("Outbox table 'registry_events' created/verified")
        self._ensure_delivery_table()

    def _ensure_delivery_table(self):
        """Per-subscriber delivery ledger (see the R3 finding).

        The event-level status used to be written once per event and shared by all
        subscribers, so the first subscriber to finish decided that every other
        subscriber was done: a slow one lost the event, a fast one could be marked
        failed again. Each (subscription, event) pair now has its own row.
        """
        self._backend._execute_write(
            "CREATE TABLE IF NOT EXISTS registry_event_deliveries ("
            "subscription_id VARCHAR(128) NOT NULL, "
            "event_id VARCHAR(64) NOT NULL, "
            "registry_version BIGINT NOT NULL, "
            "status VARCHAR(16) NOT NULL DEFAULT 'pending', "
            "attempts INT NOT NULL DEFAULT 0, "
            "last_error VARCHAR(512), "
            "updated_at VARCHAR(64) NOT NULL, "
            "PRIMARY KEY (subscription_id, event_id))"
        )
        self._backend.ensure_index(
            "CREATE INDEX IF NOT EXISTS idx_event_deliveries_pending "
            "ON registry_event_deliveries(subscription_id, status, registry_version)",
            "CREATE INDEX idx_event_deliveries_pending "
            "ON registry_event_deliveries(subscription_id, status, registry_version)"
        )
        logger.info("Outbox table 'registry_event_deliveries' created/verified")

    def _ensure_version_counter(self):
        """Create the version counter and seed it from existing events.

        Seeding from MAX(registry_version) keeps an upgrade from reusing versions
        that are already visible to consumers. Two instances starting at once can
        both try to insert; the primary key makes one of them lose, which is
        harmless because the winner already seeded the same value.
        """
        ph = self._ph
        self._backend._execute_write(
            "CREATE TABLE IF NOT EXISTS registry_event_counter ("
            "counter_name VARCHAR(64) PRIMARY KEY, version BIGINT NOT NULL)"
        )
        row = self._backend._execute_read_one(
            f"SELECT version FROM registry_event_counter WHERE counter_name = {ph}",
            (REGISTRY_VERSION_COUNTER,)
        )
        if row is not None:
            return
        try:
            self._backend._execute_write(
                f"INSERT INTO registry_event_counter (counter_name, version) "
                f"SELECT {ph}, COALESCE(MAX(registry_version), 0) FROM registry_events",
                (REGISTRY_VERSION_COUNTER,)
            )
        except Exception as e:
            logger.info(f"registry_event_counter seeded concurrently, using existing row: {e}")
        logger.info("Outbox version counter 'registry_event_counter' created/verified")

    def append(self, event: RegistryEvent) -> RegistryEvent:
        # SQLite's connection lock already serializes the complete outer UoW.
        # Other backends allocate under a DATABASE row lock until outer commit.
        # Do not hold a process lock while waiting for that row: another thread
        # may own it and need to append a second event before it can commit.
        with self._backend._serialize():
            with self._backend.transaction():
                event.registry_version = self._allocate_version()
                self._backend._execute_write(
                    "INSERT INTO registry_events (event_id, registry_version, event_type, "
                    "payload, status, retry_count, created_at, dispatched_at) "
                    f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, 'pending', 0, {self._ph}, NULL)",
                    (event.event_id, event.registry_version, event.event_type.value,
                     json.dumps(event.to_dict()), event.timestamp)
                )
                return event

    def _allocate_version(self) -> int:
        """Take the next registry version inside the caller's unit of work.

        ``registry_event_counter`` is a single row updated in the same transaction
        as the event insert. The UPDATE keeps a row lock until the transaction
        commits, so a second writer cannot take version N+1 before the writer of N
        has committed: a consumer reading ``list_after(version)`` can never observe
        a gap that later fills in behind its cursor. A plain database sequence
        cannot offer that (it is non-transactional and can leave gaps).
        """
        counter = f"WHERE counter_name = {self._ph}"
        if getattr(self._backend, "dialect", "") == "mysql":
            # No UPDATE ... RETURNING in MySQL; LAST_INSERT_ID(expr) is
            # connection-local and read on the same connection in this unit of work.
            updated = self._backend._execute_write(
                f"UPDATE registry_event_counter SET version = LAST_INSERT_ID(version + 1) {counter}",
                (REGISTRY_VERSION_COUNTER,)
            )
            if updated != 1:
                raise RuntimeError(
                    'registry_event_counter is missing its row; refusing to allocate a '
                    'registry_version without the commit-order guarantee')
            row = self._backend._execute_read_one("SELECT LAST_INSERT_ID()")
        else:
            self._backend._execute_write(
                f"UPDATE registry_event_counter SET version = version + 1 {counter}",
                (REGISTRY_VERSION_COUNTER,)
            )
            row = self._backend._execute_read_one(
                f"SELECT version FROM registry_event_counter {counter}",
                (REGISTRY_VERSION_COUNTER,)
            )
        if row is None:
            raise RuntimeError(
                "registry_event_counter is missing its row; refusing to allocate a "
                "registry_version without the commit-order guarantee"
            )
        return int(row[0])

    def _set_event_status(self, event_id: str, status: str,
                          dispatched_at: Optional[str] = None) -> bool:
        return self._backend._execute_write(
            f"UPDATE registry_events SET status = {self._ph}, "
            f"dispatched_at = {self._ph} WHERE event_id = {self._ph}",
            (status, dispatched_at or utc_now_iso(), event_id)
        ) > 0

    @_sql_delivery_transaction
    def mark_status(self, event_id: str, status: str,
                    dispatched_at: Optional[str] = None) -> bool:
        if not self._set_event_status(event_id, status, dispatched_at):
            return False
        # A direct status write overrides every subscriber's view.
        self._backend._execute_write(
            f"UPDATE registry_event_deliveries SET status = {self._ph}, "
            f"updated_at = {self._ph} WHERE event_id = {self._ph}",
            (status, utc_now_iso(), event_id)
        )
        return True

    def _row_to_event(self, row) -> RegistryEvent:
        event = RegistryEvent.from_dict(json.loads(row[0]))
        event.status = row[1]  # type: ignore[attr-defined]
        return event

    def list_pending(self, limit: Optional[int] = None, after_version: int = 0) -> List[RegistryEvent]:
        suffix = f' LIMIT {int(limit)}' if limit else ''
        rows = self._backend._execute_read_all(
            f"SELECT payload, status FROM registry_events WHERE status = {self._ph} "
            f"AND registry_version > {self._ph} ORDER BY registry_version ASC" + suffix,
            ('pending', int(after_version))
        )
        return [self._row_to_event(r) for r in rows]

    def list_after(self, version: int, limit: int) -> List[RegistryEvent]:
        rows = self._backend._execute_read_all(
            f"SELECT payload, status FROM registry_events WHERE registry_version > {self._ph} "
            f"ORDER BY registry_version ASC LIMIT {int(limit)}",
            (version,)
        )
        return [self._row_to_event(r) for r in rows]

    def max_version(self) -> int:
        row = self._backend._execute_read_one(
            "SELECT COALESCE(MAX(registry_version), 0) FROM registry_events"
        )
        return int(row[0] or 0)

    # ---- per-subscriber delivery ledger ----

    @_sql_delivery_transaction
    def ensure_deliveries(self, event: RegistryEvent, subscription_ids,
                          skipped_subscription_ids=()) -> None:
        for subscription_id, status in (
            [(s, DELIVERY_PENDING) for s in subscription_ids]
            + [(s, DELIVERY_SKIPPED) for s in skipped_subscription_ids]
        ):
            # A caught duplicate exception still aborts a PostgreSQL transaction.
            # Use a targeted conflict clause, preserving existing delivery state.
            conflict = (" ON DUPLICATE KEY UPDATE event_id = event_id"
                        if self._backend.dialect == 'mysql' else
                        " ON CONFLICT (subscription_id, event_id) DO NOTHING")
            self._backend._execute_write(
                "INSERT INTO registry_event_deliveries "
                "(subscription_id, event_id, registry_version, status, attempts, "
                "last_error, updated_at) "
                f"VALUES ({self._ph}, {self._ph}, {self._ph}, {self._ph}, 0, NULL, {self._ph})" + conflict,
                (str(subscription_id), event.event_id, event.registry_version,
                 status, utc_now_iso())
            )
        # Fanning out to nobody (or only to non-matching subscribers) makes the
        # event terminal right away; with a pending row it stays pending.
        self._sync_event_status(event.event_id, force=True)

    def delivery_status(self, event_id: str, subscription_id: str) -> Optional[str]:
        row = self._backend._execute_read_one(
            f"SELECT status FROM registry_event_deliveries WHERE subscription_id = {self._ph} "
            f"AND event_id = {self._ph}",
            (str(subscription_id), event_id)
        )
        return row[0] if row else None

    def delivery_attempts(self, event_id: str, subscription_id: str) -> int:
        row = self._backend._execute_read_one(
            f"SELECT attempts FROM registry_event_deliveries WHERE subscription_id = {self._ph} "
            f"AND event_id = {self._ph}",
            (str(subscription_id), event_id)
        )
        return int(row[0]) if row and row[0] is not None else 0

    def list_retryable_for(self, subscription_id: str,
                           max_attempts: int, limit: Optional[int] = None) -> List[RegistryEvent]:
        sql = ("SELECT e.payload, e.status FROM registry_events e "
               "JOIN registry_event_deliveries d ON d.event_id = e.event_id "
               f"WHERE d.subscription_id = {self._ph} AND d.status = {self._ph} "
               f"AND d.attempts < {self._ph} "
               "ORDER BY e.registry_version ASC")
        if limit:
            sql += f' LIMIT {int(limit)}'
        params = (str(subscription_id), DELIVERY_FAILED, int(max_attempts))
        rows = self._backend._execute_read_all(sql, params)
        return [self._row_to_event(r) for r in rows]

    @_sql_delivery_transaction
    def retry_delivery(self, subscription_id: str, event_id: str,
                       max_attempts: Optional[int] = None) -> bool:
        params = [DELIVERY_PENDING, utc_now_iso(), str(subscription_id), event_id,
                  DELIVERY_FAILED]
        budget = ""
        if max_attempts is not None:
            budget = f" AND attempts < {self._ph}"
            params.append(int(max_attempts))
        updated = self._backend._execute_write(
            "UPDATE registry_event_deliveries SET status = "
            f"{self._ph}, updated_at = {self._ph} "
            f"WHERE subscription_id = {self._ph} AND event_id = {self._ph} "
            f"AND status = {self._ph}{budget}",
            tuple(params)
        )
        if updated == 0:
            return False
        self._sync_event_status(event_id, force=True)
        return True

    def list_pending_for(self, subscription_id: str,
                         limit: Optional[int] = None) -> List[RegistryEvent]:
        sql = ("SELECT e.payload, e.status FROM registry_events e "
               "JOIN registry_event_deliveries d ON d.event_id = e.event_id "
               f"WHERE d.subscription_id = {self._ph} AND d.status = {self._ph} "
               "ORDER BY e.registry_version ASC")
        params = (str(subscription_id), DELIVERY_PENDING)
        if limit:
            sql += f" LIMIT {int(limit)}"
        rows = self._backend._execute_read_all(sql, params)
        return [self._row_to_event(r) for r in rows]

    @_sql_delivery_transaction
    def mark_delivery(self, subscription_id: str, event_id: str, status: str,
                      dispatched_at: Optional[str] = None) -> bool:
        updated = self._backend._execute_write(
            "UPDATE registry_event_deliveries SET status = "
            f"{self._ph}, attempts = attempts + 1, updated_at = {self._ph} "
            f"WHERE subscription_id = {self._ph} AND event_id = {self._ph}",
            (status, utc_now_iso(), str(subscription_id), event_id)
        )
        if updated == 0:
            # No ledger row for this pair (event predates the subscription, or the
            # caller drives the outbox without subscriptions): fall back to the
            # event-level contract instead of silently dropping the mark.
            return self.mark_status(event_id, status, dispatched_at)
        self._sync_event_status(event_id, dispatched_at)
        return True

    @_sql_delivery_transaction
    def drop_deliveries(self, subscription_id: str) -> int:
        rows = self._backend._execute_read_all(
            f"SELECT DISTINCT event_id FROM registry_event_deliveries "
            f"WHERE subscription_id = {self._ph}",
            (str(subscription_id),)
        )
        removed = self._backend._execute_write(
            f"DELETE FROM registry_event_deliveries WHERE subscription_id = {self._ph}",
            (str(subscription_id),)
        )
        for row in rows:
            remaining = self._backend._execute_read_one(
                f"SELECT COUNT(*) FROM registry_event_deliveries WHERE event_id = {self._ph}",
                (row[0],)
            )
            if remaining and int(remaining[0] or 0) == 0:
                # Nobody owes this event any more; leaving it "pending" would
                # recover and keep it forever.
                self._set_event_status(row[0], DELIVERY_ABANDONED)
            else:
                self._sync_event_status(row[0])
        return removed

    def _prune_deliveries(self, active_subscription_ids) -> int:
        if active_subscription_ids is None:
            return 0
        active = tuple(str(s) for s in active_subscription_ids)
        placeholders = ", ".join([self._ph] * len(active))
        where = f"WHERE subscription_id NOT IN ({placeholders})" if active else ''
        rows = self._backend._execute_read_all(
            f"SELECT DISTINCT subscription_id FROM registry_event_deliveries {where} "
            "ORDER BY subscription_id", active
        )
        # Reuse the atomic deletion path, including the last-subscriber case:
        # an empty remaining ledger is abandoned, not permanently pending.
        return sum(self.drop_deliveries(row[0]) for row in rows)

    def _sync_event_status(self, event_id: str,
                           dispatched_at: Optional[str] = None,
                           force: bool = False) -> None:
        rows = self._backend._execute_read_all(
            f"SELECT status FROM registry_event_deliveries WHERE event_id = {self._ph}",
            (event_id,)
        )
        aggregate = aggregate_delivery_status({i: r[0] for i, r in enumerate(rows)})
        if aggregate is None or (aggregate == DELIVERY_PENDING and not force):
            return
        self._set_event_status(event_id, aggregate, dispatched_at)

    @_sql_delivery_transaction
    def cleanup(self, retention_days: int, active_subscription_ids=None) -> int:
        cutoff = _iso_minus_days(retention_days)
        self._prune_deliveries(active_subscription_ids)
        # Delivery rows of the pruned events go first, then the events themselves.
        self._backend._execute_write(
            "DELETE FROM registry_event_deliveries WHERE event_id IN ("
            f"SELECT event_id FROM registry_events WHERE status != {self._ph} "
            f"AND created_at < {self._ph})",
            ("pending", cutoff)
        )
        return self._backend._execute_write(
            f"DELETE FROM registry_events WHERE status != {self._ph} AND created_at < {self._ph}",
            ("pending", cutoff)
        )

    def close(self):
        pass


class FileOutbox(OutboxStore):
    """
    JSONL append-only outbox for file persistence mode. Each mutation appends
    a full event snapshot line; the latest line per event_id wins on load.
    """

    def __init__(self, file_path: str):
        self._path = Path(file_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._events: List[RegistryEvent] = self._load()
        # Per-subscriber deliveries live in a sibling append-only file so a
        # restart keeps knowing which subscribers still need which events.
        self._deliveries_path = Path(str(file_path) + ".deliveries")

    def _ledger(self) -> "_DeliveryLedger":
        ledger = getattr(self, "_delivery_ledger", None)
        if ledger is None:
            ledger = _DeliveryLedger(persist=self._append_delivery_line)
            ledger.load(self._load_delivery_lines())
            self._delivery_ledger = ledger
        return ledger

    def _load_delivery_lines(self) -> List[tuple]:
        if not self._deliveries_path.exists():
            return []
        entries = []
        try:
            with open(self._deliveries_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        entry = json.loads(line)
                        entries.append((entry["event_id"], entry["subscription_id"],
                                        entry["status"], entry.get("attempts", 0)))
                    except (json.JSONDecodeError, KeyError, TypeError, ValueError) as e:
                        logger.warning(f"Skipping malformed delivery line: {e}")
        except OSError as e:
            logger.error(f"Failed to load delivery ledger: {e}")
        return entries

    def _append_delivery_line(self, event_id: str, subscription_id: str, status: str,
                              attempts: int = 0):
        with open(self._deliveries_path, "a", encoding="utf-8") as f:
            f.write(json.dumps({"event_id": event_id, "subscription_id": subscription_id,
                                "status": status, "attempts": int(attempts)},
                               ensure_ascii=False) + "\n")
        os.chmod(self._deliveries_path, 0o600)

    def _all_events(self) -> List[RegistryEvent]:
        return list(self._events)

    def _load(self) -> List[RegistryEvent]:
        if not self._path.exists():
            return []
        latest: dict = {}
        order: List[str] = []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        event = RegistryEvent.from_dict(json.loads(line))
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.warning(f"Skipping malformed outbox line: {e}")
                        continue
                    if event.event_id not in latest:
                        order.append(event.event_id)
                    latest[event.event_id] = event
        except OSError as e:
            logger.error(f"Failed to load outbox file: {e}")
            return []
        return [latest[eid] for eid in order]

    def _append_line(self, event: RegistryEvent):
        payload = dict(event.to_dict())
        payload["status"] = getattr(event, "status", "pending")
        payload["dispatched_at"] = getattr(event, "dispatched_at", None)
        with open(self._path, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
        os.chmod(self._path, 0o600)

    def append(self, event: RegistryEvent) -> RegistryEvent:
        with self._lock:
            # File mode backs the JSON FileStorage deployment, which is
            # single-instance; two processes sharing this file would allocate
            # the same version. Multi-instance deployments must use a SQL backend.
            event.registry_version = self.max_version() + 1
            event.status = "pending"  # type: ignore[attr-defined]
            self._events.append(event)
            self._append_line(event)
            return event

    def _set_event_status(self, event_id: str, status: str,
                          dispatched_at: Optional[str] = None) -> bool:
        with self._lock:
            for event in self._events:
                if event.event_id == event_id:
                    event.status = status  # type: ignore[attr-defined]
                    event.dispatched_at = dispatched_at or utc_now_iso()  # type: ignore[attr-defined]
                    self._append_line(event)
                    return True
            return False

    def mark_status(self, event_id: str, status: str,
                    dispatched_at: Optional[str] = None) -> bool:
        if not self._set_event_status(event_id, status, dispatched_at):
            return False
        # A direct status write overrides every subscriber's view.
        self._mark_all_deliveries(event_id, status)
        return True

    def _all_events(self) -> List[RegistryEvent]:
        return list(self._events)

    def list_pending(self, limit: Optional[int] = None, after_version: int = 0) -> List[RegistryEvent]:
        with self._lock:
            pending = (e for e in self._events if e.registry_version > after_version
                       and getattr(e, 'status', 'pending') == 'pending')
            return list(islice(pending, limit)) if limit else list(pending)

    def list_after(self, version: int, limit: int) -> List[RegistryEvent]:
        with self._lock:
            matched = [e for e in self._events if e.registry_version > version]
            return matched[:limit]

    def max_version(self) -> int:
        return max((e.registry_version for e in self._events), default=0)

    def cleanup(self, retention_days: int, active_subscription_ids=None) -> int:
        cutoff = _iso_minus_days(retention_days)
        self._prune_deliveries(active_subscription_ids)
        with self._lock:
            kept, removed = [], 0
            for event in self._events:
                status = getattr(event, "status", "pending")
                if status != "pending" and event.timestamp < cutoff:
                    removed += 1
                    # Persist the prune so a restart does not resurrect "pending".
                    self._ledger().drop_event(event.event_id)
                else:
                    kept.append(event)
            if removed > 0:
                self._events = kept
                lines = []
                for event in self._events:
                    payload = dict(event.to_dict())
                    payload["status"] = getattr(event, "status", "pending")
                    payload["dispatched_at"] = getattr(event, "dispatched_at", None)
                    lines.append(json.dumps(payload, ensure_ascii=False))
                tmp_path = self._path.with_suffix(".tmp")
                with open(tmp_path, "w", encoding="utf-8") as f:
                    f.write("\n".join(lines) + ("\n" if lines else ""))
                os.replace(tmp_path, self._path)
                os.chmod(self._path, 0o600)
            return removed

    def close(self):
        pass
