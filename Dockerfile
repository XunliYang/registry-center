# Copyright (c) 2026 Huawei Technologies Co., Ltd.
# All Rights Reserved.
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

# OpenAN Registry Center Container Image
# Multi-stage build for OpenShift / Kubernetes deployment.
#
# Build:
#   podman build -t registry-center:latest .
#
# Mount deployment certificates/configuration at runtime, never at build time.
# See docs/container-deployment.md for secure and local-development examples.
# `init` validates preconfigured settings without stdin; it does not issue certs.

FROM python:3.12-slim AS builder

USER root

# Install build dependencies for packages that may need compilation
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libc6-dev \
    libpq-dev \
    libssl-dev \
    libffi-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt /tmp/requirements.txt

RUN python3 -m venv /opt/venv --copies \
    && . /opt/venv/bin/activate \
    && pip install --no-cache-dir -r /tmp/requirements.txt \
    && rm -rf /tmp/requirements.txt /root/.cache/pip

FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends bash libpq5 && rm -rf /var/lib/apt/lists/*

COPY --from=builder /opt/venv /opt/venv

ENV PATH="/opt/venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    REGISTRY_IP=0.0.0.0 \
    REGISTRY_PORT=8080 \
    REGISTRY_ENABLE_HTTPS=true \
    REGISTRY_VERIFY_CLIENT=true \
    REGISTRY_OWNER_ISOLATION_ENABLED=true \
    REGISTRY_FORWARDED_ALLOW_IPS="127.0.0.1" \
    REGISTRY_STARTUP_STRICT_IDENTITY=true

# An allowlist is a second boundary in addition to .dockerignore. Public
# templates, not the developer's mutable deployment files, seed the image.
COPY agent_registry/ /opt/registry-center/agent_registry/
COPY common/ /opt/registry-center/common/
COPY bin/entrypoint.sh /opt/registry-center/bin/entrypoint.sh
COPY etc/conf/server.conf.example /opt/registry-center/etc/conf/server.conf
COPY etc/conf/persistence.conf.example /opt/registry-center/etc/conf/persistence.conf
COPY etc/conf/server.properties etc/conf/log_config.conf /opt/registry-center/etc/conf/

RUN useradd --uid 10001 -m appuser \
    && ln -sf /opt/registry-center /opt/app \
    && mkdir -p /opt/registry-center/log /opt/registry-center/run /opt/registry-center/data \
    && mkdir -p /opt/registry-center/etc/ssl /opt/registry-center/etc/sign_cert \
    && sed -i 's/\r$//' /opt/registry-center/bin/entrypoint.sh \
    && chmod 0755 /opt/registry-center/bin/entrypoint.sh \
    && chmod 0600 /opt/registry-center/etc/conf/*.conf \
    && chown -R appuser:appuser /opt/registry-center /opt/venv

WORKDIR /opt/registry-center

USER appuser

EXPOSE 8080

ENTRYPOINT ["/opt/registry-center/bin/entrypoint.sh"]
CMD ["serve"]
