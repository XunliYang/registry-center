# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Two Uvicorn listeners must not re-enable raw integration query logs."""
import logging
import subprocess
import sys
import textwrap

from agent_registry.integration.listener import _IntegrationAccessLogFilter


def record(path):
    return logging.LogRecord('uvicorn.access', logging.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d', ('127.0.0.1', 'POST', path, '1.1', 400), None)


def test_filter_redacts_queries_on_all_paths_but_keeps_path_logs():
    guard = _IntegrationAccessLogFilter()
    for path in ('/integration/v1/oauth2/token', '/integration/v1/agent-cards',
                 '/wrong-token-path', '/proxy/integration/v1/oauth2/token'):
        event = record(path + '?client_secret=synthetic-secret')
        assert guard.filter(event)
        assert event.args[2] == path
        assert 'synthetic-secret' not in event.getMessage()
    assert guard.filter(record('/rest/v1/registry-center/agent-cards'))


def test_filter_survives_main_listener_logging_reconfiguration():
    # dictConfig closes existing handlers globally; run its real reset in a child
    # rather than interfering with pytest captures or other tests' file writers.
    code = textwrap.dedent('''
        import logging
        import logging.config
        import uvicorn
        from agent_registry.integration.listener import _IntegrationAccessLogFilter
        logging.config.dictConfig(uvicorn.config.LOGGING_CONFIG)
        logger = logging.getLogger('uvicorn.access')
        assert any(isinstance(f, _IntegrationAccessLogFilter) for f in logger.filters)
        def record(path):
            return logging.LogRecord('uvicorn.access', logging.INFO, '', 1,
                '%s - "%s %s HTTP/%s" %d', ('peer', 'POST', path, '1.1', 400), None)
        event = record('/integration/v1/oauth2/token?client_secret=synthetic-secret')
        assert logger.filter(event)
        assert 'synthetic-secret' not in event.getMessage()
        assert logger.filter(record('/rest/v1/registry-center/agent-cards'))
    ''')
    result = subprocess.run([sys.executable, '-c', code], capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
