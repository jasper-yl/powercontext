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
forget and restore, and cursor safety when the model fails. User changes use the
generic Artifact operations core runs without the plugin.
"""

import asyncio
import shutil
import sqlite3
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select

from powercontext.builtin.inference import InferenceTimeoutError
from powercontext.builtin.persistence.cursors import SourceCursorRepository
from powercontext.builtin.persistence.tables import ARTIFACT_HEADS_TABLE
from powercontext.builtin.plugins.search import PluginArtifactSearcher, PluginArtifactSearchRequest
from powercontext.builtin.runtime.composition import open_builtin_contexts
from powercontext.builtin.runtime.config import BuiltinConfig, SQLiteConfig
from powercontext.builtin.scope import ScopeDraft
from tests.plugin_support import current, edit, governance, invoke

EXAMPLE = Path(__file__).resolve().parents[2] / "examples/plugins/atomic_memory"
FAMILY = "atomic-memory-plugin"
BINDING = f"plugin:{FAMILY}"


def deploy(root: Path) -> Path:
    directory = root / FAMILY
    shutil.copytree(EXAMPLE, directory, ignore=shutil.ignore_patterns("__pycache__", "*.sqlite3"))
    return directory


def environment(**settings: object) -> dict[str, str]:
    values = {"model": "python:tests.atomic_memory_plugin_fake:pipeline", **settings}
    return {f"ATOMIC_MEMORY_PLUGIN_{name.upper()}": str(value) for name, value in values.items()}


@pytest.fixture
def settings(monkeypatch):
    def apply(**values: object) -> None:
        for name, value in environment(**values).items():
            monkeypatch.setenv(name, value)

    apply()
    return apply


async def heads(database, scope: str) -> dict[str, dict[str, Any]]:
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


def by_text(memories: dict[str, dict[str, Any]], text: str) -> tuple[str, dict[str, Any]]:
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


def test_extraction_revision_and_noop_follow_model_decisions(tmp_path, config, settings):
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


def test_merge_freezes_inputs_and_undo_restores_them(tmp_path, config, settings):
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

            await governance(database, directory, scope).restore(merged_id, operation="undo_merge")
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
            await governance(database, directory, scope).restore(region_id)
            restored = await heads(database, scope)
            assert restored[second_id]["state"] == "retired"
            assert restored[region_id]["merged_into"] is None

    asyncio.run(scenario())


def test_user_changes_edit_forget_and_restore_without_the_plugin(tmp_path, config, settings):
    directory = deploy(tmp_path / "plugins")

    async def scenario():
        async with open_builtin_contexts(config) as contexts:
            database = contexts.database
            scope = await _scope(contexts)
            # Remembering is writing a Source; the plugin extracts it like any other.
            await contexts.records.create_source(scope, "content", "create|pref|prefers concise answers")
            await invoke(database, directory, scope)
            ((pref_id, pref),) = (await heads(database, scope)).items()

            edited = {**pref["content"], "text": "pref: prefers concise answers with code"}
            await edit(database, directory, scope, pref_id, edited, expected_revision=1)
            memories = await heads(database, scope)
            assert memories[pref_id]["text"] == "pref: prefers concise answers with code"
            assert memories[pref_id]["revision"] == 2
            # Like built-in edits, the submitted content is kept as the revision's own lineage Source.
            (edit_source,) = memories[pref_id]["lineage"].sources
            assert edit_source.source_type == "content"
            assert await searchable(contexts, scope, "code") == ["pref: prefers concise answers with code"]

            changes = governance(database, directory, scope)
            current = await changes.get(pref_id)
            await changes.forget(
                pref_id,
                expected_revision=current.artifact.revision,
                expected_state_version=current.state.governance_generation,
            )
            assert (await heads(database, scope))[pref_id]["state"] == "deprecated"
            assert await searchable(contexts, scope, "concise") == []

            await changes.restore(pref_id)
            restored = (await heads(database, scope))[pref_id]
            # Shared restoration records the restore as a new revision with the same content.
            assert restored["state"] == "active" and restored["revision"] == 3
            assert restored["text"] == "pref: prefers concise answers with code"
            assert await searchable(contexts, scope, "concise") == ["pref: prefers concise answers with code"]

            # The next window recalls the edited memory: core's current revision is what the model sees.
            await contexts.records.create_source(scope, "content", "update|pref|prefers detailed answers")
            await invoke(database, directory, scope)
            revised = (await heads(database, scope))[pref_id]
            assert revised["text"] == "pref: prefers detailed answers" and revised["revision"] == 4
            assert (pref_id, 3) in {(ref.artifact_id, ref.revision) for ref in revised["lineage"].artifacts}

    asyncio.run(scenario())


def test_model_failure_keeps_the_window_and_owned_index_failure_rolls_back(tmp_path, config, settings):
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


def test_owned_index_entries_only_nominate_current_memories(tmp_path, config, settings):
    settings(index_threshold=0.1)
    directory = deploy(tmp_path / "plugins")

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
            # The stale entry still nominated the memory, and core's current revision was revised.
            assert memories[region_id]["revision"] == 2
            assert memories[region_id]["text"] == "region: deploys go to north"

    asyncio.run(scenario())
