---
title: Write a plugin
description: Extend PowerContext with a directory and plugin.py — generate your own Artifacts, define Source types, and provide APIs and Dashboard pages.
---

# Write a plugin

A plugin is a folder in the Server's plugin directory, with `plugin.py` as its entry point. A plugin can:

| Capability | What it means |
| --- | --- |
| Generate its own Artifacts | Declare an Artifact family, read Sources, and write Artifacts of that family; callers read, search, and tag them through the existing Artifact APIs |
| Support user changes | Implement create, update, merge, forget, and restore methods so the generic Artifact APIs work on the plugin's family |
| Define Source types | Declare the plugin's own Source types and Projections to record what users asked for, such as an upload |
| Provide APIs | Declare HTTP routes under `/v1/plugins/<plugin name>/api/`; core authenticates callers and forwards requests to the plugin |
| Provide Dashboard pages | Ship static pages that the Dashboard shows in a sandboxed iframe |
| Use dedicated storage and models | Keep the plugin's own data in separate storage that core allocates, and call the embedding and generation models Server configures |

Plugin code runs in the Server's Python environment and is treated as trusted code with the deployer's permissions. Load
only plugins you trust.

## Enable the plugin directory

Set the plugin directory and restart Server:

```bash
export POWERCONTEXT_SERVER_PLUGINS_DIR=/srv/powercontext/plugins
powercontext server run
```

- Each subdirectory is a plugin, and its name is the plugin name (it starts with a lowercase letter and contains only
  lowercase letters, digits, and `-`).
- Install the third-party libraries a plugin needs into the Server's Python environment, for example
  `uv pip install pypdf`. A `requirements.txt` in the plugin directory only informs deployers; Server does not install it.
- Restart Server after changing a plugin. Server records a content fingerprint of the plugin directory when it loads and
  refuses to run plugin files that changed afterward, so old and new code never mix.

Check what loaded with administrator credentials:

```bash
curl -H "Authorization: Bearer $ADMIN_TOKEN" http://127.0.0.1:8000/v1/plugins
```

Each plugin's `status` is `loaded` or `failed`; for failures, `error` gives the reason, such as an invalid declaration
field or a failed import.

## A first plugin: turn text into notes

Create `notes/plugin.py` in the plugin directory:

```python
from powercontext.plugins import ArtifactPlugin, ArtifactWriter, GenerateRequest, Inputs


class Notes:
    async def generate(self, request: GenerateRequest, writer: ArtifactWriter) -> None:
        async with writer.transaction() as tx:
            for source in request.sources:
                text = source.projection["content"]
                await tx.put(writer.stable_id(source.ref.source_id), content={"text": text}, source_refs=[source.ref])


async def create(ctx):
    return Notes()


plugin = ArtifactPlugin(
    family="notes",
    inputs=Inputs(source_types=("content",), projection="powercontext.text-evidence@1"),
    on_sources=True,
    create=create,
)
```

After restarting Server, write a text Source; a few seconds later the plugin's Artifact can be read:

```bash
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/v1/scopes/$SCOPE_ID/sources -d '{"content": "Deploys go to the east region first"}'

curl -H "Authorization: Bearer $TOKEN" http://127.0.0.1:8000/v1/scopes/$SCOPE_ID/artifacts/notes
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  http://127.0.0.1:8000/v1/scopes/$SCOPE_ID/artifacts/notes/search -d '{"query": "east"}'
```

This plugin has the three basic parts of every plugin:

- **The declaration** `plugin = ArtifactPlugin(...)` tells core which family the plugin owns, which Sources it consumes,
  and when it runs.
- **The factory** `create(ctx)` returns the plugin object. Read the plugin's own settings from environment variables and
  open the resources it needs here.
- **Generation** `generate(request, writer)` receives a batch of new Sources and writes Artifacts.

## The declaration

| Field | Meaning |
| --- | --- |
| `family` | The Artifact family the plugin owns; it must not reuse a built-in family or another plugin's |
| `inputs` | `Inputs(source_types=..., projection=..., projections={...}, max_sources=50)`: the Source types consumed, the Projection each type arrives in, and the most Sources delivered at once. `projection` applies to every type; `projections` names one per type |
| `on_sources` | Run when new Sources arrive |
| `schedule` | `Interval(seconds=...)` or `Cron(expression=..., timezone=...)` to run on a schedule; declare this, `on_sources`, or both |
| `scopes` | Serve only these Scopes; required for plugins that consume no Sources |
| `search_fields` | Top-level content fields indexed for full-text search; all top-level string fields when unset |
| `timeout_seconds`, `transaction_timeout_seconds` | Time limits for one generation run and its write transaction |
| `change_timeout_seconds` | Time limit for one user change or API call |
| `definitions` | The plugin's own Source Definitions; see "Define Source types" |
| `api` | The plugin's APIs; see "Provide APIs" |
| `pages` | Dashboard pages; see "Dashboard pages" |
| `create` | The factory; it must be an async function |

Server imports `plugin.py` and checks the declaration in a separate child process. If the import fails or the declaration
is invalid, the plugin is `failed`, without affecting Server or other plugins.

## Generate Artifacts

`generate` runs in a separate Worker process that Server starts, once per Scope:

- `request.scope_id` is the call's Scope; `request.sources` are the Sources delivered, each with a `ref` (its reference)
  and a `projection` (its content in the declared Projection).
- Put writes inside `async with writer.transaction() as tx:`. The writes in the block commit in one transaction, together
  with the record that this batch of Sources was handled; if generation fails or times out, the batch is delivered again.
- Do slow work such as model calls and file parsing outside the transaction, and only write inside it.
- `tx.put(artifact_id, content=..., source_refs=[...])` writes a new revision and records its provenance in
  `source_refs`. Content identical to the current revision adds no revision, so repeated delivery never writes duplicate
  revisions. `writer.stable_id(...)` derives a stable ID from its inputs, so the same Source always writes to the same
  Artifact.
- Other operations include `tx.set_lifecycle`, `tx.merge`, `tx.revert`, and `tx.set_tags`; read current state with
  `writer.get`, `writer.list`, and `writer.search`.

Generated Artifacts behave like built-in ones: read, search, and tag them through the
`/v1/scopes/{scope_id}/artifacts/{family}` APIs.

## Support user changes

When the plugin object implements the methods below, the generic Artifact APIs work on the plugin's family; APIs whose
method is missing return 422:

| Method | API |
| --- | --- |
| `create_artifact(request, writer)` | `POST /v1/scopes/{scope_id}/artifacts` with body `{"family": "<family>", "content": {...}}` |
| `update_artifact(request, writer)` | `PUT /v1/scopes/{scope_id}/artifacts/{family}/{artifact_id}` with `If-Match` |
| `merge_artifacts(request, writer)` | `POST /v1/scopes/{scope_id}/artifacts/{family}/merges` |
| `forget_artifact(request, writer)` | `POST .../{artifact_id}/forget` |
| `restore_artifact(request, writer)` | `POST .../{artifact_id}/restorations` |

```python
from powercontext.plugins import InvalidContent


class Notes:
    async def update_artifact(self, request, writer) -> None:
        if not request.content.get("text"):
            raise InvalidContent("text is required", field="text")
        async with writer.transaction() as tx:
            await tx.put(request.artifact_id, content=request.content, expected_revision=request.expected_revision)

    async def forget_artifact(self, request, writer) -> None:
        async with writer.transaction() as tx:
            await tx.set_lifecycle(request.artifact_id, "deprecated")
```

- Core checks the caller's permissions and the revision before calling the method; the method only validates content and
  makes its one agreed write.
- Raising `InvalidContent` returns 422 with the message shown to the caller.
- User changes record provenance as the person who made them, as edits of built-in Artifacts do.

## Define Source types

Sources record what happened and what users asked for. A plugin can define its own Source types, such as "a file was
uploaded" or "some content was edited", and consume them in generation:

```python
from pydantic import BaseModel
from powercontext.sources import Source, SourceMaterialization


class TicketInput(BaseModel):            # the input when submitting
    key: str
    title: str


class TicketSource(Source):              # the Source core stores
    title: str


class TicketProjection:                  # what generate receives
    name = "tickets.ticket"
    version = "1"
    source_class = TicketSource
    output_class = TicketInput

    def project(self, source, /):
        return TicketInput(key=source.name, title=source.title)


class TicketDefinition:
    name = "ticket"                      # the Source type name
    version = "1"
    input_class = TicketInput
    source_class = TicketSource
    projections = (TicketProjection(),)

    async def resolve(self, value, /):
        return TicketSource(
            name=value.key, definition_version=self.version,
            materialization=SourceMaterialization.CAPTURED, description="A ticket", title=value.title,
        )

    async def read(self, source, /):
        return TicketProjection().project(source)


plugin = ArtifactPlugin(
    family="tickets",
    inputs=Inputs(source_types=("ticket",), projection="tickets.ticket@1"),
    on_sources=True,
    definitions=(TicketDefinition(),),
    create=create,
)
```

- When the plugin loads, core registers these Source types. Callers can then write them through the generic Source API:
  `POST /v1/scopes/{scope_id}/sources` with body `{"source_type": "ticket", "content": {"key": "OPS-7", "title": "..."}}`.
  The plugin validates the input and resolves it into a Source; invalid input returns 422.
- The plugin's own APIs submit them with `ctx.sources.submit(scope_id, "ticket", TicketInput(...))`.
- A Source's `name` is its identity in the Scope; submitting the same `name` again yields the same Source.
- When a Source or Projection changes shape, raise the definition's `version` and set `definition_version` to it in
  `resolve`. Core registers definitions by name and version, so old and new versions coexist, and stored Sources keep
  their old version's meaning.
- A plugin that consumes several Source types, each needing its own Projection, names them in `projections`:

  ```python
  Inputs(source_types=("upload", "edit"), projections={"upload": "kb.upload@1", "edit": "kb.edit@1"})
  ```

  In `generate`, branch on `source.ref.source_type`.

## Provide APIs

A plugin can declare its own HTTP routes; handlers are async methods of the plugin object:

```python
from pydantic import BaseModel
from powercontext.plugins import ApiError, ApiRequest, PluginApi, Route


class NewTicket(BaseModel):
    scope_id: str
    key: str
    title: str


class Tickets:
    def __init__(self, ctx):
        self.ctx = ctx

    async def open_ticket(self, request: ApiRequest) -> dict:
        body: NewTicket = request.body
        await self.ctx.require("scope.contribute", body.scope_id)
        ref = await self.ctx.sources.submit(body.scope_id, "ticket", TicketInput(key=body.key, title=body.title))
        return {"source_id": ref.source_id}


plugin = ArtifactPlugin(
    ...,
    api=PluginApi(routes=(
        Route(method="POST", path="/tickets", handler="open_ticket", access="authenticated",
              body="json", request_model=NewTicket),
    )),
)
```

- Routes mount at `/v1/plugins/<plugin name>/api<path>`, for example `POST /v1/plugins/tickets/api/tickets`. Callers use
  the same credentials as for every other API.
- `access` sets the check core runs before calling the handler:
  - `authenticated`: the caller must be signed in;
  - `scope.read` / `scope.contribute`: the caller needs that permission on the Scope in the path parameter named by
    `scope_param`.
- In handlers, `ctx.require(action, scope_id)` checks further permissions and makes the API return 403 when the caller
  lacks them; `ctx.allowed(...)` only answers true or false.
- Reading and writing core data: `ctx.reads(scope_id)` reads with the caller's permissions; `ctx.writer(scope_id)` and
  `ctx.sources.submit(...)` work only for Scopes whose `scope.contribute` was confirmed earlier in the request (by the
  route's `access` or a prior `ctx.require`).
- Writes in APIs do not record who did what. When you need that provenance, submit the operation as a Source, as in the
  example above, and let `generate` write the new revision referencing it.
- To create a Scope, call `ctx.scopes.create(title=..., summary=..., idempotency_key=...)`; the caller needs
  `server.admin`.
- `request.body` is the validated `request_model` instance, `bytes`, or `None`, according to `body`;
  `request.path_params`, `request.query`, and `request.caller` are the path parameters, query parameters, and caller.
- Return a Pydantic model or a dict for JSON, `BinaryResponse(content=..., content_type=..., filename=...)` for a file,
  or `None` for 204. Raise `ApiError(status, code, message)` for a 4xx error.
- Request body limits default to 1 MiB for JSON and 32 MiB for binary bodies. A route can declare `max_body_bytes`, which
  never exceeds the deployer's `POWERCONTEXT_SERVER_PLUGIN_API_MAX_BODY_BYTES`. Core returns 413 without calling the
  plugin when a body is too large.
- Each call is bounded by `timeout_seconds` (30 seconds by default) and returns 504 when it runs over.

APIs and user-change methods run in a long-lived host process that starts once the plugin loads; the factory
`create(ctx)` runs once in it, and the plugin object serves every request. A host process that crashes or times out is
restarted, and APIs return 503 meanwhile.

## Dedicated storage and models

```python
from sqlalchemy import text


async def create(ctx):
    engine = ctx.storage.engine()          # points only to this plugin's storage
    async with engine.begin() as connection:
        await connection.execute(text("CREATE TABLE IF NOT EXISTS notes_index (id TEXT PRIMARY KEY, body TEXT)"))
    return Notes(ctx)
```

- `ctx.storage.engine()` returns an async SQLAlchemy engine for `plugins/<plugin name>.sqlite3` in the data directory. The
  plugin owns its tables and migrations; core never reads or backs up this storage. Only SQLite deployments are supported
  for now; on Servers using OceanBase or seekdb, accessing `ctx.storage` raises an error.
- This storage is outside core's transactions. Store large content such as files and vectors under its content digest and
  reference the digest from Artifacts: retried writes and rolled-back transactions then leave only unreferenced records,
  never Artifacts pointing at the wrong data.
- The storage belongs to the whole plugin and is not split by Scope. Partition data by `scope_id` and confirm on reads that
  data belongs to the request's Scope.
- `ctx.models` holds the models Server configures, or `None` when it configures neither:
  - `await ctx.models.embed(["..."])` returns vectors; `ctx.models.embedding_dimension` is their dimension;
  - `await ctx.models.generate([ChatMessage(role="user", content="...")], max_tokens=500)` returns generated text;
  - check `embedding_available` and `generation_available`; calling a model Server lacks raises `ModelUnavailable`.

## Dashboard pages

Plugins that declare APIs can ship pages:

```python
plugin = ArtifactPlugin(..., pages=PluginPages(directory="ui", entry="index.html", title={"zh": "工单", "en": "Tickets"}))
```

`ui/index.html` loads the client script core provides and calls the plugin's APIs through it:

```html
<script src="/dashboard/plugins/_client.js"></script>
<script>
  const client = powercontextPluginClient();
  const { language, theme, scopeId } = await client.context();
  const response = await client.request("POST", "/tickets", { json: { scope_id: scopeId, key: "OPS-7", title: "Disk full" } });
  if (response.ok) console.log(response.json);
</script>
```

- The page appears in the Dashboard's side navigation at `/dashboard/plugins/<plugin name>`.
- The page runs in a sandboxed iframe without the user's session; the Dashboard forwards its requests as the current user,
  only to routes the plugin declares.
- `client.request(method, path, options)` accepts `json`, `body` (such as a file's `ArrayBuffer`), `contentType`, and
  `query`, and returns `status`, `ok`, `json`, `blob`, and `filename`.
- Build the page as a single-page app: after the page navigates, the Dashboard stops forwarding and asks the user to reopen
  it. Browser dialogs such as `alert` and `confirm` are unavailable in the sandbox.
- Pages are static files only (HTML, JavaScript, CSS, images, fonts), up to 5 MiB each; scripts may come only from the
  plugin's own directory and `/dashboard/plugins/_client.js`.

## Troubleshooting

| Symptom | What to do |
| --- | --- |
| The plugin is `failed` in `/v1/plugins` | Read `error`; for import failures, check that dependencies are installed in the Server environment |
| APIs return 503 | The host process is starting or restarting; check the Server log for plugin errors |
| APIs return 500 `plugin_error` | A handler raised an unhandled exception; the Server log records its type |
| Workers stop running after a plugin change | Plugin files changed while Server was running; restart Server |
| Generated Artifacts do not appear | Check `GET /v1/plugins/<plugin name>/scopes` for Scopes with pending Sources |

## Current limits

- Updating a plugin requires a Server restart; hot reload is not available yet.
- Dedicated plugin storage supports only SQLite deployments for now.
- Deployers install plugin dependencies into the Server environment by hand.
- Plugin code has the Server's permissions; host processes and Workers isolate failures, not untrusted code.
