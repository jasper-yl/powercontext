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

"""Collection target identity, computed the same way by the central Server and every runner."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence

from pydantic import JsonValue


def normalize(value: str, steps: Sequence[str]) -> str:
    for step in steps:
        if step == "strip":
            value = value.strip()
        elif step == "lower":
            value = value.lower()
        elif step == "collapse_spaces":
            value = " ".join(value.split())
        elif step.startswith("remove_prefix:"):
            value = value.removeprefix(step.split(":", 1)[1])
        elif step.startswith("remove_suffix:"):
            value = value.removesuffix(step.split(":", 1)[1])
        else:
            raise ValueError(f"unknown normalization step {step}")  # noqa: TRY003
    return value


def target_key(capability: str, parameters: Mapping[str, JsonValue], identity: Mapping[str, Sequence[str]]) -> str:
    """SHA-256 of the capability and each identity parameter's normalized value, ordered by name."""

    values = {}
    for name in sorted(identity):
        value = parameters.get(name)
        if not isinstance(value, str):
            raise ValueError(f"identity parameter {name} must be a string")  # noqa: TRY003
        values[name] = normalize(value, identity[name])
    encoded = json.dumps([capability, values], ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(encoded.encode()).hexdigest()
