# System design: Plugin Reload and APIs

| Item | Content |
| --- | --- |
| Design source | [RFC #2579 Plugin hot reload and custom APIs](https://github.com/oceanbase/powercontext/pull/2579) |
| Prerequisite design | [Plugin interfaces design](2549_plugin_interfaces_design.md) (RFC #2549) |
| Status | Design draft |
| Date | 2026-11-11 |
| Stack | Python 3.11+ / pydantic v2 / FastAPI / Starlette; the Dashboard is server-rendered pages with small scripts |

This is the implementation design for RFC #2579, built on the implementation design for RFC #2549: host processes, the
active plugin record, the writer, and user-change mode all come from that document, and this one describes only what is
added or changed. Where this conflicts with the RFC, the RFC wins and this document is revised.

---

## 0. Invariants

| # | Invariant | How the implementation guarantees it |
| --- | --- | --- |
| C1 | One call uses one version of plugin code | Child processes import only from read-only version snapshots; a snapshot is not deleted while in use |
| C2 | The version identity detects any content change | The version digest is computed from file contents, never from sizes and modification times |
| C3 | Core performs plugin API authorization | The route layer checks `access` first, and each read and write capability on `ctx` checks again as the caller |
| C4 | Plugin pages cannot borrow the user's session | Pages run in an opaque origin; the hosting page forwards only for an iframe that has not navigated, and only to routes the plugin declared |

---

## 1. Modules and files

| File | Status | Responsibility |
| --- | --- | --- |
| `src/powercontext/server/plugin_reload.py` | New | Scan loop, digests, settling, snapshot creation and cleanup |
| `src/powercontext/server/plugins.py` | Changed | `PluginManager` gains switching for additions, updates, and removals; the active plugin record gains version information |
| `src/powercontext/server/plugin_hosts.py` | Changed | Host process replacement: switch once the new process is ready, stop the old one once drained |
| `src/powercontext/server/plugin_api.py` | New | The plugin route group: route matching, authorization, request bodies, handing off to the host process, response mapping, OpenAPI generation |
| `src/powercontext/builtin/plugins/host.py` | Changed | Handles `api` requests; provides `ctx.require`, `ctx.reads`, `ctx.writer`, `ctx.sources`, `ctx.scopes` |
| `src/powercontext/builtin/plugins/host_protocol.py` | Changed | Adds `api` requests and responses and capability callback messages |
| `src/powercontext/plugins.py` | Changed | `PluginApi`, `Route`, `ApiRequest`, `BinaryResponse`, `ApiError`, `Caller`, `PluginPages` |
| `src/powercontext/server/dashboard/plugins.py` | New | Hosting pages for plugin pages, static files, server-side forwarding, `_client.js` |
| `src/powercontext/server/dashboard/static/plugin-host.js` | New | Hosting page script: iframe management and message forwarding |
| `src/powercontext/server/dashboard/static/plugin-client.js` | New | Client script for plugin pages |
| `openapi/powercontext.yaml` | Changed | `GET /v1/plugins/{name}/openapi.json`; `/v1/plugins` response fields |

---

## 2. Scanning

### 2.1 Scan loop

```text
Each Server process, every rescan seconds (and at startup):
  -> list the direct subdirectories of the plugin directory
  -> compute each one's version digest in a thread pool (§2.2)
  -> digest equals the previous scan and the current version: skip
  -> digest differs from the previous scan: mark as settling and do not check this round
  -> digest is the same for two consecutive scans and differs from the current version: create a version snapshot (§3)
     and check it
  -> add, update, or unload according to the result (§4)
  -> directories present before but missing now: unload
  -> clean up unused snapshots (§3.4)
  -> record scan_ms
```

- The scan loop is an asyncio task in the Server process; digests are computed in a thread pool so the event loop is
  not blocked.
- The first scan at startup does not wait for settling; it creates a snapshot for each directory and checks it.
- Only one scan runs at a time; a trigger is skipped while the previous scan is still running.
- When `POWERCONTEXT_SERVER_PLUGINS_RESCAN_SECONDS` is 0, the scan loop does not start, plugins load directly from the
  plugin directory, and the code fingerprint is checked under RFC #2549's rules.

### 2.2 Version digest

The version digest uses the same algorithm as RFC #2549's code fingerprint: every file in the directory (excluding
`__pycache__`) is sorted by relative path, and each relative path and file content is fed into SHA-256 in turn.

On a 2-core development machine, one digest over 14 MB in 676 files (`src/powercontext`, with files in the page cache)
takes about 230 ms; reading only metadata takes about 33 ms. Plugin directories hold no dependencies and are usually
much smaller.

### 2.3 Per-directory scan state

```python
@dataclass
class ScanState:
    last_digest: str | None      # digest from the previous scan
    stable_rounds: int           # consecutive rounds with the same digest
    failed_digest: str | None    # digest of the latest failed check, so the same content is not checked again
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

1. Copy to `<data directory>/plugin-versions/<plugin name>/.tmp-<random>/`, skipping `__pycache__`;
2. recompute the digest of the temporary directory and give up for this round if it differs from the scanned digest
   (the directory changed during the copy);
3. make the files read-only (files `0444`, directories `0555`);
4. rename to `<version digest>/`; if the target already exists, delete the temporary directory and use the existing
   snapshot.

### 3.3 Use

- Check child processes, host processes, and Workers all use the snapshot directory as the plugin directory: it goes on
  `sys.path`, and `ctx.plugin_dir` points to it.
- `PluginWorkerSpec` gains a `snapshot` field; the Supervisor binding's launcher reads the current snapshot at dispatch
  time, so after a version switch newly dispatched Workers use the new snapshot without re-registering the binding.
- Code fingerprints are not checked when using snapshots: snapshots are read-only, and the digest in their path is their
  content.

### 3.4 References and cleanup

`PluginManager` keeps an in-process reference count per snapshot: 1 for the current version, 1 for each running host
process, and 1 for each dispatched Worker that has not finished. The end of each scan deletes snapshots with a reference
count of 0.

At startup, after the first scan creates the current snapshots, Server deletes every other snapshot and leftover
temporary directory under `plugin-versions/`. When several Server processes share one data directory (several instances
on one machine), each process deletes only snapshots it created and no longer uses, and startup cleanup deletes only
snapshots modified before this process started.

---

## 4. Switching

### 4.1 State

The active plugin record (RFC #2549 design §3.2) gains:

```python
    version_digest: str | None         # digest of the current snapshot
    snapshot: Path | None              # current snapshot directory
    activated_at: datetime | None
    last_check_failure: str | None     # category of the latest failed check; cleared after a successful switch
    state: Literal["loaded", "failed"]
```

### 4.2 Addition

Once the check passes, registration runs as in RFC #2549 design §3.4, plus plugin routes (§5) and pages (§7). A failed
check sets `state="failed"`.

### 4.3 Update

| Case | Handling |
| --- | --- |
| Check passes, declaration digest unchanged | Update `version_digest` and `snapshot`; replace the host process (§4.5) |
| Check passes, declaration digest changed | Compute the difference between old and new declarations: unregister removed Source types, routes, and pages and register added ones; a renamed family is a removed family plus an added one; update the binding's scheduling parameters with `replace_binding`; update the snapshot; replace the host process |
| Check fails | Record only `last_check_failure` and a `check_failed` event; the current snapshot and host process are unchanged |

### 4.4 Removal

Unregister Source types, the family, the merge registration, routes, pages, and the binding (`replace_binding(name,
None)`); stop accepting new synchronous calls for the plugin; stop the host process once in-flight synchronous calls finish
or time out; decrement the current snapshot's reference count.

### 4.5 Host process replacement

```text
  -> start a new host process from the new snapshot and wait for ready (a timeout counts as a failed start)
  -> failed start: stop the new process, keep the old one, record check_failed
  -> ready: atomically replace PluginHostHandle with the new process; new requests go to it
  -> mark the old process draining: it accepts no requests; once in-flight requests finish it receives shutdown;
     if any request exceeds its time limit, the old process is terminated
  -> once the old process exits, decrement the old snapshot's reference count
```

When the host process has not started yet (the plugin has had no synchronous call), only the snapshot is updated, and
the next synchronous call starts directly from the new snapshot.

### 4.6 Multiple instances

- Instances scan independently, and `/v1/plugins` returns this instance's view, including `version_digest`,
  `declaration_digest`, `state`, `activated_at`, and `last_check_failure`.
- Only the leader registers bindings; when leadership moves, the new leader registers from its own record.

---

## 5. Plugin routes

### 5.1 Mounting

Server registers one catch-all route for the plugin group:

```text
/v1/plugins/{name}/api/{path:path}     (GET, POST, PUT, PATCH, DELETE)
```

Handling:

1. find the active plugin by `name`; return 404 if it is missing or `state != "loaded"`;
2. match `(method, path)` against the plugin's route table, compiled from the declaration into Starlette `compile_path`
   regular expressions; paths without parameters match first, then routes in declaration order;
3. authenticate the caller;
4. for Scope access, take the path parameter named by `scope_param` as the Scope ID and authorize `scope.read` or
   `scope.contribute`; `authenticated` requires only successful authentication;
5. read the request body: at most 1 MiB for `json`, and at most `POWERCONTEXT_SERVER_PLUGIN_API_MAX_BODY_BYTES` for
   `binary`, returning 413 beyond that;
6. send `HostRequest(method="api", ...)` to the host process;
7. map the response (§5.3).

### 5.2 Binary request and response bodies

Request and response bodies do not use the stdin / stdout JSON channel. Server writes a binary request body to
`<data directory>/plugin-io/<request id>.in`, and `HostRequest` carries only the path; the host process writes a binary
response to `<request id>.out` and returns its path, `content_type`, and `filename` in `HostResponse`. Server streams it
back and deletes both files when the response ends or the request fails.

### 5.3 Response mapping

| Host process result | HTTP |
| --- | --- |
| A `response_model` instance | 200, JSON |
| `BinaryResponse` | 200, with headers from `content_type` and `filename` |
| `None` | 204 |
| `ApiError(status, code, message)` | `status`, with body `{"code", "message"}`; treated as 500 when `status` is outside 400–499 or is 401 or 403 |
| `ctx.require` or write authorization fails | 403 |
| `request_model` validation fails | 422 |
| Other exceptions | 500, logging only the exception type |
| Exceeds `timeout_seconds` | 504 |
| Host process unavailable or in backoff | 503 |

### 5.4 OpenAPI

`GET /v1/plugins/{name}/openapi.json` is generated from the route table: one operation per route with `operationId`
`<name>_<handler>`; JSON Schemas of `request_model` and `response_model` from the declaration the check child process
exported; `application/octet-stream` for `binary` bodies; shared 401, 403, 404, 413, 422, 500, 503, and 504 error
responses; and the extension `x-powercontext-access` recording `access` and `scope_param`.

---

## 6. Handler capabilities

Handlers run in the host process, while authorization and Scope management need Server's authentication context. The
capabilities on `ctx` are therefore implemented in two ways:

| Capability | Where | Implementation |
| --- | --- | --- |
| `ctx.require(action, scope_id)` | Callback to Server | The host process sends `CapabilityCall(kind="require")`, and Server runs the authorization check with the request's authentication context |
| `ctx.reads(scope_id)` | Host process | First calls back to Server to check `scope.read`, then creates a read-only view in the host process, filtering reads by the caller's identity |
| `ctx.writer(scope_id)` | Host process | Accepts only `request.scope_id` on a `scope.contribute` route; creates a writer as the caller (authorized as in RFC #2549 design §5.5), without restricting the kinds of writes |
| `ctx.sources.submit(scope_id, definition, value)` | Host process | Same scope restriction; runs `resolve` and Projections in the host process and saves as the caller |
| `ctx.scopes.create(...)` | Callback to Server | Server calls `ScopeApplication.create` with the external reference `{"kind": "powercontext.plugin", "value": <plugin name>}`, then `bootstrap_static_scope` so the caller becomes the administrator; authorized as `POST /v1/scopes` (`server.admin`) |
| `ctx.scopes.update(...)` | Callback to Server | First confirms the Scope carries this plugin's external reference, then follows the implementation and authorization of `PUT /v1/scopes/{scope_id}` |
| `ctx.scopes.list()` | Callback to Server | Lists Scopes the caller can see that carry this plugin's external reference |

Callbacks share the stdin / stdout channel: the host process sends a `CapabilityCall` with a `call_id`, and Server
replies with a `CapabilityResult`. A callback is valid only while its API request is in progress; callbacks arriving
after the request ends are rejected.

---

## 7. Plugin pages

### 7.1 Routes

| Route | Authentication | Description |
| --- | --- | --- |
| `GET /dashboard/plugins/{name}` | Dashboard session | Hosting page; `?scope=` selects the Scope |
| `GET /dashboard/plugins/{name}/ui/{path:path}` | Not required | Static files from `directory` in the current snapshot |
| `* /dashboard/plugins/{name}/api/{path:path}` | Dashboard session + same origin | Server-side forwarding to `/v1/plugins/{name}/api/{path}` |
| `GET /dashboard/plugins/_client.js` | Not required | Client script for plugin pages |

### 7.2 Static files

- After resolving the path, confirm it lies inside `directory` in the snapshot, rejecting `..` and symbolic links
  pointing outside;
- extension allowlist: `.html`, `.js`, `.css`, `.json`, `.svg`, `.png`, `.jpg`, `.webp`, `.woff2`; each file at most
  5 MiB;
- response headers:

  ```text
  Content-Security-Policy: sandbox allow-scripts allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox;
                           default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline';
                           img-src 'self' data: https:; font-src 'self'; connect-src https:; frame-ancestors 'self'
  X-Content-Type-Options: nosniff
  Cache-Control: no-cache
  ETag: "<version digest>:<relative path>"
  ```

### 7.3 Hosting page script

```text
render <iframe sandbox="allow-scripts allow-forms allow-downloads allow-popups allow-popups-to-escape-sandbox"
              src="/dashboard/plugins/{name}/ui/{entry}">
loads = 0
iframe.onload:
  loads += 1
  loads == 1: postMessage(iframe.contentWindow, {type: "powercontext.context", scopeId, locale, theme}, "*")
  loads > 1: stop forwarding and show "Reopen the plugin page"; clicking it removes and rebuilds the iframe and resets loads
window.onmessage(event):
  ignore when event.source !== iframe.contentWindow or loads != 1
  data.type == "powercontext.request":
    path matches a route the plugin declared (the route table ships with the page) → fetch("/dashboard/plugins/{name}/api" + path, {method, body})
    otherwise reply with status 404
    reply {type: "powercontext.response", id, status, headers, body}; binary responses come back as a Blob
```

### 7.4 Message formats

| Direction | Message |
| --- | --- |
| Hosting page → plugin page | `{type: "powercontext.context", scopeId: string, locale: "zh" \| "en", theme: "light" \| "dark"}` |
| Plugin page → hosting page | `{type: "powercontext.request", id: string, method: string, path: string, query?: Record<string, string[]>, body?: unknown \| Blob}` |
| Hosting page → plugin page | `{type: "powercontext.response", id: string, status: number, headers: Record<string, string>, body: unknown \| Blob}` |

`_client.js` exports `powercontextPluginClient()`, which provides `context(): Promise<Context>` and
`request(method, path, {query, body}): Promise<{status, headers, body}>`, matching responses by `id` with a default
60-second timeout.

### 7.5 Server-side forwarding

`/dashboard/plugins/{name}/api/...` requires the Dashboard session cookie and `Sec-Fetch-Site: same-origin`; when that
header is missing, `same_origin` from `dashboard/session.py` checks `Origin`, and `Origin: null` is always rejected. It
then takes the session identity with `authentication_headers`, calls `/v1/plugins/{name}/api/...`, and returns the status
code, `Content-Type`, `Content-Disposition`, and body unchanged.

---

## 8. Observability

| Field | Meaning |
| --- | --- |
| `plugin_event` | `loaded`, `updated`, `removed`, `check_failed` |
| `version_digest` | Digest of the snapshot that took effect |
| `declaration_digest` | Declaration digest that took effect |
| `plugin_route` | Route template of a plugin API, without actual parameter values |
| `plugin_method` | Adds `api` |
| `scan_ms` | Duration of one scan, in milliseconds |

Metrics:

- `powercontext_plugin_scan_duration_seconds` (histogram);
- `powercontext_plugin_api_requests_total`, labeled `plugin_name`, `plugin_route`, and status class (2xx, 4xx, 5xx);
- `powercontext_plugin_api_duration_seconds` (histogram), with the same labels.

---

## 9. Defaults

| Item | Default | Configuration |
| --- | --- | --- |
| Scan interval | 60 seconds | `POWERCONTEXT_SERVER_PLUGINS_RESCAN_SECONDS` (0 disables hot reload) |
| Settling | 2 consecutive rounds with the same digest | Fixed |
| Host process ready timeout | 60 seconds | Fixed |
| Default route time limit | 30 seconds | Declaration `timeout_seconds` |
| JSON request body limit | 1 MiB | Fixed |
| Binary request body and response limit | 32 MiB | `POWERCONTEXT_SERVER_PLUGIN_API_MAX_BODY_BYTES` |
| Page static file limit | 5 MiB | Fixed |
| `_client.js` request timeout | 60 seconds | Plugin pages may pass their own |

---

## 10. Task breakdown

| # | Task | Depends on |
| --- | --- | --- |
| T1 | Scan loop, version digest, settling | RFC #2549 design T2 |
| T2 | Version snapshots: creation, read-only files, reference counts, cleanup; check child processes, host processes, and Workers import from snapshots | T1, RFC #2549 design T3 |
| T3 | Switching: addition, update, removal; registering and unregistering declaration differences; host process replacement | T2 |
| T4 | Plugin routes: declaration, matching, authorization, request bodies, response mapping, binary file exchange | RFC #2549 design T3 |
| T5 | Handler capabilities: callback channel, `require`, `reads`, `writer`, `sources.submit`, `scopes` | T4 |
| T6 | Plugin OpenAPI generation | T4 |
| T7 | Plugin pages: static files, hosting page, forwarding, `_client.js` | T4 |
| T8 | `/v1/plugins` response fields and observability | T3, T4 |
| T9 | Browser verification: how `'self'` matches under the CSP `sandbox` directive and cookie behavior for opaque-origin requests, on Chromium, Firefox, and Safari | T7 |

---

## 11. Open items

| Item | Notes |
| --- | --- |
| Exposing plugin APIs as MCP tools | The existing MCP server generates tools from Server's OpenAPI, and plugin APIs could be generated the same way. To settle: route-level opt-in (for example `mcp=True`), JSON routes only, tool names prefixed with the plugin name, and adding or removing tools with `tools/list_changed` notifications on hot reload |
| Several instances sharing a data directory | Whether distinguishing snapshots by "created by this process" is enough, or each instance must use its own data directory |
| Streaming large request bodies | The first version reads the whole body and exchanges it through files; whether to stream it directly to the host process |
