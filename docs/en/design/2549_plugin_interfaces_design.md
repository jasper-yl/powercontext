# System Design: Plugin Interfaces

| Item | Content |
| --- | --- |
| Design source | [RFC #2549 Plugin interfaces](https://github.com/oceanbase/powercontext/pull/2549) |
| Related designs | [Plugin reload and APIs design](2579_plugin_reload_and_apis_design.md) (RFC #2579); Source plugins and collection are in [RFC #2617](https://github.com/oceanbase/powercontext/pull/2617) |
| Implementation | [oceanbase/powercontext#2616](https://github.com/oceanbase/powercontext/pull/2616) |
| Date | 2026-11-13 |
| Stack | Python 3.11+ / pydantic v2 / SQLAlchemy 2.x / FastAPI; contract source of truth `openapi/powercontext.yaml` |

This is the implementation design of RFC #2549. The RFC defines the contract seen by plugin authors, callers, and
deployers; this document defines how core implements that contract: module layout, execution flows, the process model,
persistence, and defaults. When the two conflict, the RFC wins and this document is revised. Section 2 lists what #2616
implements; the remaining parts describe the target design.

---

## 0. Interfaces a plugin implements

There are two plugin templates: Artifact plugins (this document, RFC #2549 and #2579) and Source plugins (RFC #2617).
Both are subdirectories of the plugin directory whose `plugin.py` assigns a declaration to the module variable `plugin`;
the directory name is the plugin name.

### 0.1 Artifact plugins: declaration

Fields of `plugin = ArtifactPlugin(...)`. "Conditional" in the Required column means required only under some
configuration.

| Field | Required | Default | Description | Source |
| --- | --- | --- | --- | --- |
| `family` | Yes | — | The Artifact family the plugin owns; must not repeat a built-in family or another plugin's | #2549 |
| `setup` | Yes | — | Async factory returning the plugin object; runs once per Worker invocation, and once per host process, which reuses it | #2549 |
| `inputs` | Conditional: when consuming Sources | `None` | `Inputs(source_types, projection, projections, max_sources=50)` | #2549 |
| `schedule` | No | `None` | `Interval` or `Cron`; `Cron` needs `inputs` and cannot combine with `on_sources`; a `Cron` time zone defaults to that of the machine running Server | #2549 |
| `on_sources` | No | `False` | Run when new Sources arrive; needs `inputs` | #2549 |
| `scopes` | Conditional: with an `Interval` and no `inputs` | `None` | The Scopes served | #2549 |
| `definitions` | No | `()` | The plugin's own Source Definitions | #2549 |
| `max_workers` | No | 1 | Concurrency across Scopes | #2549 |
| `host_processes` | No | 1 | Suggested limit on this plugin's host processes in each Server process; deployers can override it (not implemented) | #2549 |
| `timeout_seconds` | No | 300 | Time limit of one background generation | #2549 |
| `transaction_timeout_seconds` | No | 30 | Time limit of the write transaction block | #2549 |
| `change_timeout_seconds` | No | 30 | Time limit of one user change, rendering, or search | #2549 |
| `api` | No | `None` | `PluginApi(routes=...)`, the plugin's own APIs | #2579 |
| `pages` | No | `None` | `PluginPages(directory, entry, title)`, a Dashboard page | #2579 |
| `upgrade` | No | `None` | Data upgrade hook called each time a plugin version loads (not implemented) | #2579 |
| `upgrade_timeout_seconds` | No | 600 | Time limit of `upgrade` (not implemented) | #2579 |

The declaration is validated at construction: `inputs` needs `schedule` or `on_sources`, and with neither the plugin has
no background generation, so its Artifacts are written only by user changes and plugin APIs;
`transaction_timeout_seconds` must not exceed `timeout_seconds`; declaring `pages` requires `api`.

### 0.2 Artifact plugins: methods of the plugin object

Methods of the object `setup(ctx)` returns. The host process checks the required methods after `setup`; a plugin missing
one is `failed`.

| Method | Required | Called when | Runs in |
| --- | --- | --- | --- |
| `render_artifact(request) -> Rendering` | Yes | `GET .../artifacts/{family}/{artifact_id}/rendering` | Host process |
| `generate(request, writer)` | Conditional: with `schedule` or `on_sources` | On schedule, or when new Sources arrive | Worker |
| `search_artifacts(request) -> SearchResult` | No; search returns 422 without it | Generic search API; Prepared Context sections that list the family | Host process |
| `create_artifact(request, writer)` | No; returns 422 without it | Generic create API | Host process |
| `update_artifact(request, writer)` | No | `PUT` replacement | Host process |
| `merge_artifacts(request, writer)` | No | Generic merge API | Host process |
| `forget_artifact(request, writer)` | No | Generic forget API | Host process |
| `restore_artifact(request, writer)` | No | Restore, restore to an earlier revision, undo a merge | Host process |
| Route handlers `handler(request)` | Conditional: with `api` | `/v1/plugins/{name}/api{path}` | Host process |

Listing, reading the current content or a given revision, revision history, governance state, and tags do not call the
plugin; core serves them directly.

### 0.3 Source Definitions

A plugin that provides Source types in `definitions` implements, for each definition:

| Member | Required | Description |
| --- | --- | --- |
| `name`, `version` | Yes | Source type name and definition version; must not repeat a built-in type |
| `input_class` | Yes | Pydantic model of the written content |
| `resolve(value)` | Yes | Pure function giving the Source identity and canonical content |
| Projection `project`, `read` | Yes | Turn the Source into the view Artifact plugins consume |

### 0.4 What `ctx` provides

| Capability | Worker | Host process | Description |
| --- | --- | --- | --- |
| `ctx.name`, `ctx.scope_id`, `ctx.plugin_dir`, `ctx.logger` | Yes | Yes (`scope_id` is `None`) | Basics |
| `ctx.storage` | Yes | Yes | Dedicated storage, section 9 |
| `ctx.models` | Yes | Yes | The Server's configured models, section 9 |
| `ctx.enter_async_context(...)` | Yes | Yes | Resources closed with the call or process |
| `ctx.caller`, `ctx.require`, `ctx.allowed` | — | Yes | The current request's caller and permission checks |
| `ctx.reads(scope_id)`, `ctx.writer(scope_id)` | — | Yes | Read and write the plugin's family as the caller |
| `ctx.sources.submit(...)` | — | Yes | Submit the plugin's Sources as the caller |
| `ctx.scopes.create/update/list` | — | Yes | Manage the Scopes the plugin created |

### 0.5 Source plugins (RFC #2617)

RFC #2617 defines the Source plugin contract, which is not implemented yet; the interfaces are listed here for
comparison:

| Interface | Required | Description |
| --- | --- | --- |
| `version` | Yes | Plugin version, compared under PEP 440; the central Server checks device versions against it |
| `min_compatible_version` | No | The lowest device version the central Server still accepts; defaults to `version` |
| `definitions` | Yes | Source Definitions, as in 0.3 |
| `target` | Conditional: when collecting | Target parameter model and identity expression |
| `collect` | Conditional: when collecting | Schedule and limits; the plugin object implements `collect(request) -> CollectResult` |
| `setup` | Conditional: when collecting | Async factory |
| `upgrade` | No | Called only when the central Server loads the plugin |

---

## 1. Invariants

| # | Invariant | How the implementation guarantees it |
| --- | --- | --- |
| C1 | The Server process never imports plugin code | Plugins are imported only in the check child process, host processes, and Workers; the Server process holds only the serializable declaration returned by the check |
| C2 | Plugins write core data only through the writer and `ctx.sources.submit` | Plugin code never receives core's database connection objects; the writer exposes only the write operations in the RFC |
| C3 | Cursors, invocation acknowledgment, and core data commit in one transaction | When the write transaction block ends, the executor performs the cursor CAS, `invocation.complete`, and the commit on the same connection |
| C4 | A user change completes exactly one agreed write | In user-change mode the writer records its operations and checks them when the block ends; otherwise it rolls back and returns 502 |
| C5 | Plugin-owned storage stays outside core transactions and schema | Core only allocates the dedicated storage location; it never creates or migrates tables there |

---

## 2. Implementation status

| Part | Status | Section |
| --- | --- | --- |
| Declarations, check child process, code fingerprint, registration | Implemented in #2616 | §4 |
| Worker executor, writer, write transaction block | Implemented in #2616 | §5, §6 |
| Host process, user changes, rendering, search, Prepared Context | Implemented in #2616 | §7, §8 |
| Host process groups (scaling with load, total limit), timeouts cancelling one request, heartbeats, Prepared Context omitting unavailable plugins, `Cron` defaulting to the machine time zone | Not implemented | §7.1, §7.3, §8.5 |
| Plugin-provided Source types | Implemented in #2616 | §8.6 |
| Dedicated storage | SQLite implemented in #2616; OceanBase / seekdb not implemented | §9.1 |
| Models | Implemented in #2616 | §9.2 |
| Removal of the #2246 Artifact plugin framework, migration `v1_5_0_005` | Implemented in #2616 | §10 |
| Plugin APIs and pages | Implemented in #2616; see the RFC #2579 design | — |
| Hot reload, data upgrades, MCP tools | Not implemented; see the RFC #2579 design | — |
| Structured observability fields | Not implemented | §12 |
| Source plugins, collection, removal of the old Source plugin framework | Not implemented; defined by RFC #2617 | — |

---

## 3. Modules and files

| File | Responsibility |
| --- | --- |
| `src/powercontext/plugins.py` | Public contract: `ArtifactPlugin`, `ArtifactPluginSpec`, `Inputs`, `Interval`, `Cron`, `PluginContext`, read and write interfaces, request and result models, `Rendering`, `SearchRequest` / `SearchResult`, `PluginApi`, `Route`, `PluginPages`, `ApiError`, `InvalidContent`, `ModelUnavailable` |
| `src/powercontext/builtin/plugins/manifest.py` | Imports `plugin.py`, validates the declaration, computes the code fingerprint, exports `PluginManifest`; entry point of the check child process |
| `src/powercontext/builtin/plugins/executor.py` | The `generate` executor in Workers: window, Projections, cursor, and acknowledgment |
| `src/powercontext/builtin/plugins/writer.py` | Writer, write transaction block, read views, user-change mode, `lineage_only` Sources, the merge service of plugin families |
| `src/powercontext/builtin/plugins/host.py` | Host process entry: rebuilds the database connection and access service, imports the plugin, calls `setup`, handles user changes, rendering, search, Source writes, and API requests; the request helpers behind `ctx` |
| `src/powercontext/builtin/plugins/host_protocol.py` | `PluginHostSpec` and the messages exchanged between the Server and host processes over a `Pipe` |
| `src/powercontext/builtin/plugins/storage.py` | Dedicated storage and models |
| `src/powercontext/builtin/plugins/families.py` | Plugin family registration and revision `metadata` |
| `src/powercontext/server/plugins.py` | `PluginManager`: checks, registration, bindings, host process preparation, the `/v1/plugins` view |
| `src/powercontext/server/plugin_hosts.py` | Server-side host process management: start, forward, terminate on timeout, restart after backoff, stop |
| `src/powercontext/server/app.py` | Forwards create, replace, merge, forget, restore, rendering, and search on plugin families to the host process; error mapping; `/v1/plugins` APIs; plugin routes |
| `src/powercontext/builtin/runtime/prepared_context.py` | Plugin family sections in Prepared Context |
| `src/powercontext/builtin/persistence/migrations/` | Migration `v1_5_0_005` (SQLite, MySQL) and the readiness check for plugin data |
| `openapi/powercontext.yaml` | Plugin-related APIs; after changing it, run `make api-generate` and `make contract-test` |

---

## 4. Declaration, check, and loading

### 4.1 Check child process

`check_plugin_directory` starts a check child process with `python -m powercontext.builtin.plugins.manifest <directory>`,
limited to 60 seconds. The child:

1. checks that the directory name matches `^[a-z][a-z0-9-]{0,63}$` and imports `plugin.py` (module name
   `powercontext_plugin_<name>`, with the plugin directory on `sys.path`);
2. confirms that the module variable `plugin` is an `ArtifactPlugin` and `setup` is an async function;
3. validates the declaration as `ArtifactPluginSpec`; with `pages`, confirms that `entry` is a file inside `directory`;
   builds a declarative manifest for each Source Definition;
4. computes the code fingerprint (§4.3);
5. writes `PluginManifest` to stdout as one line of JSON; on failure it writes only the `PluginConfigError` message or the
   exception type name.

The Server checks plugins one at a time in directory-name order. After a successful check, a family that repeats an
already loaded plugin's, or a Source type that repeats a built-in type, fails the load.

### 4.2 Plugin records

```python
class PluginManifest(BaseModel):            # serializable; crosses process boundaries
    name: str
    spec: ArtifactPluginSpec                # without setup, definitions, or route model classes
    fingerprint: str                        # code fingerprint
    source_manifests: tuple[dict, ...]      # declarative manifests of the Source Definitions

@dataclass
class _Plugin:                              # the record in PluginManager
    name: str
    directory: Path
    manifest: PluginManifest | None
    error: str | None                       # why the check or registration failed
    tasks: list[asyncio.Task]               # the plugin's background tasks
    host: PluginHost | None                 # host process handle, see §7
```

`PluginManager.plugins: dict[str, _Plugin]` is the single record in each Server process. It is not persisted and is
rebuilt from the plugin directory at Server startup. A plugin is `failed` when its check failed or its host process
reported a permanent failure, such as an object from `setup` that lacks a required method.

### 4.3 Code fingerprint

```python
def code_fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in root.rglob("*") if p.is_file() and "__pycache__" not in p.relative_to(root).parts):
        digest.update(str(path.relative_to(root)).encode()); digest.update(b"\0")
        digest.update(path.read_bytes()); digest.update(b"\0")
    return digest.hexdigest()
```

- Only file contents count; sizes and modification times are not used.
- Workers and host processes run `load_plugin` again when they import the plugin; a `PluginManifest` (including the
  fingerprint) that differs from the Server's check fails, asking for a Server restart.
- The fingerprint is recorded in `pc_artifacts.metadata` on every revision the plugin writes (§10).

### 4.4 Registration

After a successful check, `PluginManager._register`:

1. registers the Source Definition manifests in `pc_source_definition_manifests`; an identical registered manifest is
   reused, and a different one fails;
2. calls `register_plugin_family(family)`;
3. registers the family's merge service in the merge registry, recording the plugin name and fingerprint on the
   revisions it writes;
4. with `inputs`, calls `register_source_binding` so Source writes mark the plugin's binding pending;
5. with `schedule` or `on_sources`, and when this instance dispatches Workers, registers the binding with the Supervisor.

The binding is named `plugin:<name>`. `on_sources` maps to a 2-second `automatic_processing_interval`; with both an
`Interval` and `on_sources`, the smaller value wins; `Cron` maps to the binding's cron. For plugins without `inputs`, the
`_tick` task writes an invocation intent for each Scope in `scopes` on the `Interval`.

Instances in the `api` role register families and Source types but no bindings; instances in the `background` role
register bindings but receive no requests and start no host processes.

---

## 5. Worker executor

```text
Worker receives (plugin:<name>, scope_id, request_generation)
  -> short transaction: ScopeInvocation.start; load the cursor of (scope_id, plugin:<name>)
     -> with inputs: read the journal high-water mark, choose the window (after, through], through ≤ after + max_sources, read it
  -> filter by inputs.source_types; compute each Source's Projection by its type (the per-type value in projections, otherwise projection)
  -> empty window with inputs: a short transaction performs the cursor CAS and acknowledgment without loading the plugin
  -> load_plugin again and compare PluginManifest; open dedicated storage and models; call setup; call generate (within timeout_seconds)
     -> reads outside the transaction: separate read-only short transactions, recording the revisions read (§6.3)
     -> write transaction block starts: open a transaction; guard (lease and invocation intent)
        writes in the block run in this transaction
        block ends: cursor CAS to through; acknowledge (with whether work remains); commit
  -> generate returns without opening the block: a short transaction performs the cursor CAS and acknowledgment
  -> generate raises after the block committed: recorded as a post-commit error; the window is not retried
```

- Sources of other types in the window are filtered out, and the cursor still advances past the whole window; a Source of
  a matching type without the required Projection fails the invocation.
- The cursor CAS expects the `generation` read earlier, so a window commits once even across leadership changes or
  duplicate dispatch.
- With `scopes`, an invocation for a Scope outside the list fails at authorization.
- Finding pending work: Source writes in the Server process mark the plugin bindings that watch the Scope as pending;
  `SourceProcessingPendingProvider` catches up Sources written by other processes from the gap between the journal
  high-water mark and the cursor.
- Workers open the database without creating tables; the Server prepares the schema.

---

## 6. Writer

### 6.1 Write operations and the core implementations they reuse

| Write operation | Reused implementation |
| --- | --- |
| `put` | The change path of the shared merge engine; creation goes through `ArtifactRepository` |
| `revert` | The shared restore flow |
| `set_lifecycle` | `deprecated` / `active` use the shared forget and restore flows; `retired` and a replacement use governance state transitions |
| `merge` | The shared merge engine (RFC #1859) |
| `preview_undo_merge` / `undo_merge` | The shared merge engine's undo preview and restore |
| `bind_handle` | Artifact handle binding |
| `set_tags` | Tag replacement |

The read view provides `get`, `revisions`, `list`, `by_source`, `merge_inputs`, `tags`, and `resolve_handle`.

### 6.2 Writes with unchanged content

- In background invocations, a `put` whose content equals the current revision writes nothing and returns the current
  snapshot; the `source_refs` and `artifact_refs` passed are ignored. This matches Atomic Memory reconciliation, where a
  `noop` decision produces no new revision.
- In user-change mode, `put` always appends a revision, as built-in family replacement does.

### 6.3 Revision checks for reads outside the transaction

The writer keeps `observed: dict[str, int]`: each current snapshot returned by a read outside the transaction records
`artifact_id → revision`; when an ID is read several times, the earliest revision is kept. A `put` in the write
transaction block without `expected_revision` uses the recorded revision as its expectation when the ID is in
`observed`, and skips the check otherwise. Reads of a given historical revision are not recorded.

### 6.4 Write scope and limits

| Rule | Implementation |
| --- | --- |
| Only this Scope and family | The writer fixes Scope and family; write operations take neither |
| `source_refs` range | In `generate`, the window's Sources; in user changes, Sources in the target Artifact's current lineage |
| `artifact_refs` range | Revisions that exist in this Scope, in any family |
| Operations per block | 1000 |
| Size of one content | 256 KiB (UTF-8 JSON) |
| Total bytes per block | 8 MiB |
| Write transaction block time limit | `transaction_timeout_seconds`, 30 seconds by default |
| Write transaction blocks per invocation | 1; opening a second raises |

Exceeding a limit raises and rolls back; nothing is truncated.

### 6.5 User-change mode

The host process creates the writer for a user change with `UserChange(operation, artifact_id, inputs)`:

- Inside the write transaction block, only the agreed write in the §8.2 table and `set_tags` on the same Artifact are
  allowed; other writes raise `PluginWriteError`.
- When the block ends, the agreed write must have completed exactly once; otherwise it rolls back and raises
  `PluginContractError`, mapped to 502.
- Writes are authorized as the caller under the shared Artifact access policy.
- On create and edit, the writer keeps the content the caller submitted as a `lineage_only` Content Source in the same
  transaction (§8.3).

---

## 7. Plugin host process

### 7.1 Process model

The host process is **core runtime plus the plugin**: like a Worker, it builds its own database connection, access
service, and needed core services from configuration, then loads the plugin on top. The Server process only
authenticates callers and forwards requests; authorization, revision prechecks, and writes happen in the host process.

- Each Server process keeps a group of host processes per successfully loaded plugin, and plugins never share a process.
  The first starts in the background right after loading, without waiting for the first request, and always stays; a
  request that arrives before it is ready waits, and the wait counts toward the request's time limit.
- The group scales with load: when every concurrency slot of the plugin's processes is taken and requests are queuing,
  another process starts, up to the plugin's limit; extra processes receive `HostShutdown` after 5 idle minutes. The
  plugin's limit is its declared `host_processes`, which `POWERCONTEXT_SERVER_PLUGIN_HOST_PROCESSES` (JSON keyed by plugin
  name) can override; host processes of all plugins together stay within `POWERCONTEXT_SERVER_PLUGIN_HOST_PROCESSES_TOTAL`,
  and at that limit no new process starts and requests queue on existing ones. Requests go to the process with the fewest
  in flight.
- Start method: `multiprocessing.get_context("spawn")` starts a fresh interpreter that inherits no modules the Server
  process imported.
- The start argument `PluginHostSpec` is picklable: database configuration, plugin directory, `PluginManifest`, access
  configuration, inference configuration, the families and Source bindings of all loaded plugins, and the dedicated
  storage root.
- Startup steps:
  1. `load_plugin` and compare `PluginManifest`;
  2. register the families and Source bindings of all loaded plugins, so reads and Source writes behave as in the Server
     process;
  3. open the database connection (no table creation) and rebuild the access service with `open_worker_security`;
  4. open dedicated storage and models; load the Source Definitions and the route table;
  5. call `setup(ctx)` once (`ctx.scope_id` is `None`);
  6. check the required methods: `render_artifact`, and `generate` with `schedule` or `on_sources`; when one is missing,
     send `HostFailed(permanent=True)`; the plugin is `failed` and is not restarted;
  7. send `HostReady(implemented=...)` with the user-change methods the plugin implements.
- The host process is a single asyncio event loop and can serve requests concurrently; the Server side limits each host
  process to 8 concurrent requests with a semaphore, and queued requests count their wait toward their time limit. The
  host process sends `HostHeartbeat` every second from a task on its event loop, so a blocked event loop stops the
  heartbeat.

### 7.2 Communication

The Server and the host process share one duplex `multiprocessing` `Pipe`. Messages are picklable dataclasses and the
`Pipe` frames them. What plugins write to stdout / stderr never enters this channel, and bytes can travel inside messages.

```python
HostMethod = Literal["create_artifact", "update_artifact", "merge_artifacts", "forget_artifact", "restore_artifact",
                     "create_source", "render_artifact", "search_artifacts", "api"]

@dataclass(frozen=True)
class HostRequest:
    id: str
    method: HostMethod
    scope_id: str | None
    caller: Any                  # the caller the Server authenticated; None without access control
    audit: Any                   # audit context of the HTTP request
    payload: dict[str, Any]
    body: Any = None             # the plugin API request body, possibly bytes

@dataclass(frozen=True)
class HostResponse:
    id: str
    outcome: Literal["ok", "invalid_content", "not_implemented", "forbidden", "not_found", "precondition_failed",
                     "conflict", "contract_violation", "api_error", "model_unavailable", "error"]
    result: Any = None
    message: str | None = None   # the plugin's message for invalid_content and api_error
    field: str | None = None     # the field of invalid_content; the error code of api_error
    status: int | None = None    # the status of api_error
    error_type: str | None = None
```

Control messages complete the protocol: `HostReady`, `HostFailed`, `HostShutdown`, `HostHeartbeat`, and `HostCancel(id)`. On the Server side,
`PluginHost` reads the `Pipe` in a background task and hands each response to the waiting request by `id`.

### 7.3 Timeouts, failures, and restarts

| Situation | Handling |
| --- | --- |
| A request exceeds its time limit | The Server sends `HostCancel(id)` and the host process cancels that request's task; its write transaction rolls back, the request returns 504, and other requests are unaffected |
| No heartbeat for more than 5 seconds | Treated as loss of response: terminate the host process (`terminate`, then `kill` after 5 seconds); timed-out requests return 504 and other in-flight requests return 503 |
| The process exits or the `Pipe` closes | In-flight requests return 503 |
| Waiting for readiness exceeds the request's time limit | That request returns 503 |
| Restart | In the background after a backoff that starts at 1 second, doubles each time, caps at 60 seconds, and resets after 5 minutes of running |
| During backoff | With no other usable process of the plugin, synchronous calls return 503; background invocations are unaffected |
| Permanent failure (`HostFailed(permanent=True)`) | No restart; the plugin is `failed` |
| Server stops | Send `HostShutdown`, wait up to 10 seconds, then terminate |

Time limits of synchronous calls: user changes, rendering, and search use the declaration's `change_timeout_seconds`;
plugin APIs use the route's `timeout_seconds`. The limit counts from when the Server receives the request, including
waits for readiness and queueing.

### 7.4 Writes in the host process

The host process uses the same writer implementation as Workers, except that there is no cursor or invocation
acknowledgment, so the write transaction block commits only this write; authorization uses the request's caller; and
reads outside the block record `observed` in the same way.

---

## 8. Synchronous calls

### 8.1 User changes

```text
HTTP request (create / PUT / merge / forget / restore)
  -> Server: authenticate; find the plugin and host process by family
  -> Server: send HostRequest (caller, audit context, request content) to the host process
  -> host: the plugin lacks the method → not_implemented
  -> host: authorize (as for built-in families) → forbidden
  -> host: revision precheck; the plugin is not called when it fails
       PUT: revision differs from If-Match → precondition_failed
       merge, forget: revision or state_version differs → conflict
  -> host: on create and merge, allocate artifact_id / into when none was given
  -> host: build the request model and call the plugin method; the writer completes the agreed write; a concurrent change at commit rolls back and returns conflict
  -> host: the method returns without a committed write → contract_violation
  -> Server: map HostResponse to the HTTP response (§8.7)
```

### 8.2 Agreed writes

| Method | Agreed write |
| --- | --- |
| `create_artifact` | Exactly one `put(request.artifact_id, ...)` on an Artifact that did not exist |
| `update_artifact` | Exactly one `put(request.artifact_id, ..., expected_revision=request.expected_revision)` |
| `merge_artifacts` | Exactly one `merge(input IDs, into=request.into, ...)` with the requested inputs |
| `forget_artifact` | Exactly one `set_lifecycle(request.artifact_id, "deprecated")` |
| `restore_artifact` | `restore`: exactly one `revert` or `set_lifecycle(..., "active")`; `undo_merge`: exactly one `undo_merge(request.artifact_id)` |

### 8.3 Provenance

| Operation | Content Source `operation` | Target revision |
| --- | --- | --- |
| Create | `artifact_create` | 1 |
| Edit | `artifact_replace` | The new revision |

The Content Source holds the content the caller submitted, with `role="lineage_only"`, as built-in families write it; it
never enters any plugin's input window. Forget, restore, and merge rely on the relationships the shared governance flows
record and write no Content Source.

### 8.4 Rendering

`GET /v1/scopes/{scope_id}/artifacts/{family}/{artifact_id}/rendering` (optionally with `revision`): the host process
checks the caller's `scope.read`, reads the revision, calls `render_artifact`, and returns the `Rendering` (Markdown or
plain text) with the revision reference; a missing Artifact returns 404.

### 8.5 Search and Prepared Context

- Generic search requests on a plugin family go to the host process unchanged (without `include_scores`), and the plugin
  interprets every field; without `search_artifacts` the request returns 422.
- The host process checks the caller's `scope.read`, then checks each hit the plugin returns: a hit of another family is a
  `contract_violation`; only hits whose revision is current and `active` are kept, in the plugin's order.
- The Server assembles the response under the caller's read permission; `include_scores` decides whether scores appear.
- When a plugin is unavailable in Prepared Context (no usable host process, a timeout, or a failed search), its section
  is omitted and the reason recorded in the receipt; other sections return as usual. Hits after permission filtering may
  number fewer than `limit`; core does not fetch more.
- `POST /v1/context/prepare` sections may list plugin families that implement search: core calls `search_artifacts` with
  the request's query and the section's `limit`, passes the other generic search fields in the section's `search` (such
  as `mode` and filters) unchanged, and passes `max_bytes`. A family that is neither built-in nor a plugin family with
  search returns 422.
- Core builds no index for plugin families.

### 8.6 Plugin-provided Source types

- The check child process exports a declarative manifest for each definition, registered in
  `pc_source_definition_manifests` at load (§4.4).
- When `POST /v1/scopes/{scope_id}/sources` names a plugin `source_type`, the Server forwards it to the host process
  (`create_source`): it checks the caller's `scope.contribute`, validates the content with `input_class`, runs `resolve`
  and the Projections, and saves the Source with the usual identity deduplication.
- `ctx.sources.submit` in plugin routes uses the same save path.

### 8.7 Error mapping

| `HostResponse.outcome` / situation | HTTP | Error code |
| --- | --- | --- |
| `ok` | As for built-in families (201 for create, 200 otherwise) | — |
| `invalid_content` | 422 with the plugin's message and field | — |
| `not_implemented` | 422 | `plugin_operation_not_supported` |
| `forbidden` | 403 | `access_denied` |
| `not_found` | 404 | `not_found` |
| `precondition_failed` | 412 | `precondition_failed` |
| `conflict` | 409 | `conflict` |
| `contract_violation` | 502 | `plugin_contract_violation` |
| `model_unavailable` | 503 | `model_unavailable` |
| `api_error` | The plugin's 4xx | The plugin's code |
| `error` | 500; only the exception type is logged | `plugin_error` |
| The request exceeds its time limit | 504 | `plugin_timeout` |
| Host process unavailable, in backoff, or other requests of a process terminated for loss of response | 503 | `plugin_unavailable` |

---

## 9. Dedicated storage and models

### 9.1 Dedicated storage

| Backend | Location |
| --- | --- |
| SQLite | `<data directory>/plugins/<plugin name>.sqlite3`, created on the first `engine()` call |
| OceanBase / seekdb | Not provided; `ctx.storage` raises `RuntimeError` |

- `engine()` returns an `AsyncEngine`; the plugin creates and migrates its own tables.
- Workers create it per invocation and release it when the invocation ends; host processes reuse it for the life of the
  process.
- Removing a plugin does not delete its storage.

### 9.2 Models

Host processes and Workers build `ctx.models` from the Server's inference configuration and reuse core's model clients:

- `embed(texts)` calls the Server's embedding model; `embedding_dimension` gives the dimension;
- `generate(messages, max_tokens=None)` calls the Server's generation model with its timeout and model settings;
- a model that is not configured raises `ModelUnavailable`, mapped to 503 in synchronous calls; with neither model
  configured, `ctx.models` is `None`;
- the inference configuration travels in the Worker spec and the host process start argument; plugin code never sees its
  addresses or credentials.

---

## 10. Persistence and migration

Migration `v1_5_0_005` (SQLite and MySQL):

| Change | Objects |
| --- | --- |
| Drop tables | The #2246 framework's `pc_artifact_family_schemas`, and the declarative index tables `pc_artifact_search_fts`, `pc_artifact_search_text`, `pc_artifact_vectors`, `pc_artifact_index_changes`, `pc_artifact_index_coverage`, `pc_artifact_search_states`, `pc_artifact_vector_indexes`, `pc_artifact_vector_spaces` |
| Drop columns | `plugin_id`, `plugin_family`, `plugin_spec`, `binding_generation`, `desired_state`, `phase`, `last_error` of `pc_artifact_processing_binding_states` |
| Add column | `pc_artifacts.metadata` (nullable): revisions written by plugins record `{"plugin", "version"}`, where `version` is the code fingerprint |

- These tables and columns are dropped only when they hold no data; otherwise the migration preflight refuses with
  `unsupported_plugin_data`, and the deployer exports and removes the #2246 plugins on the previous version first.
  Migration resources of released versions are unchanged.
- The readiness check accepts bindings with the `plugin:` prefix: the plugin directory decides their pending invocation
  intents, and intents left behind by a removed plugin are no longer processed.
- Revision reads omit `metadata`, so migration steps of earlier revisions still run.
- `artifact_revision` records in portable exports move to schema version 3 and still restore version 2.
- Unaffected: `pc_sources`, Projections, the journal, `pc_source_definition_manifests`, the other columns of
  `pc_artifacts` with heads, lineage, merge records, handles, and tags; `pc_source_cursors`; the Supervisor's intent,
  pending, and lease tables.
- The old Source plugin framework (management service, `pc_source_write_plugins`) stays; the RFC #2617 implementation
  removes it.

---

## 11. Public reads

- Core keeps plugin family Artifacts in `pc_artifacts` and stores `content` unchanged. Listing, reads, revision history,
  governance state, and tags are served by core without calling the plugin.
- A plugin can keep large content in its dedicated storage and record only a summary or reference in the Artifact; its
  APIs provide the full content.
- After a plugin is removed, its family is no longer registered and the generic APIs return 404; the data stays.

---

## 12. Observability

The current implementation emits plugin load failures, host process start, termination, and restart, and post-commit
errors through `log_safely`, with only the plugin name and exception type in the message. Structured fields are not yet
registered; the plan:

| Field | Meaning |
| --- | --- |
| `plugin_name` | Plugin name |
| `plugin_method` | `generate`, `render_artifact`, `search_artifacts`, `create_source`, a user-change method name, `api` |
| `plugin_process` | `worker` or `host` |
| `trigger` | `schedule`, `sources`; `request` for synchronous calls |
| `operation_count` | Write operations committed in this invocation |
| `transaction_ms` | Milliseconds from the start of the write transaction block to commit or rollback |

- Host process termination reasons: `timeout`, `crash`, `server_stop`.
- Metric labels use `plugin_name`, `plugin_method`, and `plugin_process`, never Scope IDs.
- Logs exclude Source content, Projections, Artifact content, and environment variable values; plugin exceptions record
  only their type.
- New fields are registered in the allowlist and documented in both locales' observability docs.

---

## 13. Defaults

| Item | Default | Configuration |
| --- | --- | --- |
| Check child process time limit | 60 seconds | Fixed |
| Write transaction block time limit | 30 seconds | Declaration `transaction_timeout_seconds` |
| Background generation time limit | 300 seconds | Declaration `timeout_seconds` |
| User change, rendering, and search time limit | 30 seconds | Declaration `change_timeout_seconds` |
| Concurrent requests per host process | 8 | Fixed |
| Host process limit per plugin | 1 | Declaration `host_processes`; `POWERCONTEXT_SERVER_PLUGIN_HOST_PROCESSES` |
| Total host process limit | To decide | `POWERCONTEXT_SERVER_PLUGIN_HOST_PROCESSES_TOTAL` |
| Idle stop of extra host processes | 5 minutes | Fixed |
| Heartbeat interval / loss-of-response threshold | 1 second / 5 seconds | Fixed |
| Host process readiness wait | 60 seconds | Fixed |
| Host process restart backoff | From 1 second, doubling, capped at 60 seconds; reset after 5 minutes of running | Fixed |
| Host process stop wait | 10 seconds | Fixed |
| Operations per block / one content / total bytes | 1000 / 256 KiB / 8 MiB | Fixed |
| `on_sources` admission interval | 2 seconds | Fixed |

---

## 14. Follow-up work

| Work | Basis |
| --- | --- |
| Hot reload, version snapshots, data upgrade `upgrade`, MCP tools | RFC #2579 and its design |
| Dedicated storage on OceanBase / seekdb | §9.1 |
| Structured observability fields | §12 |
| Source plugins, collection targets and runners, version checks, removal of the old Source plugin framework | RFC #2617 |
| Plugin installation and dependencies: install by package name, Git URL, or wheel; per-plugin environments; separate dependencies for the central Server and devices | A later proposal |

---

## 15. Open items

| Item | Notes |
| --- | --- |
| Dedicated storage on OceanBase / seekdb | A database per plugin needs the privilege to create databases; whether to fall back to plugin-prefixed tables in the core database without it |
| Host process defaults | Concurrency, heartbeat threshold, total limit, and backoff need confirmation under load with real plugins |
| Check concurrency | Plugins are checked one at a time, each about 0.6 seconds plus the plugin's own imports; whether to check concurrently when there are many plugins |

---

## 16. Design decisions beyond the RFC

The RFC defines only the contract. The table lists decisions this document makes to implement it that the RFC does not
prescribe.

| # | Decision | Rationale |
| --- | --- | --- |
| D1 | Host processes start with `multiprocessing` spawn and exchange picklable messages with the Server over a duplex `Pipe` (§7.1, §7.2) | Matches the existing Worker start method; plugin stdout cannot break the channel; bytes pass directly; cross-platform. Line-delimited JSON over stdin / stdout needs its own framing and breaks on plugin output |
| D2 | The host process is "core runtime + plugin": like a Worker, it builds its own database connection and access service from configuration and registers the families and Source bindings of all loaded plugins; the Server only authenticates callers and forwards requests (§7.1) | Database connections cannot cross processes; authorization, Scope management, and writes happen in one process with no callback to the Server. The cost is importing all of core in each host process, about 5.5 seconds and 150 MB |
| D3 | Authorization and revision prechecks of user changes run in the host process, and the plugin is not called when they fail (§8.1) | Follows from D2 |
| D4 | The host process checks required methods after `setup`; a missing one is a permanent failure without restart; it also reports the implemented user-change methods (§7.1) | The methods live on the object `setup` returns, which only the host process sees; a missing method is the plugin's fault and a restart cannot fix it |
| D5 | Plugins are checked one at a time in directory order (§4.1) | Simple, with a deterministic startup order; concurrency can come later for many plugins (§15) |
| D6 | Reads outside the transaction record the earliest revision read of each Artifact; historical revision reads are not recorded (§6.3) | The earliest read is where the plugin's decision started |
| D7 | A registered Source Definition manifest identical to the plugin's is the same definition; only different content is a conflict (§4.4) | Follows the existing manifest registration; the same plugin loading on several Server instances relies on it |
| D8 | Workers create the dedicated storage engine per invocation; host processes reuse it (§9.1) | Matches the lifetime of the object `setup` returns |
| D9 | A request timeout first cancels that request, and the host process is terminated only when its heartbeat stops (a blocked event loop); the timed-out request returns 504 and requests terminated with it return 503 (§7.3) | Cancellation works while the plugin awaits I/O, so other requests in the process need not be sacrificed; only a blocked event loop defeats cancellation, and then the process's other requests could not finish anyway |
| D10 | Every successfully loaded plugin gets one host process started in advance and kept, more on demand under load and fewer when idle, within a total limit (§7.1) | Starting takes seconds and should not fall on the first request; every plugin has the required `render_artifact` and needs a host process. A group per plugin instead of a shared pool keeps blocking or crashes from spreading across plugins and leaves room for per-plugin dependency isolation; the total limit bounds memory |
| D11 | Revisions written by plugins record the plugin name and code fingerprint in `metadata` (§10) | Troubleshooting and later upgrades can tell which version wrote each revision; it stays out of Artifact content and does not affect identity or deduplication |
| D12 | Search results keep only current, `active` revisions, in the plugin's order (§8.5) | A plugin index may lag behind core data; core's current state wins, so stale or forgotten content never appears in results |
