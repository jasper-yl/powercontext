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

"""Durable manual requests, delivered in order and removed with the invocation commit.

Requests are queued rather than coalesced to the last one: plugins may carry user
commands in them, and an earlier command must not disappear behind a later one.
The queue reuses the per-Scope plugin state row in ``pc_connector_checkpoints``.
"""

from __future__ import annotations

import json
from typing import Any

import rfc8785
from pydantic import JsonValue
from sqlalchemy import insert, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.persistence.tables import CONNECTOR_CHECKPOINTS_TABLE

MAX_PENDING_REQUESTS = 100
MAX_REQUEST_BYTES = 64 * 1024
_KIND = "plugin-requests"


class PluginRequestRejected(ValueError):
    """The request is too large or the Scope already has too many pending requests."""


def _identity(binding_name: str, scope_id: str):
    table = CONNECTOR_CHECKPOINTS_TABLE
    return table.c.scope_id == scope_id, table.c.binding_id == f"{binding_name}#requests"


async def _load(connection: AsyncConnection, binding_name: str, scope_id: str) -> dict[str, Any] | None:
    payload = await connection.scalar(
        select(CONNECTOR_CHECKPOINTS_TABLE.c.checkpoint).where(*_identity(binding_name, scope_id))
    )
    return None if payload is None else json.loads(bytes(payload))


async def accept_request(connection: AsyncConnection, binding_name: str, scope_id: str, params: JsonValue) -> int:
    """Persist one request and accept an invocation in the caller's transaction."""

    if len(rfc8785.dumps(params)) > MAX_REQUEST_BYTES:
        raise PluginRequestRejected("params exceed the request size limit")  # noqa: TRY003
    # Accepting the intent first serializes concurrent requests on its row.
    await ArtifactProcessingIntentRepository().request(connection, scope_id, binding_name)
    state = await _load(connection, binding_name, scope_id)
    queue = {"next": 1, "items": []} if state is None else state
    if len(queue["items"]) >= MAX_PENDING_REQUESTS:
        raise PluginRequestRejected("too many pending requests for this Scope")  # noqa: TRY003
    sequence = queue["next"]
    queue = {"next": sequence + 1, "items": [*queue["items"], [sequence, params]]}
    await _store(connection, binding_name, scope_id, queue, insert_row=state is None)
    return sequence


async def pending_requests(
    connection: AsyncConnection, binding_name: str, scope_id: str
) -> tuple[tuple[int, JsonValue], ...]:
    state = await _load(connection, binding_name, scope_id)
    return () if state is None else tuple((int(sequence), params) for sequence, params in state["items"])


async def clear_requests(connection: AsyncConnection, binding_name: str, scope_id: str, through: int) -> None:
    state = await _load(connection, binding_name, scope_id)
    if state is None:
        return
    remaining = [item for item in state["items"] if item[0] > through]
    await _store(connection, binding_name, scope_id, {"next": state["next"], "items": remaining}, insert_row=False)


async def _store(
    connection: AsyncConnection, binding_name: str, scope_id: str, queue: dict[str, Any], *, insert_row: bool
):
    payload = rfc8785.dumps(queue)
    table = CONNECTOR_CHECKPOINTS_TABLE
    if insert_row:
        await connection.execute(
            insert(table).values(
                scope_id=scope_id,
                binding_id=f"{binding_name}#requests",
                connector_name=_KIND,
                connector_version="1",
                checkpoint=payload,
            )
        )
    else:
        await connection.execute(update(table).where(*_identity(binding_name, scope_id)).values(checkpoint=payload))
