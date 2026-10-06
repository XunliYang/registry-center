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

"""
Integration access plane (FastAPI application).

Served on its own port (see listener.py) with an independent auth policy:

- Authentication: standard Bearer/OAuth 2.0 providers or X.509
  client certificates (task 2.3), producing a unified Principal
- Authorization: fixed four-role model; each route declares the roles it
  accepts; vendor agents are bound to their credential owner
- Audit: every operation (success or failure, read or write) produces an
  audit record carrying the caller identity

The main port's behavior is untouched: business flows are shared with the
main endpoints via the extracted _process_* helpers in server.py.
"""

from typing import Any, List, Optional
from datetime import datetime, timezone
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from google.protobuf.json_format import MessageToDict
from loguru import logger

from agent_registry.broadcast import get_broadcast_service
from agent_registry.broadcast.subscriptions import Subscription
from agent_registry.core import RegistryCore
from agent_registry.errors import AuthoritativeStoreUnavailable, RegistryUnavailableError
from agent_registry.server import (
    CustomHTTPException,
    get_registry_signer,
    get_signature_validator,
    _is_discoverable,
    _process_register_cards,
    _process_update_cards,
    _validate_callback_url,
    _parse_json_object,
)
from agent_registry.server import (
    deregister_semaphore,
    get_semaphore,
    query_semaphore,
    retrieve_semaphore,
    semaphore_guard,
    subscription_semaphore,
)
from agent_registry.signature.agent_card_signature_validator import AgentCardSignatureValidator
from agent_registry.agent_registry.agent_card_signer import AgentCardSigner
import agent_registry.integration.authn  # noqa: F401 - registers the built-in authn handler
from agent_registry.request_validation import card_batch, semantic_query as validate_semantic_query
from common.custom.custom_handle import HandlerRegistry
from common.custom.interface_type import InterfaceType
from common.log.audit_logger import (
    LogLevel,
    OperationName,
    OperationResult,
    OperatorObject,
    audit_logger,
)
from common.util.authenticate_util import (
    AuthFailureReason,
    AuthenticationError,
    AuthorizationError,
    CallerRole,
    Principal,
)
from agent_registry.integration.audit import audit_integration, audit_integration_failure
from agent_registry.integration.audit_query import read_audit_records
from agent_registry.integration.ban import BanTracker
from limits import parse as parse_rate_limit, storage as limit_storage, strategies as limit_strategies

# ---------- Application ----------
# Interactive API docs are disabled on the integration surface.
integration_app = FastAPI(title="Registry Center Integration Access",
                          docs_url=None, redoc_url=None, openapi_url=None)

# ---------- Auth failure ban + per-credential rate limiting (lazy singletons) ----------

_ban_tracker: Optional[BanTracker] = None
_tp_rate_item = None
_tp_prerate_item = None
_tp_limiter = limit_strategies.MovingWindowRateLimiter(limit_storage.MemoryStorage())
_tp_prerate_limiter = limit_strategies.MovingWindowRateLimiter(limit_storage.MemoryStorage())


def get_ban_tracker() -> BanTracker:
    global _ban_tracker
    if _ban_tracker is None:
        from common.util.app_config import get_conf
        from common.log.audit_logger import audit_logger
        conf = get_conf()

        def _audit_ban_lift(key: str):
            # Sync fire-and-forget via the audit writer's INPUT format (snake
            # keys) — the writer normalizes to the on-disk camelCase schema.
            # bannedKey records which bucket was lifted (cred:/ip:).
            audit_logger.audit({
                "operation_name": OperationName.AUTH_BAN,
                "level": LogLevel.MINOR,
                "result": OperationResult.SUCCESS,
                "object_name": OperatorObject.AGENT,
                "details": {"message": "authentication ban lifted after cooldown",
                            "bannedKey": key},
                "client_ip": key[3:] if key.startswith("ip:") else "",
                "user_name": key[5:] if key.startswith("cred:") else "",
            })

        _ban_tracker = BanTracker(
            threshold=int(conf.get('integration.ban.threshold', 5)),
            cooldown_seconds=int(conf.get('integration.ban.cooldown_seconds', 300)),
            on_lift=_audit_ban_lift,
        )
    return _ban_tracker


def get_tp_rate():
    global _tp_rate_item
    if _tp_rate_item is None:
        from common.util.app_config import get_conf
        raw = str(get_conf().get('integration.ratelimit', '100/second')).strip()
        _tp_rate_item = parse_rate_limit(raw if '/' in raw else f"{raw}/second")
    return _tp_rate_item


def get_tp_prerate():
    """Pre-authentication per-IP rate limit: throttles unauthenticated
    credential guessing before the auth handler runs (task 5.2 hardening)."""
    global _tp_prerate_item
    if _tp_prerate_item is None:
        from common.util.app_config import get_conf
        raw = str(get_conf().get('integration.preratelimit', '50/second')).strip()
        _tp_prerate_item = parse_rate_limit(raw if '/' in raw else f"{raw}/second")
    return _tp_prerate_item


def _failure_ban_keys(credential_hint: str, client_ip: str) -> list:
    """Use only an irreversible token fingerprint or the source IP."""
    keys = [f"token:{credential_hint}"] if credential_hint else []
    keys.append(f"ip:{client_ip}")
    return keys


def _success_ban_key(principal: Principal, client_ip: str) -> str:
    """Bucket cleared on successful auth — mirrors the failure derivation.

    Token successes clear their credential bucket; certificate successes
    clear the source-IP bucket (certificate failures are recorded there).
    """
    if principal.auth_method == 'certificate' or not principal.credential_id:
        return f"ip:{client_ip}"
    return f"cred:{principal.credential_id}"


# Operation attribution for auth-failure audit records (finding: failures
# were all misattributed to REGISTER_AGENT regardless of endpoint).
_OP_BY_ENDPOINT = {
    'register_agent': OperationName.REGISTER_AGENT,
    'update_agent': OperationName.UPDATE_AGENT,
    'deregister_agent': OperationName.DEREGISTER_AGENT,
    'list_agents': OperationName.QUERY_AGENT,
    'get_agent': OperationName.QUERY_AGENT,
    'semantic_query': OperationName.RETRIEVE_AGENT,
    'create_subscription': OperationName.CREATE_SUBSCRIPTION,
    'list_subscriptions': OperationName.QUERY_AGENT,
    'delete_subscription': OperationName.DELETE_SUBSCRIPTION,
    'pull_audit_records': OperationName.PULL_AUDIT_RECORDS,
}


def _resolve_operation(request: Request) -> OperationName:
    route = request.scope.get('route')
    name = getattr(route, 'name', '') or getattr(getattr(route, 'endpoint', None), '__name__', '')
    return _OP_BY_ENDPOINT.get(name, OperationName.QUERY_AGENT)

# ---------- Authentication dependency ----------

async def integration_auth(request: Request) -> Principal:
    """Authenticate a integration request via the INTEGRATION_AUTHENTICATE slot.

    Pipeline: per-IP pre-auth rate limit → ban check → credential
    authentication (standard Bearer or TLS peer cert) → success counter
    reset → per-credential rate limit. Failures never expose token material.
    """
    client_ip = request.client.host if request.client else ''
    handler = HandlerRegistry.get_handler(InterfaceType.INTEGRATION_AUTHENTICATE)
    credential_hint = handler.credential_hint(request) if hasattr(handler, 'credential_hint') else ''
    op_name = _resolve_operation(request)
    tracker = get_ban_tracker()

    # 0. Pre-auth per-IP rate limit: throttle unauthenticated credential
    #    guessing before any auth work (ban buckets alone are escapable by
    #    rotating attacker-controlled credentials).
    if not _tp_prerate_limiter.hit(get_tp_prerate(), 'tp_prerate', client_ip):
        logger.warning(f"Integration pre-auth rate limit exceeded: ip={client_ip}")
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Rate limit exceeded")

    # 1. Ban check on both the credential bucket and the source-IP bucket
    ban_keys = [f"token:{credential_hint}"] if credential_hint else []
    ban_keys.append(f"ip:{client_ip}")
    banned_key = next((k for k in ban_keys if tracker.is_banned(k)), None)
    if banned_key:
        probe = Principal(client_ip=client_ip, credential_id=credential_hint)
        await audit_integration_failure(
            OperationName.AUTH_BAN, probe,
            {"message": "banned credential rejected",
             "bannedKey": banned_key.split(':', 1)[0],
             "retryInSeconds": tracker.remaining_seconds(banned_key)})
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED,
                            detail="Temporarily banned due to repeated failures")

    # 2. Credential authentication (token headers first, then TLS peer cert)
    try:
        principal = await handler.handle(client_ip, request)
        if not isinstance(principal, Principal):
            # A misbehaving custom handler returned None/non-Principal:
            # treat as authentication failure, never leak a 500.
            raise AuthenticationError(AuthFailureReason.INVALID_CREDENTIALS,
                                      "Authentication failed")
    except Exception as e:
        reason = getattr(e, 'reason', None)
        just_banned = False
        for key in _failure_ban_keys(credential_hint, client_ip):
            if tracker.record_failure(key):
                just_banned = True
        probe = Principal(client_ip=client_ip, credential_id=credential_hint)
        details = {"message": f"authentication failed: "
                              f"{reason.value if reason else 'invalid credentials'}"}
        if just_banned:
            details["message"] += " (credential temporarily banned)"
        await audit_integration_failure(op_name, probe, details)
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Temporarily banned due to repeated failures" if just_banned
            else (e.detail if isinstance(e, AuthenticationError) else 'Authentication failed')) from e

    tracker.record_success(_success_ban_key(principal, client_ip))
    tracker.record_success(f"ip:{client_ip}")

    # 3. Per-credential rate limit (task 5.2)
    if not _tp_limiter.hit(get_tp_rate(), 'integration', principal.credential_id):
        logger.warning(f"Integration rate limit exceeded: credential={principal.credential_id}")
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS,
                            detail="Rate limit exceeded")
    return principal


def require_roles(*allowed_roles: CallerRole, op_name: OperationName = None):
    """Dependency factory: authenticate, then enforce the route's role set."""

    async def guard(request: Request,
                    principal: Principal = Depends(integration_auth)) -> Principal:
        if principal.role not in allowed_roles:
            details = {"message": f"role '{principal.role.value if principal.role else 'none'}' "
                                  f"is not permitted for this operation"}
            await audit_integration_failure(op_name or OperationName.REGISTER_AGENT,
                                            principal, details)
            logger.warning(f"Integration access denied: subject={principal.subject}, "
                           f"role={principal.role}, ip={principal.client_ip}")
            raise AuthorizationError("Insufficient permissions for this operation")
        return principal

    return guard


# ---------- Exception handlers (mirror the main port's response shape) ----------

@integration_app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException):
    content = {"errors": {"error": [{"errorMessage": exc.detail}]}}
    if getattr(exc, "extra", None):
        content.update(exc.extra)
    return JSONResponse(status_code=exc.status_code, content=content)


@integration_app.exception_handler(RequestValidationError)
async def request_validation_exception_handler(request: Request, exc: RequestValidationError):
    error_messages = "; ".join(
        "{}: {}".format(".".join(str(loc) for loc in error.get("loc", [])), error.get("msg", ""))
        for error in exc.errors()
    )
    return JSONResponse(
        status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
        content={"errors": {"error": [{"errorMessage": error_messages or "Request validation failed"}]}},
    )


@integration_app.exception_handler(RegistryUnavailableError)
async def registry_unavailable_handler(request: Request, exc: RegistryUnavailableError):
    """Same 503 contract as the main port (R4/R12: no record store, no model)."""
    logger.warning(f"Rejecting {request.url.path}: {exc}")
    return JSONResponse(status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        content={"errors": {"error": [{"errorMessage": str(exc)}]}})


@integration_app.exception_handler(AuthorizationError)
async def authorization_exception_handler(request: Request, exc: AuthorizationError):
    return JSONResponse(status_code=status.HTTP_403_FORBIDDEN,
                        content={"errors": {"error": [{"errorMessage": exc.detail}]}})


def _require_broadcast_enabled() -> None:
    """Parity with the main port: subscription management requires change
    broadcast to be enabled — otherwise callers would create subscriptions
    that silently never receive events."""
    if not get_broadcast_service().broadcast_enabled:
        raise CustomHTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                                  "Change broadcast is disabled")


def _vendor_owns_card(stored_owner: Optional[str], principal: Principal) -> bool:
    """Vendor-agent ownership check: `identity` is the single trust anchor.

    `principal.owner` is admin-configured credential metadata, not an
    authorization anchor — keying on it would let a credential whose owner
    field names another vendor identity manage that vendor's cards.
    Ownerless (public) cards remain operable, matching main-port semantics.
    """
    if not stored_owner:
        return True
    return stored_owner == principal.identity


# ---------- Agent card routes ----------

@integration_app.post("/integration/v1/agent-cards", status_code=status.HTTP_201_CREATED,
                      summary="Register agent cards (integration)")
async def register_agent(
        request: Request,
        principal: Principal = Depends(require_roles(
            CallerRole.NMS_OSS, CallerRole.VENDOR_AGENT,
            op_name=OperationName.REGISTER_AGENT)),
):
    """Register new agent cards. New cards are owned by the credential identity."""
    body = await _parse_json_object(request)
    agent_cards = card_batch(body)
    registry = get_registry_dependency()
    signature_validator = get_signature_validator()
    registry_signer = get_registry_signer()
    # Identity-anchored ownership (see _vendor_owns_card): the credential's
    # owner field is attribution metadata and must not redirect card ownership.
    owner = principal.identity
    return await _process_register_cards(
        agent_cards, principal.client_ip, owner, registry,
        signature_validator, registry_signer, caller=principal.audit_identity())


@integration_app.get("/integration/v1/agent-cards", summary="Query agent cards (integration)")
async def list_agents(
        request: Request,
        principal: Principal = Depends(require_roles(
            CallerRole.NMS_OSS, CallerRole.VENDOR_AGENT,
            CallerRole.PARTNER_SERVICE, CallerRole.ANALYTICS_TOOL,
            op_name=OperationName.QUERY_AGENT)),
        name: Optional[str] = Query(None, description="Exact agent name"),
        organization: Optional[str] = Query(None, description="Exact organization"),
):
    """Query published agent cards by exact fields (all roles, read-only)."""
    client_ip = principal.client_ip
    logger.info(f"Integration query agents: name={name}, org={organization}, "
                f"subject={principal.identity}, client={client_ip}")
    async with semaphore_guard(query_semaphore):
        query_handle = HandlerRegistry.get_handler(InterfaceType.QUERY)
        agents = await query_handle.handle(name, organization)
        registry = get_registry_dependency()
        published_agents = []
        for agent in agents:
            # Same predicate as the main port: status *and* health hiding. Keeping a
            # second copy of the rule here is how a pending card leaks out of one port.
            if not _is_discoverable(agent.name, agent.provider.organization, registry):
                continue
            published_agents.append(MessageToDict(agent))
        await audit_integration(OperationName.QUERY_AGENT, principal, True,
                                {"count": len(published_agents), "name": name or '',
                                 "organization": organization or ''})
        return {"agentCards": published_agents}


@integration_app.get("/integration/v1/agent-cards/{organization}/{name}",
                     summary="Get agent card by exact key (integration)")
async def get_agent(
        request: Request,
        principal: Principal = Depends(require_roles(
            CallerRole.NMS_OSS, CallerRole.VENDOR_AGENT,
            CallerRole.PARTNER_SERVICE, CallerRole.ANALYTICS_TOOL,
            op_name=OperationName.QUERY_AGENT)),
        name: str = Path(..., description="Agent name"),
        organization: str = Path(..., description="Agent organization"),
):
    """Get a single published agent card by (name, organization)."""
    async with semaphore_guard(get_semaphore):
        get_handle = HandlerRegistry.get_handler(InterfaceType.GET)
        record = await get_handle.handle(name, organization)
        registry = get_registry_dependency()
        result: List[dict] = []
        if record is not None and _is_discoverable(name, organization, registry):
            result = [MessageToDict(record.agent_card)]
        await audit_integration(OperationName.QUERY_AGENT, principal, True,
                                {"name": name, "organization": organization,
                                 "found": bool(result)})
        return {"agentCards": result}


@integration_app.put("/integration/v1/agent-cards/{organization}/{name}",
                     summary="Update agent card (integration)")
async def update_agent(
        request: Request,
        principal: Principal = Depends(require_roles(
            CallerRole.NMS_OSS, CallerRole.VENDOR_AGENT,
            op_name=OperationName.UPDATE_AGENT)),
        name: str = Path(..., description="Agent name"),
        organization: str = Path(..., description="Agent organization"),
):
    """Fully replace an agent card. Vendor agents may only update cards they own."""
    registry = get_registry_dependency()
    record = registry.get_by_key_with_owner(name, organization)
    if principal.role == CallerRole.VENDOR_AGENT and record is not None \
            and not _vendor_owns_card(record.owner, principal):
        details = {"agentName": name, "organization": organization,
                   "message": "vendor agent may only update its own cards"}
        await audit_integration_failure(OperationName.UPDATE_AGENT, principal, details)
        raise AuthorizationError("Vendor agents may only update their own agent cards")

    body = await _parse_json_object(request)
    agent_cards = card_batch(body)
    owner_param = principal.identity if principal.role == CallerRole.VENDOR_AGENT else None
    return await _process_update_cards(
        agent_cards, principal.client_ip, name, organization, owner_param,
        get_signature_validator(), get_registry_signer(),
        caller=principal.audit_identity())


@integration_app.delete("/integration/v1/agent-cards/{organization}/{name}",
                        summary="Deregister agent card (integration)")
async def deregister_agent(
        request: Request,
        principal: Principal = Depends(require_roles(
            CallerRole.NMS_OSS, CallerRole.VENDOR_AGENT,
            op_name=OperationName.DEREGISTER_AGENT)),
        name: str = Path(..., description="Agent name"),
        organization: str = Path(..., description="Agent organization"),
):
    """Remove an agent card. Vendor agents may only remove cards they own."""
    registry = get_registry_dependency()
    if principal.role == CallerRole.VENDOR_AGENT:
        record = registry.get_by_key_with_owner(name, organization)
        if record is not None and not _vendor_owns_card(record.owner, principal):
            details = {"agentName": name, "organization": organization,
                       "message": "vendor agent may only delete its own cards"}
            await audit_integration_failure(OperationName.DEREGISTER_AGENT, principal, details)
            raise AuthorizationError("Vendor agents may only delete their own agent cards")

    details = {"agentName": name, "organization": organization}
    owner_param = principal.identity if principal.role == CallerRole.VENDOR_AGENT else None
    async with semaphore_guard(deregister_semaphore):
        deregister_handle = HandlerRegistry.get_handler(InterfaceType.DEREGISTER)
        success = await deregister_handle.handle(name, organization, owner=owner_param)
        await audit_integration(OperationName.DEREGISTER_AGENT, principal, success, details)
        if not success:
            raise CustomHTTPException(status.HTTP_404_NOT_FOUND, "Agent not found")
        return JSONResponse(status_code=status.HTTP_200_OK,
                            content={"name": name, "organization": organization, "deleted": True})


@integration_app.post("/integration/v1/agent-cards/semantic-query",
                      summary="Semantic agent search (integration)")
async def semantic_query(
        request: Request,
        principal: Principal = Depends(require_roles(
            CallerRole.NMS_OSS, CallerRole.PARTNER_SERVICE,
            op_name=OperationName.RETRIEVE_AGENT)),
):
    """Find agents semantically relevant to a task description (LLM-backed)."""
    body = await _parse_json_object(request)
    task, top_n = validate_semantic_query(body, body.get('topN', 10))
    async with semaphore_guard(retrieve_semaphore):
        retrieve_handle = HandlerRegistry.get_handler(InterfaceType.RETRIEVE)
        agents = await retrieve_handle.handle(task, top_n)
        registry = get_registry_dependency()
        agents = [a for a in agents if _is_discoverable(a.name, a.provider.organization, registry)]
        result = [MessageToDict(a) for a in agents]
        await audit_integration(OperationName.QUERY_AGENT, principal, True,
                                {"top_n": top_n, "count": len(result)})
        return {"agentCards": result}


# ---------- Audit pull API ----------

@integration_app.get("/integration/v1/audit-records",
                     summary="Pull audit records (integration, analytics tool role)")
async def pull_audit_records(
        principal: Principal = Depends(require_roles(
            CallerRole.ANALYTICS_TOOL,
            op_name=OperationName.PULL_AUDIT_RECORDS)),
        start_time: Optional[str] = Query(None, description="ISO-8601 lower bound (inclusive)"),
        end_time: Optional[str] = Query(None, description="ISO-8601 upper bound (inclusive)"),
        identity: Optional[str] = Query(None, description="Filter by caller identity"),
        operation: Optional[str] = Query(None, description="Filter by operation name"),
        limit: int = Query(100, ge=1, le=1000),
        offset: int = Query(0, ge=0),
):
    """Paginated audit record pull for operator security analytics tools."""
    from common.log.audit_logger import audit_logger
    result = read_audit_records(
        log_file=audit_logger.log_file,
        backup_count=int(audit_logger.backup_count),
        start_time=start_time, end_time=end_time,
        identity=identity, operation=operation,
        limit=limit, offset=offset)
    await audit_integration(OperationName.PULL_AUDIT_RECORDS, principal, True,
                            {"pulled": len(result["records"]), "total": result["total"]})
    return JSONResponse(status_code=status.HTTP_200_OK, content=result)


# ---------- Subscription routes ----------

@integration_app.post("/integration/v1/subscriptions", status_code=status.HTTP_201_CREATED,
                      summary="Create a change subscription (integration)")
async def create_subscription(
        request: Request,
        principal: Principal = Depends(require_roles(
            CallerRole.NMS_OSS, CallerRole.PARTNER_SERVICE,
            op_name=OperationName.CREATE_SUBSCRIPTION)),
):
    """Subscribe to registry change events (webhook callback). Behavior
    parity with the main port: broadcast gate, event-type validation,
    dispatcher wiring, caller-provided secret."""
    _require_broadcast_enabled()
    body = await _parse_json_object(request)
    callback_url = body.get("callbackUrl", '')
    if not callback_url:
        raise CustomHTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                  "callbackUrl is required")
    _validate_callback_url(callback_url)

    event_types = body.get("eventTypes")
    if event_types is not None:
        from agent_registry.broadcast.events import EventType
        if not isinstance(event_types, list) or \
                any(t not in (e.value for e in EventType) for t in event_types):
            raise CustomHTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT,
                                      "eventTypes must be a list of valid event type names")

    broadcast_service = get_broadcast_service()
    subscription = Subscription(
        subscription_id='',
        callback_url=callback_url,
        event_types=event_types,
        organizations=body.get("organizations"),
        tags=body.get("tags"),
        secret=body.get("secret"),
    )
    created = broadcast_service.subscription_store.create(subscription)
    if broadcast_service.dispatcher is not None:
        broadcast_service.dispatcher.add_subscription(created)
    await audit_integration(OperationName.CREATE_SUBSCRIPTION, principal, True,
                            {"subscriptionId": created.subscription_id,
                             "callbackHost": urlsplit(callback_url).hostname or ''})
    payload = created.to_dict(include_secret=True)
    return JSONResponse(status_code=status.HTTP_201_CREATED, content=payload)


@integration_app.get("/integration/v1/subscriptions",
                     summary="List change subscriptions (integration)")
async def list_subscriptions(
        principal: Principal = Depends(require_roles(
            CallerRole.NMS_OSS, CallerRole.PARTNER_SERVICE,
            op_name=OperationName.QUERY_AGENT)),
):
    """List existing change subscriptions."""
    _require_broadcast_enabled()
    subs = get_broadcast_service().subscription_store.list_all()
    await audit_integration(OperationName.QUERY_AGENT, principal, True,
                            {"count": len(subs)})
    return {"subscriptions": [s.to_dict(include_secret=False) for s in subs]}


@integration_app.delete("/integration/v1/subscriptions/{subscription_id}",
                        summary="Delete a change subscription (integration)")
async def delete_subscription(
        subscription_id: str = Path(..., description="Subscription ID"),
        principal: Principal = Depends(require_roles(
            CallerRole.NMS_OSS,
            op_name=OperationName.DELETE_SUBSCRIPTION)),
):
    """Remove a change subscription (NMS/OSS role only). Parity with the
    main port: 404 before dispatcher removal, so deleted subscriptions stop
    receiving callbacks immediately."""
    _require_broadcast_enabled()
    broadcast_service = get_broadcast_service()
    if not broadcast_service.subscription_store.delete(subscription_id):
        await audit_integration_failure(OperationName.DELETE_SUBSCRIPTION, principal,
                                        {"subscriptionId": subscription_id,
                                         "message": "subscription not found"})
        raise CustomHTTPException(status.HTTP_404_NOT_FOUND, "Subscription not found")
    if broadcast_service.dispatcher is not None:
        broadcast_service.dispatcher.remove_subscription(subscription_id)
    await audit_integration(OperationName.DELETE_SUBSCRIPTION, principal, True,
                            {"subscriptionId": subscription_id})
    return JSONResponse(status_code=status.HTTP_200_OK,
                        content={"subscriptionId": subscription_id, "deleted": True})


def get_registry_dependency() -> RegistryCore:
    """Registry access for route bodies (resolved late to ease test overrides)."""
    from agent_registry.registry_instance import get_registry
    return get_registry()
