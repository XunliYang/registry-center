# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0

"""Tests for the heartbeat detection and change broadcast subsystems."""

import asyncio
import hashlib
import hmac as hmac_mod
import json
from pathlib import Path
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

from agent_registry import broadcast as broadcast_mod
from agent_registry import health as health_mod
from agent_registry.broadcast import BroadcastService, EventBus
from agent_registry.broadcast.dispatcher import TokenBucket, WebhookDispatcher, sign_payload
from agent_registry.broadcast.events import EventType, build_event, build_sync_required, merge_events
from agent_registry.broadcast.outbox import MemoryOutbox
from agent_registry.broadcast.subscriptions import (
    FileSubscriptionStore, MemorySubscriptionStore, Subscription, event_matches,
)
from agent_registry.health import HealthService
from agent_registry.health.state import HealthStatus, compute_status
from agent_registry.health.store import MemoryHeartbeatStore
from agent_registry.health.sweeper import HealthSweeper
from agent_registry.persistence.base import AgentRecord
from agent_registry.server import _enqueue_drop_oldest, app, get_registry


T0 = datetime(2026, 9, 17, 12, 0, 0, tzinfo=timezone.utc)
HEALTH_CONFIG = {
    "heartbeat.enabled": "true",
    "heartbeat.interval": "30",
    "heartbeat.failure.threshold": "3",
    "heartbeat.grace.period": "10",
    "heartbeat.sweep.interval": "10",
    "heartbeat.offline.ttl": "0",
}
BROADCAST_CONFIG = {"broadcast.enabled": "true"}


@pytest.fixture(autouse=True)
def authorized_callbacks(monkeypatch):
    monkeypatch.setattr('agent_registry.broadcast.callback_policy.get_conf', lambda: {
        'broadcast.callback.allowlist': 'cb.example.com,operator.example.com'})


def _make_agent_record(name="TestAgent", org="TestOrg"):
    from a2a.types import AgentCard
    card = {
        "name": name,
        "provider": {"organization": org, "url": "https://test.org"},
        "description": "test", "version": "1.0.0",
        "capabilities": {"streaming": False},
        "default_input_modes": [], "default_output_modes": [], "skills": [],
    }
    return AgentRecord(agent_card=AgentCard(**card))


@pytest.fixture(autouse=True)
def reset_singletons():
    health_mod.reset_for_tests()
    broadcast_mod.reset_for_tests()
    yield
    health_mod.reset_for_tests()
    broadcast_mod.reset_for_tests()


@pytest.fixture
def auth_mock():
    handler = MagicMock()
    handler.handle = AsyncMock(return_value=None)
    return handler


# ---------- state machine ----------

class TestComputeStatus:
    def test_healthy_within_interval_plus_grace(self):
        assert compute_status(T0, T0 + timedelta(seconds=40), 30, 3, 10) is HealthStatus.HEALTHY

    def test_suspect_window(self):
        assert compute_status(T0, T0 + timedelta(seconds=41), 30, 3, 10) is HealthStatus.SUSPECT
        assert compute_status(T0, T0 + timedelta(seconds=100), 30, 3, 10) is HealthStatus.SUSPECT

    def test_offline_after_threshold(self):
        assert compute_status(T0, T0 + timedelta(seconds=101), 30, 3, 10) is HealthStatus.OFFLINE


class TestMemoryHeartbeatStore:
    def test_first_heartbeat_has_no_previous(self):
        store = MemoryHeartbeatStore()
        previous, state = store.record_heartbeat("a", "org", T0)
        assert previous is None
        assert state.status is HealthStatus.HEALTHY

    def test_recovery_from_suspect_is_immediate(self):
        store = MemoryHeartbeatStore()
        store.record_heartbeat("a", "org", T0)
        store.update_status("a", "org", HealthStatus.OFFLINE, T0)
        previous, state = store.record_heartbeat("a", "org", T0 + timedelta(seconds=5))
        assert previous is HealthStatus.OFFLINE
        assert state.status is HealthStatus.HEALTHY


class TestHealthService:
    def test_disabled_service_reports_no_status(self):
        service = HealthService(MemoryHeartbeatStore(), {"heartbeat.enabled": "false"})
        assert service.enabled is False
        assert service.status_of("a", "org") is None

    def test_metadata_health_defaults_to_unknown(self):
        service = HealthService(MemoryHeartbeatStore(), dict(HEALTH_CONFIG))
        assert service.metadata_health("a", "org") == "unknown"
        service.record_heartbeat("a", "org")
        assert service.metadata_health("a", "org") == "healthy"


# ---------- sweeper ----------

class TestSweeper:
    def _build(self, offline_ttl=0, startup_time=T0):
        config = dict(HEALTH_CONFIG, **{"heartbeat.offline.ttl": str(offline_ttl)})
        service = HealthService(MemoryHeartbeatStore(), config)
        # Record at the fixed T0 baseline so sweep assertions use exact offsets.
        service.store.record_heartbeat("agentA", "orgA", T0)
        outbox = MemoryOutbox()
        bus = EventBus(outbox, dispatch_enabled=False)
        registry = MagicMock()
        registry.get_by_key_with_owner.return_value = _make_agent_record()
        registry.get_agent_tags.return_value = []
        sweeper = HealthSweeper(service, registry, bus, interval=30, failure_threshold=3,
                                grace_period=10, sweep_interval=10, offline_ttl=offline_ttl,
                                startup_time=startup_time)
        return service, outbox, registry, sweeper

    def test_no_transition_within_window(self):
        service, outbox, _, sweeper = self._build()
        transitions = sweeper.sweep_once(T0 + timedelta(seconds=35))
        assert transitions == 0
        assert outbox.max_version() == 0

    def test_healthy_to_suspect_to_offline(self):
        service, outbox, _, sweeper = self._build()
        assert sweeper.sweep_once(T0 + timedelta(seconds=50)) == 1
        assert service.status_of("agentA", "orgA") == "suspect"
        assert sweeper.sweep_once(T0 + timedelta(seconds=120)) == 1
        assert service.status_of("agentA", "orgA") == "offline"
        events = outbox.list_after(0, 10)
        assert [e.event_type for e in events] == [EventType.AGENT_HEALTH_CHANGED] * 2
        assert events[1].data["previous_health_status"] == "suspect"

    def test_startup_grace_prevents_mass_offline(self):
        service, outbox, _, sweeper = self._build(startup_time=T0 + timedelta(seconds=1000))
        assert sweeper.sweep_once(T0 + timedelta(seconds=1005)) == 0
        assert outbox.max_version() == 0

    def test_missing_agent_is_cleaned(self):
        service, outbox, registry, sweeper = self._build()
        registry.get_by_key_with_owner.return_value = None
        sweeper.sweep_once(T0 + timedelta(seconds=120))
        assert service.list_monitored() == []

    def test_pending_agent_health_transition_is_not_public(self):
        service, outbox, registry, sweeper = self._build()
        registry.get_by_key_with_owner.return_value.status = 'registered'
        sweeper.sweep_once(T0 + timedelta(seconds=120))
        assert service.status_of('agentA', 'orgA') == 'offline'
        assert outbox.max_version() == 0

    def test_offline_ttl_triggers_deregistration(self):
        _, _, registry, sweeper = self._build(offline_ttl=60)
        sweeper.sweep_once(T0 + timedelta(seconds=120))
        sweeper.sweep_once(T0 + timedelta(seconds=200))
        registry.deregister.assert_called_once_with("agentA", "orgA")


# ---------- events & outbox ----------

class TestEvents:
    def test_merge_registered_then_deregistered(self):
        reg = build_event(EventType.AGENT_REGISTERED, {"name": "a"}, 1)
        dereg = build_event(EventType.AGENT_DEREGISTERED, {"name": "a"}, 2)
        assert merge_events(reg, dereg) is dereg

    def test_merge_deregistered_then_registered_resets(self):
        dereg = build_event(EventType.AGENT_DEREGISTERED, {"name": "a"}, 1)
        reg = build_event(EventType.AGENT_REGISTERED, {"name": "a"}, 2)
        assert merge_events(dereg, reg) is reg

    def test_outbox_versioning_and_reconciliation(self):
        outbox = MemoryOutbox()
        e1 = outbox.append(build_event(EventType.AGENT_REGISTERED, {"name": "a"}, 0))
        e2 = outbox.append(build_event(EventType.AGENT_UPDATED, {"name": "a"}, 0))
        assert (e1.registry_version, e2.registry_version) == (1, 2)
        assert outbox.max_version() == 2
        page = outbox.list_after(1, 10)
        assert [e.event_id for e in page] == [e2.event_id]
        outbox.mark_status(e2.event_id, "dispatched")
        assert outbox.list_pending() == [e1]


class TestSubscriptionMatching:
    def test_type_org_tag_filters(self):
        sub = Subscription("s1", "https://cb", event_types=["AGENT_UPDATED"],
                           organizations=["acme"], tags=["prod"])
        matching = {"name": "a", "organization": "acme", "tags": ["prod"]}
        assert event_matches(build_event(EventType.AGENT_UPDATED, matching, 1), sub)
        assert not event_matches(build_event(EventType.AGENT_REGISTERED, matching, 1), sub)
        assert not event_matches(build_event(EventType.AGENT_UPDATED,
                                             {"name": "a", "organization": "other", "tags": ["prod"]}, 1), sub)
        assert not event_matches(build_event(EventType.AGENT_UPDATED,
                                             {"name": "a", "organization": "acme", "tags": ["dev"]}, 1), sub)


class TestFileSubscriptionStoreRoundtrip:
    def test_save_and_reload(self):
        import shutil
        base_dir = Path(__file__).parent / "_tmp_health_broadcast"
        base_dir.mkdir(parents=True, exist_ok=True)
        try:
            store = FileSubscriptionStore(str(base_dir / "subs.json"))
            created = store.create(Subscription("", "https://cb", secret="s3cret"))
            reloaded = FileSubscriptionStore(str(base_dir / "subs.json"))
            assert reloaded.get(created.subscription_id).secret == "s3cret"
        finally:
            shutil.rmtree(base_dir, ignore_errors=True)


# ---------- dispatcher ----------

class TestTokenBucket:
    def test_burst_and_refusal(self):
        bucket = TokenBucket(rate=50, capacity=100)
        assert bucket.try_consume(100) is True
        assert bucket.try_consume(1) is False


class TestSignPayload:
    def test_hmac_roundtrip(self):
        body = b'{"events": []}'
        signature = sign_payload("secret", "1234", body)
        expected = "sha256=" + hmac_mod.new(b"secret", b"1234." + body, hashlib.sha256).hexdigest()
        assert signature == expected


def _make_dispatcher(subscription, handler, **overrides):
    store = MemorySubscriptionStore()
    created = store.create(subscription)
    defaults = dict(debounce_window=0.01, max_events_per_second=1000.0,
                    webhook_timeout=1.0, max_retries=2,
                    backoff_base=0.01, backoff_max=0.02)
    defaults.update(overrides)
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    dispatcher = WebhookDispatcher(store, MemoryOutbox(), client=client, **defaults)
    return dispatcher, created


class TestDispatcherDelivery:
    @pytest.mark.asyncio
    async def test_successful_delivery_with_signature(self):
        received = {}

        def handler(request: httpx.Request) -> httpx.Response:
            received["headers"] = dict(request.headers)
            received["body"] = request.content
            return httpx.Response(200)

        dispatcher, created = _make_dispatcher(
            Subscription("", "https://cb.example.com/hook", secret="whsec"), handler)
        event = build_event(EventType.AGENT_UPDATED, {"name": "a", "organization": "acme"}, 7)
        assert await dispatcher._deliver_events(created, [event], persist=False) is True
        assert received["headers"]["x-registry-signature"] == \
            sign_payload("whsec", received["headers"]["x-registry-timestamp"], received["body"])
        payload = json.loads(received["body"])
        assert payload["subscription_id"] == created.subscription_id
        assert payload["events"][0]["registry_version"] == 7
        await dispatcher.stop()

    @pytest.mark.asyncio
    async def test_failed_delivery_after_retries(self):
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500)

        outbox = MemoryOutbox()
        store = MemorySubscriptionStore()
        created = store.create(Subscription("", "https://cb.example.com/hook"))
        client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        dispatcher = WebhookDispatcher(store, outbox, client=client, max_retries=1,
                                       backoff_base=0.01, backoff_max=0.02, webhook_timeout=1.0)
        event = outbox.append(build_event(EventType.AGENT_UPDATED, {"name": "a"}, 1))
        assert await dispatcher._deliver_events(created, [event], persist=True) is False
        outbox.mark_status(event.event_id, "delivery_failed")
        assert outbox.list_pending() == []
        await dispatcher.stop()

    @pytest.mark.asyncio
    async def test_debounce_batches_without_discarding_durable_event_ids(self):
        store = MemorySubscriptionStore()
        created = store.create(Subscription("", "https://cb.example.com/hook"))
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(200)))
        outbox = MemoryOutbox()
        dispatcher = WebhookDispatcher(store, outbox, client=client,
                                       debounce_window=0.01, max_events_per_second=1000.0)
        dispatcher.add_subscription(created)
        e1 = outbox.append(build_event(EventType.AGENT_REGISTERED, {"name": "a", "organization": "acme"}, 1))
        e2 = outbox.append(build_event(EventType.AGENT_UPDATED, {"name": "a", "organization": "acme"}, 2))
        dispatcher.submit(e1)
        dispatcher.submit(e2)
        dispatcher._flush_buffers()
        batch = dispatcher._workers[created.subscription_id].queue.get_nowait()
        assert [event.registry_version for event in batch] == [1, 2]
        assert [event.event_type for event in batch] == [EventType.AGENT_REGISTERED,
                                                       EventType.AGENT_UPDATED]
        await dispatcher.stop()


class TestSyncRequired:
    def test_build(self):
        event = build_sync_required(42)
        assert event.event_type is EventType.SYNC_REQUIRED
        assert event.data["since_version"] == 42


# ---------- endpoints ----------

def _install_services(monkeypatch, health_config=None, broadcast_config=None):
    health_service = HealthService(MemoryHeartbeatStore(), health_config or dict(HEALTH_CONFIG))
    broadcast_service = BroadcastService(MemoryOutbox(), MemorySubscriptionStore(),
                                         broadcast_config or dict(BROADCAST_CONFIG))
    monkeypatch.setattr(health_mod, "_service", health_service)
    monkeypatch.setattr(broadcast_mod, "_service", broadcast_service)
    return health_service, broadcast_service


def _override_registry():
    mock_registry = MagicMock()
    mock_registry.get_by_key_with_owner.return_value = _make_agent_record()
    mock_registry.get_agent_tags.return_value = []
    mock_registry.get_status.return_value = 'published'
    app.dependency_overrides[get_registry] = lambda: mock_registry
    return mock_registry


class TestHeartbeatEndpoint:
    @pytest.fixture(autouse=True)
    def heartbeat_ownership_off(self, monkeypatch):
        """Pin owner isolation off: these cases cover health tracking.

        The heartbeat endpoint enforces ownership when isolation is enabled (see
        tests/test_owner_identity.py), and the ambient etc/conf/server.conf may
        have it on — without this pin the suite would depend on local config.
        """
        monkeypatch.setattr('agent_registry.server.OWNER_ISOLATION_ENABLED', False)

    def test_first_heartbeat_returns_config_and_no_event(self, monkeypatch, auth_mock):
        _, broadcast_service = _install_services(monkeypatch)
        _override_registry()
        monkeypatch.setattr(
            "common.custom.custom_handle.HandlerRegistry.get_handler", lambda t: auth_mock)
        client = TestClient(app)
        response = client.post("/rest/v1/registry-center/agent-cards/TestOrg/TestAgent/heartbeat")
        assert response.status_code == 200
        body = response.json()
        assert body["heartbeat_enabled"] is True
        assert body["interval"] == 30
        assert body["health_status"] == "healthy"
        assert broadcast_service.outbox.max_version() == 0

    def test_unknown_agent_404(self, monkeypatch, auth_mock):
        _install_services(monkeypatch)
        mock_registry = _override_registry()
        mock_registry.get_by_key_with_owner.return_value = None
        monkeypatch.setattr(
            "common.custom.custom_handle.HandlerRegistry.get_handler", lambda t: auth_mock)
        client = TestClient(app)
        response = client.post("/rest/v1/registry-center/agent-cards/TestOrg/Ghost/heartbeat")
        assert response.status_code == 404

    def test_recovery_heartbeat_publishes_event(self, monkeypatch, auth_mock):
        health_service, broadcast_service = _install_services(monkeypatch)
        _override_registry()
        monkeypatch.setattr(
            "common.custom.custom_handle.HandlerRegistry.get_handler", lambda t: auth_mock)
        health_service.record_heartbeat("TestAgent", "TestOrg")
        health_service.store.update_status("TestAgent", "TestOrg", HealthStatus.SUSPECT, T0)
        client = TestClient(app)
        response = client.post("/rest/v1/registry-center/agent-cards/TestOrg/TestAgent/heartbeat")
        assert response.status_code == 200
        assert response.json()["health_status"] == "healthy"
        events = broadcast_service.outbox.list_after(0, 10)
        assert events[0].event_type is EventType.AGENT_HEALTH_CHANGED
        assert events[0].data["previous_health_status"] == "suspect"


class TestSubscriptionEndpoints:
    def test_create_list_delete(self, monkeypatch, auth_mock):
        _install_services(monkeypatch)
        _override_registry()
        monkeypatch.setattr(
            "common.custom.custom_handle.HandlerRegistry.get_handler", lambda t: auth_mock)
        client = TestClient(app)
        created = client.post("/rest/v1/registry-center/subscriptions", json={
            "callback_url": "https://operator.example.com/hook",
            "event_types": ["AGENT_UPDATED"],
            "filters": {"organizations": ["acme"]},
            "secret": "whsec",
        })
        assert created.status_code == 201
        sub_id = created.json()["subscription_id"]
        assert "secret" not in created.json()

        listed = client.get("/rest/v1/registry-center/subscriptions")
        assert listed.status_code == 200
        assert len(listed.json()["subscriptions"]) == 1

        deleted = client.delete(f"/rest/v1/registry-center/subscriptions/{sub_id}")
        assert deleted.status_code == 200
        missing = client.delete(f"/rest/v1/registry-center/subscriptions/{sub_id}")
        assert missing.status_code == 404

    def test_http_callback_rejected_by_default(self, monkeypatch, auth_mock):
        _install_services(monkeypatch)
        _override_registry()
        monkeypatch.setattr(
            "common.custom.custom_handle.HandlerRegistry.get_handler", lambda t: auth_mock)
        client = TestClient(app)
        response = client.post("/rest/v1/registry-center/subscriptions", json={
            "callback_url": "http://operator.example.com/hook",
        })
        assert response.status_code == 422


class TestChangesEndpoint:
    def test_pagination(self, monkeypatch, auth_mock):
        _, broadcast_service = _install_services(monkeypatch)
        _override_registry()
        monkeypatch.setattr(
            "common.custom.custom_handle.HandlerRegistry.get_handler", lambda t: auth_mock)
        for i in range(5):
            broadcast_service.outbox.append(
                build_event(EventType.AGENT_UPDATED, {"name": f"a{i}"}, 0))
        client = TestClient(app)
        page1 = client.get("/rest/v1/registry-center/changes",
                           params={"since": 0, "limit": 3}).json()
        assert len(page1["changes"]) == 3
        assert page1["has_more"] is True
        page2 = client.get("/rest/v1/registry-center/changes",
                           params={"since": page1["next_since"], "limit": 3}).json()
        assert len(page2["changes"]) == 2
        assert page2["has_more"] is False


class TestAdminHealthEndpoint:
    def test_lists_monitored_agents(self, monkeypatch, auth_mock):
        health_service, _ = _install_services(monkeypatch)
        health_service.record_heartbeat("A1", "org1")
        _override_registry()
        monkeypatch.setattr(
            "common.custom.custom_handle.HandlerRegistry.get_handler", lambda t: auth_mock)
        client = TestClient(app)
        response = client.get("/rest/v1/registry-center/agents/health")
        assert response.status_code == 200
        body = response.json()
        agents = body["agents"]
        assert len(agents) == 1
        assert agents[0]["health_status"] == "healthy"
        assert body["config"]["enabled"] is True
        assert body["config"]["interval"] == 30
        assert body["config"]["failure_threshold"] == 3
        assert body["config"]["grace_period"] == 10

        filtered = client.get("/rest/v1/registry-center/agents/health",
                              params={"status": "offline"})
        assert filtered.json()["agents"] == []


class TestHealthHistory:
    def test_service_records_transitions(self):
        service = HealthService(MemoryHeartbeatStore(), dict(HEALTH_CONFIG))
        service.record_heartbeat("A1", "org1")
        service.update_status("A1", "org1", HealthStatus.SUSPECT, T0)
        service.update_status("A1", "org1", HealthStatus.OFFLINE, T0 + timedelta(seconds=60))
        entries = service.history("A1", "org1")
        assert len(entries) == 2
        assert entries[0]["previous_health_status"] == "suspect"
        assert entries[0]["health_status"] == "offline"
        assert entries[1]["previous_health_status"] == "healthy"
        assert entries[1]["health_status"] == "suspect"
        # Recovery via heartbeat is recorded too.
        previous, _ = service.record_heartbeat("A1", "org1")
        assert previous is HealthStatus.OFFLINE
        entries = service.history("A1", "org1")
        assert len(entries) == 3
        assert entries[0]["health_status"] == "healthy"

    def test_history_endpoint(self, monkeypatch, auth_mock):
        health_service, _ = _install_services(monkeypatch)
        health_service.record_heartbeat("A1", "org1")
        health_service.update_status("A1", "org1", HealthStatus.SUSPECT, T0)
        _override_registry()
        monkeypatch.setattr(
            "common.custom.custom_handle.HandlerRegistry.get_handler", lambda t: auth_mock)
        client = TestClient(app)
        response = client.get("/rest/v1/registry-center/agents/health/history",
                              params={"name": "A1", "organization": "org1"})
        assert response.status_code == 200
        history = response.json()["history"]
        assert len(history) == 1
        assert history[0]["previous_health_status"] == "healthy"

    def test_history_endpoint_disabled_503(self, monkeypatch, auth_mock):
        _install_services(monkeypatch, health_config={"heartbeat.enabled": "false"})
        _override_registry()
        monkeypatch.setattr(
            "common.custom.custom_handle.HandlerRegistry.get_handler", lambda t: auth_mock)
        client = TestClient(app)
        response = client.get("/rest/v1/registry-center/agents/health/history")
        assert response.status_code == 503


class TestEventBusListeners:
    def test_listeners_receive_published_events(self):
        bus = EventBus(MemoryOutbox())
        seen = []
        bus.add_listener(seen.append)
        bus.publish(EventType.AGENT_HEALTH_CHANGED, {"name": "a"})
        assert len(seen) == 1
        assert seen[0].event_type is EventType.AGENT_HEALTH_CHANGED
        bus.remove_listener(seen.append)
        bus.publish(EventType.AGENT_REGISTERED, {"name": "b"})
        assert len(seen) == 1

    def test_failing_listener_does_not_break_publish(self):
        bus = EventBus(MemoryOutbox())
        bus.add_listener(lambda _e: 1 / 0)
        event = bus.publish(EventType.AGENT_UPDATED, {"name": "a"})
        assert event.data["name"] == "a"


class TestHealthStreamEndpoint:
    def test_stream_disabled_503(self, monkeypatch, auth_mock):
        _install_services(monkeypatch, health_config={"heartbeat.enabled": "false"})
        _override_registry()
        monkeypatch.setattr(
            "common.custom.custom_handle.HandlerRegistry.get_handler", lambda t: auth_mock)
        client = TestClient(app)
        response = client.get("/rest/v1/registry-center/agents/health/stream")
        assert response.status_code == 503

    @pytest.mark.asyncio
    async def test_stream_pushes_health_events(self, monkeypatch, auth_mock):
        from starlette.requests import Request as StarletteRequest
        from agent_registry.server import stream_agent_health

        _, broadcast_service = _install_services(monkeypatch)
        registry = _override_registry()
        monkeypatch.setattr(
            "common.custom.custom_handle.HandlerRegistry.get_handler", lambda t: auth_mock)

        scope = {
            "type": "http", "http_version": "1.1", "method": "GET",
            "path": "/rest/v1/registry-center/agents/health/stream",
            "query_string": b"", "headers": [], "client": ("test", 12345),
        }

        async def receive():
            return {"type": "http.request", "body": b"", "more_body": False}

        request = StarletteRequest(scope, receive=receive)
        response = await stream_agent_health(request, registry=registry, _=None)
        assert response.media_type == "text/event-stream"

        frames = []
        got_event = False
        try:
            async for chunk in response.body_iterator:
                text = chunk.decode("utf-8") if isinstance(chunk, bytes) else chunk
                frames.append(text)
                if ": connected" in text:
                    # Listener is live now — publish and expect the frame next.
                    broadcast_service.event_bus.publish(EventType.AGENT_HEALTH_CHANGED, {
                        "name": "A1", "organization": "org1",
                        "health_status": "offline", "previous_health_status": "suspect",
                    })
                if "event: health_changed" in text:
                    got_event = True
                    break
        finally:
            await response.body_iterator.aclose()
        assert got_event
        event_frame = [f for f in frames if "event: health_changed" in f][0]
        data_line = [l for l in event_frame.splitlines() if l.startswith("data: ")][0]
        payload = json.loads(data_line[len("data: "):])
        assert payload["event_type"] == "AGENT_HEALTH_CHANGED"
        assert payload["data"]["health_status"] == "offline"


def test_stream_queue_accepts_events_while_it_has_room():
    queue = asyncio.Queue(maxsize=2)

    _enqueue_drop_oldest(queue, "only")

    assert queue.get_nowait() == "only"


def test_stream_queue_drops_the_oldest_event_when_full():
    """A slow SSE consumer must not raise QueueFull inside the loop callback."""
    queue = asyncio.Queue(maxsize=2)

    for seq in range(5):
        _enqueue_drop_oldest(queue, seq)

    assert queue.qsize() == 2
    assert [queue.get_nowait(), queue.get_nowait()] == [3, 4]


@pytest.mark.asyncio
async def test_invalid_destination_url_fails_fast_without_retries():
    """httpx.InvalidURL is not an HTTPError/OSError/ValueError (verified), so it
    must be handled explicitly instead of escaping the batch worker."""
    attempts = []

    class RejectingClient:
        async def post(self, url, **kwargs):
            attempts.append(url)
            raise httpx.InvalidURL("Port out of range 0-65535")

    store = MemorySubscriptionStore()
    created = store.create(Subscription("", "https://cb.example.com/hook"))
    dispatcher = WebhookDispatcher(store, MemoryOutbox(), client=RejectingClient(),
                                   max_retries=2, backoff_base=0.01, backoff_max=0.02,
                                   webhook_timeout=1.0)
    event = build_event(EventType.AGENT_UPDATED, {"name": "a"}, 1)

    assert await dispatcher._deliver_events(created, [event], persist=False) is False
    assert len(attempts) == 1
