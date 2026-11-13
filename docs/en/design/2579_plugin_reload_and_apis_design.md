# System Design: Plugin Reload and APIs

| Item | Content |
| --- | --- |
| Design source | [RFC #2579 Plugin hot reload and custom APIs](https://github.com/oceanbase/powercontext/pull/2579) |
| Prerequisite design | [Plugin interfaces design](2549_plugin_interfaces_design.md) (RFC #2549) |
| Implementation | Plugin APIs and pages: [oceanbase/powercontext#2616](https://github.com/oceanbase/powercontext/pull/2616); hot reload, data upgrades, MCP tools: follow-up |
| Date | 2026-11-13 |
| Stack | Python 3.11+ / pydantic v2 / FastAPI / Starlette; the Dashboard is server-rendered pages with small scripts |

This is the implementation design of RFC #2579, built on the implementation design of RFC #2549: host processes, the
plugin record, the writer, and user-change mode all come from that document, and this one describes only what is added
or changed. When this conflicts with the RFC, the RFC wins and this document is revised. The overview of the interfaces a
plugin implements is section 0 of the RFC #2549 design.

| Part | Status | Section |
| --- | --- | --- |
| Plugin routes, binary request and response bodies, handler capabilities | Implemented in #2616 | §5.1–§5.3, §6 |
| Plugin pages | Implemented in #2616 | §7 |
| Scanning, version snapshots, switching (hot reload) | Not implemented; plugins load at Server startup, and updating one needs a restart | §2–§4.6 |
| Data upgrade `upgrade` | Not implemented | §4.7 |
| Plugin API description (OpenAPI) | Not implemented | §5.4 |
| MCP tools | Not implemented; the declaration's `mcp` field is validated but no tools are generated | §5.5 |
| Version fields of `/v1/plugins` and structured observability | Not implemented | §4.6, §8 |

---

## 0. Invariants

| # | Invariant | How the implementation guarantees it |
| --- | --- | --- |
| C1 | One call uses one version of plugin code | Child processes import only from read-only version snapshots; a snapshot is not deleted while in use |
| C2 | The version identity detects any content change | The version digest is computed from file contents, never from sizes and modification times |
| C3 | Core performs plugin API authorization | Core code in the host process authorizes `access` first, and each read and write capability on `ctx` checks again as the caller |
| C4 | Plugin pages cannot borrow the user's session | Pages run in an opaque origin; the hosting page forwards only for an iframe that has not navigated, and only to routes the plugin declared |

---

## 1. Modules and files

| File | Status | Responsibility |
| --- | --- | --- |
| `src/powercontext/plugins.py` | Implemented | `PluginApi`, `Route`, `ApiRequest`, `BinaryResponse`, `ApiError`, `Caller`, `PluginPages`, `PluginSources`, `PluginScopes` |
| `src/powercontext/server/app.py` | Implemented | `call_plugin_api`: route matching, body limits, handing off to the host process, response mapping |
| `src/powercontext/builtin/plugins/host.py` | Implemented | Handles `api` requests: Scope authorization, `request_model` validation, calling the handler; provides `ctx.require`, `ctx.reads`, `ctx.writer`, `ctx.sources`, `ctx.scopes` locally |
| `src/powercontext/builtin/plugins/host_protocol.py` | Implemented | `api` requests and responses |
| `src/powercontext/server/dashboard/plugin_pages.py` | Implemented | Navigation entries, hosting page, static files, server-side forwarding, `_client.js` |
| `src/powercontext/server/dashboard/templates/plugin.html` | Implemented | The hosting page and its script: iframe management and message forwarding |
| `src/powercontext/server/dashboard/static/plugin-client.js` | Implemented | Client script for plugin pages |
| `src/powercontext/server/plugin_reload.py` | To implement | Scan loop, digests, settling, snapshot creation and cleanup |
| `src/powercontext/server/plugins.py` | To change | `PluginManager` gains switching for additions, updates, and removals; the plugin record gains version information |
| `src/powercontext/server/plugin_hosts.py` | To change | Host process replacement: switch once the new process is ready, stop the old one once drained |
| `openapi/powercontext.yaml` | To change | Plugin API description; version fields of `/v1/plugins` |

---

## 2. Scanning

### 2.1 Scan loop

```text
In each Server process, every rescan seconds (and at startup):
  -> list the direct subdirectories of the plugin directory
  -> compute each directory's version digest in a thread pool (§2.2)
  -> digest equals the previous scan's and the current version's: skip
  -> digest differs from the previous scan's: mark as settling; do not check this round
  -> digest unchanged for two consecutive scans and different from the current version: create a version snapshot (§3) and check it
  -> add, update, or unload according to the check result (§4)
  -> directories present before but gone this round: unload
  -> clean up snapshots no longer in use (§3.4)
  -> record scan_ms
```

- The scan loop is an asyncio task in the Server process; digests are computed in a thread pool so they never block the
  event loop.
- The first scan at startup does not wait for settling; it snapshots and checks every directory right away.
- Only one scan runs at a time; a trigger that arrives while one is running is skipped.
- With `POWERCONTEXT_SERVER_PLUGINS_RESCAN_SECONDS` set to 0, no scan loop starts; plugins load directly from the plugin
  directory and the code fingerprint is checked under the RFC #2549 rules.

### 2.2 Version digest

The version digest uses the same algorithm as the RFC #2549 code fingerprint: sort every file under the directory
(excluding `__pycache__`) by relative path and feed each relative path and file content into SHA-256.

On a 2-core development machine, one digest over 14 MB in 676 files (`src/powercontext`, files in the page cache) takes
about 230 ms; reading only metadata takes about 33 ms. Plugin directories hold no dependencies and are usually much
smaller.

### 2.3 Per-directory scan state

```python
@dataclass
class ScanState:
    last_digest: str | None      # digest from the previous scan
    stable_rounds: int           # consecutive scans with the same digest
    failed_digest: str | None    # digest of the latest failed check, so the same content is not checked repeatedly
```

After a digest fails its check, it is not checked again until the content changes.

---

## 3. Version snapshots

### 3.1 Layout

```text
<data directory>/plugin-versions/
└── <plugin name>/
    └── <version digest>/      # read-only
        ├── plugin.py
        └── ...
```

### 3.2 Creation

1. Copy into `<data directory>/plugin-versions/<plugin name>/.tmp-<random>/`, skipping `__pycache__`;
2. recompute the digest of the temporary directory and abandon this round if it differs from the scanned digest (the
   directory changed during the copy);
3. make the files read-only (files `0444`, directories `0555`);
4. rename to `<version digest>/`; if the target already exists, delete the temporary directory and use the existing
   snapshot.

### 3.3 Use

- Check child processes, host processes, and Workers all use the snapshot as the plugin directory: the snapshot goes on
  `sys.path` and `ctx.plugin_dir` points to it.
- `PluginWorkerSpec` gains a `snapshot` field; the Supervisor binding's launcher reads the current snapshot at dispatch
  time, so Workers dispatched after a version switch use the new snapshot without re-registering the binding.
- With snapshots the code fingerprint is no longer checked: snapshots are read-only, and the digest in the path is their
  content.

### 3.4 References and cleanup

`PluginManager` keeps an in-process reference count for each snapshot: 1 for the current version, 1 for each running host
process, and 1 for each dispatched, unfinished Worker. The end of each scan deletes snapshots with a count of 0.

At Server startup, after the first scan creates the current snapshots, every other snapshot and leftover temporary
directory under `plugin-versions/` is deleted. When several Server processes share one data directory (several instances
on one machine), each process deletes only snapshots it created and no longer uses; startup cleanup deletes only
snapshots modified before the process started.

---

## 4. Switching

### 4.1 State

The plugin record (RFC #2549 design §4.2) gains:

```python
    version_digest: str | None         # digest of the current snapshot
    snapshot: Path | None              # current snapshot directory
    activated_at: datetime | None
    last_check_failure: str | None     # category of the latest failed check; cleared after a successful switch
    state: Literal["loaded", "failed"]
```

### 4.2 Addition

After a successful check, registration runs as in RFC #2549 design §4.4, plus plugin routes (§5) and pages (§7). A failed
check sets `state="failed"`.

### 4.3 Update

| Situation | Handling |
| --- | --- |
| Check passes, declaration digest unchanged | Update `version_digest` and `snapshot`; replace the host process (§4.5) |
| Check passes, declaration digest changed | Compute the difference between the declarations: unregister removed Source types, routes, and pages and register new ones; treat a family rename as removing the old family and adding the new one; update the binding's schedule with `replace_binding`; update the snapshot; replace the host process |
| Check fails | Record only `last_check_failure` and a `check_failed` event; the current snapshot and host process stay |

### 4.4 Removal

Unregister the Source types, family, merge registration, routes, pages, and binding (`replace_binding(name, None)`); stop
accepting new synchronous calls to the plugin; stop the host process once in-flight synchronous calls finish or time out;
decrement the current snapshot's reference count.

### 4.5 Host process replacement

```text
  -> start a new host process from the new snapshot and wait for ready (a timeout counts as a failed start)
  -> failed start: stop the new process, keep the old one, record check_failed
  -> ready: atomically swap the PluginHost to the new process; new requests go to it
  -> mark the old process draining: it accepts no requests; send shutdown once in-flight requests finish;
     terminate it if any request exceeds its time limit
  -> after the old process exits, decrement the old snapshot's reference count
```

If the host process is in restart backoff and not running, only the snapshot is updated, and it starts from the new
snapshot when the backoff ends.

### 4.6 Multiple instances

- Each instance scans independently, and `/v1/plugins` returns the serving instance's view, including `version_digest`,
  `declaration_digest`, `state`, `activated_at`, and `last_check_failure`.
- Only the leader registers bindings; after a leadership change, the new leader registers them from its own records.

### 4.7 Data upgrades

```text
Host process starts (new snapshot)
  -> import the plugin, compare the declaration, open the database, access service, dedicated storage, and models
  -> with upgrade declared: run upgrade(ctx) as the plugin itself, within upgrade_timeout_seconds
       -> exception or timeout: send HostFailed(category="upgrade_failed") and exit
  -> run setup(ctx), send HostReady
```

- During `upgrade`, the request helpers on `ctx` act as the plugin itself: `reads` and `writer` do not check a caller or
  require a prior `require`; `caller`, `require`, `sources`, and `scopes` are unavailable. After `setup`, they act as each
  request's caller again.
- When the Server receives `upgrade_failed`, it handles it as a failed check (§4.3) and records an `upgrade_failed` event;
  the host process does not enter restart backoff, and the next scanned version tries again.
- A plugin that declares `upgrade` starts its host process as soon as it loads. At Server startup, such a plugin's binding
  registers only after the host process is ready; on hot reload, the snapshot switches only after the new host process in
  §4.5 is ready, so background invocations use the new snapshot only after the switch.
- A host process restarted after a crash runs `upgrade` again, consistent with "called on every load"; plugins must make
  it repeatable anyway.
- `background` instances run no host process: when loading a new version, they spawn a child process that runs only
  `upgrade` (also as the plugin itself, within `upgrade_timeout_seconds`), and switch the binding's launcher to the new
  snapshot only after it succeeds; a failure counts as a failed check and dispatch continues from the old snapshot. Every
  instance runs its own `upgrade`, so new-version `generate` runs only after this instance's `upgrade` completes.
- `upgrade` makes only quick, repeatable changes; format changes over large data are migrated lazily on read by the
  plugin, or by a migration script the plugin ships and the deployer runs before upgrading, with no extra core mechanism.
  The writer in `upgrade` follows the Worker write transaction rules and opens at most one write transaction block.

---

## 5. Plugin routes

### 5.1 Mounting

The Server registers one wildcard route for the plugin group:

```text
/v1/plugins/{name}/api/{path:path}     (GET, POST, PUT, PATCH, DELETE)
```

Flow:

1. authenticate the caller; unauthenticated requests return 401;
2. find the successfully loaded plugin by `name`; return 404 if there is none;
3. match the plugin's routes by method and path. Each route path is compiled with Starlette's `compile_path`; paths
   without parameters match first, then routes in declaration order; no match returns 404;
4. read the request body up to the route's `max_body_bytes`, which defaults to 1 MiB for `json` and to the deployment
   ceiling for `binary`; no route exceeds `POWERCONTEXT_SERVER_PLUGIN_API_MAX_BODY_BYTES` (32 MiB by default). Larger
   bodies return 413;
5. build `HostRequest(method="api", ...)` with the caller, route template, path parameters, query parameters, and body,
   and hand it to the host process;
6. host process: when `access` is a Scope permission, take the path parameter named by `scope_param` as the Scope ID and
   authorize `scope.read` or `scope.contribute`, returning 403 when it fails; `authenticated` needs no further check;
   validate `request_model`; call the handler;
7. the Server maps the response (§5.3).

The Server does only what needs neither the plugin nor the access service: finding the plugin and route, authentication,
and body limits. Authorization and the later steps happen in the host process, matching the split in RFC #2549 design §7.

### 5.2 Binary request and response bodies

A binary request body travels as bytes inside `HostRequest` over the `Pipe` to the host process; a `BinaryResponse` from
the handler returns as bytes inside `HostResponse`. The Server and the host process each hold a copy in transit, so a
single request's peak memory is about twice the limit.

### 5.3 Response mapping

| Host process result | HTTP |
| --- | --- |
| A Pydantic model or JSON value | 200, JSON |
| `BinaryResponse` | 200, with headers from `content_type` and `filename` |
| `None` | 204 |
| `ApiError(status, code, message)` | `status`, with the code and message from `ApiError`; `ApiError` accepts only 4xx other than 401 and 403 at construction, otherwise construction itself raises and the request returns 500 |
| `ctx.require` or write authorization fails | 403 |
| `request_model` validation fails or `InvalidContent` | 422 |
| Any other exception | 500; only the exception type is logged |
| `timeout_seconds` exceeded | 504; only that request is cancelled (see RFC #2549 design §7.3) |
| Host process unavailable, in backoff, or other requests of a process terminated for loss of response | 503 |

Error responses use the Server's standard error format.

### 5.4 OpenAPI (not implemented)

`GET /v1/plugins/{name}/openapi.json` is generated from the route table: one operation per route with `operationId`
`<name>__<handler>`; the JSON Schemas of `request_model` and `response_model` come from the declaration the check child
process exports; `binary` bodies are `application/octet-stream`; every operation lists 401, 403, 404, 413, 422, 500, 503,
and 504 error responses; the extension `x-powercontext-access` records `access` and `scope_param`.

### 5.5 MCP tools (not implemented)

- The existing `create_mcp_server` generates tools from the Server's OpenAPI document at startup and calls HTTP routes
  through an internal bridge (`_InternalBridgeTransport`). Plugin tools reuse the same bridge: each `mcp=True` route
  becomes a tool that requests `/v1/plugins/{name}/api{path}` with the MCP session's credentials, so authentication,
  authorization, and errors all go through the HTTP route.
- Input schema: path and query parameters are string properties, and the `request_model` JSON Schema is the `body`
  property; the output schema is the `response_model` JSON Schema.
- Annotations: `GET` routes with `scope.read` or `authenticated` access get `readOnlyHint=True` and
  `idempotentHint=True`; other routes get `readOnlyHint=False`. `openWorldHint` is always `False`.
- Hot reload: after a switch, FastMCP 3.4's `add_tool` / `remove_tool` update the tools, so `tools/list` always returns
  the current set. FastMCP can send the tool-list-changed notification only inside a session's request context, while
  hot reload happens outside requests; the implementation sends the notification on the session's next request, and
  otherwise clients see the change the next time they list tools.
- Tool names are `<plugin name>__<handler>` with the plugin name unchanged. Plugin names contain no underscores, so
  everything before the first double underscore is the plugin name and tools of different plugins cannot collide; a name
  over 64 characters, or one colliding with a built-in tool, fails the plugin's check.

---

## 6. Handler capabilities

Handlers run in the host process. The host process is "core runtime + plugin" (RFC #2549 design §7.1) and already has a
database connection, an access service rebuilt the way Workers rebuild it (`AccessControlService`), and the Scope service,
so every capability on `ctx` is implemented locally in the host process and authorized as the request's caller:

| Capability | Implementation |
| --- | --- |
| `ctx.require(action, scope_id)` | Checks the caller's permission with the local access service |
| `ctx.allowed(action, scope_id)` | As above, returning a boolean instead of raising |
| `ctx.caller` | The current request's caller |
| `ctx.reads(scope_id)` | Checks `scope.read`, then creates a read view filtered as the caller |
| `ctx.writer(scope_id)` | Creates a writer as the caller (authorized as in RFC #2549 design §6.5); writes are authorized against the caller's `scope.contribute`, without restricting the kinds of writes |
| `ctx.sources.submit(scope_id, definition, value)` | Checks the caller's `scope.contribute`; runs `resolve` and the Projections and saves as the caller |
| `ctx.scopes.create(...)` | Checks that the caller has `server.admin`; creates the Scope with the external reference `{"kind": "powercontext.plugin", "value": <plugin name>}`; then makes the caller the Scope's administrator, as `POST /v1/scopes` does |
| `ctx.scopes.update(...)` | Checks that the caller has `server.admin` and that the Scope carries this plugin's external reference, then runs as `PUT /v1/scopes/{scope_id}` does |
| `ctx.scopes.list()` | Lists the Scopes the caller can see that carry this plugin's external reference |

---

## 7. Plugin pages

### 7.1 Routes

| Route | Authentication | Description |
| --- | --- | --- |
| `GET /dashboard/plugins/{name}` | Dashboard session | The hosting page; `?scope=` selects the Scope |
| `GET /dashboard/plugins/{name}/ui/{path:path}` | None | Static files from the plugin's `directory` (from the current snapshot once hot reload exists) |
| `* /dashboard/plugins/{name}/api/{path:path}` | Dashboard session + same origin | Server-side forwarding to `/v1/plugins/{name}/api/{path}` |
| `GET /dashboard/plugins/_client.js` | None | Client script for plugin pages |

### 7.2 Static files

- The resolved path must lie inside `directory`; `..` and symlinks leading outside are rejected;
- extension allowlist: `.html`, `.js`, `.css`, `.json`, `.svg`, `.png`, `.jpg`, `.webp`, `.woff2`; at most 5 MiB per file;
- response headers (`<own>` is the absolute URL of `/dashboard/plugins/{name}/ui/`, `<client>` that of `_client.js`):

  ```text
  Content-Security-Policy: sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads;
                           default-src 'none'; script-src <own> <client>; style-src <own> 'unsafe-inline';
                           img-src <own> data: https:; font-src <own>; connect-src https:; form-action https:;
                           base-uri 'none'; frame-ancestors 'self'
  X-Content-Type-Options: nosniff
  Referrer-Policy: no-referrer
  Cache-Control: no-cache
  ```

  `sandbox` puts the page in an opaque origin where `'self'` cannot match the plugin's own URLs, so scripts, styles,
  images, and fonts are listed by absolute URL.

### 7.3 Hosting page script

```text
render <iframe sandbox="allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-downloads">
register listeners first, then set src="/dashboard/plugins/{name}/ui/{entry}", so the first load is the plugin page
loads = 0
iframe.onload:
  loads += 1
  loads == 1: send {type: "powercontext.context", context}
  loads > 1: stop forwarding and show a notice asking the user to reopen the plugin page
window.onmessage(event):
  not forwarding, or event.source !== iframe.contentWindow: ignore
  data.type == "powercontext.ready": send context again
  data.type == "powercontext.request":
    method and path match a route the plugin declared (the route table ships with the page), and the path has no . or .. segment
      → fetch("/dashboard/plugins/{name}/api" + path, {method, body, credentials: "same-origin"})
    otherwise reply {status: 404, error: "route_not_declared"}
    reply {type: "powercontext.response", id, status, contentType, filename, json | blob}; on network errors {status: 0, error: "network_error"}
```

### 7.4 Message format

| Direction | Message |
| --- | --- |
| Hosting page → plugin page | `{type: "powercontext.context", context: {scopeId, locale, theme}}` |
| Plugin page → hosting page | `{type: "powercontext.ready"}`: sent once the client script loads; the hosting page replies with context |
| Plugin page → hosting page | `{type: "powercontext.request", id, method, path, query, json?, body?, contentType?}` |
| Hosting page → plugin page | `{type: "powercontext.response", id, status, contentType, filename?, json? \| blob?, error?}` |

`_client.js` defines `window.powercontextPluginClient()`, which returns `context(): Promise<Context>` and
`request(method, path, {query, json, body, contentType}): Promise<{status, ok, contentType, filename, json, blob}>`. It
matches responses by `id`, accepts messages only from the parent window, and rejects with an error carrying `status` on
`error`. The client sets no timeout; the server route's limit applies.

### 7.5 Server-side forwarding

`/dashboard/plugins/{name}/api/...` requires a Dashboard session and `Sec-Fetch-Site: same-origin`; without that header,
`Origin` must equal the Server's origin. `Origin: null` is always rejected with 403. A plugin that is not loaded or
declares no pages returns 404. Otherwise the request is handled as `/v1/plugins/{name}/api/...` is (§5.1) under the
session's identity, returning the status, `Content-Type`, `Content-Disposition`, and body.

---

## 8. Observability

Implemented: plugin API requests use the audit operation name `plugin:<plugin name>:<handler>`. The following fields and
metrics ship with hot reload:

| Field | Meaning |
| --- | --- |
| `plugin_event` | `loaded`, `updated`, `removed`, `check_failed` |
| `version_digest` | Digest of the snapshot that took effect |
| `declaration_digest` | Digest of the declaration that took effect |
| `plugin_route` | Route template of a plugin API, without actual parameter values |
| `plugin_method` | Gains `api` |
| `scan_ms` | Duration of one scan, in milliseconds |

Metrics:

- `powercontext_plugin_scan_duration_seconds` (histogram);
- `powercontext_plugin_api_requests_total`, labeled `plugin_name`, `plugin_route`, and status class (2xx, 4xx, 5xx);
- `powercontext_plugin_api_duration_seconds` (histogram), with the same labels.

---

## 9. Defaults

| Item | Default | Configuration |
| --- | --- | --- |
| Scan interval (to implement) | 60 seconds | `POWERCONTEXT_SERVER_PLUGINS_RESCAN_SECONDS` (0 disables hot reload) |
| Settling | Same digest for 2 consecutive scans | Fixed |
| Host process readiness timeout | 60 seconds | Fixed |
| Default route time limit | 30 seconds | Declaration `timeout_seconds` |
| JSON request body limit | 1 MiB | Route `max_body_bytes` |
| Binary request body limit | 32 MiB | Route `max_body_bytes`, capped by `POWERCONTEXT_SERVER_PLUGIN_API_MAX_BODY_BYTES` |
| Routes per plugin | At most 200 | Fixed |
| Page static file limit | 5 MiB | Fixed |
| Data upgrade time limit (to implement) | 600 seconds | Declaration `upgrade_timeout_seconds` |

---

## 10. Task breakdown

| # | Task | Depends on | Status |
| --- | --- | --- | --- |
| T1 | Scan loop, version digest, settling | RFC #2549 implementation (#2616) | To implement |
| T2 | Version snapshots: creation, read-only files, reference counts, cleanup; check child processes, host processes, and Workers import from snapshots | T1 | To implement |
| T3 | Switching: addition, update, removal; registering and unregistering declaration differences; host process replacement | T2 | To implement |
| T4 | Plugin routes: declaration, matching, authorization, request bodies, response mapping, binary file exchange | RFC #2549 implementation (#2616) | Done in #2616 |
| T5 | Handler capabilities: `require`, `reads`, `writer`, `sources.submit`, `scopes`, implemented locally in the host process | T4 | Done in #2616 |
| T6 | Plugin OpenAPI generation | T4 | To implement |
| T7 | Plugin pages: static files, hosting page, forwarding, `_client.js` | T4 | Done in #2616 |
| T8 | `/v1/plugins` response fields and observability | T3, T4 | To implement |
| T9 | Browser verification: how `'self'` matches under the CSP `sandbox` directive and cookie behavior for opaque-origin requests, on Chromium, Firefox, and Safari | T7 | Chromium verified manually; Firefox and Safari to verify |
| T10 | MCP tools: generation, annotations, adding and removing on hot reload with notifications | T3, T4 | To implement |
| T11 | Data upgrades: running `upgrade` at host process startup, the plugin-identity context, handling `upgrade_failed`, bindings waiting for readiness | T3, T5 | To implement |

---

## 11. Open items

| Item | Notes |
| --- | --- |
| Several instances sharing a data directory on one machine | Whether cleaning up only "snapshots this process created" is enough, or each instance should use its own data directory |
| Streaming large request bodies | The body is read whole and passed as bytes over the `Pipe`; whether to stream it directly to the host process |
| Browser compatibility | Page behavior under the CSP `sandbox` directive was verified manually on Chromium with Playwright; Firefox and Safari remain (T9) |

---

## 12. Design decisions beyond the RFC

The RFC defines only the contract. The table lists decisions this document makes to implement it that the RFC does not
prescribe, to confirm one by one in review. The host process model and communication are D1 and D2 in §16 of the RFC
#2549 design.

| # | Decision | Rationale | Status |
| --- | --- | --- | --- |
| E1 | Version snapshots live in the data directory, named by content digest and made read-only (§3) | Matches Dify unpacking plugin packages per version; read-only files keep snapshots unchanged | Adopted |
| E2 | When several instances on one machine share a data directory, each process cleans up only snapshots it created and no longer uses (§3.4) | Avoids deleting snapshots another instance is using | To confirm: each instance could instead use its own data directory |
| E3 | Binary request and response bodies of plugin APIs pass as bytes over the `Pipe`, under one limit (§5.2) | Follows RFC #2549 design D1; no temporary files to clean up. The cost is a peak memory of about twice the limit per request | Adopted |
| E4 | Plugin API authorization and every capability on `ctx` are implemented locally in the host process; the Server only finds the route, authenticates, and limits body size (§5.1, §6) | Follows RFC #2549 design D2; no callback channel | Adopted |
| E5 | Route matching tries paths without parameters first, then declaration order (§5.1) | Consistent with common web frameworks; parameterized paths cannot swallow fixed ones | Adopted |
| E6 | The OpenAPI `operationId` and the MCP tool name are both `<plugin name>__<handler>` (§5.4, §5.5) | Stable, predictable, and easy to cross-reference; plugin names contain no underscores, so the double-underscore separator cannot collide | To implement |
| E7 | MCP tools reuse the existing internal HTTP bridge, with authorization done by the HTTP route itself (§5.5) | Consistent with built-in tools, with no second authorization path | To implement |
| E8 | After a plugin page navigates or reloads, forwarding stops and a notice asks the user to reopen it (§7.3) | After navigation the plugin page cannot be told apart from an external page | Adopted |
| E9 | Static files are served with `no-cache` (§7.2) | Browsers fetch new files right after a plugin update; once hot reload exists, an ETag prefixed with the version digest can be added | Adopted |
| E10 | Instances that receive requests run `upgrade` in the new host process before `setup`; `background` instances use a short-lived child process that runs only `upgrade` (§4.7) | The host process has to start before the new version is used anyway, so it reuses that runtime and its resources; `background` instances have no host process yet must keep new `generate` on upgraded data. A failure follows the keep-the-old-version path | To confirm |
| E11 | During `upgrade`, `reads` and `writer` act as the plugin itself and caller-related capabilities are unavailable (§4.7) | An upgrade has no request caller, consistent with the Worker permission model | To confirm |
| E12 | A host process restarted after a crash runs `upgrade` again (§4.7) | Consistent with "called on every load and must be repeatable", with no need to record whether this process already upgraded | To confirm |
