# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0

import pytest
import httpx

from agent_registry.broadcast.callback_policy import validate_callback_destination
from agent_registry.broadcast.dispatcher import WebhookDispatcher
from agent_registry.broadcast.events import EventType, build_event
from agent_registry.broadcast.outbox import MemoryOutbox
from agent_registry.broadcast.subscriptions import MemorySubscriptionStore, Subscription


@pytest.mark.parametrize('url,config', [
    ('https://operator.example/hook', {}),
    ('https://other.example/hook', {'broadcast.callback.allowlist': 'operator.example'}),
    ('https://user:pass@operator.example/hook', {'broadcast.callback.allowlist': 'operator.example'}),
    ('http://operator.example/hook', {'broadcast.callback.allowlist': 'operator.example'}),
    ('https://operator.example:99999/hook', {'broadcast.callback.allowlist': 'operator.example'}),
])
def test_unapproved_destination_is_rejected(url, config):
    with pytest.raises(ValueError):
        validate_callback_destination(url, config)


def test_business_can_explicitly_authorize_an_internal_callback():
    validate_callback_destination('https://10.0.0.8/hook', {'broadcast.callback.allowlist': '10.0.0.8'})


@pytest.mark.asyncio
async def test_revoked_persisted_destination_makes_no_request(monkeypatch):
    monkeypatch.setattr('agent_registry.broadcast.callback_policy.get_conf', lambda: {})
    requests = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: requests.append(request) or httpx.Response(200)))
    dispatcher = WebhookDispatcher(MemorySubscriptionStore(), MemoryOutbox(), client=client)
    sub = Subscription('sub', 'https://operator.example/hook')
    event = build_event(EventType.AGENT_UPDATED, {'name': 'a'}, 1)
    try:
        assert not await dispatcher._deliver_events(sub, [event], persist=False)
        assert requests == []
    finally:
        await client.aclose()


@pytest.mark.asyncio
async def test_webhook_never_follows_redirects(monkeypatch):
    monkeypatch.setattr('agent_registry.broadcast.callback_policy.get_conf', lambda: {
        'broadcast.callback.allowlist': 'operator.example'})
    requests = []
    def redirect(request):
        requests.append(str(request.url))
        return httpx.Response(302, headers={'Location': 'https://internal.example/target'})
    async with httpx.AsyncClient(transport=httpx.MockTransport(redirect), follow_redirects=True) as client:
        dispatcher = WebhookDispatcher(MemorySubscriptionStore(), MemoryOutbox(), client=client, max_retries=0)
        assert not await dispatcher._deliver_events(Subscription('sub', 'https://operator.example/hook'),
            [build_event(EventType.AGENT_UPDATED, {}, 1)], persist=False)
    assert requests == ['https://operator.example/hook']
