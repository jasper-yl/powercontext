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

"""A device collection runner: pull targets from the central Server, collect, and report.

Design demo of the Source plugins RFC. The runner only connects out to the central
Server: each cycle it reports its plugins, fetches every target assigned to it, runs
``collect`` per plugin and Scope, submits Sources, then advances each checkpoint with
the old value as the expected one, so a failure anywhere repeats the same Sources,
which the Server deduplicates. Plugins run in this process in the demo; the RFC runs
them in Supervisor Workers.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import defaultdict
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

import httpx
import typer
from pydantic import JsonValue

from powercontext.plugins import CollectRequest, CollectTarget, PluginContext, PluginResponse, SourcePlugin
from powercontext.sources import ConnectorBinding, SourceDefinitionRegistry, project_source_for_transport

logger = logging.getLogger("powercontext.source_agent")
CONNECTOR_VERSION = "1"


class _Center:
    """``ctx.center``: the plugin's own routes on the central Server, called with the runner's credential."""

    def __init__(self, client: httpx.AsyncClient, plugin: str) -> None:
        self.client = client
        self.plugin = plugin

    async def call_plugin(
        self,
        method: str,
        path: str,
        /,
        *,
        json: JsonValue | None = None,
        content: bytes | None = None,
        content_type: str | None = None,
    ) -> PluginResponse:
        headers = {} if content_type is None else {"Content-Type": content_type}
        response = await self.client.request(
            method, f"/v1/plugins/{self.plugin}/api{path}", json=json, content=content, headers=headers
        )
        body = (
            response.json()
            if response.content and response.headers.get("content-type", "").startswith("application/json")
            else None
        )
        return PluginResponse(status=response.status_code, json_body=body)


class _Plugin:
    def __init__(self, name: str, directory: Path, declaration: SourcePlugin) -> None:
        self.name = name
        self.directory = directory
        self.declaration = declaration
        self.registry = SourceDefinitionRegistry(declaration.definitions)
        self.collector: Any = None

    def capability(self) -> dict[str, Any]:
        target = self.declaration.target
        assert target is not None
        return {
            "name": self.name,
            "description": target.description,
            "parameters_schema": target.schema_json(),
            "identity": {key: list(value) for key, value in target.identity.items()},
        }


def load_source_plugins(directory: Path) -> dict[str, _Plugin]:
    from powercontext.builtin.plugins.manifest import load_module

    plugins = {}
    for entry in sorted(path for path in directory.iterdir() if (path / "plugin.py").is_file()):
        declaration = getattr(load_module(entry), "plugin", None)
        if isinstance(declaration, SourcePlugin) and declaration.collect is not None:
            plugins[entry.name] = _Plugin(entry.name, entry.resolve(), declaration)
    return plugins


class SourceAgent:
    def __init__(self, server: str, token: str, plugins_dir: Path, *, name: str | None = None) -> None:
        self.server = server
        self.token = token
        self.plugins = load_source_plugins(plugins_dir)
        self.name = name
        self.resources = AsyncExitStack()

    async def __aenter__(self) -> SourceAgent:
        from powercontext.client.client import PowerContextClient

        headers = {"Authorization": f"Bearer {self.token}"}
        self.http = await self.resources.enter_async_context(
            httpx.AsyncClient(base_url=self.server, headers=headers, timeout=30)
        )
        self.client = await self.resources.enter_async_context(
            PowerContextClient(base_url=self.server, token=self.token)
        )
        if self.name is None:
            # The runner is named after its credential's subject unless told otherwise.
            self.name = (await self.client.get_access_principal()).principal.id
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.resources.aclose()

    async def register_definitions(self) -> None:
        """Register each plugin version's definition manifests; needs server.admin, done once per release."""

        from powercontext.http import RegisterSourceDefinitionRequest
        from powercontext.http import SourceDefinitionManifest as HttpManifest
        from powercontext.sources import manifest_for_definition

        for plugin in self.plugins.values():
            for definition in plugin.declaration.definitions:
                manifest = manifest_for_definition(definition).model_dump(mode="json", by_alias=True)
                await self.client.register_source_definition(
                    RegisterSourceDefinitionRequest(manifest=HttpManifest.model_validate(manifest))
                )

    async def cycle(self) -> list[dict[str, Any]]:
        response = await self.http.post(
            f"/v1/source-runners/{self.name}/sync",
            json={"capabilities": [plugin.capability() for plugin in self.plugins.values()]},
        )
        response.raise_for_status()
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        for target in response.json()["targets"]:
            groups[(target["capability"], target["scope_id"])].append(target)
        outcomes = []
        for (capability, scope_id), targets in groups.items():
            plugin = self.plugins.get(capability)
            if plugin is None:
                for target in targets:
                    await self._report(target, error=("capability_unavailable", "This runner has no such plugin"))
                continue
            outcomes.extend(await self._collect(plugin, scope_id, targets))
        return outcomes

    async def _collector(self, plugin: _Plugin) -> Any:
        if plugin.collector is None:
            context = PluginContext(
                name=plugin.name,
                scope_id=None,
                plugin_dir=plugin.directory,
                logger=logging.getLogger(f"powercontext.plugin.{plugin.name}"),
                resources=self.resources,
            )
            context.center = _Center(self.http, plugin.name)  # type: ignore[attr-defined]
            assert plugin.declaration.setup is not None
            plugin.collector = await plugin.declaration.setup(context)
        return plugin.collector

    def _binding(self, plugin: _Plugin, scope_id: str, target_id: str) -> ConnectorBinding:
        return ConnectorBinding(
            scope_id=scope_id, binding_id=target_id, connector_name=plugin.name, connector_version=CONNECTOR_VERSION
        )

    async def _collect(self, plugin: _Plugin, scope_id: str, targets: list[dict[str, Any]]) -> list[dict[str, Any]]:
        from powercontext.client.ingestion import _RemoteConnectorCheckpointStore, submit_materialized_observation

        checkpoints = _RemoteConnectorCheckpointStore(self.client)
        collect = plugin.declaration.collect
        assert collect is not None
        outcomes = []
        try:
            items = []
            for target in targets:
                checkpoint = await checkpoints.load(self._binding(plugin, scope_id, target["target_id"]))
                items.append(
                    CollectTarget(target_id=target["target_id"], parameters=target["parameters"], checkpoint=checkpoint)
                )
            request = CollectRequest(scope_id=scope_id, targets=tuple(items), max_items=collect.max_items)
            result = await (await self._collector(plugin)).collect(request)
        except Exception as error:  # A failed collect commits nothing; the same checkpoints are offered next time.
            for target in targets:
                await self._report(target, error=(type(error).__name__, str(error)[:1024]))
            logger.warning("collect failed", exc_info=True)
            return [{"target_id": target["target_id"], "error": type(error).__name__} for target in targets]
        for item in request.targets:
            outcome = result.targets.get(item.target_id)
            target = next(value for value in targets if value["target_id"] == item.target_id)
            if outcome is None:
                await self._report(target)
                continue
            binding = self._binding(plugin, scope_id, item.target_id)
            try:
                submitted = []
                for collected in outcome.items:
                    source = await plugin.registry.resolve(collected.value)
                    observation = project_source_for_transport(plugin.registry, source)
                    submitted.append(await submit_materialized_observation(self.client, binding, observation))
                # Sources first, then the checkpoint against its old value.
                await checkpoints.save(binding, outcome.checkpoint, expected=item.checkpoint)
            except Exception as error:
                await self._report(target, error=(type(error).__name__, str(error)[:1024]))
                logger.warning("submit failed", exc_info=True)
                outcomes.append({"target_id": item.target_id, "error": type(error).__name__})
                continue
            await self._report(target)
            outcomes.append({"target_id": item.target_id, "sources": [ref.source_id for ref in submitted]})
        return outcomes

    async def _report(self, target: dict[str, Any], *, error: tuple[str, str] | None = None) -> None:
        body: dict[str, Any] = {
            "scope_id": target["scope_id"],
            "target_id": target["target_id"],
            "succeeded": error is None,
        }
        if error is not None:
            body["error_code"], body["error_message"] = error
        try:
            (await self.http.post(f"/v1/source-runners/{self.name}/report", json=body)).raise_for_status()
        except httpx.HTTPError as failure:
            logger.warning("Status report failed (%s)", type(failure).__name__)


def source_agent(
    server: str = typer.Option(..., "--server", help="The central PowerContext Server URL"),
    plugins: Path = typer.Option(..., "--plugins", help="Directory of Source plugins"),
    name: str | None = typer.Option(None, "--name", help="Runner name; defaults to the credential's subject"),
    interval: float = typer.Option(30.0, "--interval", help="Seconds between syncs"),
    once: bool = typer.Option(False, "--once", help="Run one sync and collection cycle, then exit"),
    register_definitions: bool = typer.Option(
        False, "--register-definitions", help="Register the plugins' definition manifests (needs server.admin)"
    ),
) -> None:
    """Collect on this device with Source plugins and report to the central Server."""

    token = os.environ.get("POWERCONTEXT_SOURCE_AGENT_TOKEN")
    if not token:
        raise typer.BadParameter("set POWERCONTEXT_SOURCE_AGENT_TOKEN to the runner's credential")

    async def run() -> None:
        async with SourceAgent(server, token, plugins, name=name) as agent:
            if register_definitions:
                await agent.register_definitions()
            while True:
                try:
                    outcomes = await agent.cycle()
                    if once:
                        import json

                        typer.echo(json.dumps({"runner": agent.name, "outcomes": outcomes}))
                        return
                except httpx.HTTPError as error:
                    if once:
                        raise
                    logger.warning("Sync failed (%s); retrying", type(error).__name__)
                await asyncio.sleep(interval)

    asyncio.run(run())
