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

"""Atomic Memory implemented as an Artifact plugin.

The domain decisions reuse the built-in extraction and reconciliation contracts:
extract candidates from the Source window, recall related memories, let the model
create, revise, merge or drop each candidate, then apply the decisions. Storage,
merge, lifecycle and restoration go through the plugin writer. User commands arrive
as manual requests: remember, edit, forget, restore and undo_merge.
"""

from __future__ import annotations

import importlib
from pathlib import Path
from typing import Any, Literal

from memory_index import MemoryIndex
from pydantic import BaseModel, Field

from powercontext.builtin.artifacts.atomic_memory.extraction import (
    AtomicMemoryEvidence,
    AtomicMemoryExtractionInput,
    AtomicMemoryGenerationPipeline,
)
from powercontext.builtin.artifacts.atomic_memory.models import (
    AtomicMemoryContent,
    AtomicMemoryCreation,
    AtomicMemoryRead,
    AtomicMemoryStateValue,
)
from powercontext.builtin.artifacts.atomic_memory.reconciliation import (
    AtomicMemoryArtifactEvidence,
    AtomicMemoryReconciliationOutput,
    AtomicMemoryWindowWorkset,
    AtomicMemoryWorkingItem,
)
from powercontext.plugins import (
    ArtifactSnapshot,
    ArtifactTransaction,
    ArtifactWriter,
    GenerateRequest,
    PluginContext,
)


class Config(BaseModel):
    # A Pydantic AI model name such as "openai:gpt-5-mini", or "python:module:attribute"
    # naming an async factory that returns an AtomicMemoryGenerationPipeline.
    model: str
    extraction_profile: Literal["coding", "general"] = "coding"
    model_timeout_seconds: float = Field(default=120, gt=0)
    related_limit: int = Field(default=20, ge=1, le=200)
    comparison_batch_size: int = Field(default=8, ge=1)
    index_path: str = "memory-index.sqlite3"
    index_threshold: float = Field(default=0.6, ge=0, le=1)


class MemoryConflict(RuntimeError):
    """A recalled memory changed before the decision committed; the window is retried."""


class AtomicMemoryPlugin:
    def __init__(self, ctx: PluginContext, config: Config, pipeline: AtomicMemoryGenerationPipeline, index) -> None:
        self.ctx = ctx
        self.config = config
        self.pipeline = pipeline
        self.index = index

    async def generate(self, request: GenerateRequest, writer: ArtifactWriter) -> None:
        workset = await self._decide(request, writer) if request.sources else None
        async with writer.transaction() as tx:
            if workset is not None:
                for item in workset.changes():
                    await self._apply(tx, writer, item)
            for command in request.requests:
                await self._command(tx, writer, command)

    # Model work outside the write transaction.

    async def _decide(self, request: GenerateRequest, writer: ArtifactWriter) -> AtomicMemoryWindowWorkset:
        evidence = tuple(
            AtomicMemoryEvidence(
                evidence_id=f"source:{source.position}",
                source_ref=source.ref,
                journal_position=source.position,
                content=source.projection,
                source_metadata=source.projection.get("metadata", {}) if isinstance(source.projection, dict) else {},
            )
            for source in request.sources
        )
        by_id = {item.evidence_id: item for item in evidence}
        candidates = await self.pipeline.extract(AtomicMemoryExtractionInput(evidence=evidence))
        candidates = sorted(
            candidates, key=lambda item: sorted(by_id[identifier].journal_position for identifier in item.evidence_ids)
        )
        workset = AtomicMemoryWindowWorkset()
        for ordinal, candidate in enumerate(candidates):
            selected = tuple(by_id[identifier] for identifier in dict.fromkeys(candidate.evidence_ids))
            key = workset.add(
                AtomicMemoryWorkingItem(
                    key=f"candidate:{ordinal}",
                    content=AtomicMemoryContent(
                        kind=candidate.kind, text=candidate.text, description=candidate.description
                    ),
                    evidence=selected,
                    sources=tuple(item.source_ref for item in selected),
                    changed=True,
                )
            )
            prior = [item.key for item in workset.items.values() if item.key != key and item.changed and item.retain]
            recalled = [workset.add(item) for item in await self._related(writer, candidate.text)]
            pending = list(dict.fromkeys((*prior, *recalled)))
            while pending:
                related = workset.related(key, pending[: self.config.comparison_batch_size])
                del pending[: self.config.comparison_batch_size]
                if not related:
                    continue
                result = await self.pipeline.reconciler.generate(workset.request(key, related))
                key = workset.apply(key, related, AtomicMemoryReconciliationOutput.model_validate(result.output))
        return workset

    async def _related(self, writer: ArtifactWriter, text: str) -> list[AtomicMemoryWorkingItem]:
        """Recall through core text search and the plugin's own index; core decides what is current."""

        found: dict[str, ArtifactSnapshot] = {
            item.ref.artifact_id: item for item in await writer.search(text, limit=self.config.related_limit)
        }
        for artifact_id, revision, _ in await self.index.nearest(
            self.ctx.scope_id, text, threshold=self.config.index_threshold, limit=self.config.related_limit
        ):
            if artifact_id in found:
                continue
            current = await writer.get(artifact_id)
            # Entries left by an uncommitted attempt or an older revision are ignored.
            if current is not None and current.ref.revision == revision:
                found[artifact_id] = current
        items = []
        for snapshot in found.values():
            if snapshot.lifecycle_state != "active" or snapshot.merged_into_id is not None:
                continue
            content = AtomicMemoryContent.model_validate(snapshot.content).without_creation()
            read = _read(snapshot)
            items.append(
                AtomicMemoryWorkingItem(
                    key=f"memory:{snapshot.ref.artifact_id}",
                    content=content,
                    origins=(read,),
                    evidence=(
                        AtomicMemoryArtifactEvidence(
                            evidence_id=f"artifact:{snapshot.ref.artifact_id}@{snapshot.ref.revision}",
                            artifact_ref=snapshot.ref,
                            content=content,
                        ),
                    ),
                    artifacts=(snapshot.ref,),
                )
            )
        return items

    # Writes inside the transaction.

    async def _apply(self, tx: ArtifactTransaction, writer: ArtifactWriter, item: AtomicMemoryWorkingItem) -> None:
        for origin in item.origins:
            current = await tx.get(origin.ref.artifact_id)
            if current is None or _read(current) != origin:
                raise MemoryConflict("a recalled memory changed while the window was being decided")  # noqa: TRY003
        origin_refs = {(read.ref.artifact_id, read.ref.revision) for read in item.origins}
        evidence_refs = [ref for ref in item.artifacts if (ref.artifact_id, ref.revision) not in origin_refs]
        if len(item.origins) >= 2:
            inputs = [read.ref.artifact_id for read in item.origins]
            # Input revisions keep a later merge of the same identities distinct from an undone one.
            into = writer.stable_id(
                "merged", *sorted(f"{read.ref.artifact_id}@{read.ref.revision}" for read in item.origins)
            )
            content = item.content.model_copy(
                update={"creation": AtomicMemoryCreation(input_artifact_ids=tuple(inputs))}
            )
            written = await tx.merge(
                inputs,
                into=into,
                content=_body(content),
                search_text=_search_text(content),
                source_refs=item.sources,
                artifact_refs=evidence_refs,
            )
            for artifact_id in inputs:
                await self.index.remove(self.ctx.scope_id, artifact_id)
        elif item.origins:
            origin = item.origins[0]
            written = await tx.put(
                origin.ref.artifact_id,
                content=_body(item.content),
                search_text=_search_text(item.content),
                source_refs=item.sources,
                artifact_refs=evidence_refs,
                expected_revision=origin.ref.revision,
            )
        else:
            identity = writer.stable_id(
                "memory", *(f"{ref.source_type}/{ref.source_id}" for ref in item.sources), item.content.text
            )
            written = await tx.put(
                identity,
                content=_body(item.content),
                search_text=_search_text(item.content),
                source_refs=item.sources,
                artifact_refs=evidence_refs,
            )
        # The plugin-owned index is written once the real revision is known.
        await self.index.upsert(self.ctx.scope_id, written.ref.artifact_id, written.ref.revision, item.content.text)

    async def _command(self, tx: ArtifactTransaction, writer: ArtifactWriter, command: Any) -> None:
        operation = command.get("op") if isinstance(command, dict) else None
        if operation == "remember":
            content = AtomicMemoryContent(
                kind=command.get("kind", "fact"), text=command["text"], description=command.get("description")
            )
            identity = command.get("id") or writer.stable_id("remembered", content.kind, content.text)
            written = await tx.put(identity, content=_body(content), search_text=_search_text(content))
            await self.index.upsert(self.ctx.scope_id, identity, written.ref.revision, content.text)
        elif operation == "edit":
            current = await tx.get(command["id"])
            if current is None:
                return
            previous = AtomicMemoryContent.model_validate(current.content)
            content = previous.model_copy(
                update={key: command[key] for key in ("text", "kind", "description") if key in command}
            )
            written = await tx.put(
                command["id"],
                content=_body(content),
                search_text=_search_text(content),
                expected_revision=command.get("expected_revision"),
            )
            await self.index.upsert(self.ctx.scope_id, command["id"], written.ref.revision, content.text)
        elif operation == "forget":
            await tx.set_lifecycle(command["id"], "deprecated")
            await self.index.remove(self.ctx.scope_id, command["id"])
        elif operation == "restore":
            await self._restore(tx, command["id"], command.get("revision"))
        elif operation == "undo_merge":
            outcome = await tx.undo_merge(command["id"])
            await self._reindex(tx, (ref.artifact_id for ref in outcome.restore))
            await self.index.remove(self.ctx.scope_id, command["id"])
        else:
            self.ctx.logger.warning("Ignoring an unknown Atomic Memory command")

    async def _restore(self, tx: ArtifactTransaction, artifact_id: str, revision: int | None) -> None:
        current = await tx.get(artifact_id)
        if current is None:
            return
        # One shared restoration both reactivates the memory and, when asked, restores old content.
        if revision is not None:
            await tx.revert(artifact_id, to_revision=revision)
        elif current.lifecycle_state != "active" or current.merged_into_id is not None:
            await tx.set_lifecycle(artifact_id, "active")
        affected = {artifact_id, *(ref.artifact_id for ref in await tx.merge_inputs(artifact_id))}
        await self._reindex(tx, affected)

    async def _reindex(self, tx: ArtifactTransaction, identities) -> None:
        for identity in identities:
            snapshot = await tx.get(identity)
            if snapshot is None or snapshot.lifecycle_state != "active" or snapshot.merged_into_id is not None:
                await self.index.remove(self.ctx.scope_id, identity)
                continue
            text = AtomicMemoryContent.model_validate(snapshot.content).text
            await self.index.upsert(self.ctx.scope_id, identity, snapshot.ref.revision, text)


def _read(snapshot: ArtifactSnapshot) -> AtomicMemoryRead:
    if snapshot.merged_into_id is not None:
        state = AtomicMemoryStateValue.MERGED
    else:
        state = {
            "active": AtomicMemoryStateValue.ACTIVE,
            "deprecated": AtomicMemoryStateValue.FORGOTTEN,
            "retired": AtomicMemoryStateValue.RETIRED,
        }[snapshot.lifecycle_state]
    return AtomicMemoryRead(
        ref=snapshot.ref.model_copy(update={"family": "atomic-memory"}),
        state=state,
        state_version=snapshot.governance_generation,
        merged_into_id=snapshot.merged_into_id,
    )


def _body(content: AtomicMemoryContent) -> dict[str, Any]:
    return content.model_dump(mode="json", by_alias=True)


def _search_text(content: AtomicMemoryContent) -> str:
    return "\n".join(part for part in (content.kind, content.description, content.text) if part)


async def _pipeline(config: Config, ctx: PluginContext) -> AtomicMemoryGenerationPipeline:
    if config.model.startswith("python:"):
        module_name, attribute = config.model.removeprefix("python:").split(":", 1)
        return await getattr(importlib.import_module(module_name), attribute)()
    from pydantic_ai.models import infer_model

    from powercontext.builtin.artifacts.atomic_memory.extraction import (
        AtomicMemoryExtractionOutput,
        atomic_memory_extraction_instructions,
    )
    from powercontext.builtin.artifacts.atomic_memory.reconciliation import (
        ATOMIC_MEMORY_RECONCILIATION_INSTRUCTIONS,
        AtomicMemoryReconciliationInput,
        validate_reconciliation_decision,
    )
    from powercontext.builtin.artifacts.memory.prompts import MemoryExtractionProfile
    from powercontext.builtin.inference import character_token_estimator
    from powercontext.builtin.inference.pydantic_ai import InferenceLimits, PydanticAIStructuredGenerator

    model = await ctx.enter_async_context(infer_model(config.model))
    limits = InferenceLimits(timeout_seconds=config.model_timeout_seconds)
    instructions = atomic_memory_extraction_instructions(MemoryExtractionProfile(config.extraction_profile))
    return AtomicMemoryGenerationPipeline(
        extractor=PydanticAIStructuredGenerator(
            model=model,
            instructions=instructions,
            input_type=AtomicMemoryExtractionInput,
            output_type=AtomicMemoryExtractionOutput,
            limits=limits,
            name="atomic_memory_extraction",
        ),
        reconciler=PydanticAIStructuredGenerator(
            model=model,
            instructions=ATOMIC_MEMORY_RECONCILIATION_INSTRUCTIONS,
            input_type=AtomicMemoryReconciliationInput,
            output_type=AtomicMemoryReconciliationOutput,
            limits=limits,
            name="atomic_memory_reconciliation",
            output_check=validate_reconciliation_decision,
        ),
        estimator=character_token_estimator(),
        extraction_instructions=instructions,
    )


async def create(ctx: PluginContext, config: Config) -> AtomicMemoryPlugin:
    path = Path(config.index_path)
    if not path.is_absolute():
        path = ctx.plugin_dir / path
    index = await ctx.enter_async_context(MemoryIndex(path))
    pipeline = await _pipeline(config, ctx)
    return AtomicMemoryPlugin(ctx, config, pipeline, index)
