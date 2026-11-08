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

"""Invoke a plugin through the real executor with a single-process Supervisor lease."""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import Any

import yaml

from powercontext.builtin.persistence.processing_intents import ArtifactProcessingIntentRepository
from powercontext.builtin.persistence.supervision import ArtifactProcessingLeaseRepository
from powercontext.builtin.plugins.executor import PluginWorkerSpec, execute_plugin
from powercontext.builtin.plugins.families import plugin_artifact_repository
from powercontext.builtin.plugins.manifest import read_manifest
from powercontext.builtin.plugins.requests import accept_request
from powercontext.builtin.runtime.processing_contracts import ArtifactProcessingWorkAssignment


def write_plugin(root: Path, name: str, code: str, **generate: Any) -> Path:
    directory = root / name
    directory.mkdir(parents=True)
    (directory / "plugin.py").write_text(textwrap.dedent(code), encoding="utf-8")
    manifest = {
        "name": name,
        "kind": "artifact",
        "entry": "./plugin.py:create",
        "generate": {
            "family": name,
            "inputs": {"source_types": ["content"], "projection": "powercontext.text-evidence@1"},
            "manual": True,
            **generate,
        },
    }
    (directory / "plugin.yml").write_text(yaml.safe_dump(manifest), encoding="utf-8")
    return directory


async def invoke(database, directory: Path, scope_id: str, *, requests: tuple[Any, ...] = ()):
    manifest = read_manifest(directory)
    async with database.transaction() as connection:
        lease = await ArtifactProcessingLeaseRepository().start_single_process_term(connection, "test-owner")
        for params in requests:
            await accept_request(connection, manifest.binding_name, scope_id, params)
        intent = await ArtifactProcessingIntentRepository().request(connection, scope_id, manifest.binding_name)
    assignment = ArtifactProcessingWorkAssignment(
        binding_name=manifest.binding_name,
        scope_id=scope_id,
        artifact_family=manifest.generate.family,
        claimed_request_generation=intent.requested_generation,
        fence=lease.fence("single-process"),
        worker_id="test-worker",
    )
    spec = PluginWorkerSpec(database=None, directory=str(directory), manifest=manifest.model_dump(mode="json"))
    return await execute_plugin(spec, assignment, database=database)


async def current(database, family: str, scope_id: str, artifact_id: str):
    async with database.transaction() as connection:
        return await plugin_artifact_repository(family).latest(connection, scope_id, family, artifact_id)
