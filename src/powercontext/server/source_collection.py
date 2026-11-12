# Copyright (c) 2026 OceanBase.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Central collection targets and runner sync (design demo of the Source plugins RFC).

Runners pull their targets and report their capabilities and status; the Server never
connects to them. This demo keeps both tables in a separate SQLite file next to the
Server data, instead of a registered migration revision of the Server database.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response
from pydantic import BaseModel, ConfigDict, Field, JsonValue

from powercontext.sources.collection import target_key

_SCHEMA = """
CREATE TABLE IF NOT EXISTS pc_source_runners (
    runner TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    capabilities TEXT NOT NULL,
    capabilities_digest TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS pc_source_collect_targets (
    scope_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    runner TEXT NOT NULL,
    capability TEXT NOT NULL,
    target_key TEXT NOT NULL,
    parameters TEXT NOT NULL,
    enabled INTEGER NOT NULL,
    updated_at TEXT NOT NULL,
    updated_by TEXT NOT NULL,
    last_attempt_at TEXT,
    last_success_at TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    last_error_code TEXT,
    last_error_message TEXT,
    PRIMARY KEY (scope_id, target_id),
    UNIQUE (scope_id, runner, capability, target_key)
);
CREATE INDEX IF NOT EXISTS ix_pc_source_collect_targets_runner ON pc_source_collect_targets (runner);
"""


class Capability(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,63}$")
    description: str
    parameters_schema: dict[str, JsonValue]
    identity: dict[str, list[str]]


class SyncRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    capabilities: list[Capability]


class AddTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    runner: str = Field(min_length=1, max_length=128)
    capability: str
    parameters: dict[str, JsonValue]


class ChangeTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool | None = None
    parameters: dict[str, JsonValue] | None = None


class TargetStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    scope_id: str
    target_id: str
    succeeded: bool
    error_code: str | None = None
    error_message: str | None = Field(default=None, max_length=1024)


class CollectionError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status, self.code, self.message = status, code, message


def _now() -> str:
    return datetime.now(UTC).isoformat()


class CollectionStore:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = asyncio.Lock()
        with sqlite3.connect(path) as connection:
            connection.executescript(_SCHEMA)

    async def run(self, operation):
        async with self.lock:
            return await asyncio.to_thread(self._run, operation)

    def _run(self, operation):
        with sqlite3.connect(self.path) as connection:
            connection.row_factory = sqlite3.Row
            return operation(connection)


def _principal_id() -> str:
    from powercontext.server.context import current_principal

    principal = current_principal()
    return "local" if principal is None else f"{principal.type}:{principal.id}"


async def _require(request: Request, action: str, scope_id: str | None) -> None:
    from powercontext.server.authz import AccessAction, AccessAuditContext, ResourceRef
    from powercontext.server.authz.errors import AccessDeniedError
    from powercontext.server.authz.service import access_control_for_mode
    from powercontext.server.context import current_principal

    access = access_control_for_mode(request.app.state.access_control, mode=request.app.state.access_mode)
    principal = current_principal()
    if access is None or principal is None:
        return
    resource = ResourceRef.server() if scope_id is None else ResourceRef.scope(scope_id)
    try:
        await access.require(
            principal, AccessAction(action), resource, context=AccessAuditContext(transport="http", operation=action)
        )
    except AccessDeniedError:
        raise CollectionError(403, "access_denied", "The caller is not allowed to perform this operation.") from None


def _require_runner(row: sqlite3.Row | None, runner: str) -> None:
    from powercontext.server.context import current_principal

    principal = current_principal()
    # The first sync records the subject; afterwards only that subject, or one named like the runner, may use it.
    if principal is None or row is None:
        return
    if principal.id != runner and row["subject"] != _principal_id():
        raise CollectionError(403, "access_denied", "A runner can sync and report only its own targets.")


def _target(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "scope_id": row["scope_id"],
        "target_id": row["target_id"],
        "runner": row["runner"],
        "capability": row["capability"],
        "target_key": row["target_key"],
        "parameters": json.loads(row["parameters"]),
        "enabled": bool(row["enabled"]),
        "updated_at": row["updated_at"],
        "status": {
            "last_attempt_at": row["last_attempt_at"],
            "last_success_at": row["last_success_at"],
            "consecutive_failures": row["consecutive_failures"],
            "last_error_code": row["last_error_code"],
            "last_error_message": row["last_error_message"],
        },
    }


def _validate_parameters(capability: dict[str, Any], parameters: dict[str, Any]) -> str:
    from jsonschema import Draft202012Validator

    error = next(Draft202012Validator(capability["parameters_schema"]).iter_errors(parameters), None)
    if error is not None:
        field = ".".join(str(part) for part in error.absolute_path) or "parameters"
        raise CollectionError(422, "invalid_parameters", f"{field}: {error.message}")
    try:
        return target_key(capability["name"], parameters, capability["identity"])
    except ValueError as invalid:
        raise CollectionError(422, "invalid_parameters", str(invalid)) from None


def mount_source_collection_routes(app: FastAPI, data_dir: Path) -> None:
    store = CollectionStore(data_dir / "source-collection.sqlite3")

    @app.exception_handler(CollectionError)
    async def collection_error(request, error: CollectionError):
        return JSONResponse(
            {"error": {"code": error.code, "message": error.message, "details": None}}, status_code=error.status
        )

    async def list_runners() -> dict[str, Any]:
        rows = await store.run(lambda c: c.execute("SELECT * FROM pc_source_runners ORDER BY runner").fetchall())
        return {
            "items": [
                {
                    "runner": row["runner"],
                    "capabilities": json.loads(row["capabilities"]),
                    "last_seen_at": row["last_seen_at"],
                }
                for row in rows
            ]
        }

    async def sync(runner: str, body: SyncRequest) -> dict[str, Any]:
        capabilities = json.dumps([item.model_dump(mode="json") for item in body.capabilities], sort_keys=True)
        subject = _principal_id()

        def operation(connection):
            row = connection.execute("SELECT * FROM pc_source_runners WHERE runner=?", (runner,)).fetchone()
            _require_runner(row, runner)
            digest = hashlib.sha256(capabilities.encode()).hexdigest()
            if row is None:
                connection.execute(
                    "INSERT INTO pc_source_runners VALUES (?,?,?,?,?)", (runner, subject, capabilities, digest, _now())
                )
            elif row["capabilities_digest"] != digest:
                connection.execute(
                    "UPDATE pc_source_runners SET capabilities=?, capabilities_digest=?, last_seen_at=? WHERE runner=?",
                    (capabilities, digest, _now(), runner),
                )
            else:
                connection.execute("UPDATE pc_source_runners SET last_seen_at=? WHERE runner=?", (_now(), runner))
            rows = connection.execute(
                "SELECT * FROM pc_source_collect_targets WHERE runner=? AND enabled=1 ORDER BY scope_id, target_id",
                (runner,),
            ).fetchall()
            return [_target(row) for row in rows]

        return {"targets": await store.run(operation)}

    async def report(runner: str, body: TargetStatus) -> dict[str, Any]:
        def operation(connection):
            _require_runner(
                connection.execute("SELECT * FROM pc_source_runners WHERE runner=?", (runner,)).fetchone(), runner
            )
            now = _now()
            if body.succeeded:
                statement = (
                    "UPDATE pc_source_collect_targets SET last_attempt_at=?, last_success_at=?, consecutive_failures=0,"
                    " last_error_code=NULL, last_error_message=NULL WHERE scope_id=? AND target_id=? AND runner=?"
                )
                values = (now, now, body.scope_id, body.target_id, runner)
            else:
                statement = (
                    "UPDATE pc_source_collect_targets SET last_attempt_at=?, consecutive_failures=consecutive_failures+1,"
                    " last_error_code=?, last_error_message=? WHERE scope_id=? AND target_id=? AND runner=?"
                )
                values = (now, body.error_code, body.error_message, body.scope_id, body.target_id, runner)
            return connection.execute(statement, values).rowcount

        if not await store.run(operation):
            raise CollectionError(404, "not_found", "The target is not assigned to this runner.")
        return {"reported": True}

    async def add_target(scope_id: str, body: AddTarget, request: Request) -> JSONResponse:
        await _require(request, "scope.contribute", scope_id)
        actor = _principal_id()

        def operation(connection):
            row = connection.execute("SELECT * FROM pc_source_runners WHERE runner=?", (body.runner,)).fetchone()
            if row is None:
                raise CollectionError(422, "unknown_runner", "The runner has never synced with this Server.")
            capability = next((c for c in json.loads(row["capabilities"]) if c["name"] == body.capability), None)
            if capability is None:
                raise CollectionError(422, "unknown_capability", "The runner does not provide this capability.")
            key = _validate_parameters(capability, body.parameters)
            parameters = json.dumps(body.parameters, sort_keys=True, ensure_ascii=False)
            existing = connection.execute(
                "SELECT * FROM pc_source_collect_targets WHERE scope_id=? AND runner=? AND capability=? AND target_key=?",
                (scope_id, body.runner, body.capability, key),
            ).fetchone()
            if existing is not None:
                if existing["parameters"] != parameters:
                    raise CollectionError(
                        409, "target_conflict", "A target with the same identity has other parameters."
                    )
                return 200, _target(existing)
            target_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO pc_source_collect_targets (scope_id,target_id,runner,capability,target_key,parameters,"
                "enabled,updated_at,updated_by) VALUES (?,?,?,?,?,?,1,?,?)",
                (scope_id, target_id, body.runner, body.capability, key, parameters, _now(), actor),
            )
            return 201, _target(
                connection.execute(
                    "SELECT * FROM pc_source_collect_targets WHERE scope_id=? AND target_id=?", (scope_id, target_id)
                ).fetchone()
            )

        status, target = await store.run(operation)
        return JSONResponse(target, status_code=status)

    async def list_targets(scope_id: str, request: Request) -> dict[str, Any]:
        await _require(request, "scope.read", scope_id)
        rows = await store.run(
            lambda c: c.execute(
                "SELECT * FROM pc_source_collect_targets WHERE scope_id=? ORDER BY target_id", (scope_id,)
            ).fetchall()
        )
        return {"items": [_target(row) for row in rows]}

    async def change_target(scope_id: str, target_id: str, body: ChangeTarget, request: Request) -> dict[str, Any]:
        await _require(request, "scope.contribute", scope_id)
        actor = _principal_id()

        def operation(connection):
            row = connection.execute(
                "SELECT t.*, r.capabilities FROM pc_source_collect_targets t JOIN pc_source_runners r USING (runner)"
                " WHERE t.scope_id=? AND t.target_id=?",
                (scope_id, target_id),
            ).fetchone()
            if row is None:
                raise CollectionError(404, "not_found", "The target does not exist.")
            parameters, key = row["parameters"], row["target_key"]
            if body.parameters is not None:
                capability = next(c for c in json.loads(row["capabilities"]) if c["name"] == row["capability"])
                key = _validate_parameters(capability, body.parameters)
                parameters = json.dumps(body.parameters, sort_keys=True, ensure_ascii=False)
            enabled = row["enabled"] if body.enabled is None else int(body.enabled)
            try:
                connection.execute(
                    "UPDATE pc_source_collect_targets SET parameters=?, target_key=?, enabled=?, updated_at=?, updated_by=?"
                    " WHERE scope_id=? AND target_id=?",
                    (parameters, key, enabled, _now(), actor, scope_id, target_id),
                )
            except sqlite3.IntegrityError:
                raise CollectionError(409, "target_conflict", "Another target already has this identity.") from None
            return _target(
                connection.execute(
                    "SELECT * FROM pc_source_collect_targets WHERE scope_id=? AND target_id=?", (scope_id, target_id)
                ).fetchone()
            )

        return await store.run(operation)

    async def delete_target(scope_id: str, target_id: str, request: Request) -> Response:
        await _require(request, "scope.contribute", scope_id)
        deleted = await store.run(
            lambda c: (
                c.execute(
                    "DELETE FROM pc_source_collect_targets WHERE scope_id=? AND target_id=?", (scope_id, target_id)
                ).rowcount
            )
        )
        if not deleted:
            raise CollectionError(404, "not_found", "The target does not exist.")
        # The demo keeps the target's checkpoint; the RFC deletes it together with the target.
        return Response(status_code=204)

    for path, endpoint, methods in (
        ("/v1/source-runners", list_runners, ["GET"]),
        ("/v1/source-runners/{runner}/sync", sync, ["POST"]),
        ("/v1/source-runners/{runner}/report", report, ["POST"]),
        ("/v1/scopes/{scope_id}/source-targets", add_target, ["POST"]),
        ("/v1/scopes/{scope_id}/source-targets", list_targets, ["GET"]),
        ("/v1/scopes/{scope_id}/source-targets/{target_id}", change_target, ["PATCH"]),
        ("/v1/scopes/{scope_id}/source-targets/{target_id}", delete_target, ["DELETE"]),
    ):
        app.router.add_api_route(path, endpoint, methods=methods, include_in_schema=False)
