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

"""A deterministic stand-in for the Atomic Memory models, driven by Source text.

Each Source reads ``op|keys|fact``. Extraction yields one candidate ``"<keys>: <fact>"``;
reconciliation creates, revises the related memory with the same key, merges the
related memories named by ``keys``, or drops the candidate. ``timeout`` fails extraction.
"""

from __future__ import annotations

from powercontext.builtin.artifacts.atomic_memory.extraction import (
    AtomicMemoryCandidate,
    AtomicMemoryExtractionOutput,
    AtomicMemoryGenerationPipeline,
)
from powercontext.builtin.artifacts.atomic_memory.reconciliation import (
    AtomicMemoryReconciliationContent,
    AtomicMemoryReconciliationOutput,
)
from powercontext.builtin.inference import GenerationResult, InferenceTimeoutError, character_token_estimator

CALLS: list[str] = []


def _parse(evidence) -> tuple[str, str, str]:
    content = evidence.content
    text = content.get("content") if isinstance(content, dict) else content
    op, keys, fact = str(text).split("|", 2)
    return op.strip(), keys.strip(), fact.strip()


def _key(text: str) -> str:
    return text.split(":", 1)[0].strip()


class Extractor:
    async def generate(self, request):
        CALLS.append("extract")
        candidates = []
        for evidence in request.evidence:
            op, keys, fact = _parse(evidence)
            if op == "timeout":
                raise InferenceTimeoutError("generate", 60)
            candidates.append(
                AtomicMemoryCandidate(
                    kind="fact",
                    text=f"{keys.replace(',', ' ')}: {fact}",
                    description=f"{op} {keys}",
                    evidence_ids=(evidence.evidence_id,),
                )
            )
        return GenerationResult(output=AtomicMemoryExtractionOutput(candidates=tuple(candidates)))


class Reconciler:
    async def generate(self, request):
        CALLS.append("reconcile")
        proposal = request.proposal
        op, _, keys = (proposal.description or "create").partition(" ")
        related = {item.item_id: item for item in request.related}
        if op == "update":
            targets = tuple(
                identifier for identifier, item in related.items() if _key(item.text) == _key(proposal.text)
            )
            action = "revise" if len(targets) == 1 else "create"
            targets = targets if action == "revise" else ()
        elif op == "merge":
            wanted = set(keys.split(","))
            targets = tuple(identifier for identifier, item in related.items() if _key(item.text) in wanted)
            action = "merge" if targets else "create"
        elif op == "drop" and not proposal.original_refs:
            return GenerationResult(
                output=AtomicMemoryReconciliationOutput(
                    action="noop", compared_ids=tuple(related), reason="The candidate is not worth keeping."
                )
            )
        else:
            action, targets = "create", ()
        return GenerationResult(
            output=AtomicMemoryReconciliationOutput(
                action=action,
                compared_ids=tuple(related),
                target_ids=targets,
                content=AtomicMemoryReconciliationContent(
                    kind=proposal.kind, text=proposal.text, description=f"{action} fixture"
                ),
                evidence_ids=proposal.evidence_ids,
                reason=f"Fixture decision {action}.",
            )
        )


async def pipeline() -> AtomicMemoryGenerationPipeline:
    return AtomicMemoryGenerationPipeline(
        extractor=Extractor(), reconciler=Reconciler(), estimator=character_token_estimator()
    )
