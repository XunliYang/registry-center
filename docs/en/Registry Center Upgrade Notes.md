# Registry Center upgrade and deployment notes

## Caller identity and ownership

For direct mTLS deployments, configure:

```properties
enable_https=true
verify_client=true
owner.isolation.enabled=true
owner.identity.mode=certificate
owner.validation.mode=strict
```

Start with `python -m agent_registry.start`. Its TLS adapter makes the verified
peer certificate available to ownership checks; the integration listener uses
the same adapter. Custom server launchers must install that adapter themselves.
Setting an HTTP identity header does not substitute for a verified certificate.

For a reverse proxy that terminates mTLS, explicitly select
`owner.identity.mode=trusted_proxy` and set `owner.trusted.proxy.ips` to the
actual proxy connection addresses. The proxy must remove incoming
`X-SSL-Client-DN` headers, write the identity it verified, and block direct
access to the application port. `forwarded_allow_ips` controls forwarded IP
handling, not ownership authentication. Never configure `owner.trusted.proxy.ips`
as `*`; `forwarded_allow_ips=*` is only acceptable on a platform that terminates
TLS and rewrites the forwarded headers itself with an unstable proxy address (for
example Cloud Run), never on a plain reverse proxy. Write the value without quotes
(`forwarded_allow_ips=127.0.0.1`): the reader strips whitespace only and uvicorn
compares each entry literally, so `"127.0.0.1"` used to trust no proxy at all.
Identity-mode environment overrides are
`REGISTRY_OWNER_IDENTITY_MODE` and `REGISTRY_OWNER_TRUSTED_PROXY_IPS`.

With isolation enabled, ownerless legacy records can still be discovered when
published, but isolated writes require administrative assignment of ownership.
Back up the database, review each ownerless record, and assign its verified
owner through a controlled administrative migration. There is no automatic
claim by the first caller. CN-based owner compatibility is retained; this
upgrade does not migrate all owners to a new issuer/subject identifier scheme.

## Approval visibility and events

Pending (`registered`) cards are excluded from public discovery and semantic
selection prompts. Approving a card emits `AGENT_REGISTERED`; withdrawing a
published card emits `AGENT_DEREGISTERED` without its full Card. Card edits do
not change approval status. Repeating a pending status does not emit an update.
Health discovery surfaces omit pending cards but still show published offline
cards for monitoring.

New public events carry `data.discovery_public=true`. Historical outbox events
without this marker are not replayed with their old Card payload; `/changes`
and webhook delivery replace them with `SYNC_REQUIRED`, retaining the event
ID and registry version and setting `data.requires_snapshot=true`. Consumers
should fetch a fresh published-card list and replace their discovery cache,
rather than applying the legacy payload. Page cursors still advance over these
envelopes. The current list endpoint is not an atomic snapshot-with-cursor;
concurrent bootstrap correctness remains part of the planned cursor upgrade.

Webhook delivery remains best-effort. Each subscriber now has its own delivery
state (one row per subscription and event, in `registry_event_deliveries` for the
SQL modes and in a sibling file for the file mode), so a slow subscriber is no
longer marked done by a fast one: recovery replays only what that subscriber still
owes, and an event is kept until every subscriber owes nothing. Failures are now
retried durably and boundedly: a failed delivery is requeued by a periodic sweep
(and once at startup, so a crash does not lose the retry) until it has spent
`broadcast.delivery.max.attempts` attempts (default 5, spacing
`broadcast.delivery.retry.interval`, default 60s). After the budget the delivery
stays `delivery_failed` in the internal delivery ledger. `/changes` returns event
content for reconciliation, not delivery status or attempts for a subscription;
it cannot tell a caller whether its webhook failed. A permanently broken destination
must not be retried forever. What is still not provided: exactly-once delivery (retries can duplicate
an event — deduplicate by `event_id`), per-attempt scheduling beyond the single
sweep interval, and a distributed lease, so every instance of a multi-instance
deployment against one outbox can independently sweep the same failure. Persist
consumer cursors, support reconciliation, and deduplicate events by `event_id`; do
not interpret a subscriber's own delivery state as an acknowledgement from another
subscriber.

Within a batch, events are sent in version order, but durable retries can deliver
an older failed event after newer ones. Deduplicate by event ID and reject stale
registry versions when updating a discovery cache; use `/changes` for ordered
reconciliation. Sequential workers do not guarantee strict order across retries.

The batch window retains every event ID in version order, including repeated
changes to the same Agent. It no longer replaces older durable events with a
newer payload. SQL delivery mutations and event aggregates share a transaction;
startup recovery reads subscriber pending rows even if a legacy aggregate is stale.
File mode can recover pending ledger rows too, but does not provide cross-file
atomic transactions. Health monitoring (list/history/SSE) filters approval status,
not the optional unhealthy task-discovery filter, so offline alerts remain visible.

Registry versions are now allocated from a single counter row
(`registry_event_counter`) updated in the same transaction as the event insert, so
the row lock is held until commit and version order equals commit order: a consumer
following `since=<version>` can no longer miss an event that was allocated earlier
but committed later. This applies to the SQL persistence modes (postgresql,
gaussdb, mysql); the file and in-memory modes keep their previous single-instance
allocation. It is a property of version allocation, not a change of supported
topology: the Registry Center is still documented as single-instance, so do not
read this as approval to run several instances against one database. Because the
counter row is the serialization point, event writes commit one at a time.
Upgrading seeds the counter from the existing maximum version; an event written
directly to `registry_events` while bypassing the outbox would leave the counter
behind (the unique index on `registry_version` still rejects a duplicate version).

## Callback destinations and graph API

Before creating webhook subscriptions, set
`broadcast.callback.allowlist=operator.example.com,authorized-internal.example`.
This setting is mandatory; an empty allowlist rejects registration and prevents
delivery to persisted subscriptions. HTTP requires the additional explicit
`broadcast.allow.http.callbacks=true` setting. URLs with embedded credentials
are rejected, and redirects are not followed. Destination permission is checked
again before each send attempt. Explicitly list internal telecom callbacks when
needed; wildcard hosts are not supported.

Hostname allowlisting does not pin DNS resolution. Deployments must control DNS
and service egress; connection-time IP pinning remains future work. Do not grant
untrusted users control of an allowlisted hostname.

The pre-embedded generic graph API returns 404 by default. To enable it, configure
`knowledge_graph.enabled=true` (or `REGISTRY_KNOWLEDGE_GRAPH_ENABLED=true`),
provide Neo4j credentials, and separately grant graph permission. An authenticated
plugin principal may carry `knowledge_graph:read` / `knowledge_graph:write`
scopes. Alternatively, list verified owners in `knowledge_graph.allowed.owners`
(or `REGISTRY_KNOWLEDGE_GRAPH_ALLOWED_OWNERS`); those owners are graph operators
with read and write access. Default anonymous authentication grants no graph
permission. Neo4j remains independent of AgentCard storage and discovery; the
API is not a tenant-isolated graph service.

## Signatures, semantic errors and tags

Registration and update await asynchronous JKU verification. A card is accepted
when any trusted signature is valid; a missing or malformed earlier signature
does not hide a valid later one. RSA JWKs use `n`/`e`; EC ES256 keys use P-256,
`x`/`y`. The JWK `alg` field is optional. JKU still requires its own operator
hostname allowlist and HTTPS. Redirects are disabled and the actual streamed
JWKS response is capped at 1 MiB, including responses without Content-Length.

Model configuration/invocation/output failures return 503 for semantic search;
successful no-match results return 200 with an empty list. Ordinary logs and
query audit details omit the task body. Exact queries and CRUD do not depend
on a working model configuration.

Tag rename updates Agent tag references; the core delete path unbinds the tag from
every Agent. The CLI/internal service keeps its existing guard: deleting a tag that
is still in use is refused with the affected Agent list, so operators must unassign
it first — only the programmatic path (and rename) exercises the unbind. SQL performs
these changes and published-Agent events in one UoW, rolling back on event
failure. File storage retains its cross-file atomicity limitation and is intended
for local/small deployments. Pending-Agent tag changes produce no public events.

Semantic search now distinguishes "no candidates" from "the model is down" on
both candidate branches: when nothing passes the discoverability filter the call
returns an empty result without consulting the model (previously the vector branch
asked the model to choose from an empty list, which could turn an empty match into
a 503), and an embedding-service failure is reported as 503
(`SemanticSearchUnavailable`) instead of escaping as an unhandled 500. The
record-store branch already behaved this way.

One visibility rule for *task discovery* now answers for both HTTP ports. The
integration endpoints used to re-implement the check (status string plus health
hiding) next to the main port's `_is_discoverable()`, so the two could disagree;
they now call the same predicate. Health monitoring uses the approval-only
predicate instead (see the note above), so it is deliberately not this one.
`get_status()` also normalizes a record whose stored status is
NULL/empty to `published`, matching what storage already does when it filters
listings with `COALESCE(status, 'published')` — a legacy row returned by a listing
is no longer judged unpublished by the visibility checks. A *missing* record still
reports None (and stays invisible), so "no such card" and "published legacy card"
remain distinguishable.

Legacy `use_vectordb=true` mode still replaces authoritative storage and is
experimental. This upgrade does not implement SQL-backed N-candidate retrieval,
projection revisions, or index rebuild/migration. What changed: the registry no
longer answers questions this mode cannot answer. Approval status, ownership,
tags (including the tag entities themselves), card metadata, card listings and the
mutations that must announce themselves to the change
feed now report **HTTP 503** (`registry.use_vectordb=true replaces the
authoritative record store ...`) instead of returning an empty list, a default
status or a mutation that no subscriber ever hears about. Card registration,
exact card lookup and the registration quota/duplicate checks keep working so an
operator can still populate the collection.
Set `startup.strict.storage=true` to refuse to start in this mode at all. Keep
`use_vectordb=false` for deployments requiring approval and ownership parity.

## Heartbeat ownership

With `owner.isolation.enabled=true` the heartbeat endpoint now requires the
verifying identity to be the card's owner (401 without a verifiable identity,
403 for a different owner, same policy as update/delete). A heartbeat is a
liveness claim: it decides whether a dead card is hidden
(`heartbeat.hide.unhealthy.results`), whether the offline TTL deregisters it, and
it can publish a public health-recovery event — previously any authenticated
caller could make that claim for any card. Deployments that let one monitor
process heartbeat on behalf of many Agents should keep isolation disabled, or give
the monitor each owner's credential. With isolation off (the default) the endpoint
behaves exactly as before.

## Containers and frontend

The image now defaults to HTTPS, mTLS and ownership isolation; mount certificates
and configure the deployment policy. Disabling HTTPS no longer silently disables
ownership and Card integrity checks. `docker-compose.yml` is an explicit local
development profile with anonymous access bound to `127.0.0.1`; do not expose
that profile publicly by changing its port binding.

The probe uses `python -m agent_registry.healthcheck`, the configured HTTP/HTTPS
protocol, and the configured CA. It never bypasses certificate verification.
For mTLS, provide `REGISTRY_HEALTHCHECK_CLIENT_CERT` and
`REGISTRY_HEALTHCHECK_CLIENT_KEY`; use a dedicated client identity with read access.
`REGISTRY_HEALTHCHECK_HOST` defaults to `127.0.0.1`; change it to a reachable name
covered by the server certificate when necessary. The certificates described in
this guide are issued without a SAN, so an HTTPS probe against the default
`127.0.0.1` cannot pass verification: an IP literal only matches a certificate
with an `iPAddress` SAN entry. Set `REGISTRY_HEALTHCHECK_HOST` to a name matching
the certificate CN (for example `agent-registry`), or issue the probe certificate
with a SAN covering the probe host. The current probe checks the discovery HTTP
path, not independent component-level readiness.

The frontend honors the saved HTTPS setting and keeps same-origin defaults.
An HTTPS page requires an HTTPS backend address. Unit tests run with `npm test`;
lint and build remain `npm run lint` / `npm run build`.
