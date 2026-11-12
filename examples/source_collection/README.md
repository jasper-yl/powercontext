# Source collection design demo

A design demo of the Source plugins RFC; it is not meant for merging.

- `camera-events/` is a Source plugin for a simulated robot camera. The device writes
  `<CAMERA_SPOOL>/<camera>/<sequence>.json`; the checkpoint is the highest sequence reported.
- `powercontext source-agent` is the device runner. It syncs with the central Server, collects the
  targets assigned to it, submits Sources, and then advances each checkpoint against its old value.
- The Server keeps runners and targets in `source-collection.sqlite3` in its data directory, instead
  of a registered migration revision, and serves the runner and target routes outside the API contract.

Run the acceptance scenario against a real Server process:

```bash
uv run pytest tests/e2e/test_source_collection_demo.py
```

Try it by hand:

```bash
# On the device: register the plugin's definitions once (needs server.admin), then run.
export POWERCONTEXT_SOURCE_AGENT_TOKEN=...
CAMERA_SPOOL=/var/spool/camera powercontext source-agent --server https://memory.example.com \
  --plugins examples/source_collection --name robot-7 --register-definitions --once

# Centrally: list runners, then assign a target.
curl -H "Authorization: Bearer $TOKEN" https://memory.example.com/v1/source-runners
curl -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" \
  https://memory.example.com/v1/scopes/$SCOPE_ID/source-targets \
  -d '{"runner": "robot-7", "capability": "camera-events", "parameters": {"camera": "front"}}'
```

Not covered by the demo: the Server's built-in runner, large files through a central plugin route,
the database migration, export and restore, and the Dashboard form.
