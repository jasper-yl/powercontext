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

"""Public text search over plugin families; no plugin code runs on the read path."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from powercontext.artifacts import ArtifactRef
from powercontext.artifacts.search import ArtifactSearchExecutionContext, ArtifactSearchMatch, ArtifactSearchQuery
from powercontext.builtin.artifacts.search import lexical_search_score
from powercontext.builtin.persistence.experience_index import search_approved_artifact_rows
from powercontext.builtin.plugins.families import plugin_artifact_repository, plugin_view


class PluginArtifactSearchRequest(ArtifactSearchQuery):
    """Text queries only, like the shared Artifact search contract."""


@dataclass(frozen=True)
class PluginArtifactSearchOutcome:
    matches: tuple[ArtifactSearchMatch, ...]
    artifacts: tuple[Any, ...]


class PluginArtifactSearcher:
    request_type = PluginArtifactSearchRequest

    def __init__(self, database, family: str) -> None:
        self.database = database
        self.family = family

    async def search(
        self,
        scope_id: str,
        request: PluginArtifactSearchRequest,
        /,
        *,
        execution_context: ArtifactSearchExecutionContext | None = None,
    ) -> PluginArtifactSearchOutcome:
        repository = plugin_artifact_repository(self.family)
        matches, artifacts = [], []
        async with self.database.transaction() as connection:
            rows = await search_approved_artifact_rows(
                connection, scope_id, self.family, request.query, request.limit, admission=None
            )
            for row in rows:
                if row.get("is_rejected") or row["artifact_id"] is None:
                    continue
                ref = ArtifactRef(
                    family=self.family, artifact_id=str(row["artifact_id"]), revision=int(row["revision"])
                )
                retrieval, channel = lexical_search_score(row["raw_score"], row["score_metric"])
                matches.append(
                    ArtifactSearchMatch(artifact_ref=ref, retrieval_score=retrieval, channel_scores={"text": channel})
                )
                artifacts.append(plugin_view(await repository.get(connection, scope_id, ref)))
        return PluginArtifactSearchOutcome(matches=tuple(matches), artifacts=tuple(artifacts))
