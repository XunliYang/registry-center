# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# SPDX-License-Identifier: Apache-2.0
"""Real HTTP/HTTPS startup with missing integration keys supplied by environment.

Run: python tests/integration_config_env_smoke.py
Synthetic configuration and certificates only; no actual deployment files/IAM.
Uses the same real start.main launcher as the OAuth integration smoke.
"""
import json
import os
from pathlib import Path
import ssl
import subprocess
import sys
import tempfile
import time

import httpx

from integration_oauth_smoke import REPO, configure, free_port, override_config

sys.path.insert(0, str(REPO))
from common.util import app_config


def run():
    parent = Path(tempfile.mkdtemp(prefix='registry-config-env-smoke-'))
    results = []
    for https in (False, True):
        root = parent / ('https' if https else 'http')
        main_port, integration_port = free_port(), free_port()
        configure(root, main_port, integration_port, free_port(), True, 'synthetic-validator-secret')
        directory = root / 'etc/conf'
        for template in ('server.conf.example', 'persistence.conf.example'):
            (directory / template).write_bytes((REPO / 'etc/conf' / template).read_bytes())
        # File intentionally specifies the opposite TLS mode: only the documented
        # canonical environment override can select the listener tested below.
        override_config(root, {'enable_https': str(not https).lower()})
        values = {}
        app_config.load_configs(str(directory / 'server.conf'), values)
        overrides = {app_config.canonical_env_name(k): v for k, v in values.items()
                     if k.startswith('integration.')}
        path = directory / 'server.conf'
        lines = [line for line in path.read_text(encoding='utf-8').splitlines()
                 if not line.strip().startswith('integration.')]
        path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
        original = path.read_bytes()
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(('REGISTRY_', 'LLM_'))}
        env.update(overrides)
        env.update(REGISTRY_ENABLE_HTTPS=str(https).lower(), PYTHONPATH=str(REPO),
                   PYTHONUNBUFFERED='1', LLM_CONFIG_FILE=str(root / 'absent-models.yaml'))
        context = ssl.create_default_context(cafile=str(root / 'ca.cer'))
        # CA and hostname verification remain enabled for both TLS listeners.
        main_url = f'{"https://localhost" if https else "http://127.0.0.1"}:{main_port}'
        integration_url = f'https://localhost:{integration_port}/integration/v1'
        with (root / 'service.log').open('w', encoding='utf-8') as log:
            proc = subprocess.Popen([sys.executable, str(REPO / 'tests/integration_oauth_smoke.py'),
                '--serve', str(root)], cwd=root, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                with httpx.Client(verify=context, trust_env=False, timeout=3) as client:
                    deadline = time.monotonic() + 40
                    while time.monotonic() < deadline:
                        if proc.poll() is not None:
                            raise RuntimeError('Configuration-profile startup failed; inspect ' + str(root / 'service.log'))
                        try:
                            response = client.get(main_url + '/rest/v1/registry-center/agent-cards')
                            if response.status_code == 200:
                                break
                        except httpx.HTTPError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise RuntimeError('Configuration-profile startup timed out')
                    assert response.status_code == 200
                    # Missing integration.enabled/auth.* in the deployment file
                    # must not silently disable the TLS listener or its auth.
                    assert client.get(integration_url + '/agent-cards').status_code == 401
                    # Authentication rejection (not disabled endpoint's 404) also
                    # proves missing integration.token.enabled/endpoint were read.
                    assert client.post(integration_url + '/oauth2/token',
                        data={'grant_type': 'client_credentials'}).status_code == 401
                    assert path.read_bytes() == original
                    results.append({'https': https, 'main_status': response.status_code,
                                    'integration_auth': 401, 'token_auth': 401,
                                    'deployment_file_unchanged': True})
            finally:
                if proc.poll() is None:
                    proc.terminate()
                try:
                    proc.wait(timeout=12)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
    (parent / 'observations.json').write_text(json.dumps(results, indent=2), encoding='utf-8')
    print('ARTIFACT_ROOT=' + str(parent))
    print('PASS HTTP and verified HTTPS; missing integration keys injected; '
          'authenticated listener and token route remain enabled; deployment files unchanged')


if __name__ == '__main__':
    run()
