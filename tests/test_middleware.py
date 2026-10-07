# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
#
# SPDX-License-Identifier: Apache-2.0
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""Tests for connection and timeout middleware."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from fastapi import Request, status
from fastapi.responses import JSONResponse


class TestConnectionLimitMiddleware:
    """Tests for ConnectionLimitMiddleware."""

    @pytest.fixture
    def middleware(self):
        from agent_registry.middleware import ConnectionLimitMiddleware
        app = AsyncMock()
        return ConnectionLimitMiddleware(app, max_connections=2)

    def create_mock_request(self):
        request = MagicMock(spec=Request)
        return request

    @pytest.mark.asyncio
    async def test_within_limit(self, middleware):
        await middleware({'type': 'http'}, AsyncMock(), AsyncMock())
        middleware.app.assert_awaited_once()
        assert middleware.active_connections == 0

    @pytest.mark.asyncio
    async def test_at_limit_returns_503(self, middleware):
        # Exhaust the response quota.
        middleware.active_connections = 2
        send = AsyncMock()
        await middleware({'type': 'http'}, AsyncMock(), send)
        assert send.call_args_list[0].args[0]['status'] == 503
        middleware.app.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_exception_propagates_not_swallowed(self, middleware):
        middleware.app.side_effect = ValueError('Boom')
        with pytest.raises(ValueError, match="Boom"):
            await middleware({'type': 'http'}, AsyncMock(), AsyncMock())
        # Connection count should still be decremented
        assert middleware.active_connections == 0


class TestTimeoutMiddleware:
    """Tests for TimeoutMiddleware."""

    @pytest.fixture
    def middleware(self):
        from agent_registry.middleware import TimeoutMiddleware
        app = MagicMock()
        return TimeoutMiddleware(app, timeout_seconds=1)

    def create_mock_request(self):
        return MagicMock(spec=Request)

    @pytest.mark.asyncio
    async def test_completes_within_timeout(self, middleware):
        request = self.create_mock_request()
        async def call_next(r):
            await asyncio.sleep(0.01)
            return JSONResponse({"ok": True})
        response = await middleware.dispatch(request, call_next)
        assert response.status_code == 200

    @pytest.mark.asyncio
    async def test_timeout_returns_504(self, middleware):
        request = self.create_mock_request()
        async def call_next(r):
            await asyncio.sleep(2.0)
            return JSONResponse({"ok": True})
        response = await middleware.dispatch(request, call_next)
        assert response.status_code == 504
