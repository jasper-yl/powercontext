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

"""Collect camera events a robot spools to disk; small thumbnails travel inside the Source.

The device writes ``<CAMERA_SPOOL>/<camera>/<sequence>.json`` files; the checkpoint is the
highest sequence already reported, so a retried cycle offers the same events again.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from pydantic import BaseModel, Field

from powercontext.plugins import (
    Collect,
    CollectedItem,
    CollectRequest,
    CollectResult,
    Interval,
    SourcePlugin,
    TargetSpec,
)
from powercontext.sources import Source, SourceMaterialization


class CameraTarget(BaseModel):
    camera: str = Field(min_length=1, description="Camera name on the device, such as front")
    min_interval_seconds: int = Field(default=5, ge=1)


class CameraEvent(BaseModel):
    camera: str
    sequence: int = Field(ge=1)
    label: str
    captured_at: str
    # Small images travel inline; larger ones would go to object storage or a central plugin route.
    thumbnail_base64: str | None = Field(default=None, max_length=64 * 1024)


class CameraEventSource(Source):
    event: CameraEvent


class CameraEventProjection:
    name = "camera-events.event"
    version = "1"
    source_class = CameraEventSource
    output_class = CameraEvent

    def project(self, source, /):
        return source.event


class CameraEventDefinition:
    name = "camera-event"
    version = "1"
    input_class = CameraEvent
    source_class = CameraEventSource
    projections = (CameraEventProjection(),)

    async def resolve(self, value: CameraEvent, /) -> CameraEventSource:
        return CameraEventSource(
            name=f"{value.camera}-{value.sequence}",
            definition_version=self.version,
            materialization=SourceMaterialization.CAPTURED,
            description=f"{value.camera}: {value.label}",
            event=value,
        )

    async def read(self, source, /):
        return source.event


class CameraCollector:
    def __init__(self, spool: Path) -> None:
        self.spool = spool

    async def collect(self, request: CollectRequest) -> CollectResult:
        result = CollectResult()
        for target in request.targets:
            camera = target.parameters["camera"].strip().lower()
            since = (target.checkpoint or {}).get("since", 0)
            files = sorted((self.spool / camera).glob("*.json"), key=lambda path: int(path.stem))
            events = [CameraEvent(**json.loads(path.read_text())) for path in files if int(path.stem) > since]
            events = events[: request.max_items]
            items = [CollectedItem(definition="camera-event", value=event) for event in events]
            result.add(target, items, checkpoint={"since": events[-1].sequence if events else since})
        return result


async def setup(ctx):
    return CameraCollector(Path(os.environ.get("CAMERA_SPOOL", ctx.plugin_dir / "spool")))


plugin = SourcePlugin(
    definitions=(CameraEventDefinition(),),
    collect=Collect(schedule=Interval(seconds=30)),
    target=TargetSpec(
        description="Collect the events a device camera records",
        parameters=CameraTarget,
        identity={"camera": ("strip", "lower")},
    ),
    setup=setup,
)
