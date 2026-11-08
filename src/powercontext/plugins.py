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

"""Interfaces that plugins implement and that PC Server calls when it needs them.

An Artifact plugin implements one method, ``generate(request, writer)``. Slow work
such as model calls runs outside the writer; Artifact writes run inside one short
``async with writer.transaction()`` block, which core commits together with the
consumption cursor and the invocation acknowledgment.
"""

from __future__ import annotations

import logging
from collections.abc import Collection, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from pathlib import Path
from typing import Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue

from powercontext.artifacts import ArtifactLineage, ArtifactRef
from powercontext.sources import SourceRef

ResourceT = TypeVar("ResourceT")
LifecycleState = Literal["active", "deprecated", "retired"]


class GenerationSource(BaseModel):
    """One Source of the invocation window with the manifest's input Projection."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: SourceRef
    position: int = Field(ge=1)
    projection: JsonValue


class GenerateRequest(BaseModel):
    """Inputs of one Scope invocation; the cursor itself is never exposed."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    scope_id: str
    reason: Literal["schedule", "sources", "manual"]
    sources: tuple[GenerationSource, ...] = ()
    # Every manual request accepted since the last committed invocation, in order.
    requests: tuple[JsonValue, ...] = ()


class ArtifactSnapshot(BaseModel):
    """One exact revision with its current governance state and direct lineage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: ArtifactRef
    content: dict[str, JsonValue]
    search_text: str | None = None
    lineage: ArtifactLineage
    lifecycle_state: LifecycleState
    merged_into_id: str | None = None
    replacement_artifact_id: str | None = None
    governance_generation: int = Field(ge=0)


class ArtifactPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    items: tuple[ArtifactSnapshot, ...]
    next_cursor: str | None = None


class UndoMergePreview(BaseModel):
    """The group an undo would restore and retire, computed from current state."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    restore: tuple[ArtifactRef, ...]
    retire: tuple[ArtifactRef, ...]
    undo_merge_results: tuple[str, ...]


class PluginContext:
    """Per-invocation context; resources registered here close when the call ends."""

    def __init__(
        self, *, name: str, scope_id: str, plugin_dir: Path, logger: logging.Logger, resources: AsyncExitStack
    ) -> None:
        self.name = name
        self.scope_id = scope_id
        self.plugin_dir = plugin_dir
        self.logger = logger
        self._resources = resources

    async def enter_async_context(self, manager: AbstractAsyncContextManager[ResourceT]) -> ResourceT:
        return await self._resources.enter_async_context(manager)


class ArtifactReads(Protocol):
    """Reads limited to the invocation Scope and the plugin's own Family."""

    async def get(self, artifact_id: str, /, *, revision: int | None = None) -> ArtifactSnapshot | None: ...

    async def revisions(self, artifact_id: str, /, *, cursor: str | None = None, limit: int = 50) -> ArtifactPage: ...

    async def list(
        self,
        *,
        states: Collection[LifecycleState] = ("active",),
        tags: Collection[str] = (),
        cursor: str | None = None,
        limit: int = 50,
    ) -> ArtifactPage: ...

    async def search(self, query: str, /, *, limit: int = 10) -> tuple[ArtifactSnapshot, ...]: ...

    async def by_source(self, ref: SourceRef, /) -> tuple[ArtifactSnapshot, ...]: ...

    async def merge_inputs(self, artifact_id: str, /) -> tuple[ArtifactRef, ...]: ...

    async def tags(self, artifact_id: str, /) -> tuple[str, ...]: ...

    async def resolve_handle(self, handle_key: str, /) -> ArtifactRef | None: ...


class ArtifactTransaction(ArtifactReads, Protocol):
    """Writes execute immediately in the open transaction and return exact revisions."""

    async def put(
        self,
        artifact_id: str,
        /,
        *,
        content: dict[str, JsonValue],
        search_text: str | None = None,
        source_refs: Sequence[SourceRef] = (),
        artifact_refs: Sequence[ArtifactRef] = (),
        expected_revision: int | None = None,
    ) -> ArtifactSnapshot: ...

    async def revert(self, artifact_id: str, /, *, to_revision: int) -> ArtifactSnapshot: ...

    async def set_lifecycle(
        self, artifact_id: str, state: LifecycleState, /, *, replacement_artifact_id: str | None = None
    ) -> ArtifactSnapshot: ...

    async def merge(
        self,
        inputs: Sequence[str],
        /,
        *,
        into: str,
        content: dict[str, JsonValue],
        search_text: str | None = None,
        source_refs: Sequence[SourceRef] = (),
        artifact_refs: Sequence[ArtifactRef] = (),
    ) -> ArtifactSnapshot: ...

    async def preview_undo_merge(self, artifact_id: str, /) -> UndoMergePreview: ...

    async def undo_merge(self, artifact_id: str, /) -> UndoMergePreview: ...

    async def bind_handle(self, handle_key: str, ref: ArtifactRef, /) -> ArtifactRef: ...

    async def set_tags(self, artifact_id: str, tags: Sequence[str], /) -> tuple[str, ...]: ...


class ArtifactWriter(ArtifactReads, Protocol):
    """Committed reads outside the transaction, plus the single write transaction."""

    @property
    def family(self) -> str: ...

    def stable_id(self, *parts: str) -> str: ...

    def transaction(self) -> AbstractAsyncContextManager[ArtifactTransaction]: ...


class ArtifactGenerator(Protocol):
    async def generate(self, request: GenerateRequest, writer: ArtifactWriter, /) -> None: ...


__all__ = [
    "ArtifactGenerator",
    "ArtifactPage",
    "ArtifactReads",
    "ArtifactSnapshot",
    "ArtifactTransaction",
    "ArtifactWriter",
    "GenerateRequest",
    "GenerationSource",
    "LifecycleState",
    "PluginContext",
    "UndoMergePreview",
]
