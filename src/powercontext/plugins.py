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

A plugin directory holds ``plugin.py``, which assigns an ``ArtifactPlugin`` declaration
to ``plugin``. The declaration tells core which Sources to deliver and when; its
``create`` factory returns an object with one method, ``generate(request, writer)``.
Slow work such as model calls runs outside the writer; Artifact writes run inside one
short ``async with writer.transaction()`` block, which core commits together with the
consumption cursor and the invocation acknowledgment.

Callers never call plugins. They write Sources and read or change Artifacts through
the existing Source and Artifact APIs.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Collection, Sequence
from contextlib import AbstractAsyncContextManager, AsyncExitStack
from datetime import timedelta
from pathlib import Path
from typing import Annotated, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, JsonValue, field_validator, model_validator

from powercontext.artifacts import ArtifactLineage, ArtifactRef
from powercontext.sources import SourceProjectionKey, SourceRef

ResourceT = TypeVar("ResourceT")
LifecycleState = Literal["active", "deprecated", "retired"]
BUILTIN_FAMILIES = frozenset({
    "memory",
    "atomic-memory",
    "experience",
    "skill",
    "handoff",
    "profile",
    "prompt",
    "topic-memory",
})


class Interval(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    seconds: float = Field(gt=0)

    @field_validator("seconds")
    @classmethod
    def representable(cls, value: float) -> float:
        if timedelta(seconds=value).total_seconds() <= 0:
            raise ValueError("seconds must be at least one microsecond")  # noqa: TRY003
        return value


class Cron(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    expression: str = Field(min_length=1)
    timezone: str = "Asia/Shanghai"


class Inputs(BaseModel):
    """The Sources delivered to ``generate`` and the Projection each one arrives in."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_types: tuple[Annotated[str, Field(min_length=1)], ...] = Field(min_length=1)
    projection: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")
    max_sources: int = Field(default=50, ge=1, le=1000)

    @property
    def projection_key(self) -> SourceProjectionKey:
        name, version = self.projection.rsplit("@", 1)
        return SourceProjectionKey(name=name, version=version)


class ArtifactPluginSpec(BaseModel):
    """The data part of a declaration: what core needs before it runs plugin code."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    family: str = Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")
    inputs: Inputs | None = None
    schedule: Interval | Cron | None = None
    on_sources: bool = False
    scopes: tuple[Annotated[str, Field(min_length=1, max_length=256)], ...] | None = Field(
        default=None, min_length=1, max_length=1000
    )
    # Top-level content fields whose text core indexes for search; None indexes every string field.
    search_fields: tuple[Annotated[str, Field(min_length=1)], ...] | None = None
    max_workers: int = Field(default=1, ge=1)
    timeout_seconds: float = Field(default=300, gt=0)
    transaction_timeout_seconds: float = Field(default=30, gt=0)

    @field_validator("family")
    @classmethod
    def not_builtin(cls, value: str) -> str:
        if value in BUILTIN_FAMILIES:
            raise ValueError("family must not reuse a built-in Artifact family")  # noqa: TRY003
        return value

    @field_validator("scopes")
    @classmethod
    def unique_scopes(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("Scope IDs must be unique")  # noqa: TRY003
        return None if value is None else tuple(sorted(value))

    @model_validator(mode="after")
    def callable_somehow(self) -> ArtifactPluginSpec:
        if self.schedule is None and not self.on_sources:
            raise ValueError("declare a schedule, on_sources, or both")  # noqa: TRY003
        if self.on_sources and self.inputs is None:
            raise ValueError("on_sources requires inputs")  # noqa: TRY003
        if isinstance(self.schedule, Cron) and (self.on_sources or self.inputs is None):
            # The Supervisor admits one interval or one cron per binding, and cron plugins
            # without inputs have no dirty work to admit.
            raise ValueError("cron schedules require inputs and cannot be combined with on_sources")  # noqa: TRY003
        if self.inputs is None and self.scopes is None:
            raise ValueError("plugins without inputs must list scopes explicitly")  # noqa: TRY003
        if self.transaction_timeout_seconds > self.timeout_seconds:
            raise ValueError("transaction_timeout_seconds must not exceed timeout_seconds")  # noqa: TRY003
        return self


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
    reason: Literal["schedule", "sources"]
    sources: tuple[GenerationSource, ...] = ()


class ArtifactSnapshot(BaseModel):
    """One exact revision with its current governance state and direct lineage."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    ref: ArtifactRef
    content: dict[str, JsonValue]
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


class ArtifactPlugin(ArtifactPluginSpec):
    """The declaration ``plugin.py`` assigns to ``plugin``; the plugin name is its directory name."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    # Called once per invocation inside the Worker; read deployment settings from the environment here.
    create: Callable[[PluginContext], Awaitable[ArtifactGenerator]] = Field(exclude=True)


__all__ = [
    "ArtifactGenerator",
    "ArtifactPage",
    "ArtifactPlugin",
    "ArtifactPluginSpec",
    "ArtifactReads",
    "ArtifactSnapshot",
    "ArtifactTransaction",
    "ArtifactWriter",
    "Cron",
    "GenerateRequest",
    "GenerationSource",
    "Inputs",
    "Interval",
    "LifecycleState",
    "PluginContext",
    "UndoMergePreview",
]
