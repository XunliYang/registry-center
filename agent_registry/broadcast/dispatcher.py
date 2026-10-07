# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""
Webhook dispatcher: debounces, rate-limits, signs, and delivers registry
events to subscribed callbacks.

Storm protection (three layers):
1. debounce window batches events without dropping durable delivery records
2. per-subscription token bucket degrades overflow batches to a SYNC_REQUIRED
   summary event (no data is lost - subscribers reconcile via /changes)
3. per-subscription sequential workers isolate slow subscribers from others
   (each batch is ordered; later durable retries can replay older versions)
"""

import asyncio
import hashlib
import hmac
import json
import random
import time
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import httpx
from loguru import logger

from agent_registry.broadcast.events import (
    RegistryEvent,
    build_sync_required,
    public_event,
)
from agent_registry.broadcast.outbox import DELIVERY_ABANDONED, OutboxStore
from agent_registry.broadcast.callback_policy import validate_callback_destination
from agent_registry.broadcast.subscriptions import Subscription, SubscriptionStore, event_matches


class TokenBucket:
    def __init__(self, rate: float, capacity: float):
        self.rate = max(rate, 0.1)
        self.capacity = max(capacity, 1.0)
        self.tokens = self.capacity
        self._last_refill = time.monotonic()

    def try_consume(self, amount: float) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self._last_refill) * self.rate)
        self._last_refill = now
        if self.tokens >= amount:
            self.tokens -= amount
            return True
        return False


@dataclass
class _Worker:
    queue: asyncio.Queue
    task: asyncio.Task


def sign_payload(secret: str, timestamp: str, body: bytes) -> str:
    digest = hmac.new(secret.encode("utf-8"), f"{timestamp}.".encode("utf-8") + body,
                      hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class WebhookDispatcher:
    def __init__(self, subscription_store: SubscriptionStore, outbox: OutboxStore,
                 debounce_window: float = 2.0,
                 max_events_per_second: float = 50.0,
                 webhook_timeout: float = 10.0,
                 max_retries: int = 5,
                 backoff_base: float = 2.0,
                 backoff_max: float = 300.0,
                 retention_days: int = 7,
                 delivery_max_attempts: int = 5,
                 retry_interval: float = 60.0,
                 client: Optional[httpx.AsyncClient] = None,
                 batch_size: int = 256, buffer_limit: int = 1024):
        self._subs = subscription_store
        self._outbox = outbox
        self._debounce_window = debounce_window
        self._max_events_per_second = max_events_per_second
        self._webhook_timeout = webhook_timeout
        self._max_retries = max_retries
        self._backoff_base = backoff_base
        self._backoff_max = backoff_max
        self._retention_days = retention_days
        self._delivery_max_attempts = max(int(delivery_max_attempts), 1)
        self._retry_interval = max(float(retry_interval), 0.0)
        self._batch_size = max(int(batch_size), 1)
        self._buffer_limit = max(int(buffer_limit), 1)
        self._fanout_cursor = 0
        self._scheduled: Dict[str, set] = {}
        self._loop = None
        self._client = client or httpx.AsyncClient(timeout=webhook_timeout)
        self._owns_client = client is None

        self._buffers: Dict[Tuple[str, str], RegistryEvent] = {}
        self._workers: Dict[str, _Worker] = {}
        self._buckets: Dict[str, TokenBucket] = {}
        self._flusher_task: Optional[asyncio.Task] = None
        self._retry_task: Optional[asyncio.Task] = None
        self._cleanup_counter = 0

    # ---------- lifecycle ----------

    def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._flusher_task = asyncio.get_running_loop().create_task(self._flusher_loop())
        if self._retry_interval > 0:
            self._retry_task = asyncio.get_running_loop().create_task(self._retry_loop())
        for subscription in self._subs.list_all():
            self._add_worker(subscription)
        self.recover()
        # A crash (or a destination that was down) leaves failed deliveries behind;
        # pick the ones that still have retry budget back up immediately.
        self.retry_failed_deliveries()
        logger.info(f"Dispatcher started with {len(self._workers)} subscription worker(s)")

    async def stop(self) -> None:
        for task in (self._flusher_task, self._retry_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        self._flusher_task = None
        self._retry_task = None
        for worker in self._workers.values():
            worker.task.cancel()
        for worker in self._workers.values():
            try:
                await worker.task
            except asyncio.CancelledError:
                pass
        self._workers.clear()
        if self._owns_client:
            await self._client.aclose()

    # ---------- subscription management ----------

    def add_subscription(self, subscription: Subscription) -> None:
        if self._loop is not None and asyncio.get_running_loop() is not self._loop:
            self._loop.call_soon_threadsafe(self.add_subscription, subscription)
            return
        self._add_worker(subscription)

    def remove_subscription(self, subscription_id: str) -> None:
        if self._loop is not None and asyncio.get_running_loop() is not self._loop:
            self._loop.call_soon_threadsafe(self.remove_subscription, subscription_id)
            return
        worker = self._workers.pop(subscription_id, None)
        self._scheduled.pop(subscription_id, None)
        self._buckets.pop(subscription_id, None)
        if worker is not None:
            worker.task.cancel()
        # Its pending deliveries would otherwise keep events "pending" forever.
        try:
            self._outbox.drop_deliveries(subscription_id)
        except Exception as e:
            logger.warning(f"Could not drop deliveries for {subscription_id}: {e}")

    def _add_worker(self, subscription: Subscription) -> None:
        if subscription.subscription_id in self._workers:
            return
        loop = asyncio.get_running_loop()
        worker = _Worker(queue=asyncio.Queue(maxsize=1), task=None)  # type: ignore[arg-type]
        worker.task = loop.create_task(self._worker_loop(subscription.subscription_id, worker.queue))
        self._workers[subscription.subscription_id] = worker
        self._buckets[subscription.subscription_id] = TokenBucket(
            rate=self._max_events_per_second,
            capacity=max(self._max_events_per_second * 2, 100.0),
        )

    # ---------- intake ----------

    def submit(self, event: RegistryEvent) -> None:
        """Accept an already-persisted event; cache only bounded delivery hints."""
        matching, skipped = [], []
        for subscription in self._subs.list_all():
            if event_matches(event, subscription):
                matching.append(subscription)
            else:
                skipped.append(subscription)
        # Record who owes a delivery (and who does not) before the event can be
        # pruned: those rows, not the shared event status, decide who is done.
        try:
            self._outbox.ensure_deliveries(
                event, [s.subscription_id for s in matching],
                [s.subscription_id for s in skipped])
        except Exception as e:
            logger.error(f"Could not record deliveries for {event.event_id}: {e}")
            return  # Leave the persisted event pending for recovery, never send untracked work.
        for subscription in matching:
            if subscription.subscription_id not in self._workers:
                self._add_worker(subscription)
            # Each durable delivery must have an actual outcome. Coalescing by
            # agent discarded older event IDs while leaving their ledger rows
            # pending forever. Only duplicate intake of the SAME event may merge.
            key = (subscription.subscription_id, event.event_id)
            if key in self._buffers or len(self._buffers) < self._buffer_limit:
                self._buffers[key] = event
            # At capacity the ledger remains pending. The flusher polls it.

    def recover(self) -> None:
        """Re-enqueue pending events after a restart, bypassing the debounce window.

        Recovery is per subscriber: an event stays pending while at least one
        subscriber still owes a delivery, and each subscriber gets only the events
        it has not received yet. Ledger rows are (re)created for matching pairs, so
        a crash between the event append and `submit()` cannot strand an event.
        """
        pending = self._outbox.list_pending(limit=self._batch_size, after_version=self._fanout_cursor)
        self._fanout_cursor = pending[-1].registry_version if pending else 0
        # Pending delivery rows remain authoritative even if an older version
        # crashed before updating the event's aggregate status.
        subscriptions = self._subs.list_all()
        if not subscriptions:
            # Nothing can ever deliver these events; mark them so recovery and
            # cleanup stop treating them as owed.
            for event in pending:
                self._outbox.mark_status(event.event_id, DELIVERY_ABANDONED)
            return
        for event in pending:
            matching = [s.subscription_id for s in subscriptions if event_matches(event, s)]
            skipped = [s.subscription_id for s in subscriptions if not event_matches(event, s)]
            try:
                self._outbox.ensure_deliveries(event, matching, skipped)
            except Exception as e:
                logger.error(f"Could not record deliveries for {event.event_id}: {e}")
        recovered = 0
        for subscription in subscriptions:
            worker = self._workers.get(subscription.subscription_id)
            if worker is None:
                continue
            events = self._outbox.list_pending_for(subscription.subscription_id,
                                                  limit=self._batch_size * 2)
            if events:
                recovered += self._enqueue(subscription.subscription_id, events)
        if recovered:
            logger.info(f"Recovering {recovered} pending delivery(-ies) across "
                        f"{len(subscriptions)} subscription(s)")

    async def _retry_loop(self):
        """Periodically requeue failed deliveries that still have retry budget."""
        try:
            while True:
                await asyncio.sleep(self._retry_interval)
                try:
                    self.retry_failed_deliveries()
                except Exception as e:
                    logger.warning(f"Durable delivery retry sweep failed: {e}")
        except asyncio.CancelledError:
            raise

    def retry_failed_deliveries(self) -> int:
        """Requeue failed deliveries below the attempt budget.

        The in-process retries inside `_deliver_events` only cover one batch; this
        sweep is what makes a failure survivable across restarts. It is bounded by
        the persisted attempt counter (`broadcast.delivery.max.attempts`), so a
        permanently broken destination stops after the budget instead of looping
        forever. The internal ledger retains `delivery_failed`; `/changes`
        exposes event contents for reconciliation, not delivery diagnostics.
        """
        requeued = 0
        for subscription in self._subs.list_all():
            worker = self._workers.get(subscription.subscription_id)
            if worker is None:
                continue
            try:
                events = self._outbox.list_retryable_for(subscription.subscription_id,
                                                         self._delivery_max_attempts,
                                                         limit=self._batch_size)
            except Exception as e:
                logger.warning(f"Could not list retryable deliveries for "
                               f"{subscription.subscription_id}: {e}")
                continue
            ready = []
            for event in events:
                try:
                    if self._outbox.retry_delivery(subscription.subscription_id,
                                                   event.event_id,
                                                   self._delivery_max_attempts):
                        ready.append(event)
                except Exception as e:
                    logger.warning(f"Could not requeue {event.event_id} for "
                                   f"{subscription.subscription_id}: {e}")
            if ready:
                self._enqueue(subscription.subscription_id, ready)
                requeued += len(ready)
        if requeued:
            logger.info(f"Requeued {requeued} failed delivery(-ies) for retry")
        return requeued

    async def _flusher_loop(self):
        try:
            while True:
                await asyncio.sleep(self._debounce_window)
                self._flush_buffers()
                # Also recover queue overflow, failed fanout and abandoned
                # in-process attempts without waiting for a process restart.
                try:
                    self.recover()
                except Exception as exc:
                    logger.warning('Pending delivery recovery failed: {}', type(exc).__name__)
                self._cleanup_counter += 1
                if self._cleanup_counter >= 3600:  # roughly once per hour
                    self._cleanup_counter = 0
                    try:
                        removed = self._outbox.cleanup(
                            self._retention_days,
                            active_subscription_ids=[s.subscription_id for s in self._subs.list_all()],
                        )
                        if removed:
                            logger.info(f"Outbox cleanup removed {removed} event(s)")
                    except Exception as e:
                        logger.warning(f"Outbox cleanup failed: {e}")
        except asyncio.CancelledError:
            raise

    def _flush_buffers(self) -> None:
        if not self._buffers:
            return
        buffered, self._buffers = self._buffers, {}
        for subscription_id in {key[0] for key in buffered}:
            worker = self._workers.get(subscription_id)
            if worker is None:
                continue
            # The ledger is the ordered source. New cache hints must not jump
            # ahead of older pending rows that overflowed the cache previously.
            events = self._outbox.list_pending_for(subscription_id, limit=self._batch_size * 2)
            self._enqueue(subscription_id, events)

    def _enqueue(self, subscription_id, events):
        worker = self._workers.get(subscription_id)
        if worker is None:
            return 0
        scheduled = self._scheduled.setdefault(subscription_id, set())
        ready = [event for event in events if event.event_id not in scheduled]
        queued = 0
        for start in range(0, len(ready), self._batch_size):
            batch = ready[start:start + self._batch_size]
            try:
                worker.queue.put_nowait(batch)
            except asyncio.QueueFull:
                break  # Unqueued rows stay durable/pending for the next poll.
            scheduled.update(event.event_id for event in batch)
            queued += len(batch)
        return queued

    # ---------- delivery ----------

    async def _worker_loop(self, subscription_id: str, queue: asyncio.Queue):
        try:
            while True:
                batch: List[RegistryEvent] = await queue.get()
                try:
                    await self._process_batch(subscription_id, batch)
                except Exception as e:
                    logger.error(f"Batch processing failed for {subscription_id}: {e}")
        except asyncio.CancelledError:
            raise

    async def _process_batch(self, subscription_id: str, batch: List[RegistryEvent]):
        try:
            await self._deliver_batch(subscription_id, batch)
        finally:
            scheduled = self._scheduled.get(subscription_id, set())
            scheduled.difference_update(event.event_id for event in batch)

    async def _deliver_batch(self, subscription_id: str, batch: List[RegistryEvent]):
        subscription = self._subs.get(subscription_id)
        if subscription is None:
            return
        bucket = self._buckets.setdefault(
            subscription_id,
            TokenBucket(rate=self._max_events_per_second,
                        capacity=max(self._max_events_per_second * 2, 100.0)),
        )
        if not bucket.try_consume(len(batch)):
            logger.warning(f"Rate limit hit for subscription {subscription_id}, "
                           f"degrading {len(batch)} event(s) to SYNC_REQUIRED")
            for event in batch:
                self._outbox.mark_delivery(subscription_id, event.event_id, "degraded")
            sync_event = build_sync_required(self._outbox.max_version())
            await self._deliver_events(subscription, [sync_event], persist=False)
            return
        delivered = await self._deliver_events(subscription, batch, persist=True)
        status = "dispatched" if delivered else "delivery_failed"
        for event in batch:
            # Per subscriber: another subscriber's success or failure must not
            # decide this one's delivery state.
            self._outbox.mark_delivery(subscription_id, event.event_id, status)

    async def _deliver_events(self, subscription: Subscription,
                              events: List[RegistryEvent], persist: bool) -> bool:
        body = json.dumps({
            "subscription_id": subscription.subscription_id,
            "events": [public_event(e).to_dict() for e in events],
        }, ensure_ascii=False).encode("utf-8")
        timestamp = str(int(time.time()))
        headers = {
            "Content-Type": "application/json",
            "X-Registry-Event-Id": events[0].event_id,
            "X-Registry-Timestamp": timestamp,
        }
        if subscription.secret:
            headers["X-Registry-Signature"] = sign_payload(subscription.secret, timestamp, body)

        max_attempts = self._max_retries + 1
        for attempt in range(max_attempts):
            try:
                # Revalidate persisted subscriptions so removing a destination
                # from the operator's allowlist stops further network traffic.
                validate_callback_destination(subscription.callback_url)
                response = await self._client.post(subscription.callback_url, content=body, headers=headers,
                                                   follow_redirects=False)
                if 200 <= response.status_code < 300:
                    return True
                logger.warning(f"Webhook delivery for {subscription.subscription_id} returned "
                               f"{response.status_code} (attempt {attempt + 1}/{max_attempts})")
            except ValueError:
                logger.warning("Webhook destination is no longer authorized: {}", subscription.subscription_id)
                return False
            except httpx.InvalidURL:
                # Malformed destination: httpx.InvalidURL is not an HTTPError, and
                # retrying can never succeed, so fail fast instead of letting the
                # exception escape the batch worker.
                logger.warning("Webhook destination is not a valid URL: {}", subscription.subscription_id)
                return False
            except (httpx.HTTPError, OSError) as e:
                logger.warning(f"Webhook delivery for {subscription.subscription_id} failed: {type(e).__name__} "
                               f"(attempt {attempt + 1}/{max_attempts})")
            if attempt < max_attempts - 1:
                delay = min(self._backoff_base * (2 ** attempt), self._backoff_max)
                delay = delay * (0.7 + random.random() * 0.6)  # +/-30% jitter
                await asyncio.sleep(delay)
        return False
