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

"""Load plugins at startup and hand their calls to the Artifact processing Supervisor.

Nothing about a plugin is persisted: the directory as it is at startup decides which
plugins exist. Plugin code runs only in check and Worker child processes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
from collections.abc import Callable
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from typing import Any

from pydantic import JsonValue

from powercontext._logging import log_safely
from powercontext.builtin.persistence.database import AsyncDatabase
from powercontext.builtin.persistence.oceanbase import OceanBaseConfig
from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.plugins.executor import PluginWorkerSpec, run_plugin_worker
from powercontext.builtin.plugins.families import (
    ensure_plugin_search,
    register_plugin_family,
    register_source_binding,
)
from powercontext.builtin.plugins.manifest import CronSchedule, IntervalSchedule, PluginManifest
from powercontext.builtin.plugins.requests import accept_request
from powercontext.builtin.records import BaseOperationNotSupportedError, BaseValueNotFoundError
from powercontext.builtin.runtime.artifact_processing import (
    ArtifactProcessingBinding,
    ArtifactProcessingSupervisor,
    ArtifactProcessingSupervisors,
    SpawnArtifactProcessingWorkerLauncher,
)
from powercontext.builtin.runtime.config import DatabaseConfig
from powercontext.builtin.runtime.processing_discovery import SourceProcessingPendingProvider
from powercontext.http._generated.models import PluginPage, PluginState

logger = logging.getLogger(__name__)
CHECK_TIMEOUT_SECONDS = 60
# How soon a dirty Scope is admitted when the plugin asks to run on new Sources.
ON_SOURCES_INTERVAL_SECONDS = 2.0


@dataclass
class _Plugin:
    name: str
    directory: Path
    manifest: PluginManifest | None = None
    error: str | None = None
    last_outcome: str | None = None
    last_finished_at: datetime | None = None
    tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def state(self) -> PluginState:
        generate = None if self.manifest is None else self.manifest.generate
        triggers = []
        if generate is not None:
            triggers = [
                name
                for name, enabled in (
                    ("schedule", generate.schedule is not None),
                    ("sources", generate.on_sources),
                    ("manual", generate.manual),
                )
                if enabled
            ]
        return PluginState.model_validate({
            "name": self.name,
            "kind": "artifact",
            "status": "failed" if self.error is not None else "loaded",
            "error": self.error,
            "family": None if generate is None else generate.family,
            "binding_name": None if self.manifest is None else self.manifest.binding_name,
            "scopes": None if self.manifest is None or self.manifest.scopes is None else list(self.manifest.scopes),
            "triggers": triggers,
            "last_outcome": self.last_outcome,
            "last_finished_at": self.last_finished_at,
        })


class _ObservedLauncher:
    """Record the latest Worker outcome in memory for the read-only plugin listing."""

    def __init__(self, launcher: SpawnArtifactProcessingWorkerLauncher, plugin: _Plugin) -> None:
        self._launcher = launcher
        self._plugin = plugin

    async def start(self, assignment):
        handle = await self._launcher.start(assignment)
        return _ObservedHandle(handle, self._plugin)


class _ObservedHandle:
    def __init__(self, handle, plugin: _Plugin) -> None:
        self._handle = handle
        self._plugin = plugin

    async def wait(self):
        try:
            completion = await self._handle.wait()
        except BaseException:
            self._finish("failed")
            raise
        self._finish("succeeded")
        return completion

    async def terminate(self) -> None:
        await self._handle.terminate()

    def _finish(self, outcome: str) -> None:
        self._plugin.last_outcome = outcome
        self._plugin.last_finished_at = datetime.now(UTC)


async def check_plugin_directory(directory: Path) -> PluginManifest:
    """Import user code only in a time-limited child process."""

    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "powercontext.builtin.plugins.manifest",
        str(directory),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), CHECK_TIMEOUT_SECONDS)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise ValueError("plugin check timed out") from None  # noqa: TRY003
    lines = output.decode("utf-8", "replace").strip().splitlines()
    try:
        result = json.loads(lines[-1])
    except (IndexError, ValueError):
        raise ValueError("plugin check produced no result") from None  # noqa: TRY003
    if "error" in result:
        raise ValueError(result["error"])
    return PluginManifest.model_validate(result["manifest"])


class PluginManager:
    def __init__(
        self,
        database: AsyncDatabase,
        config: DatabaseConfig,
        supervisor: ArtifactProcessingSupervisors | None,
        security_builder: Callable[[], dict[str, Any] | None],
        on_supervisor: Callable[[ArtifactProcessingSupervisors], None],
        plugins_dir: Path | None,
        *,
        dispatch: bool = True,
    ) -> None:
        self.database = database
        # API-only instances register families for reads but never dispatch plugin Workers.
        self.dispatch = dispatch
        self.config = config
        self.supervisor = supervisor
        self.security_builder = security_builder
        self.on_supervisor = on_supervisor
        self.plugins_dir = None if plugins_dir is None else plugins_dir.expanduser().resolve()
        self.plugins: dict[str, _Plugin] = {}
        self._resources = AsyncExitStack()

    async def __aenter__(self) -> PluginManager:
        directory = self.plugins_dir
        if directory is None:
            return self
        try:
            entries = await asyncio.to_thread(lambda: sorted(path for path in directory.iterdir() if path.is_dir()))
        except OSError as error:
            log_safely(logger, logging.WARNING, f"Plugin directory unavailable ({type(error).__name__})")
            return self
        families: set[str] = set()
        for entry in entries:
            plugin = _Plugin(name=entry.name, directory=entry.resolve())
            self.plugins[entry.name] = plugin
            try:
                manifest = await check_plugin_directory(plugin.directory)
                if manifest.generate.family in families:
                    raise ValueError("family is already provided by another plugin")  # noqa: TRY003, TRY301
                plugin.manifest = manifest
                families.add(manifest.generate.family)
                await self._register(plugin)
            except Exception as error:
                plugin.error = str(error) if isinstance(error, ValueError) else type(error).__name__
                log_safely(logger, logging.WARNING, f"Plugin {entry.name!r} failed to load")
        return self

    async def __aexit__(self, *_: object) -> None:
        for plugin in self.plugins.values():
            for task in plugin.tasks:
                task.cancel()
            await asyncio.gather(*plugin.tasks, return_exceptions=True)
        await self._resources.aclose()

    async def _ensure_supervisor(self) -> ArtifactProcessingSupervisors:
        if self.supervisor is None:
            controller = await self._resources.enter_async_context(
                ArtifactProcessingSupervisor(
                    database=self.database,
                    bindings=(),
                    lease_mode="oceanbase" if isinstance(self.config, OceanBaseConfig) else "single-process",
                )
            )
            self.supervisor = ArtifactProcessingSupervisors((controller,))
            self.on_supervisor(self.supervisor)
        return self.supervisor

    async def _register(self, plugin: _Plugin) -> None:
        manifest = plugin.manifest
        if manifest is None:
            raise ValueError("plugin manifest is unavailable")  # noqa: TRY003
        generate = manifest.generate
        register_plugin_family(generate.family)
        async with self.database.transaction() as connection:
            await ensure_plugin_search(connection)
        scopes = None if manifest.scopes is None else frozenset(manifest.scopes)
        inputs = generate.inputs
        if inputs is not None:
            register_source_binding(manifest.binding_name, scopes, frozenset(inputs.source_types))
        interval = None
        cron = None
        if inputs is not None:
            if isinstance(generate.schedule, CronSchedule):
                cron = generate.schedule
            seconds = [
                *([generate.schedule.interval_seconds] if isinstance(generate.schedule, IntervalSchedule) else []),
                *([ON_SOURCES_INTERVAL_SECONDS] if generate.on_sources else []),
            ]
            interval = timedelta(seconds=min(seconds)) if seconds else None

        async def in_scope(connection, candidates):
            return frozenset(candidates if scopes is None else set(candidates) & scopes)

        if not self.dispatch:
            return
        spec = PluginWorkerSpec(
            database=self.config,
            directory=str(plugin.directory),
            manifest=manifest.model_dump(mode="json"),
            security=self.security_builder(),
        )
        supervisor = await self._ensure_supervisor()
        await supervisor.replace_binding(
            manifest.binding_name,
            ArtifactProcessingBinding(
                binding_name=manifest.binding_name,
                artifact_family=generate.family,
                launcher=_ObservedLauncher(
                    SpawnArtifactProcessingWorkerLauncher(partial(run_plugin_worker, spec)), plugin
                ),
                max_workers=generate.max_workers,
                worker_timeout_seconds=generate.timeout_seconds,
                automatic_processing_interval=interval,
                cron=None if cron is None else cron.cron,
                timezone="Asia/Shanghai" if cron is None else cron.timezone,
                pending_provider=None
                if inputs is None
                else SourceProcessingPendingProvider(self.database, manifest.binding_name, generate.family),
                automatic_scope_filter=in_scope,
                invocation_scope_filter=in_scope,
            ),
        )
        supervisor.wake(manifest.binding_name)
        if inputs is None and isinstance(generate.schedule, IntervalSchedule):
            # Without inputs there is no dirty Source work; request each listed Scope when due.
            plugin.tasks.append(
                asyncio.create_task(
                    self._tick(manifest, generate.schedule.interval_seconds), name=f"plugin-{plugin.name}"
                )
            )

    async def _tick(self, manifest: PluginManifest, seconds: float) -> None:
        intents = ArtifactProcessingIntentRepository()
        while True:
            await asyncio.sleep(seconds)
            try:
                async with self.database.transaction() as connection:
                    for scope_id in manifest.scopes or ():
                        await intents.request(connection, scope_id, manifest.binding_name)
                if self.supervisor is not None:
                    self.supervisor.wake(manifest.binding_name)
            except Exception as error:
                log_safely(logger, logging.WARNING, f"Plugin schedule request failed ({type(error).__name__})")

    def page(self) -> PluginPage:
        return PluginPage(items=[plugin.state() for _, plugin in sorted(self.plugins.items())])

    def get(self, name: str) -> PluginState:
        plugin = self.plugins.get(name)
        if plugin is None:
            raise BaseValueNotFoundError("plugin", name)
        return plugin.state()

    def plugin_for_family(self, family: str) -> PluginManifest | None:
        for plugin in self.plugins.values():
            if plugin.error is None and plugin.manifest is not None and plugin.manifest.generate.family == family:
                return plugin.manifest
        return None

    async def request(self, scope_id: str, family: str, params: JsonValue) -> int:
        manifest = self.plugin_for_family(family)
        if manifest is None:
            raise BaseValueNotFoundError("plugin_family", family)
        if not manifest.generate.manual:
            raise BaseOperationNotSupportedError("artifact_family", family, "manual generation")
        if manifest.scopes is not None and scope_id not in manifest.scopes:
            raise BaseOperationNotSupportedError("artifact_family", family, "generation outside the plugin's scopes")
        async with self.database.transaction() as connection:
            sequence = await accept_request(connection, manifest.binding_name, scope_id, params)
        if self.supervisor is not None:
            self.supervisor.wake(manifest.binding_name)
        return sequence
