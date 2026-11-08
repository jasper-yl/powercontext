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

"""Plugin writes commit with the consumption cursor, or not at all."""

import asyncio
from uuid import uuid4

import pytest

from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.errors import RepositoryNotFoundError
from powercontext.builtin.runtime.composition import open_builtin_contexts
from powercontext.builtin.runtime.config import BuiltinConfig, SQLiteConfig
from powercontext.builtin.scope import ScopeDraft
from tests.plugin_support import current, invoke, write_plugin

REPORTS = """
from powercontext.plugins import ArtifactWriter, GenerateRequest


class Reports:
    def __init__(self, ctx):
        self.fail = (ctx.plugin_dir / "fail").exists()

    async def generate(self, request: GenerateRequest, writer: ArtifactWriter) -> None:
        async with writer.transaction() as tx:
            for source in request.sources:
                text = source.projection["content"]
                ref = await tx.put(
                    writer.stable_id(source.ref.source_id),
                    content={"text": text},
                    search_text=text,
                    source_refs=[source.ref],
                )
                summary = await tx.get("summary")
                count = 0 if summary is None else summary.content["count"]
                await tx.put("summary", content={"count": count + 1}, source_refs=[source.ref])
                # A plugin-owned store would be written here, after the real revision is known.
                assert ref.ref.revision >= 1
            if self.fail:
                raise RuntimeError("model output rejected")


async def create(ctx, config):
    return Reports(ctx)
"""


@pytest.fixture
def runtime(tmp_path):
    config = BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'plugin.db'}"))
    return config


async def _scope(contexts) -> str:
    draft = ScopeDraft(title="Plugins", summary="Plugins", idempotency_key=str(uuid4()))
    return (await contexts.scopes.create(draft)).scope_id


def test_writes_commit_with_cursor_and_failures_leave_both_unchanged(tmp_path, runtime):
    directory = write_plugin(tmp_path / "plugins", "reports", REPORTS)

    async def scenario():
        async with open_builtin_contexts(runtime) as contexts:
            database = contexts.database
            scope = await _scope(contexts)
            first = await contexts.records.create_source(scope, "content", "Deploy to east")
            await contexts.records.create_source(scope, "content", "Rotate keys")

            (directory / "fail").touch()
            with pytest.raises(RuntimeError, match="model output rejected"):
                await invoke(database, directory, scope)
            async with database.transaction() as connection:
                assert await SourceCursorRepository().load(connection, scope, "plugin:reports") is None
            with pytest.raises(RepositoryNotFoundError):
                await current(database, "reports", scope, "summary")

            (directory / "fail").unlink()
            await invoke(database, directory, scope)
            summary = await current(database, "reports", scope, "summary")
            assert summary.content.content == {"count": 2}
            assert summary.revision == 2
            async with database.transaction() as connection:
                cursor = await SourceCursorRepository().load(connection, scope, "plugin:reports")
            assert cursor is not None and cursor.cursor.sequence == 2

            # The committed window is never delivered again.
            await invoke(database, directory, scope)
            assert (await current(database, "reports", scope, "summary")).revision == 2
            assert first.source_id

    asyncio.run(scenario())


LIFECYCLE = """
from powercontext.plugins import ArtifactWriter, GenerateRequest


class Lifecycle:
    async def generate(self, request: GenerateRequest, writer: ArtifactWriter) -> None:
        results = {}
        async with writer.transaction() as tx:
            for command in request.requests:
                op = command["op"]
                if op == "put":
                    await tx.put(command["id"], content={"text": command["text"]}, search_text=command["text"])
                elif op == "merge":
                    await tx.merge(command["inputs"], into=command["into"], content={"text": command["text"]},
                                   search_text=command["text"])
                elif op == "undo":
                    await tx.undo_merge(command["id"])
                elif op == "lifecycle":
                    await tx.set_lifecycle(command["id"], command["state"])
                elif op == "revert":
                    await tx.revert(command["id"], to_revision=command["revision"])
                elif op == "tags":
                    await tx.set_tags(command["id"], command["tags"])
                elif op == "handle":
                    snapshot = await tx.get(command["id"])
                    await tx.bind_handle(command["key"], snapshot.ref)
                elif op == "search":
                    results[command["query"]] = [item.ref.artifact_id for item in await tx.search(command["query"])]
            if results:
                await tx.put("search-log", content={"results": results})


async def create(ctx, config):
    return Lifecycle()
"""


def test_writer_covers_shared_lifecycle_merge_and_search(tmp_path, runtime):
    directory = write_plugin(tmp_path / "plugins", "facts", LIFECYCLE)

    async def run(database, scope, *commands):
        await invoke(database, directory, scope, requests=commands)

    async def scenario():
        async with open_builtin_contexts(runtime) as contexts:
            database = contexts.database
            scope = await _scope(contexts)
            await run(
                database,
                scope,
                {"op": "put", "id": "a", "text": "region east"},
                {"op": "put", "id": "b", "text": "region north"},
                {"op": "put", "id": "a", "text": "region east"},
                {"op": "search", "query": "region"},
            )
            log = await current(database, "facts", scope, "search-log")
            # Reads inside the transaction see writes not yet committed.
            assert sorted(log.content.content["results"]["region"]) == ["a", "b"]
            assert (await current(database, "facts", scope, "a")).revision == 1

            await run(
                database, scope, {"op": "merge", "inputs": ["a", "b"], "into": "c", "text": "region east and north"}
            )
            await run(
                database, scope, {"op": "merge", "inputs": ["a", "b"], "into": "c", "text": "region east and north"}
            )
            await run(database, scope, {"op": "search", "query": "region"})
            log = await current(database, "facts", scope, "search-log")
            assert log.content.content["results"]["region"] == ["c"]

            await run(database, scope, {"op": "undo", "id": "c"})
            await run(database, scope, {"op": "search", "query": "region"})
            log = await current(database, "facts", scope, "search-log")
            assert sorted(log.content.content["results"]["region"]) == ["a", "b"]
            assert (await current(database, "facts", scope, "a")).revision == 2

            await run(
                database,
                scope,
                {"op": "put", "id": "a", "text": "region west"},
                {"op": "revert", "id": "a", "revision": 1},
                {"op": "lifecycle", "id": "b", "state": "deprecated"},
                {"op": "tags", "id": "a", "tags": ["Ops", "region"]},
                {"op": "handle", "id": "a", "key": "primary-region"},
                {"op": "search", "query": "region"},
            )
            log = await current(database, "facts", scope, "search-log")
            assert log.content.content["results"]["region"] == ["a"]
            reverted = await current(database, "facts", scope, "a")
            assert reverted.revision == 4 and reverted.content.content == {"text": "region east"}

            await run(database, scope, {"op": "lifecycle", "id": "b", "state": "active"})
            await run(database, scope, {"op": "lifecycle", "id": "b", "state": "retired"})
            with pytest.raises(Exception, match="retired"):
                await run(database, scope, {"op": "lifecycle", "id": "b", "state": "active"})

    asyncio.run(scenario())
