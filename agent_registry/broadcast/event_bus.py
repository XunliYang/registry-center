# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""
In-process event bus bridging the synchronous registry core (worker threads /
event loop handlers) to the asyncio-side dispatcher.

Publishing is always non-blocking: the event is persisted to the outbox
(assigning its registry_version) and then queued for asynchronous dispatch.
Even with broadcasting disabled, events stay in the outbox so subscribers can
reconcile through the /changes endpoint.
"""

import asyncio
import queue
from typing import List, Optional

from loguru import logger

from agent_registry.broadcast.events import EventType, RegistryEvent, build_event
from agent_registry.broadcast.outbox import OutboxStore


class EventBus:
    def __init__(self, outbox: OutboxStore, dispatch_enabled: bool = False):
        self._outbox = outbox
        self._dispatch_enabled = dispatch_enabled
        # A bounded wake-up cache; the outbox, not this queue, owns the events.
        self._queue: "queue.Queue[RegistryEvent]" = queue.Queue(maxsize=1024)
        self._overflowed = False
        self._dispatcher = None
        self._consumer_task: Optional[asyncio.Task] = None
        self._listeners: List = []

    def attach_dispatcher(self, dispatcher) -> None:
        self._dispatcher = dispatcher

    def shares_backend(self, backend) -> bool:
        """Whether the outbox persists through the given authoritative storage.

        Used by RegistryCore's unit of work: an authoritative SQL write must
        only be paired with an outbox bound to the same backend, or a
        committed record could lose its durable change event.
        """
        return self._outbox is not None and self._outbox.shares_backend(backend)

    def add_listener(self, listener) -> None:
        """Register a synchronous callback invoked with every published event.

        Used by in-process consumers (e.g. the SSE health stream endpoint).
        Listener exceptions never affect publishing.

        Listeners run while the registry's core lock may still be held (the
        event is published inside the authoritative mutation), and the lock is
        a non-reentrant ``threading.Lock``: a listener that re-enters
        ``RegistryCore`` mutations deadlocks. Keep listeners fast and schedule
        any follow-up work onto another thread or event loop (see the health
        SSE listener in server.py for the pattern).
        """
        if listener not in self._listeners:
            self._listeners.append(listener)

    def remove_listener(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def persist(self, event_type: EventType, data: dict) -> RegistryEvent:
        """Append the event to the outbox WITHOUT waking any consumer.

        Used inside a unit of work: the event must only become visible to the
        queue/SSE listeners after the surrounding transaction committed,
        otherwise consumers can observe a change that is later rolled back.
        """
        return self._outbox.append(build_event(event_type, data, registry_version=0))

    def notify(self, event: RegistryEvent) -> None:
        """Wake cross-thread/cross-process consumers for an already persisted event."""
        if self._dispatch_enabled and self._dispatcher is not None:
            try:
                self._queue.put_nowait(event)
            except queue.Full:
                self._overflowed = True  # Coalesce hints, never drop the durable fact.
        for listener in list(self._listeners):
            try:
                listener(event)
            except Exception as e:
                logger.error(f"Event listener failed on {event.event_id}: {e}")

    def publish(self, event_type: EventType, data: dict) -> RegistryEvent:
        event = self.persist(event_type, data)
        self.notify(event)
        return event

    async def run_consumer(self, poll_interval: float = 0.05):
        """Drain the cross-thread queue and hand events to the dispatcher."""
        logger.info("Event bus consumer started")
        try:
            while True:
                forwarded = False
                # Also bound work per loop turn: a continuously refilled queue
                # must not starve HTTP handlers, heartbeats or delivery workers.
                for _ in range(256):
                    try:
                        event = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    try:
                        self._dispatcher.submit(event)
                    except Exception as e:
                        logger.error(f"Dispatcher failed to accept event {event.event_id}: {e}")
                    forwarded = True
                if not forwarded:
                    await asyncio.sleep(poll_interval)
                else:
                    await asyncio.sleep(0)
                if self._overflowed:
                    self._overflowed = False
                    try:
                        self._dispatcher.recover()
                    except Exception as exc:
                        logger.error('Overflow recovery failed: {}', type(exc).__name__)
                        self._overflowed = True
        except asyncio.CancelledError:
            logger.info("Event bus consumer stopped")
            raise

    def start_consumer(self) -> None:
        if self._consumer_task is None or self._consumer_task.done():
            self._consumer_task = asyncio.get_running_loop().create_task(self.run_consumer())

    async def stop_consumer(self) -> None:
        if self._consumer_task is not None:
            self._consumer_task.cancel()
            try:
                await self._consumer_task
            except asyncio.CancelledError:
                pass
            self._consumer_task = None
