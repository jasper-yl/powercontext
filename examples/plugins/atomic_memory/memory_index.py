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

"""A plugin-owned similarity index kept beside core's authoritative records.

Entries are keyed by Artifact ID and record the revision they describe. The index is
not part of core's transaction, so readers trust an entry only when core still has
that exact revision active.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import re
import sqlite3
from pathlib import Path

DIMENSIONS = 256
_WORD = re.compile(r"\w+", re.UNICODE)


def embed(text: str) -> list[float]:
    """Deterministic hashed bag of words; a real deployment would call an embedding model."""

    vector = [0.0] * DIMENSIONS
    for word in _WORD.findall(text.casefold()):
        digest = hashlib.sha256(word.encode("utf-8")).digest()
        vector[int.from_bytes(digest[:4], "big") % DIMENSIONS] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class MemoryIndex:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def __aenter__(self) -> MemoryIndex:
        await asyncio.to_thread(self._execute, _SCHEMA)
        return self

    async def __aexit__(self, *_: object) -> None:
        return None

    def _execute(self, statement: str, parameters: tuple = ()) -> list[tuple]:
        with sqlite3.connect(self.path, timeout=30) as connection:
            return connection.execute(statement, parameters).fetchall()

    async def upsert(self, scope_id: str, artifact_id: str, revision: int, text: str) -> None:
        await asyncio.to_thread(
            self._execute,
            "INSERT INTO memory_vectors (scope_id, artifact_id, revision, vector) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (scope_id, artifact_id) DO UPDATE SET revision = excluded.revision, vector = excluded.vector",
            (scope_id, artifact_id, revision, json.dumps(embed(text))),
        )

    async def remove(self, scope_id: str, artifact_id: str) -> None:
        await asyncio.to_thread(
            self._execute, "DELETE FROM memory_vectors WHERE scope_id = ? AND artifact_id = ?", (scope_id, artifact_id)
        )

    async def nearest(self, scope_id: str, text: str, *, threshold: float, limit: int) -> list[tuple[str, int, float]]:
        rows = await asyncio.to_thread(
            self._execute, "SELECT artifact_id, revision, vector FROM memory_vectors WHERE scope_id = ?", (scope_id,)
        )
        query = embed(text)
        scored = []
        for artifact_id, revision, payload in rows:
            score = sum(left * right for left, right in zip(query, json.loads(payload), strict=True))
            if score >= threshold:
                scored.append((artifact_id, int(revision), score))
        return sorted(scored, key=lambda item: (-item[2], item[0]))[:limit]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS memory_vectors (
    scope_id TEXT NOT NULL,
    artifact_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    vector TEXT NOT NULL,
    PRIMARY KEY (scope_id, artifact_id)
)
"""
