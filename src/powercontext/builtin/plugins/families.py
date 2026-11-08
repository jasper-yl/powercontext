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

"""Process-local registration and shared storage of plugin-owned Artifact families.

Plugin families keep no durable declaration: the Server and each Worker register
them from the plugin directory at startup. Content is opaque JSON stored with the
revision's search text so that restored revisions remain searchable.
"""

from __future__ import annotations

from functools import cache
from typing import Any

from pydantic import BaseModel, ConfigDict, JsonValue, RootModel
from sqlalchemy import text, update
from sqlalchemy.ext.asyncio import AsyncConnection

from powercontext.artifacts import Artifact
from powercontext.builtin.artifacts.search import analyze_text
from powercontext.builtin.persistence.artifacts import ArtifactRepository
from powercontext.builtin.persistence.revision_meta import attach_revision_meta, revision_meta
from powercontext.builtin.persistence.tables import ARTIFACT_HEADS_TABLE

_PLUGIN_FAMILIES: set[str] = set()
# binding name -> (Scope allowlist or None, consumed Source types)
_SOURCE_BINDINGS: dict[str, tuple[frozenset[str] | None, frozenset[str]]] = {}

_CREATE_SQLITE_FTS = """
CREATE VIRTUAL TABLE IF NOT EXISTS pc_artifact_fts USING fts5(
    scope_id UNINDEXED,
    family UNINDEXED,
    artifact_id UNINDEXED,
    revision UNINDEXED,
    searchable_text,
    tokenize='unicode61'
)
"""
_DELETE_SQLITE_FTS = text(
    "DELETE FROM pc_artifact_fts WHERE scope_id = :scope_id AND family = :family AND artifact_id = :artifact_id"
)
_INSERT_SQLITE_FTS = text(
    "INSERT INTO pc_artifact_fts (scope_id, family, artifact_id, revision, searchable_text) "
    "VALUES (:scope_id, :family, :artifact_id, :revision, :searchable_text)"
)


class PluginArtifactContent(BaseModel):
    """Stored body of one plugin revision; core never interprets ``content``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: dict[str, JsonValue]
    search_text: str | None = None


class PluginContentView(RootModel[dict[str, JsonValue]]):
    """The public shape of a plugin revision: the plugin's content only."""


def register_plugin_family(family: str) -> None:
    from powercontext.server.authz.profiles import register_custom_artifact_access_family

    _PLUGIN_FAMILIES.add(family)
    register_custom_artifact_access_family(family)


def register_source_binding(binding_name: str, scopes: frozenset[str] | None, source_types: frozenset[str]) -> None:
    """Let Source writes in this process mark the plugin's Scope invocation dirty."""

    _SOURCE_BINDINGS[binding_name] = (scopes, source_types)


def source_bindings(scope_id: str) -> tuple[str, ...]:
    return tuple(sorted(name for name, (scopes, _) in _SOURCE_BINDINGS.items() if scopes is None or scope_id in scopes))


def is_plugin_family(family: str) -> bool:
    return family in _PLUGIN_FAMILIES


def plugin_families() -> frozenset[str]:
    return frozenset(_PLUGIN_FAMILIES)


@cache
def plugin_artifact_type(family: str) -> type[Artifact[Any]]:
    return type("PluginArtifact", (Artifact[PluginArtifactContent],), {"family": family})


@cache
def plugin_view_type(family: str) -> type[Artifact[Any]]:
    return type("PluginArtifactView", (Artifact[PluginContentView],), {"family": family})


def plugin_artifact_repository(family: str, sources: Any = None) -> ArtifactRepository:
    return ArtifactRepository((plugin_artifact_type(family),), sources=sources)


def plugin_view(artifact: Artifact[Any]) -> Artifact[Any]:
    """Present a stored revision without the search text core keeps beside it."""

    view = plugin_view_type(artifact.family)(
        artifact_id=artifact.artifact_id,
        revision=artifact.revision,
        content=PluginContentView(artifact.content.content),
        lineage=artifact.lineage,
    )
    attach_revision_meta(view, revision_meta(artifact))
    return view


async def ensure_plugin_search(connection: AsyncConnection) -> None:
    if connection.dialect.name == "sqlite":
        await connection.exec_driver_sql(_CREATE_SQLITE_FTS)


async def publish_search_text(
    connection: AsyncConnection,
    scope_id: str,
    family: str,
    artifact_id: str,
    revision: int | None,
    search_text: str | None,
) -> None:
    """Replace the head projection; ``revision=None`` removes it from search."""

    searchable = None if revision is None or not search_text else analyze_text(search_text)
    await connection.execute(
        update(ARTIFACT_HEADS_TABLE)
        .where(
            ARTIFACT_HEADS_TABLE.c.scope_id == scope_id,
            ARTIFACT_HEADS_TABLE.c.family == family,
            ARTIFACT_HEADS_TABLE.c.artifact_id == artifact_id,
        )
        .values(searchable_text=searchable)
    )
    if connection.dialect.name != "sqlite":
        return
    identity = {"scope_id": scope_id, "family": family, "artifact_id": artifact_id}
    await connection.execute(_DELETE_SQLITE_FTS, identity)
    if searchable:
        await connection.execute(_INSERT_SQLITE_FTS, {**identity, "revision": revision, "searchable_text": searchable})
