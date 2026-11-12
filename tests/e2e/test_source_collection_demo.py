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

"""Design demo of the Source plugins RFC: central targets, pulled by a device runner."""

import asyncio
import base64
import json
import shutil
from pathlib import Path

import pytest

from powercontext.client import ingestion
from powercontext.source_agent import SourceAgent
from tests.e2e.plugin_server import ADMIN_AUTHORIZATION_VALUE, create_scope, running_server

EXAMPLE = Path(__file__).parents[2] / "examples/source_collection/camera-events"
THUMBNAIL = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\0" * 64).decode()


def spool(directory: Path, camera: str, sequence: int, label: str) -> None:
    (directory / camera).mkdir(parents=True, exist_ok=True)
    event = {
        "camera": camera,
        "sequence": sequence,
        "label": label,
        "captured_at": f"2026-11-12T08:00:{sequence:02d}Z",
        "thumbnail_base64": THUMBNAIL,
    }
    (directory / camera / f"{sequence}.json").write_text(json.dumps(event), encoding="utf-8")


def test_device_runner_pulls_central_targets_and_reports_idempotently(tmp_path, monkeypatch):
    plugins = tmp_path / "device-plugins"
    shutil.copytree(EXAMPLE, plugins / "camera-events")
    device_spool = tmp_path / "spool"
    monkeypatch.setenv("CAMERA_SPOOL", str(device_spool))

    async def sources(client, scope):
        response = await client.get(f"/v1/scopes/{scope}/sources", params={"limit": 100})
        assert response.status_code == 200, response.text
        return [item["source_id"] for item in response.json()["items"] if item["source_type"] == "camera-event"]

    async def targets(client, scope):
        response = await client.get(f"/v1/scopes/{scope}/source-targets")
        assert response.status_code == 200, response.text
        return {item["target_id"]: item for item in response.json()["items"]}

    async def scenario():
        async with running_server(tmp_path) as client:
            scope = await create_scope(client, "robot-7-data")
            base = str(client.base_url)
            async with SourceAgent(base, ADMIN_AUTHORIZATION_VALUE, plugins, name="robot-7") as agent:
                # Deployment step: register the plugin version's definitions once.
                await agent.register_definitions()
                # The first sync reports the runner and its capabilities; nothing is assigned yet.
                assert await agent.cycle() == []

                runners = (await client.get("/v1/source-runners")).json()["items"]
                assert [runner["runner"] for runner in runners] == ["robot-7"]
                (capability,) = runners[0]["capabilities"]
                assert capability["name"] == "camera-events"
                assert capability["parameters_schema"]["required"] == ["camera"]

                # Targets are added centrally; validation and deduplication happen at once.
                add = f"/v1/scopes/{scope}/source-targets"
                created = await client.post(
                    add, json={"runner": "robot-7", "capability": "camera-events", "parameters": {"camera": "front"}}
                )
                assert created.status_code == 201, created.text
                again = await client.post(
                    add, json={"runner": "robot-7", "capability": "camera-events", "parameters": {"camera": "front"}}
                )
                assert again.status_code == 200 and again.json()["target_id"] == created.json()["target_id"]
                variant = await client.post(
                    add,
                    json={"runner": "robot-7", "capability": "camera-events", "parameters": {"camera": " Front "}},
                )
                assert variant.status_code == 409, variant.text
                invalid = await client.post(
                    add, json={"runner": "robot-7", "capability": "camera-events", "parameters": {}}
                )
                assert invalid.status_code == 422, invalid.text
                unknown = await client.post(
                    add, json={"runner": "robot-8", "capability": "camera-events", "parameters": {"camera": "x"}}
                )
                assert unknown.status_code == 422, unknown.text
                target_id = created.json()["target_id"]

                spool(device_spool, "front", 1, "person at door")
                spool(device_spool, "front", 2, "door closed")
                (outcome,) = await agent.cycle()
                assert outcome == {"target_id": target_id, "sources": ["front-1", "front-2"]}
                assert await sources(client, scope) == ["front-1", "front-2"]
                status = (await targets(client, scope))[target_id]["status"]
                assert status["consecutive_failures"] == 0 and status["last_success_at"] is not None

                # A disconnect after the Sources but before the checkpoint repeats the same Sources next time.
                spool(device_spool, "front", 3, "package left")
                original = ingestion._RemoteConnectorCheckpointStore.save
                calls = {"count": 0}

                async def lost(self, binding, checkpoint, /, *, expected):
                    calls["count"] += 1
                    raise ConnectionError("network unreachable")

                monkeypatch.setattr(ingestion._RemoteConnectorCheckpointStore, "save", lost)
                (failed,) = await agent.cycle()
                assert failed == {"target_id": target_id, "error": "ConnectionError"}
                status = (await targets(client, scope))[target_id]["status"]
                assert (status["consecutive_failures"], status["last_error_code"]) == (1, "ConnectionError")
                assert await sources(client, scope) == ["front-1", "front-2", "front-3"]
                monkeypatch.setattr(ingestion._RemoteConnectorCheckpointStore, "save", original)
                (recovered,) = await agent.cycle()
                assert recovered == {"target_id": target_id, "sources": ["front-3"]}
                assert await sources(client, scope) == ["front-1", "front-2", "front-3"]
                assert (await agent.cycle()) == [{"target_id": target_id, "sources": []}]

                # Disabling leaves the target out of sync; a new Scope is served without restarting anything.
                disabled = await client.patch(f"{add}/{target_id}", json={"enabled": False})
                assert disabled.status_code == 200 and disabled.json()["enabled"] is False
                assert await agent.cycle() == []
                other = await create_scope(client, "robot-7-lab")
                rear = await client.post(
                    f"/v1/scopes/{other}/source-targets",
                    json={"runner": "robot-7", "capability": "camera-events", "parameters": {"camera": "rear"}},
                )
                assert rear.status_code == 201, rear.text
                spool(device_spool, "rear", 1, "lab bench empty")
                (lab,) = await agent.cycle()
                assert lab == {"target_id": rear.json()["target_id"], "sources": ["rear-1"]}
                assert await sources(client, other) == ["rear-1"]
                assert await sources(client, scope) == ["front-1", "front-2", "front-3"]

                deleted = await client.delete(f"{add}/{target_id}")
                assert deleted.status_code == 204
                assert target_id not in await targets(client, scope)

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("parameters", "identity", "same"),
    [
        ({"camera": " Front "}, {"camera": ["strip", "lower"]}, True),
        ({"camera": "Front"}, {"camera": ["strip"]}, False),
    ],
)
def test_target_identity_normalizes_declared_parameters(parameters, identity, same):
    from powercontext.sources.collection import target_key

    assert (
        target_key("camera-events", parameters, identity) == target_key("camera-events", {"camera": "front"}, identity)
    ) is same
