# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Registry change event model shared by the event bus, outbox, and dispatcher."""

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict


class EventType(str, Enum):
    AGENT_REGISTERED = "AGENT_REGISTERED"
    AGENT_UPDATED = "AGENT_UPDATED"
    AGENT_DEREGISTERED = "AGENT_DEREGISTERED"
    AGENT_HEALTH_CHANGED = "AGENT_HEALTH_CHANGED"
    SYNC_REQUIRED = "SYNC_REQUIRED"


@dataclass
class RegistryEvent:
    event_id: str
    event_type: EventType
    timestamp: str
    registry_version: int
    data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type.value if isinstance(self.event_type, EventType) else self.event_type,
            "timestamp": self.timestamp,
            "registry_version": self.registry_version,
            "data": self.data,
        }

    @classmethod
    def from_dict(cls, payload: Dict[str, Any]) -> "RegistryEvent":
        return cls(
            event_id=payload.get("event_id", ""),
            event_type=EventType(payload.get("event_type")),
            timestamp=payload.get("timestamp", ""),
            registry_version=int(payload.get("registry_version", 0)),
            data=payload.get("data") or {},
        )


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def build_event(event_type: EventType, data: Dict[str, Any],
                registry_version: int) -> RegistryEvent:
    """Build an event envelope. Version is normally assigned by the outbox on append."""
    return RegistryEvent(
        event_id=str(uuid.uuid4()),
        event_type=event_type,
        timestamp=utc_now_iso(),
        registry_version=registry_version,
        data=data,
    )


def build_sync_required(since_version: int) -> RegistryEvent:
    """Control-plane signal telling a subscriber to catch up via /changes."""
    return build_event(EventType.SYNC_REQUIRED, {"since_version": since_version}, since_version)


def public_event(event: RegistryEvent) -> RegistryEvent:
    """Quarantine old log payloads whose approval visibility was not recorded.

    Preserve the cursor/envelope, but require a published-card snapshot instead
    of replaying a possibly pending Card from a pre-policy outbox.
    """
    if event.event_type == EventType.SYNC_REQUIRED or event.data.get('discovery_public') is True:
        return event
    return RegistryEvent(event.event_id, EventType.SYNC_REQUIRED, event.timestamp,
                         event.registry_version, {'since_version': 0, 'requires_snapshot': True,
                                                  'reason': 'legacy_visibility_unknown'})


def merge_events(previous: RegistryEvent, incoming: RegistryEvent) -> RegistryEvent:
    """
    Debounce coalescing for two buffered events of the same agent:
    REGISTERED+DEREGISTERED collapses to the terminal DEREGISTERED, and a
    DEREGISTERED followed by a REGISTERED resets to REGISTERED. Otherwise the
    incoming event (newer, carrying the full payload) wins.
    """
    prev_type = previous.event_type
    new_type = incoming.event_type
    if prev_type == EventType.AGENT_DEREGISTERED and new_type == EventType.AGENT_REGISTERED:
        return incoming
    return incoming
