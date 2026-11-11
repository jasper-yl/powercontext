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

"""Text handling for the knowledge base plugin: decoding, chunking, embedding, and ranking."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Sequence
from itertools import pairwise

MEDIA_TYPES = {".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain"}
CHUNK_CHARACTERS = 800
CHUNK_OVERLAP = 120
HASH_DIMENSION = 256
_WORD = re.compile(r"[a-z0-9]+")
_CJK = re.compile(r"[㐀-鿿]")


def media_type_for(name: str) -> str | None:
    for suffix, media_type in MEDIA_TYPES.items():
        if name.lower().endswith(suffix):
            return media_type
    return None


def decode(content: bytes) -> str:
    text = content.decode("utf-8")
    return text.replace("\r\n", "\n").strip()


def chunk(text: str) -> list[str]:
    """Split on paragraphs, packing them into windows with a small overlap between windows."""

    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", text) if part.strip()]
    chunks: list[str] = []
    current = ""
    for paragraph in paragraphs:
        while len(paragraph) > CHUNK_CHARACTERS:
            if current:
                chunks.append(current)
                current = ""
            chunks.append(paragraph[:CHUNK_CHARACTERS])
            paragraph = paragraph[CHUNK_CHARACTERS - CHUNK_OVERLAP :]
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) > CHUNK_CHARACTERS:
            chunks.append(current)
            current = f"{current[-CHUNK_OVERLAP:]}\n\n{paragraph}"
        else:
            current = candidate
    if current:
        chunks.append(current)
    return chunks


def tokens(text: str) -> list[str]:
    """Lowercase words, plus character bigrams for Chinese, which has no spaces between words."""

    lowered = text.lower()
    found = _WORD.findall(lowered)
    characters = _CJK.findall(lowered)
    found.extend(characters)
    found.extend(a + b for a, b in pairwise(characters))
    return found


def hash_embedding(text: str) -> tuple[float, ...]:
    """A deterministic bag-of-tokens vector, used when the Server has no embedding model."""

    vector = [0.0] * HASH_DIMENSION
    for token in tokens(text):
        digest = hashlib.sha256(token.encode()).digest()
        index = int.from_bytes(digest[:4], "big") % HASH_DIMENSION
        vector[index] += 1.0 if digest[4] % 2 == 0 else -1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return tuple(value / norm for value in vector)


def cosine(a: Sequence[float], b: Sequence[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return 0.0 if norm == 0 else dot / norm


def bm25(query: str, documents: Sequence[str]) -> list[float]:
    """Okapi BM25 over the candidate chunks only; enough to rank a knowledge base's text."""

    query_terms = set(tokens(query))
    bags = [Counter(tokens(document)) for document in documents]
    if not bags or not query_terms:
        return [0.0] * len(documents)
    average = sum(sum(bag.values()) for bag in bags) / len(bags) or 1.0
    scores = []
    for bag in bags:
        length = sum(bag.values())
        score = 0.0
        for term in query_terms:
            frequency = bag.get(term, 0)
            if not frequency:
                continue
            containing = sum(1 for other in bags if term in other)
            idf = math.log(1 + (len(bags) - containing + 0.5) / (containing + 0.5))
            score += idf * frequency * 2.2 / (frequency + 1.2 * (0.25 + 0.75 * length / average))
        scores.append(score)
    return scores


def reciprocal_rank_fusion(rankings: Sequence[Sequence[int]], *, k: int = 60) -> dict[int, float]:
    fused: dict[int, float] = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking):
            fused[item] = fused.get(item, 0.0) + 1.0 / (k + rank + 1)
    return fused
