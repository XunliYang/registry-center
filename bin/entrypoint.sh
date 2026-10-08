#!/bin/bash
set -e
umask 077

# Container entrypoint. Every variable below is optional: a variable that is
# unset or empty leaves the value already present in the shipped .conf files
# untouched. Malformed values exit before the service starts (exit 2 for
# arguments, ports and owner mode; exit 1 for an unusable model configuration).
#
#   APP_HOME                         install root, default /opt/registry-center
#   REGISTRY_IP                      -> server.conf IP
#   PORT                             platform port (Cloud Run); wins over REGISTRY_PORT
#   REGISTRY_PORT                    -> server.conf PORT (1..65535)
#   REGISTRY_ENABLE_HTTPS            -> server.conf enable_https
#   REGISTRY_FORWARDED_ALLOW_IPS     -> server.conf forwarded_allow_ips (bare value)
#   REGISTRY_OWNER_VALIDATION_MODE   -> server.conf owner.validation.mode
#   REGISTRY_OWNER__VALIDATION__MODE    deprecated alias of the previous line
#   PERSISTENCE_MODE                 -> persistence.conf persistence.mode
#   DB_HOST DB_PORT DB_NAME DB_USERNAME DB_PASSWORD DB_POOL_MIN DB_POOL_MAX
#                                    -> the active backend's keys in persistence.conf
#   DB_CONNECT_TIMEOUT               -> gauss/mysql connect_timeout only
#   LLM_CHAT_MODEL LLM_CHAT_URL LLM_CHAT_PROVIDER LLM_CHAT_API_KEY
#                                    -> generate etc/config/models.yaml [chat] when
#                                       no models.yaml file exists
#
# Any other REGISTRY_* variable is deliberately not rewritten into a file here:
# common/util/app_config.py applies REGISTRY_* overrides to the loaded
# configuration itself (REGISTRY_FOO_BAR -> foo.bar or foobar). Overrides only
# take effect for keys that already exist in server.conf or server.properties,
# and only when the underscored variable name maps back onto the key exactly:
# a key containing an underscore after a dot (integration.auth.static.hmac_key,
# integration.auth.fingerprint_key, integration.oauth2.client_id, jwks_uri,
# ca_file, integration.token.allowed_scopes, knowledge_graph.ratelimit,
# audit.mysql.batch_size ...) is NOT reachable this way, because every
# underscore becomes a dot and the value lands on a name no consumer reads.
# Configure those keys in the file, or through the ${ENV_VAR} placeholders the
# file already contains (INTEGRATION_FINGERPRINT_KEY, INTEGRATION_TOKEN_HMAC_KEY,
# OAUTH_INTROSPECTION_CLIENT_ID/SECRET, AUDIT_MYSQL_*, ...), not with REGISTRY_*.

# Reject unsupported serve flags before touching configuration. Additional
# commands retain the standard container pass-through behavior.
if [ "$#" -eq 0 ]; then
    set -- serve
fi
if [ "$1" = "serve" ] && [ "$#" -ne 1 ]; then
    echo "serve accepts no arguments; configure PORT/REGISTRY_* environment variables instead" >&2
    exit 2
fi

APP_HOME="${APP_HOME:-/opt/registry-center}"
cd "$APP_HOME"

export PATH="/opt/venv/bin:$PATH"

# ─────────────────────────────────────────────────────────────────────
# Container conventions → configuration consumed by all application loaders.
# REGISTRY_* is also read by Python, so normalize aliases before writing files.
# ─────────────────────────────────────────────────────────────────────

SERVER_CONF="etc/conf/server.conf"
PERSISTENCE_CONF="etc/conf/persistence.conf"
MODELS_CONF="etc/config/models.yaml"

# Escape sed metacharacters (&, backslash, delimiter #) in override values so
# credentials or hosts containing them don't corrupt the config file.
sed_escape() {
    printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/[&#]/\\&/g'
}

# --- server.conf overrides (using # as sed delimiter to handle paths safely) ---
# Each block rewrites one existing line of server.conf; a key that the shipped
# server.conf.example does not contain is not added, and the file must be
# writable by the container user.
if [ -n "${REGISTRY_IP}" ]; then
    sed -i "s#^IP=.*#IP=${REGISTRY_IP}#" "${SERVER_CONF}"
    echo "Config override: IP=${REGISTRY_IP}"
fi

# Cloud Run injects PORT env var
# PORT is a platform convention, not a REGISTRY_* override: it is normalized into
# REGISTRY_PORT so the application and the health probe agree. An invalid value
# (non-numeric, 0, >65535) exits 2 instead of falling back to a default.
if [ -n "${PORT}" ]; then
    export REGISTRY_PORT="${PORT}"
fi
if [ -n "${REGISTRY_PORT}" ]; then
    if ! [[ "${REGISTRY_PORT}" =~ ^[0-9]{1,5}$ ]] ||
        [ "$((10#${REGISTRY_PORT}))" -lt 1 ] || [ "$((10#${REGISTRY_PORT}))" -gt 65535 ]; then
        echo "PORT/REGISTRY_PORT must be an integer between 1 and 65535" >&2
        exit 2
    fi
    export REGISTRY_PORT="$((10#${REGISTRY_PORT}))"
fi
if [ -n "${PORT}" ]; then
    sed -i "s#^PORT=.*#PORT=${PORT}#" "${SERVER_CONF}"
    echo "Config override: PORT=${PORT} (Cloud Run)"
elif [ -n "${REGISTRY_PORT}" ]; then
    # Leading zeros are normalized away (10#) so the value stays a valid port.
    sed -i "s#^PORT=.*#PORT=${REGISTRY_PORT}#" "${SERVER_CONF}"
    echo "Config override: PORT=${REGISTRY_PORT}"
fi

# Feature switch only. Setting this to false does NOT disable caller
# authentication, owner isolation or signature validation; those stay under
# their own switches (see the REGISTRY_* variables in the header).
if [ -n "${REGISTRY_ENABLE_HTTPS}" ]; then
    sed -i "s#^enable_https=.*#enable_https=${REGISTRY_ENABLE_HTTPS}#" "${SERVER_CONF}"
    echo "Config override: enable_https=${REGISTRY_ENABLE_HTTPS}"
fi

if [ -n "${REGISTRY_FORWARDED_ALLOW_IPS}" ]; then
    # No quotes around the value: the config reader strips whitespace only, and
    # uvicorn compares each trusted-proxy entry literally.
    # Comma-separated. Unset keeps the template value (127.0.0.1); an empty
    # value trusts no proxy; "*" trusts every proxy and is only appropriate when
    # the platform front end is the sole route to the service (deploy-all.ps1).
    sed -i "s#^forwarded_allow_ips=.*#forwarded_allow_ips=${REGISTRY_FORWARDED_ALLOW_IPS}#" "${SERVER_CONF}"
    echo "Config override: forwarded_allow_ips=${REGISTRY_FORWARDED_ALLOW_IPS}"
fi

# REGISTRY_OWNER_VALIDATION_MODE maps to owner.validation.mode through the
# REGISTRY_* env overrides in common/util/app_config.py. The legacy
# double-underscore spelling is still accepted here so existing deployments keep
# working when they override the image default.
# Because this block only rewrites an existing line, a value of "strict" survives
# the image default: the image ships no owner-mode variable of its own.
# Any value other than strict|relaxed exits 2.
OWNER_VALIDATION_MODE_OVERRIDE="${REGISTRY_OWNER_VALIDATION_MODE:-${REGISTRY_OWNER__VALIDATION__MODE}}"
if [ -n "${OWNER_VALIDATION_MODE_OVERRIDE}" ]; then
    case "${OWNER_VALIDATION_MODE_OVERRIDE}" in
        strict|relaxed) ;;
        *) echo "owner validation mode must be strict or relaxed" >&2; exit 2 ;;
    esac
    export REGISTRY_OWNER_VALIDATION_MODE="${OWNER_VALIDATION_MODE_OVERRIDE}"
    sed -i "s#^owner.validation.mode=.*#owner.validation.mode=${OWNER_VALIDATION_MODE_OVERRIDE}#" "${SERVER_CONF}"
    echo "Config override: owner.validation.mode=${OWNER_VALIDATION_MODE_OVERRIDE}"
fi
if [ -n "${REGISTRY_OWNER__VALIDATION__MODE}" ]; then
    echo "REGISTRY_OWNER__VALIDATION__MODE is deprecated; REGISTRY_OWNER_VALIDATION_MODE takes precedence when both are set" >&2
    unset REGISTRY_OWNER__VALIDATION__MODE
fi

# TLS may terminate at a trusted reverse proxy. Disabling listener HTTPS must
# not disable independent caller authorization or AgentCard integrity checks.
# Development deployments explicitly choose their own policy via REGISTRY_*.
#
# Not rewritten here (the image already supplies a value and Python applies
# REGISTRY_* overrides itself): REGISTRY_VERIFY_CLIENT -> verify_client,
# REGISTRY_OWNER_ISOLATION_ENABLED -> owner.isolation.enabled,
# REGISTRY_AGENT_APPROVAL_ENABLED -> agent_approval_enabled,
# REGISTRY_SIGNATURE_VALIDATION_ENABLED -> signature_validation_enabled,
# REGISTRY_REGISTRY_SIGN_ENABLED -> registry.sign.enabled,
# REGISTRY_STARTUP_STRICT_IDENTITY -> startup.strict.identity.

# --- persistence.conf overrides (using # to handle /cloudsql/ paths safely) ---
# PERSISTENCE_MODE selects which backend block below is rewritten; the value
# (file/postgresql/sqlite/gauss/mysql) is validated by the storage layer.
if [ -n "${PERSISTENCE_MODE}" ]; then
    sed -i "s#^persistence.mode=.*#persistence.mode=${PERSISTENCE_MODE}#" "${PERSISTENCE_CONF}"
    echo "Config override: persistence.mode=${PERSISTENCE_MODE}"
fi

# DB_HOST also accepts a Cloud SQL Unix socket path (/cloudsql/<project>:<region>:<instance>).
# DB_PASSWORD is written verbatim (shell metacharacters escaped); an enc:v1: value
# is decrypted at load time using REGISTRY_CIPHER_KEY / REGISTRY_CIPHER_KEY_FILE.
if [ -n "${DB_HOST}" ]; then
    sed -i "s#^postgresql.host=.*#postgresql.host=$(sed_escape "${DB_HOST}")#" "${PERSISTENCE_CONF}"
    echo "Config override: postgresql.host=${DB_HOST}"
fi

if [ -n "${DB_PORT}" ]; then
    sed -i "s#^postgresql.port=.*#postgresql.port=$(sed_escape "${DB_PORT}")#" "${PERSISTENCE_CONF}"
    echo "Config override: postgresql.port=${DB_PORT}"
fi

if [ -n "${DB_NAME}" ]; then
    sed -i "s#^postgresql.name=.*#postgresql.name=$(sed_escape "${DB_NAME}")#" "${PERSISTENCE_CONF}"
    echo "Config override: postgresql.name=${DB_NAME}"
fi

if [ -n "${DB_USERNAME}" ]; then
    sed -i "s#^postgresql.username=.*#postgresql.username=$(sed_escape "${DB_USERNAME}")#" "${PERSISTENCE_CONF}"
    echo "Config override: postgresql.username=${DB_USERNAME}"
fi

if [ -n "${DB_PASSWORD}" ]; then
    sed -i "s#^postgresql.password=.*#postgresql.password=$(sed_escape "${DB_PASSWORD}")#" "${PERSISTENCE_CONF}"
    echo "Config override: postgresql.password=***"
fi

if [ -n "${DB_POOL_MIN}" ]; then
    sed -i "s#^postgresql.pool.min=.*#postgresql.pool.min=$(sed_escape "${DB_POOL_MIN}")#" "${PERSISTENCE_CONF}"
    echo "Config override: postgresql.pool.min=${DB_POOL_MIN}"
fi

if [ -n "${DB_POOL_MAX}" ]; then
    sed -i "s#^postgresql.pool.max=.*#postgresql.pool.max=$(sed_escape "${DB_POOL_MAX}")#" "${PERSISTENCE_CONF}"
    echo "Config override: postgresql.pool.max=${DB_POOL_MAX}"
fi

# --- generic DB_* override for gauss / mysql modes (postgresql handled above) ---
# The gauss/mysql blocks in persistence.conf resolve ${GAUSS_*} / ${MYSQL_*}
# env vars natively; DB_* is the container convention shared with the
# postgresql block above.
# Only the values listed below are carried over. The remaining backend-specific
# settings keep their persistence.conf placeholder or ${VAR:default} value:
#   sqlite.path       <- SQLITE_PATH (default data/agents.db)
#   *.connect_timeout <- PG_CONNECT_TIMEOUT for postgresql (default 10);
#                        DB_CONNECT_TIMEOUT is honoured for gauss/mysql only
#   audit.mysql.*     <- AUDIT_MYSQL_* (defaults in persistence.conf.example)
# These blocks run only when PERSISTENCE_MODE is exactly "gauss" or "mysql", so
# switching a running container between backends requires the mode to be set.
if [ "${PERSISTENCE_MODE}" = "gauss" ] || [ "${PERSISTENCE_MODE}" = "mysql" ]; then
    _db_prefix="${PERSISTENCE_MODE}"
    _db_name_key="name"
    if [ "${PERSISTENCE_MODE}" = "gauss" ]; then
        _db_name_key="database"
    fi

    if [ -n "${DB_HOST}" ]; then
        sed -i "s#^${_db_prefix}.host=.*#${_db_prefix}.host=$(sed_escape "${DB_HOST}")#" "${PERSISTENCE_CONF}"
        echo "Config override: ${_db_prefix}.host=${DB_HOST}"
    fi

    if [ -n "${DB_PORT}" ]; then
        sed -i "s#^${_db_prefix}.port=.*#${_db_prefix}.port=$(sed_escape "${DB_PORT}")#" "${PERSISTENCE_CONF}"
        echo "Config override: ${_db_prefix}.port=${DB_PORT}"
    fi

    if [ -n "${DB_NAME}" ]; then
        sed -i "s#^${_db_prefix}.${_db_name_key}=.*#${_db_prefix}.${_db_name_key}=$(sed_escape "${DB_NAME}")#" "${PERSISTENCE_CONF}"
        echo "Config override: ${_db_prefix}.${_db_name_key}=${DB_NAME}"
    fi

    if [ -n "${DB_USERNAME}" ]; then
        sed -i "s#^${_db_prefix}.username=.*#${_db_prefix}.username=$(sed_escape "${DB_USERNAME}")#" "${PERSISTENCE_CONF}"
        echo "Config override: ${_db_prefix}.username=${DB_USERNAME}"
    fi

    if [ -n "${DB_PASSWORD}" ]; then
        sed -i "s#^${_db_prefix}.password=.*#${_db_prefix}.password=$(sed_escape "${DB_PASSWORD}")#" "${PERSISTENCE_CONF}"
        echo "Config override: ${_db_prefix}.password=***"
    fi

    if [ -n "${DB_POOL_MIN}" ]; then
        sed -i "s#^${_db_prefix}.pool.min=.*#${_db_prefix}.pool.min=$(sed_escape "${DB_POOL_MIN}")#" "${PERSISTENCE_CONF}"
        echo "Config override: ${_db_prefix}.pool.min=${DB_POOL_MIN}"
    fi

    if [ -n "${DB_POOL_MAX}" ]; then
        sed -i "s#^${_db_prefix}.pool.max=.*#${_db_prefix}.pool.max=$(sed_escape "${DB_POOL_MAX}")#" "${PERSISTENCE_CONF}"
        echo "Config override: ${_db_prefix}.pool.max=${DB_POOL_MAX}"
    fi

    if [ -n "${DB_CONNECT_TIMEOUT}" ]; then
        sed -i "s#^${_db_prefix}.connect_timeout=.*#${_db_prefix}.connect_timeout=$(sed_escape "${DB_CONNECT_TIMEOUT}")#" "${PERSISTENCE_CONF}"
        echo "Config override: ${_db_prefix}.connect_timeout=${DB_CONNECT_TIMEOUT}"
    fi
fi

# --- models.yaml generation (LLM model definitions) ---
# models.yaml is local configuration and is not shipped in the image. A platform
# that can only supply environment variables gets the chat entry built here; a
# file that already exists (for example one bind-mounted by Docker Compose) is
# left untouched. The key itself is never written: api_key_env only names the
# variable that holds it. Only the chat capability is generated this way;
# semantic search needs an embed entry from a complete models.yaml.
#
# Precedence: an existing models.yaml always wins over these variables.
# LLM_CHAT_PROVIDER accepts openai or openai_compatible (default
# openai_compatible); any other provider name exits 1 because the generated entry
# cannot express it. Setting LLM_CHAT_MODEL/URL is all-or-nothing: providing just
# one of them (or only LLM_CHAT_API_KEY) exits 1.
if [ -f "${MODELS_CONF}" ]; then
    echo "Model config: using existing ${MODELS_CONF}"
elif [ -n "${LLM_CHAT_MODEL}" ] && [ -n "${LLM_CHAT_URL}" ]; then
    case "${LLM_CHAT_PROVIDER:-openai_compatible}" in
        openai|openai_compatible) ;;
        *) echo "LLM_CHAT_PROVIDER=${LLM_CHAT_PROVIDER} cannot be generated from the simplified environment settings; provide a complete models.yaml" >&2; exit 1 ;;
    esac
    mkdir -p "$(dirname "${MODELS_CONF}")"
    python3 -c "
import os, yaml
chat = {
    'provider': 'openai_compatible',
    'model': os.environ['LLM_CHAT_MODEL'],
    'url': os.environ['LLM_CHAT_URL'],
}
if os.environ.get('LLM_CHAT_API_KEY'):
    chat['api_key_env'] = 'LLM_CHAT_API_KEY'
with open('${MODELS_CONF}', 'w') as f:
    yaml.safe_dump({'models': {'chat': chat}}, f, sort_keys=False)
"
    echo "Config override: models.yaml[chat] generated from environment variables"
elif [ -n "${LLM_CHAT_MODEL}" ] || [ -n "${LLM_CHAT_URL}" ] || [ -n "${LLM_CHAT_API_KEY}" ]; then
    echo "Incomplete chat model configuration: set both LLM_CHAT_MODEL and LLM_CHAT_URL" >&2
    exit 1
fi

# Ensure run/ directory exists for internal UDS service
# (etc/conf/integration_credentials.conf and the integration.* keys are NOT
# created or rewritten here; provide the credential file by mounting it and the
# secrets through the placeholders server.conf already uses:
# INTEGRATION_FINGERPRINT_KEY, INTEGRATION_TOKEN_HMAC_KEY,
# OAUTH_INTROSPECTION_CLIENT_ID, OAUTH_INTROSPECTION_CLIENT_SECRET.)
mkdir -p run

# init validates the configuration written above without reading stdin and
# without issuing certificates. Anything else is executed verbatim (pass-through).
if [ "${1}" = "init" ]; then
    shift
    echo "Running registry-center initialization (non-interactive)..."
    exec python -m agent_registry.init --non-interactive "$@"
fi

if [ "${1}" = "serve" ]; then
    echo "Starting registry-center service..."
    exec python -m agent_registry.start
fi

exec "$@"
