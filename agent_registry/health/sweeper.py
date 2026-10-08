# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""
Background sweep task: classifies heartbeat freshness into health states and
publishes AGENT_HEALTH_CHANGED events on transitions.

The startup grace uses effective_last_seen = max(last_heartbeat_at,
registry_startup_time) so a long registry downtime is never misread as a mass
agent outage: every agent gets one full detection window after restart.
"""

import asyncio
from datetime import datetime, timezone
from typing import Optional

from loguru import logger

from agent_registry.broadcast.events import EventType
from agent_registry.broadcast.event_bus import EventBus
from agent_registry.health.state import HealthStatus, compute_status
from agent_registry.status import is_discoverable_status


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class HealthSweeper:
    def __init__(self, health_service, registry, event_bus: EventBus,
                 interval: int, failure_threshold: int, grace_period: int,
                 sweep_interval: float, offline_ttl: int = 0,
                 startup_time: Optional[datetime] = None):
        self._service = health_service
        self._registry = registry
        self._event_bus = event_bus
        self._interval = interval
        self._failure_threshold = failure_threshold
        self._grace_period = grace_period
        self._sweep_interval = sweep_interval
        self._offline_ttl = offline_ttl
        self._startup_time = startup_time or _utcnow()
        self._task: Optional[asyncio.Task] = None

    def sweep_once(self, now: Optional[datetime] = None) -> int:
        """Run a single sweep. Returns the number of published transitions."""
        now = now or _utcnow()
        transitions = 0
        for state in self._service.list_monitored():
            effective_last_seen = max(state.last_heartbeat_at, self._startup_time)
            target = compute_status(effective_last_seen, now,
                                    self._interval, self._failure_threshold,
                                    self._grace_period)
            if target == state.status:
                continue
            record = self._registry.get_by_key_with_owner(state.name, state.organization)
            if record is None:
                self._service.remove(state.name, state.organization)
                continue
            previous_status = state.status
            if self._service.update_status(state.name, state.organization, target, now):
                # Same policy as the public read paths: a stored record with an
                # empty legacy status counts as published.
                if not is_discoverable_status(record.status):
                    continue
                self._event_bus.publish(EventType.AGENT_HEALTH_CHANGED, {
                    "name": state.name,
                    "organization": state.organization,
                    "health_status": target.value,
                    "previous_health_status": previous_status.value,
                    "tags": self._registry.get_agent_tags(state.name, state.organization),
                    "discovery_public": True,
                })
                transitions += 1
        if self._offline_ttl > 0:
            self._cleanup_expired(now)
        return transitions

    def _cleanup_expired(self, now: datetime) -> None:
        for state in self._service.list_monitored():
            if state.status != HealthStatus.OFFLINE:
                continue
            offline_for = (now - state.status_changed_at).total_seconds()
            if offline_for >= self._offline_ttl:
                logger.info(f"Auto-deregistering offline agent "
                            f"{state.name}({state.organization}) after TTL {self._offline_ttl}s")
                self._registry.deregister(state.name, state.organization)

    async def run(self):
        logger.info(f"Health sweeper started (interval={self._sweep_interval}s)")
        try:
            while True:
                await asyncio.sleep(self._sweep_interval)
                try:
                    self.sweep_once()
                except Exception as e:
                    logger.error(f"Health sweep failed: {e}")
        except asyncio.CancelledError:
            logger.info("Health sweeper stopped")
            raise

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self.run())

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
