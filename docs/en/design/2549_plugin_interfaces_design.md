# System design: Plugin Interfaces

| Item | Content |
| --- | --- |
| Design source | [RFC #2549 Plugin interfaces](https://github.com/oceanbase/powercontext/pull/2549) |
| Related design | [Plugin hot reload and custom APIs design](2579_plugin_reload_and_apis_design.md) (RFC #2579) |
| Status | Design draft |
| Date | 2026-11-11 |
| Stack | Python 3.11+ / pydantic v2 / SQLAlchemy 2.x / FastAPI; contract source of truth `openapi/powercontext.yaml` |

This is the implementation design for RFC #2549: the RFC defines the contract that plugin authors, callers, and deployers
see, and this document defines how core implements it, including module layout, execution flows, the process model,
persistence structures, defaults, and task breakdown. Where the two conflict, the RFC wins and this document is revised.

---

## 0. Invariants

| # | Invariant | How the implementation guarantees it |
| --- | --- | --- |
| C1 | The Server process never imports plugin code | Plugins are imported only in check child processes, host processes, and Workers; the Server process holds only the serializable declaration returned by the check child process |
| C2 | Plugin writes to core data go only through the writer and `collect` return values | Plugin code never gets core's database connection objects; the writer exposes only the write operations in the RFC |
| C3 | Cursors, checkpoints, and invocation acknowledgments commit in the same transaction as core data | When the write transaction block ends, the executor performs the cursor CAS, `invocation.complete`, and the commit on the same connection |
| C4 | A user change completes exactly one agreed write operation | In user-change mode the writer records write operations and checks them when the block ends, rolling back and returning 502 on mismatch |
| C5 | Plugin-owned storage stays out of core's transactions and schema | Core only allocates the location of dedicated storage and never creates or migrates tables in it |

---

## 1. Current state and gaps

The `feat/plugin-interfaces-v3` branch implements the core of Artifact plugins. The table lists the gaps against the
RFC:

| Capability | Current state | Section |
| --- | --- | --- |
| `ArtifactPlugin` declaration, check child process, directory loading | Implemented (`plugins.py`, `builtin/plugins/manifest.py`, `server/plugins.py`) | §3 |
| Worker executor, writer, write transaction block, cursor and acknowledgment | Implemented (`builtin/plugins/executor.py`, `builtin/plugins/writer.py`) | §4, §5 |
| Reads, search, tags, and generic merge APIs for plugin families | Implemented (`builtin/plugins/families.py`, `builtin/plugins/search.py`, `server/app.py`) | §11 |
| User changes through the plugin (five methods) | Not implemented; core currently runs `replace_plugin_artifact` and the merge engine directly | §6, §7 |
| Plugin host processes | Not implemented | §6 |
| `ctx.storage`, `ctx.models` | Not implemented | §9 |
| Code fingerprint, read-revision checks, `ArtifactChange`, unchanged-content write rules | Not implemented | §3.3, §5 |
| Source plugins (definitions, `identify_target`, `collect`, collection targets) | Not implemented | §8 |
| Edge mode `source-agent` | Not implemented | §8.5 |
| Removal of the old Artifact and Source plugin frameworks | Not implemented | §10 |

---

## 2. Modules and files

| File | Status | Responsibility |
| --- | --- | --- |
| `src/powercontext/plugins.py` | Changed | Public contract: `ArtifactPlugin`, `SourcePlugin`, `Inputs`, `Interval`, `Cron`, `PluginContext`, `PluginStorage`, `PluginModels`, read and write interfaces, `ArtifactChange`, user-change request models, `InvalidContent`, Source plugin request and result models |
| `src/powercontext/builtin/plugins/manifest.py` | Changed | Imports `plugin.py`, validates the declaration, exports its serializable part; adds code fingerprinting; supports `SourcePlugin` |
| `src/powercontext/builtin/plugins/executor.py` | Changed | The `generate` executor in Workers; adds the `collect` executor |
| `src/powercontext/builtin/plugins/writer.py` | Changed | Writer, write transaction block, read views; adds user-change mode, recorded read revisions, `ArtifactChange` |
| `src/powercontext/builtin/plugins/host.py` | New | Host process entry point: imports the plugin, runs the factory, handles requests, runs the writer |
| `src/powercontext/builtin/plugins/host_protocol.py` | New | Message models between Server and host processes |
| `src/powercontext/server/plugin_hosts.py` | New | Server-side host process management: start on demand, reuse, terminate on timeout, restart with backoff, stop |
| `src/powercontext/builtin/plugins/storage.py` | New | `PluginStorage`: SQLite file paths and OceanBase / seekdb database allocation |
| `src/powercontext/builtin/plugins/models.py` | New | `PluginModels`: wraps Server's inference configuration |
| `src/powercontext/builtin/plugins/sources.py` | New | Source plugin definition registration, write path, and collection target repository |
| `src/powercontext/builtin/plugins/families.py` | Changed | Plugin family registration; unregistration when a plugin is removed |
| `src/powercontext/server/plugins.py` | Changed | `PluginManager`: checks, registration, the active plugin record, binding registration, routing of user changes and Source writes |
| `src/powercontext/server/app.py` | Changed | The plugin branch of the generic create API; PUT, merge, forget, and restoration hand plugin families to the host process; `/v1/plugins` operational APIs |
| `src/powercontext/cli/source_agent.py` | New | The `powercontext source-agent` command |
| `openapi/powercontext.yaml` | Changed | The create API's plugin branch and the `/v1/plugins` APIs; run `make api-generate` and `make contract-test` after changing it |
| `src/powercontext/builtin/persistence/migrations/` | New migration | Adds `pc_source_collect_targets` and removes the old plugin frameworks' tables and columns |

---

## 3. Declarations, checks, and loading

### 3.1 Check child process

`check_plugin_directory` starts the check child process with `python -m powercontext.builtin.plugins.manifest
<directory>`, with a 60-second limit. The child process:

1. checks that the directory name matches `^[a-z][a-z0-9-]{0,63}$` and imports `plugin.py` (module name
   `powercontext_plugin_<name>`, with the plugin directory on `sys.path`);
2. reads the module-level variable `plugin`, confirms it is an `ArtifactPlugin` or `SourcePlugin`, and that `create` is
   an async function;
3. validates the declaration with Pydantic and exports its serializable part: family or Source types, the JSON Schema of
   each Source definition input, the JSON Schema of `target_input`, `search_fields`, trigger configuration, and which
   user-change methods are implemented;
4. computes the code fingerprint (§3.3);
5. prints the result as one line of JSON on stdout; on failure it prints only a known error category, never the
   exception message.

On receiving the result, Server checks for conflicts: a family or Source type that duplicates a built-in one or another
plugin's fails the check.

Plugin checks run concurrently with a default limit of 4. The check child process imports only the plugin and
`powercontext.plugins`, not core's execution modules; on a 2-core development machine, starting it and importing
`powercontext.builtin.plugins.manifest` takes about 0.6 seconds and 33 MB, excluding the plugin's own dependencies.

### 3.2 Active plugin record

```python
@dataclass
class ActivePlugin:
    name: str
    directory: Path
    kind: Literal["artifact", "source"]
    manifest: PluginManifest | None        # the serializable declaration when the check passes
    fingerprint: str | None                # code fingerprint
    declaration_digest: str | None         # SHA-256 of the serializable declaration
    error: str | None                      # category of a failed check
    host: PluginHostHandle | None          # host process handle, see §6
```

`PluginManager.plugins: dict[str, ActivePlugin]` is each Server process's only record. Routing user requests, starting
host processes, and registering bindings all read it; it is not persisted and is rebuilt from the plugin directory at
startup.

### 3.3 Code fingerprint

```python
def code_fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file() and "__pycache__" not in p.relative_to(directory).parts):
        digest.update(str(path.relative_to(directory)).encode()); digest.update(b"\0")
        digest.update(path.read_bytes()); digest.update(b"\0")
    return digest.hexdigest()
```

- The check child process computes it once, and it is kept in `ActivePlugin.fingerprint` and `PluginWorkerSpec`.
- Workers recompute it before importing the plugin, and host processes when they start; a mismatch fails with the error
  category `plugin_changed` and asks for a Server restart.
- It is computed only from file contents, never from sizes and modification times. The old Artifact plugin framework
  (`artifact_plugin_validation.code_fingerprint`) uses the same algorithm.

### 3.4 Registration

Once a check passes, `PluginManager._register` does:

| Plugin kind | Registration |
| --- | --- |
| Artifact plugin | `register_plugin_family(family)`; registers `plugin_merge_service(family, ...)` in the merge registry, now handing changes to the host process (§7); `ensure_plugin_search`; `register_source_binding` when `inputs` is configured; registers the binding with the Supervisor when this instance dispatches Workers |
| Source plugin | Registers each definition in the Source definition manifests (§8.1); registers the collection binding when `collect` is configured |

The binding name is `plugin:<name>`. `on_sources` maps to an `automatic_processing_interval` of 2 seconds; with both
`Interval` and `on_sources`, the smaller value wins; `Cron` maps directly to the binding's cron. For plugins without
`inputs`, the `_tick` task writes an invocation intent for each Scope in `scopes` on the `Interval`.

Instances in the `api` role register only families and Source types, not bindings (`dispatch=False`); instances in the
`background` role register bindings but receive no requests and start no host processes.

---

## 4. Worker executor

### 4.1 generate

```text
Worker receives (plugin:<name>, scope_id, request_generation)
  -> short transaction: ScopeInvocation.start; read the cursor for (scope_id, plugin:<name>) from pc_source_cursors
     -> read the journal high-water mark and fix the window (after, through], through ≤ after + max_sources
     -> list_window_for_generation reads the window, filters by inputs.source_types, computes inputs.projection
  -> empty window with inputs configured: a short transaction performs the cursor CAS and acknowledge without loading the plugin
  -> check the code fingerprint, import plugin.py, verify the declaration, call create, build the writer, call generate
     (bounded by timeout_seconds)
     -> reads outside the transaction: separate short read-only transactions, recording revisions read (§5.3)
     -> write transaction block starts: open a transaction; guard (lease and invocation intent)
        writes in the block run in that transaction
        block ends: cursor CAS to through; acknowledge; commit
  -> generate returns without opening a write transaction block: a short transaction performs the cursor CAS and acknowledge
  -> generate raises after the write transaction block committed: logged as a post-commit error; the window is not retried
```

- Sources of unmatched types in the window are filtered out, but the cursor still advances past the whole window; a
  matching Source missing the required Projection fails the invocation.
- The cursor CAS uses the `generation` read earlier as the expected value, so a window commits only once even across
  leadership changes or duplicate dispatches.
- Pending work is discovered in two ways: Source writes in the Server process mark the plugin bindings that watch that
  Scope as pending; at startup, `SourceProcessingPendingProvider` fills in Sources written by other processes from the
  gap between the journal high-water mark and the cursor.
- The database readiness check verifies that every pending invocation intent's binding is registered; bindings with the
  `plugin:` prefix depend on the plugin directory, so the readiness check lets them through, and intents left by a
  removed plugin are no longer processed.
- Workers open the database without creating tables (`tables=()`); Server prepares the schema.

### 4.2 collect

```text
Worker receives (plugin:<name>, scope_id, request_generation)
  -> short transaction: ScopeInvocation.start; read the plugin's enabled collection targets in this Scope, take up to
     max_targets in target-key rotation with has_more targets first; read each target's checkpoint
     (pc_connector_checkpoints, keyed by the target's binding ID)
  -> check the code fingerprint, import the plugin, call create, call collect (bounded by timeout_seconds)
  -> validate the result: total items ≤ max_items; every target key is in the request; each item is validated against
     its definition's input_class, then resolve and Projections run
  -> one transaction: save every Source (with identity deduplication); write each target's new checkpoint with the old
     one as the expected value; update last collection times; acknowledge; commit
  -> any failure: nothing is committed; each target's latest failure category is recorded (in a separate short
     transaction); the Supervisor backs off
```

Adding or restoring a collection target writes one invocation intent through `ArtifactProcessingIntentRepository.request`
and wakes the binding.

---

## 5. Writer

### 5.1 Write operations and the core implementations they reuse

| Write operation | Implementation reused |
| --- | --- |
| `put` | The change path of the shared merge engine (`ArtifactMergeService.prepare_change` / `commit`); creation goes through `ArtifactRepository` |
| `revert` | The shared restoration flow `inspect_restore(operation="restore", revision=...)` |
| `set_lifecycle` | `deprecated` / `active` use the shared forget and restoration flows; `retired` and replacements use `ArtifactGovernanceRepository.transition` |
| `merge` | The shared merge engine (RFC #1859) |
| `preview_undo_merge` / `undo_merge` | Undo preview and restoration of the shared merge engine |
| `bind_handle` | The Artifact handle binding implementation |
| `set_tags` | The tag replacement implementation (`builtin/persistence/tags.py`) |

`revert` and `set_lifecycle` turn the `ArtifactMergeMutationResult` returned by the shared flows into an `ArtifactChange`:
`artifact` is the target's new snapshot, and `restored` and `retired` come from the result's `restored` and `retired`
(excluding the target itself).

### 5.2 Writes with unchanged content

- In background calls (`generate`), a `put` whose content equals the current revision writes nothing and returns the
  current snapshot; the supplied `source_refs` and `artifact_refs` are ignored. This matches Atomic Memory's reconcile:
  a `noop` decision updates only in-memory evidence, and only `changed` items are written
  (`builtin/artifacts/atomic_memory/reconciliation.py`).
- In user-change mode, `put` always appends a revision, matching replacement for built-in families (`artifact_replace`
  in `records.py`).

### 5.3 Checking revisions read outside the transaction

The writer keeps `observed: dict[str, int]`: every current snapshot returned by `get`, `list`, `search`, or `by_source`
outside the transaction records `artifact_id → revision`; when an ID is read several times, the earliest is kept. When a
`put` in the write transaction block gives no `expected_revision` and its ID is in `observed`, the recorded revision is
the expected value; otherwise nothing is checked. Reads of a specific historical revision (`get(..., revision=n)`) are
not recorded.

This corresponds to Atomic Memory checking before commit whether the memories it read have changed
(`AtomicMemoryConflictError("A recalled memory changed during window preparation")`).

### 5.4 Write scope and limits

| Rule | Implementation |
| --- | --- |
| Only this Scope and family | The writer fixes the Scope and family; write operations take neither as a parameter |
| `source_refs` scope | In `generate`, limited to `window_sources`; in user changes, to Sources in the target Artifact's current lineage |
| `artifact_refs` scope | Existing revisions of any family in this Scope |
| Operations per block | 1000 (`MAX_WRITES_PER_TRANSACTION`) |
| Size of one content | 256 KiB (serialized as UTF-8 JSON) |
| Total bytes per block | 8 MiB |
| Write transaction block time limit | `transaction_timeout_seconds`, default 30 seconds |
| Write transaction blocks per invocation | 1; opening a second one raises |

Exceeding a limit raises and rolls back, with no truncation.

### 5.5 User-change mode

The host process creates the writer for a user change with `mode=UserChange(operation, target)`:

- The write transaction block allows only the agreed write operation from the table in §7.2, plus `set_tags` on the same
  Artifact; any other write raises `PluginWriteError` immediately.
- When the block ends, the writer checks that the agreed operation completed exactly once; otherwise it rolls back, the
  host process returns `contract_violation`, and Server maps it to 502.
- Writes are authorized as the caller: the writer's `_Security` uses the caller identity carried by the request, not the
  Worker's deployment identity.
- On create, edit, restore, and undo merge, the writer writes the `lineage_only` Content Source in the same transaction
  (§7.3).

---

## 6. Plugin host processes

### 6.1 Process model

- Each Server process keeps at most one host process per plugin that needs synchronous calls, started on the first
  synchronous call. Plugins that need one: Artifact plugins implementing any user-change method, Source plugins providing
  Source definitions, and Source plugins providing `identify_target`.
- Start: `asyncio.create_subprocess_exec(sys.executable, "-m", "powercontext.builtin.plugins.host", ...)`, a fresh
  interpreter like the check child process, inheriting no modules from the Server process.
- At startup the host process checks the code fingerprint, imports `plugin.py` and verifies the declaration, calls
  `create(ctx)` once (`ctx.scope_id` is `None`), opens core's database connection (`open_plugin_database`, no table
  creation) and dedicated storage, and then sends `ready`.
- A host process runs one asyncio event loop and can handle several requests concurrently, with a default limit of 8;
  requests beyond the limit queue on the Server side, and queueing time counts toward the request's time limit.

### 6.2 Protocol

Server and the host process exchange newline-delimited JSON messages over stdin / stdout; stderr carries only logs. The
message models live in `host_protocol.py`:

```python
class HostRequest(BaseModel):
    id: str
    method: Literal["resolve", "identify_target", "create_artifact", "update_artifact",
                    "merge_artifacts", "forget_artifact", "restore_artifact"]
    scope_id: str
    caller: CallerIdentity | None          # the caller identity for user changes
    deadline_seconds: float
    payload: dict[str, JsonValue]          # JSON of the corresponding request model

class HostResponse(BaseModel):
    id: str
    outcome: Literal["ok", "invalid_content", "not_implemented", "contract_violation", "conflict", "error"]
    result: dict[str, JsonValue] | None = None   # snapshot or Source reference on success
    message: str | None = None                   # the plugin's message, only for invalid_content
    field: str | None = None
    error_type: str | None = None                # only the exception type name, for error
```

Binary content does not use this channel; RFC #2579's plugin APIs define how large request bodies are passed.

### 6.3 Timeouts, failures, and restarts

| Case | Handling |
| --- | --- |
| A request exceeds its time limit | Server terminates the whole host process (`SIGTERM`, then `SIGKILL` after 5 seconds), and every in-flight request returns 503 |
| The process exits, stdout closes, or an unparseable message arrives | Same as above |
| Restart | Started on the next synchronous call after a backoff; the backoff starts at 1 second, doubles each time, caps at 60 seconds, and resets after 5 minutes without failure |
| During backoff | The plugin's synchronous calls return 503 immediately; background calls are unaffected |
| Server stop | Stop accepting synchronous calls, wait up to 10 seconds for in-flight requests, then terminate the host process |

Time limits for synchronous calls: Source writes and `identify_target` default to 10 seconds
(`POWERCONTEXT_SERVER_PLUGIN_SYNC_TIMEOUT_SECONDS`); user changes use the declaration's `change_timeout_seconds`.

### 6.4 Writes in the host process

The writer in the host process shares its implementation with Workers, except:

- there is no cursor or invocation acknowledgment; the write transaction block commits only this request's writes;
- authorization uses the caller identity carried by the request;
- reads outside the write transaction block also record `observed` (§5.3).

---

## 7. User change call path

### 7.1 Flow

```text
HTTP request (create / PUT / merge / forget / restore)
  -> Server: authenticate; find the plugin by family; the plugin does not implement the method → 422 (the declaration
     records implemented methods, so the host process is not involved)
  -> Server: authorize (as for built-in families); version precheck (If-Match → 412; revision / state_version → 409)
  -> Server: allocate artifact_id / into for create and merge
  -> Server: send a HostRequest to the host process and wait for the HostResponse
  -> host process: build the request model and call the plugin method; the writer completes the agreed write in the
     write transaction block and writes the lineage_only Content Source; at commit it checks versions again and rolls
     back with conflict if another write changed them
  -> Server: map the HostResponse to the HTTP response (§7.4)
```

### 7.2 Agreed write operations

| Method | Agreement |
| --- | --- |
| `create_artifact` | Exactly one `put(request.artifact_id, ...)`; the target did not exist before |
| `update_artifact` | Exactly one `put(request.artifact_id, ..., expected_revision=request.expected_revision)` |
| `merge_artifacts` | Exactly one `merge(input IDs, into=request.into, ...)` with the inputs from the request |
| `forget_artifact` | Exactly one `set_lifecycle(request.artifact_id, "deprecated")` |
| `restore_artifact` | `restore`: exactly one `revert` or `set_lifecycle(..., "active")`; `undo_merge`: exactly one `undo_merge(request.artifact_id)` |

### 7.3 Provenance

| Operation | Content Source `operation` | Target revision |
| --- | --- | --- |
| Create | `artifact_create` | 1 |
| Edit | `artifact_replace` | The new revision |
| Restore (when appending a revision) | `artifact_restore` | The new revision |
| Undo merge | `artifact_undo_merge` | The new revisions of each restored input |

The Content Source holds the content the caller submitted (before normalization) with `role="lineage_only"`, written the
same way as for built-in families in `records.py`, and never enters any plugin's input window.

### 7.4 Error mapping

| `HostResponse.outcome` / case | HTTP |
| --- | --- |
| `ok` | Same as built-in families (201 for create, 200 otherwise) |
| `invalid_content` | 422 with the plugin's message and field |
| `not_implemented` | 422 |
| `conflict` | 409 |
| `contract_violation` | 502 |
| `error` | 500, logging only the exception type |
| Timeout, host process unavailable, or in backoff | 503 |

---

## 8. Source plugins

### 8.1 Definition registration and writes

- The startup check yields each definition's declarative manifest (Source type, input JSON Schema, Projection keys),
  registered in the Source definition manifests (`pc_source_definition_manifests`). Registration reuses the existing
  manifest registration: an identical manifest already registered is reused, and only different contents conflict and
  fail the check.
- When `POST /v1/scopes/{scope_id}/sources` or the write use of MCP `pc_source_add` meets a plugin Source type, Server
  hands the input to the host process for `input_class` validation, `resolve`, and Projections
  (`HostRequest.method="resolve"`), then saves the returned Source and Projections in the Server process with identity
  deduplication.

### 8.2 Collection target table

```text
pc_source_collect_targets
  scope_id            VARCHAR(256)  PK
  plugin_name         VARCHAR(64)   PK
  target_key          VARCHAR(512)  PK
  binding_id          VARCHAR(256)  NOT NULL   -- derived from plugin name and target key; SHA-256 when too long
  normalized_input    JSON          NOT NULL
  enabled             BOOLEAN       NOT NULL
  created_at          TIMESTAMP     NOT NULL
  updated_at          TIMESTAMP     NOT NULL
  last_collected_at   TIMESTAMP     NULL
  last_failure        VARCHAR(64)   NULL       -- latest failure category
```

### 8.3 Target management

| Operation | Implementation |
| --- | --- |
| Add | Validate against the JSON Schema of `target_input` in the Server process; run `identify_target` in the host process; the same key with the same normalized input returns the existing target, the same key with a different input returns 409; a new target is written and one collection is requested |
| Inspect | Read the target table and checkpoints |
| Disable / restore | Update `enabled`; restoring requests one collection |

### 8.4 Binding IDs

`binding_id = f"plugin:{plugin_name}:{target_key}"`, or `f"plugin:{plugin_name}:sha256:{digest}"` beyond 256 characters.
Edge mode prefixes the edge identity (§8.5).

### 8.5 Edge mode

`powercontext source-agent --server URL` starts a process that runs only Source plugin collection:

| Part | Implementation |
| --- | --- |
| Local storage | SQLite in the edge data directory, with only the collection target table and the Supervisor's scheduling tables |
| Scheduling | A local Supervisor registering only Source plugin collection bindings |
| Target management | Local `pc_source_add`, `pc_source_status`, `pc_source_target_set_enabled` |
| Definition registration | At startup, `POST /v1/source-definitions/register` for each definition; skipped when the credential lacks `server.admin`, which then requires registering in advance |
| Submission | Reuses `RemoteConnectorWorker` from `client/ingestion.py` with its Source sink and checkpoint store |
| Host process | None; `resolve` and Projections run in the collection Worker |

Each `collect`:

1. `POST /v1/connector-checkpoints/get` reads each target's checkpoint;
2. the plugin's `collect` runs;
3. `resolve` and Projections run in the Worker, and each item is submitted with `POST /v1/source-observations`;
4. once all succeed, `POST /v1/connector-checkpoints/commit` with the old value as the expected value;
5. any failed step fails the invocation, which backs off; submitted Sources are kept, and the next collection is
   deduplicated by identity at the center.

The edge binding ID is `f"agent:{agent_id}:plugin:{plugin_name}:{target_key}"`, digested by length in the same way.

---

## 9. Dedicated storage and models

### 9.1 Dedicated storage

| Backend | Location | Allocation |
| --- | --- | --- |
| SQLite | `<data directory>/plugins/<plugin name>.sqlite3` | The file is created with WAL on the first `engine()` call |
| OceanBase / seekdb | Database `powercontext_plugin_<plugin name>` (`-` replaced by `_`) | `CREATE DATABASE IF NOT EXISTS` on the first `engine()` call; without permission it raises and asks the deployer to create the database in advance |

- `engine()` returns an `AsyncEngine` whose URL replaces the database name or file path in Server's database
  configuration, with the same login.
- Workers create the engine per invocation and release it at the end; host processes reuse it for their lifetime.
- Removing a plugin does not delete this storage.

### 9.2 Models

`PluginModels` is built in the host process or Worker from the inference configuration Server passes, reusing core's
generation and embedding model clients:

- `embed(texts)` calls Server's embedding model, and `embedding_profile` gives the profile ID and dimension;
- `generate(messages, max_tokens)` calls Server's generation model;
- calls count toward the existing model usage statistics, with `operation` labeled `plugin:<name>`;
- the inference configuration travels through the Worker spec and host process startup arguments, and plugin code never
  sees its addresses or credentials.

---

## 10. Persistence and migration

One new database migration performs:

| Change | Objects |
| --- | --- |
| Add | `pc_source_collect_targets` (§8.2) |
| Drop columns | In `pc_artifact_processing_binding_states`: `plugin_id`, `plugin_family`, `binding_generation`, `desired_state`, `phase`, `last_error`, `plugin_spec` |
| Drop tables | `pc_artifact_family_schemas`; `pc_source_write_plugins`; search state and vector index tables of custom-family declarative indexes (once built-in families are confirmed not to depend on them) |
| Delete data | Artifacts, heads, and lineage written by old Artifact plugins (by old plugin family) |

Unaffected: `pc_sources`, Projections, the journal, `pc_source_definition_manifests`, `pc_artifacts` with its heads,
lineage, merge records, handles, and tags; `pc_source_cursors`; `pc_connector_checkpoints`; the Supervisor's invocation
intent, pending, and lease tables.

The migration notes list added and removed tables, columns, and data ranges. Code removed at the same time:
`/v1/admin/artifact-plugins` and its implementation, the `source_management` package (management service, Runtime,
poll / reply channel, `source load`), the old Worker plugin executor, and declarative indexes such as
`ArtifactSearchDeclaration`.

---

## 11. Public reads and search

- Plugin family Artifacts are stored as `PluginArtifactContent`: `content` as is, and `searchable_text` extracted by
  `search_text(content, search_fields)`, run through the existing lexical analysis, and written to
  `pc_artifact_heads.searchable_text`, saved per revision.
- On SQLite, plugin families share `pc_artifact_fts` with Experience and Skill; `ensure_plugin_search` makes sure the
  auxiliary table exists, rebuilds backfill every head with search text, and readiness checks compare only built-in
  family rows.
- The OceanBase search approach and scale validation come with the implementation PR.
- After a plugin is removed, its family is no longer registered and the generic APIs return 404; the data is kept.

---

## 12. Observability

| Field | Meaning |
| --- | --- |
| `plugin_name` | Plugin name |
| `plugin_method` | `collect`, `generate`, `resolve`, `identify_target`, `create_artifact`, `update_artifact`, `merge_artifacts`, `forget_artifact`, `restore_artifact` |
| `plugin_process` | `worker` or `host` |
| `trigger` | `schedule`, `sources`, `target_added`; `request` for synchronous calls |
| `operation_count` | Write operations committed (Artifacts) or Sources committed (collect) |
| `target_count` | Targets in this collect |
| `transaction_ms` | Time from the start of the write transaction block to commit or rollback, in milliseconds |

- Host process start, termination, and restart each record one event with `plugin_name` and the termination reason
  (`timeout`, `crash`, `server_stop`).
- Metric labels use `plugin_name`, `plugin_method`, and `plugin_process`, never Scope IDs or target keys.
- Logs go through `log_safely` and exclude Source content, Projections, Artifact content, target inputs, and environment
  variable values; plugin exceptions log only their type.
- New fields are registered in the allowlist and documented in both locales' observability docs.

---

## 13. Defaults

| Item | Default | Configuration |
| --- | --- | --- |
| Check child process time limit | 60 seconds | Fixed |
| Check concurrency | 4 | Fixed |
| Write transaction block time limit | 30 seconds | Declaration `transaction_timeout_seconds` |
| Worker total time limit | 300 seconds (Artifact), 120 seconds (collect) | Declaration |
| User change time limit | 30 seconds | Declaration `change_timeout_seconds` |
| Source write and `identify_target` time limit | 10 seconds | `POWERCONTEXT_SERVER_PLUGIN_SYNC_TIMEOUT_SECONDS` |
| Host process concurrency | 8 | `POWERCONTEXT_SERVER_PLUGIN_HOST_CONCURRENCY` |
| Host process restart backoff | From 1 second, doubling, capped at 60 seconds | Fixed |
| Operations / single content / total bytes per write block | 1000 / 256 KiB / 8 MiB | Fixed |
| `on_sources` admission interval | 2 seconds | Fixed |

---

## 14. Task breakdown

| # | Task | Depends on |
| --- | --- | --- |
| T1 | Writer: unchanged-content rule, read-revision checks, `ArtifactChange`, user-change mode | — |
| T2 | Code fingerprint: computed in the check child process, verified before Worker imports | — |
| T3 | Host processes: `host.py`, `host_protocol.py`, `server/plugin_hosts.py`, with timeouts, backoff, and stop | T2 |
| T4 | User changes through plugins: Server routing, version prechecks, error mapping, `lineage_only` Sources; replaces core's direct execution | T1, T3 |
| T5 | The create API's plugin branch: OpenAPI, generated code, Server implementation | T4 |
| T6 | `ctx.storage`, `ctx.models` | T3 |
| T7 | Source plugins: declaration, definition registration, write path in the host process | T3 |
| T8 | Collection: target table migration, target management APIs, collect executor | T7 |
| T9 | Edge mode `source-agent` | T8 |
| T10 | Removal of the old plugin frameworks and migration | T4, T8 |
| T11 | Observability field registration and docs | T3, T8 |

---

## 15. Open items

| Item | Notes |
| --- | --- |
| Search for plugin families on OceanBase | Approach and scale validation settled before T5 |
| Host process defaults | Concurrency, synchronous time limits, and backoff parameters need confirming under load with real plugins |
| Permission to create the dedicated database | Whether to fall back to plugin-prefixed tables in core's database when Server's login cannot create databases |
| Check concurrency | Whether 4 fits, to be measured in deployments with many plugins |

---

## 16. Design decisions beyond the RFC

The RFC defines only the contract. The table lists decisions this document makes to implement it that the RFC does not
specify, to be confirmed one by one in review. "Proposed change" means a better option exists, and the corresponding
section will be rewritten once it is confirmed.

| # | Decision | Reason | Status |
| --- | --- | --- | --- |
| D1 | Server and the host process exchange newline-delimited JSON over stdin / stdout (§6.2) | Matches the check child process and works across platforms | Proposed change: use `multiprocessing` spawn with a duplex `Pipe`, the same as the existing Worker launcher (`SpawnArtifactProcessingWorkerLauncher`); a plugin's `print` cannot break the channel, and bytes pass directly |
| D2 | The host process carries only the plugin, and authorization and Scope management call back to Server (§6; RFC #2579 design §6) | The authentication context lives in the Server process | Proposed change: the host process is "core runtime + plugin", building its own database connections and authorization service from configuration like a Worker (`open_worker_security`); Server only authenticates the caller and forwards the request, and callbacks go away |
| D3 | Version prechecks for user changes run in the Server process (§7.1) | Invalid requests never reach the host process | Follows D2: moves into the host process, and Server only authenticates and forwards |
| D4 | When a plugin does not implement a user-change method, Server returns 422 from the declaration (§7.1) | No need to involve the host process | Adopted |
| D5 | Plugin checks run concurrently, up to 4 (§3.1) | A check child process takes about 0.6 seconds; checking many plugins serially slows startup | Adopted; the limit is to be measured |
| D6 | Reads outside the transaction record the earliest revision read for each Artifact, and reads of historical revisions are not recorded (§5.3) | The earliest read is where the plugin's decision starts | Adopted |
| D7 | A Source definition manifest already registered at the center that is identical to the plugin's counts as the same definition; only different contents conflict (§8.1) | Reuses the existing manifest registration (`SourceDefinitionManifestRepository.register` returns the existing record for an identical manifest and conflicts only on different contents); several edge agents registering the same definition, or the same plugin installed on both an edge agent and the center, rely on this | Adopted |
| D8 | The binding ID format for collection targets, digested when too long, with an edge identity prefix in edge mode (§8.4, §8.5) | Deterministic derivation; two edge agents collecting the same external resource never overwrite each other's checkpoints | Adopted |
| D9 | The dedicated storage engine is created per invocation in Workers and reused in host processes (§9.1) | Matches the lifetime of the factory instance | Adopted |
| D10 | Plugin model calls are counted in usage statistics as `plugin:<name>` (§9.2) | Separates plugin model usage from built-in features | Adopted |
| D11 | A single request timing out terminates the whole host process (§6.3) | asyncio cannot reliably interrupt one blocked request | Adopted |
| D12 | The synchronous time limits, host process concurrency, and restart backoff in §13 | Initial values | To be confirmed under load |
