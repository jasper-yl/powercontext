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

"""Dedicated plugin storage and the models a plugin may call."""

from __future__ import annotations

from collections.abc import Sequence
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Literal

from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine


class SQLitePluginStorage:
    """One SQLite file per plugin; plugins own its tables and migrations."""

    backend: Literal["sqlite"] = "sqlite"

    def __init__(self, path: Path) -> None:
        self.path = path
        self._engine: AsyncEngine | None = None

    def engine(self) -> AsyncEngine:
        if self._engine is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self._engine = create_async_engine(f"sqlite+aiosqlite:///{self.path}")
        return self._engine

    async def aclose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()


def plugin_storage(database: Any, root: str | None, name: str) -> SQLitePluginStorage | None:
    """Allocate the plugin's area on the deployment backend; other backends are not served yet."""

    from powercontext.builtin.persistence.sqlite.profile import SQLiteConfig

    if root is None or not isinstance(database, SQLiteConfig):
        return None
    return SQLitePluginStorage(Path(root) / f"{name}.sqlite3")


class ServerModels:
    """Server's configured embedding model; the plugin never sees its address or credentials."""

    def __init__(self, embedding: Any) -> None:
        self._embedding = embedding

    @property
    def embedding_dimension(self) -> int | None:
        return self._embedding.profile.dimension

    async def embed(self, texts: Sequence[str], /) -> tuple[tuple[float, ...], ...]:
        result = await self._embedding.embed(tuple(texts))
        return tuple(tuple(float(value) for value in vector) for vector in result.vectors)


async def plugin_models(inference: Any, resources: AsyncExitStack) -> ServerModels | None:
    if inference is None or inference.embedding_model is None:
        return None
    from powercontext.builtin.runtime.composition import _embedding_models

    embedding, _ = await _embedding_models(inference, resources, None)
    return None if embedding is None else ServerModels(embedding)


async def open_plugin_resources(
    database: Any, root: str | None, name: str, inference: Any, resources: AsyncExitStack
) -> tuple[SQLitePluginStorage | None, ServerModels | None]:
    storage = plugin_storage(database, root, name)
    if storage is not None:
        resources.push_async_callback(storage.aclose)
    return storage, await plugin_models(inference, resources)
