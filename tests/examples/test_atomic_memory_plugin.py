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

"""Atomic Memory rebuilt as a plugin keeps the built-in memory lifecycle.

These scenarios mirror the built-in Atomic Memory behavior: one Artifact per memory,
revision on update, merge into a new identity with frozen inputs, undo of a merge,
forget and restore, and cursor safety when the model fails.
"""

import asyncio
import shutil
import sqlite3
from pathlib import Path
from uuid import uuid4

import pytest
import yaml
from sqlalchemy import select

from powercontext.builtin.inference import InferenceTimeoutError
from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.tables import ARTIFACT_HEADS_TABLE
from powercontext.builtin.plugins.search import PluginArtifactSearcher, PluginArtifactSearchRequest
from powercontext.builtin.runtime.composition import open_builtin_contexts
from powercontext.builtin.runtime.config import BuiltinConfig, SQLiteConfig
from powercontext.builtin.scope import ScopeDraft
from tests.plugin_support import current, invoke

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/plugins/atomic_memory"
FAMILY = "atomic-memory-plugin"
BINDING = f"plugin:{FAMILY}"


def deploy(root: Path, **config) -> Path:
    directory = root / FAMILY
    shutil.copytree(EXAMPLE, directory, ignore=shutil.ignore_patterns("__pycache__", "*.sqlite3"))
    manifest = yaml.safe_load((directory / "plugin.yml").read_text())
    manifest["config"] = {"model": "python:tests.atomic_memory_plugin_fake:pipeline", **config}
    (directory / "plugin.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return directory


async def heads(database, scope: str) -> dict[str, dict]:
    async with database.transaction() as connection:
        rows = (
            await connection.execute(
                select(ARTIFACT_HEADS_TABLE).where(
                    ARTIFACT_HEADS_TABLE.c.scope_id == scope, ARTIFACT_HEADS_TABLE.c.family == FAMILY
                )
            )
        ).mappings()
        result = {}
        for row in rows:
            artifact = await current(database, FAMILY, scope, row["artifact_id"])
            result[row["artifact_id"]] = {
                "text": artifact.content.content["text"],
                "revision": artifact.revision,
                "state": row["lifecycle_state"],
                "merged_into": row["merged_into_id"],
                "lineage": artifact.lineage,
                "content": artifact.content.content,
            }
        return result


def by_text(memories: dict[str, dict], text: str) -> tuple[str, dict]:
    return next((key, value) for key, value in memories.items() if value["text"] == text)


async def searchable(contexts, scope: str, query: str) -> list[str]:
    result = await PluginArtifactSearcher(contexts.database, FAMILY).search(
        scope, PluginArtifactSearchRequest(query=query, limit=20)
    )
    return [artifact.content.root["text"] for artifact in result.artifacts]


@pytest.fixture
def config(tmp_path):
    return BuiltinConfig(database=SQLiteConfig(url=f"sqlite+aiosqlite:///{tmp_path / 'memory.db'}"))


async def _scope(contexts) -> str:
    draft = ScopeDraft(title="Memory", summary="Memory", idempotency_key=str(uuid4()))
    return (await contexts.scopes.create(draft)).scope_id


def test_extraction_revision_and_noop_follow_model_decisions(tmp_path, config):
    directory = deploy(tmp_path / "plugins")

    async def scenario():
        async with open_builtin_contexts(config) as contexts:
            database = contexts.database
            scope = await _scope(contexts)
            first = await contexts.records.create_source(scope, "content", "create|region|deploys go to east")
            await contexts.records.create_source(scope, "content", "create|keys|rotate signing keys monthly")
            await contexts.records.create_source(scope, "content", "drop|chatter|thanks for the help")
            await invoke(database, directory, scope)
            memories = await heads(database, scope)
            assert sorted(item["text"] for item in memories.values()) == [
                "keys: rotate signing keys monthly",
                "region: deploys go to east",
            ]
            region_id, region = by_text(memories, "region: deploys go to east")
            assert region["revision"] == 1
            assert [ref.source_id for ref in region["lineage"].sources] == [first.source_id]
            assert region["content"]["schema"] == "powercontext.atomic-memory.v1"

            update = await contexts.records.create_source(scope, "content", "update|region|deploys go to north")
            await invoke(database, directory, scope)
            memories = await heads(database, scope)
            assert len(memories) == 2
            revised = memories[region_id]
            assert revised["text"] == "region: deploys go to north" and revised["revision"] == 2
            assert [ref.source_id for ref in revised["lineage"].sources] == [update.source_id]
            assert (region_id, 1) in {(ref.artifact_id, ref.revision) for ref in revised["lineage"].artifacts}
            assert await searchable(contexts, scope, "north") == ["region: deploys go to north"]
            assert await searchable(contexts, scope, "east") == []

    asyncio.run(scenario())


def test_merge_freezes_inputs_and_undo_restores_them(tmp_path, config):
    directory = deploy(tmp_path / "plugins")

    async def scenario():
        async with open_builtin_contexts(config) as contexts:
            database = contexts.database
            scope = await _scope(contexts)
            await contexts.records.create_source(scope, "content", "create|region|deploys go to east")
            await contexts.records.create_source(scope, "content", "create|zone|primary zone is east-1")
            await invoke(database, directory, scope)
            await contexts.records.create_source(scope, "content", "merge|region,zone|region east and zone east-1")
            await invoke(database, directory, scope)
            memories = await heads(database, scope)
            merged_id, merged = by_text(memories, "region zone: region east and zone east-1")
            region_id, region = by_text(memories, "region: deploys go to east")
            zone_id, zone = by_text(memories, "zone: primary zone is east-1")
            assert merged["state"] == "active" and merged["content"]["creation"]["type"] == "merge"
            assert region["merged_into"] == merged_id and zone["merged_into"] == merged_id
            assert set(await searchable(contexts, scope, "east")) == {"region zone: region east and zone east-1"}

            await invoke(database, directory, scope, requests=({"op": "undo_merge", "id": merged_id},))
            memories = await heads(database, scope)
            assert memories[merged_id]["state"] == "retired"
            assert memories[region_id]["merged_into"] is None and memories[region_id]["state"] == "active"
            assert memories[zone_id]["revision"] == 2
            assert set(await searchable(contexts, scope, "east")) == {
                "region: deploys go to east",
                "zone: primary zone is east-1",
            }

            # Restoring an input of an effective merge undoes that merge as one group.
            await contexts.records.create_source(scope, "content", "merge|region,zone|combined east placement")
            await invoke(database, directory, scope)
            again = await heads(database, scope)
            second_id, _ = by_text(again, "region zone: combined east placement")
            await invoke(database, directory, scope, requests=({"op": "restore", "id": region_id},))
            restored = await heads(database, scope)
            assert restored[second_id]["state"] == "retired"
            assert restored[region_id]["merged_into"] is None

    asyncio.run(scenario())


def test_user_commands_forget_restore_remember_and_edit(tmp_path, config):
    directory = deploy(tmp_path / "plugins")

    async def scenario():
        async with open_builtin_contexts(config) as contexts:
            database = contexts.database
            scope = await _scope(contexts)
            await invoke(
                database,
                directory,
                scope,
                requests=(
                    {"op": "remember", "id": "pref", "text": "prefers concise answers", "kind": "preference"},
                    {"op": "edit", "id": "pref", "text": "prefers concise answers with code"},
                ),
            )
            memories = await heads(database, scope)
            assert memories["pref"]["text"] == "prefers concise answers with code"
            assert memories["pref"]["revision"] == 2

            await invoke(database, directory, scope, requests=({"op": "forget", "id": "pref"},))
            assert (await heads(database, scope))["pref"]["state"] == "deprecated"
            assert await searchable(contexts, scope, "concise") == []

            await invoke(database, directory, scope, requests=({"op": "restore", "id": "pref", "revision": 1},))
            restored = (await heads(database, scope))["pref"]
            assert restored["state"] == "active" and restored["text"] == "prefers concise answers"
            assert restored["revision"] == 3
            assert await searchable(contexts, scope, "concise") == ["prefers concise answers"]

    asyncio.run(scenario())


def test_model_failure_keeps_the_window_and_owned_index_failure_rolls_back(tmp_path, config):
    directory = deploy(tmp_path / "plugins")

    async def scenario():
        async with open_builtin_contexts(config) as contexts:
            database = contexts.database
            scope = await _scope(contexts)
            await contexts.records.create_source(scope, "content", "timeout|region|model is slow")
            with pytest.raises(InferenceTimeoutError):
                await invoke(database, directory, scope)
            async with database.transaction() as connection:
                assert await SourceCursorRepository().load(connection, scope, BINDING) is None

        # A different scope and window: the plugin's own index cannot be written.
        async with open_builtin_contexts(config) as contexts:
            database = contexts.database
            scope = await _scope(contexts)
            await contexts.records.create_source(scope, "content", "create|region|deploys go to east")
            index = directory / "memory-index.sqlite3"
            with sqlite3.connect(index) as connection:
                connection.execute("DROP TABLE IF EXISTS memory_vectors")
                connection.execute("CREATE TABLE memory_vectors (scope_id TEXT)")
            with pytest.raises(sqlite3.OperationalError):
                await invoke(database, directory, scope)
            assert await heads(database, scope) == {}
            index.unlink()
            await invoke(database, directory, scope)
            assert [item["text"] for item in (await heads(database, scope)).values()] == ["region: deploys go to east"]

    asyncio.run(scenario())


def test_stale_owned_index_entries_are_ignored(tmp_path, config):
    directory = deploy(tmp_path / "plugins", index_threshold=0.1)

    async def scenario():
        async with open_builtin_contexts(config) as contexts:
            database = contexts.database
            scope = await _scope(contexts)
            await contexts.records.create_source(scope, "content", "create|region|deploys go to east")
            await invoke(database, directory, scope)
            ((region_id, _),) = (await heads(database, scope)).items()
            # Leftovers of an attempt that never committed and of an older revision.
            with sqlite3.connect(directory / "memory-index.sqlite3") as connection:
                connection.execute(
                    "INSERT INTO memory_vectors VALUES (?, ?, ?, ?)",
                    (scope, "ghost", 1, "[" + ",".join(["0.0625"] * 256) + "]"),
                )
                connection.execute("UPDATE memory_vectors SET revision = 9 WHERE artifact_id = ?", (region_id,))
            await contexts.records.create_source(scope, "content", "update|region|deploys go to north")
            await invoke(database, directory, scope)
            memories = await heads(database, scope)
            assert "ghost" not in memories
            # Core text search still found the current memory, so the update revised it.
            assert memories[region_id]["revision"] == 2

    asyncio.run(scenario())
