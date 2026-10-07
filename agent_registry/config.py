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

import platform

from common.util.app_config import get_conf, get_persistence_conf

# Platform detection
IS_WINDOWS = platform.system() == "Windows"

PERSISTENCE_CONF = get_persistence_conf()
PERSISTENCE_MODE = PERSISTENCE_CONF.get("persistence.mode", "file")
PERSISTENCE_FILE = "agentcard.json"
PERSISTENCE_METADATA_FILE = "agentregistry.json"
PERSISTENCE_TAGS_FILE = "tags.json"
USE_VECTORDB = str(get_conf().get("use_vectordb", False)).lower() == 'true'
COLLECTION_NAME = "agent_card_collection"
MAX_REQUEST_BODY_SIZE = 1024 * 1024  # 1MB default limit
MAX_URL_LENGTH = 1024  # 1KB default limit
# Maximum file size limit: 100MB
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024
TLS_CIPHER = "tls.cipher"
CONN_TIMEOUT = "connection.timeout"
CONN_MAX = "connection.max"
FLOW_CTL_REGISTER = "flowcontrol.ratelimit.register"
FLOW_CTL_PARALLEL_REGISTER = "flowcontrol.parallelism.register"

FLOW_CTL_QUERY = "flowcontrol.ratelimit.query"
FLOW_CTL_PARALLEL_QUERY = "flowcontrol.parallelism.query"

FLOW_CTL_UPDATE = "flowcontrol.ratelimit.update"
FLOW_CTL_PARALLEL_UPDATE = "flowcontrol.parallelism.update"

FLOW_CTL_GET = "flowcontrol.ratelimit.get"
FLOW_CTL_PARALLEL_GET = "flowcontrol.parallelism.get"

FLOW_CTL_RETRIEVE = "flowcontrol.ratelimit.retrieve"
FLOW_CTL_PARALLEL_RETRIEVE = "flowcontrol.parallelism.retrieve"

FLOW_CTL_DEREGISTER = "flowcontrol.ratelimit.deregister"
FLOW_CTL_PARALLEL_DEREGISTER = "flowcontrol.parallelism.deregister"

FLOW_CTL_JWK = "flowcontrol.ratelimit.jwk"
FLOW_CTL_PARALLEL_JWK = "flowcontrol.parallelism.jwk"

AGENT_NUM_MAX = "agent.num.max"
FORWARDED_ALLOW_IPS = "forwarded_allow_ips"
TAG_MAX_COUNT = "tag.max.count"
TAG_MAX_LENGTH = "tag.max.length"

OWNER_ISOLATION_ENABLED = str(get_conf().get("owner.isolation.enabled", "false")).lower() == 'true'
OWNER_VALIDATION_MODE = get_conf().get("owner.validation.mode", "strict")
# Identity source for ownership enforcement: certificate | trusted_proxy | none.
# A bare X-SSL-Client-DN header is only honoured in trusted_proxy mode, and only
# for connections whose direct peer is listed in owner.trusted.proxy.ips.
OWNER_IDENTITY_MODE = "owner.identity.mode"
OWNER_TRUSTED_PROXY_IPS = "owner.trusted.proxy.ips"

# ---------- Heartbeat detection ----------
# Keys use dot-separated names so REGISTRY_* env overrides map cleanly.
HEARTBEAT_ENABLED = "heartbeat.enabled"
HEARTBEAT_INTERVAL = "heartbeat.interval"
HEARTBEAT_FAILURE_THRESHOLD = "heartbeat.failure.threshold"
HEARTBEAT_GRACE_PERIOD = "heartbeat.grace.period"
HEARTBEAT_SWEEP_INTERVAL = "heartbeat.sweep.interval"
HEARTBEAT_OFFLINE_TTL = "heartbeat.offline.ttl"
HEARTBEAT_HIDE_UNHEALTHY_RESULTS = "heartbeat.hide.unhealthy.results"
FLOW_CTL_HEARTBEAT = "flowcontrol.ratelimit.heartbeat"
FLOW_CTL_SUBSCRIPTION = "flowcontrol.ratelimit.subscription"
FLOW_CTL_PARALLEL_HEARTBEAT = "flowcontrol.parallelism.heartbeat"
FLOW_CTL_PARALLEL_SUBSCRIPTION = "flowcontrol.parallelism.subscription"

# ---------- Change broadcast ----------
BROADCAST_ENABLED = "broadcast.enabled"
BROADCAST_DEBOUNCE_WINDOW = "broadcast.debounce.window"
BROADCAST_MAX_EVENTS_PER_SECOND = "broadcast.max.events.per.second"
BROADCAST_WEBHOOK_TIMEOUT = "broadcast.webhook.timeout"
BROADCAST_WEBHOOK_MAX_RETRIES = "broadcast.webhook.max.retries"
BROADCAST_WEBHOOK_BACKOFF_BASE = "broadcast.webhook.backoff.base"
BROADCAST_WEBHOOK_BACKOFF_MAX = "broadcast.webhook.backoff.max"
BROADCAST_OUTBOX_RETENTION_DAYS = "broadcast.outbox.retention.days"
BROADCAST_ALLOW_HTTP_CALLBACKS = "broadcast.allow.http.callbacks"
BROADCAST_CALLBACK_ALLOWLIST = "broadcast.callback.allowlist"
