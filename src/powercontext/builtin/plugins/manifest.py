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

"""Plugin manifests and module loading shared by the Server check and Workers."""

from __future__ import annotations

import importlib.util
import inspect
import os
import re
import sys
from datetime import timedelta
from pathlib import Path
from types import ModuleType
from typing import Annotated, Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, field_validator, model_validator

from powercontext.sources import SourceProjectionKey

MAX_MANIFEST_BYTES = 1024 * 1024
BUILTIN_FAMILIES = frozenset({
    "memory",
    "atomic-memory",
    "experience",
    "skill",
    "handoff",
    "profile",
    "prompt",
    "topic-memory",
})
_NAME = r"^[a-z][a-z0-9-]{0,63}$"
_REFERENCE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


class PluginConfigError(ValueError):
    """A manifest, entry module, or configuration cannot be loaded."""


class IntervalSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    interval_seconds: float = Field(gt=0)

    @field_validator("interval_seconds")
    @classmethod
    def representable(cls, value: float) -> float:
        if timedelta(seconds=value).total_seconds() <= 0:
            raise ValueError("interval_seconds must be at least one microsecond")  # noqa: TRY003
        return value


class CronSchedule(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    cron: str = Field(min_length=1)
    timezone: str = "Asia/Shanghai"


class GenerateInputs(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_types: tuple[Annotated[str, Field(min_length=1)], ...] = Field(min_length=1)
    projection: str = Field(pattern=r"^[^@\s]+@[^@\s]+$")
    max_sources: int = Field(default=50, ge=1, le=1000)

    @property
    def projection_key(self) -> SourceProjectionKey:
        name, version = self.projection.rsplit("@", 1)
        return SourceProjectionKey(name=name, version=version)


class GenerateConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)

    family: str = Field(pattern=r"^[a-z][a-z0-9-]{0,127}$")
    inputs: GenerateInputs | None = None
    schedule: IntervalSchedule | CronSchedule | None = None
    on_sources: bool = False
    manual: bool = False
    max_workers: int = Field(default=1, ge=1)
    timeout_seconds: float = Field(default=300, gt=0)
    transaction_timeout_seconds: float = Field(default=30, gt=0)

    @field_validator("family")
    @classmethod
    def not_builtin(cls, value: str) -> str:
        if value in BUILTIN_FAMILIES:
            raise ValueError("family must not reuse a built-in Artifact family")  # noqa: TRY003
        return value

    @model_validator(mode="after")
    def callable_somehow(self) -> GenerateConfig:
        if self.schedule is None and not self.on_sources and not self.manual:
            raise ValueError("configure at least one of schedule, on_sources or manual")  # noqa: TRY003
        if self.on_sources and self.inputs is None:
            raise ValueError("on_sources requires inputs")  # noqa: TRY003
        if isinstance(self.schedule, CronSchedule) and (self.on_sources or self.inputs is None):
            # The Supervisor admits one interval or one cron per binding, and cron plugins
            # without inputs have no dirty work to admit.
            raise ValueError("cron schedules require inputs and cannot be combined with on_sources")  # noqa: TRY003
        if self.transaction_timeout_seconds > self.timeout_seconds:
            raise ValueError("transaction_timeout_seconds must not exceed timeout_seconds")  # noqa: TRY003
        return self


class PluginManifest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=_NAME)
    kind: Literal["artifact"]
    entry: str = Field(min_length=3, max_length=4096, pattern=r"^[^:]+\.py:[A-Za-z_][A-Za-z0-9_]*$")
    scopes: tuple[Annotated[str, Field(min_length=1, max_length=256)], ...] | None = Field(
        default=None, min_length=1, max_length=1000
    )
    generate: GenerateConfig
    config: dict[str, JsonValue] = Field(default_factory=dict)

    @field_validator("scopes")
    @classmethod
    def unique_scopes(cls, value: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if value is not None and len(set(value)) != len(value):
            raise ValueError("Scope IDs must be unique")  # noqa: TRY003
        return None if value is None else tuple(sorted(value))

    @model_validator(mode="after")
    def scoped_without_inputs(self) -> PluginManifest:
        if self.generate.inputs is None and self.scopes is None:
            raise ValueError("plugins without inputs must list scopes explicitly")  # noqa: TRY003
        return self

    @property
    def binding_name(self) -> str:
        return f"plugin:{self.name}"


def read_manifest(directory: Path) -> PluginManifest:
    path = directory / "plugin.yml"
    try:
        if path.stat().st_size > MAX_MANIFEST_BYTES:
            raise PluginConfigError("plugin.yml is too large")  # noqa: TRY003
        manifest = PluginManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValueError) as error:
        if isinstance(error, PluginConfigError):
            raise
        if isinstance(error, ValidationError):
            fields = ", ".join(".".join(str(part) for part in item["loc"]) or "manifest" for item in error.errors())
            raise PluginConfigError(f"invalid plugin.yml fields: {fields}") from error  # noqa: TRY003
        raise PluginConfigError(f"invalid plugin.yml: {type(error).__name__}") from error  # noqa: TRY003
    if manifest.name != directory.name:
        raise PluginConfigError("plugin name must match its directory name")  # noqa: TRY003
    return manifest


def resolve_environment(value: JsonValue, environ: dict[str, str] | None = None) -> JsonValue:
    """Replace ``${NAME}`` references; a missing variable is a configuration error."""

    environ = os.environ if environ is None else environ
    if isinstance(value, str):

        def substitute(match: re.Match[str]) -> str:
            name = match.group(1)
            if name not in environ:
                raise PluginConfigError(f"environment variable {name} is not set")  # noqa: TRY003
            return environ[name]

        return _REFERENCE.sub(substitute, value)
    if isinstance(value, list):
        return [resolve_environment(item, environ) for item in value]
    if isinstance(value, dict):
        return {key: resolve_environment(item, environ) for key, item in value.items()}
    return value


def load_module(directory: Path, manifest: PluginManifest) -> ModuleType:
    root = directory.resolve()
    entry, _ = manifest.entry.rsplit(":", 1)
    path = (root / entry).resolve()
    if Path(entry).is_absolute() or not path.is_relative_to(root) or not path.is_file():
        raise PluginConfigError("entry must name a Python file inside the plugin directory")  # noqa: TRY003
    module_name = f"powercontext_plugin_{manifest.name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise PluginConfigError("plugin entry is unavailable")  # noqa: TRY003
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    # Plugins may import sibling modules from their own directory.
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))
    spec.loader.exec_module(module)
    return module


def plugin_factory(module: ModuleType, manifest: PluginManifest) -> Any:
    factory = getattr(module, manifest.entry.rsplit(":", 1)[1], None)
    if factory is None or not inspect.iscoroutinefunction(factory):
        raise PluginConfigError("entry must name an async factory function")  # noqa: TRY003
    return factory


def plugin_config(module: ModuleType, manifest: PluginManifest, *, resolve: bool = True) -> Any:
    raw = resolve_environment(manifest.config) if resolve else manifest.config
    config_type = getattr(module, "Config", None)
    if config_type is None:
        if raw:
            raise PluginConfigError("config requires the entry module to export Config")  # noqa: TRY003
        return None
    if not isinstance(config_type, type) or not issubclass(config_type, BaseModel):
        raise PluginConfigError("Config must be a Pydantic model")  # noqa: TRY003
    try:
        return config_type.model_validate(raw)
    except ValueError as error:
        raise PluginConfigError("config does not match the plugin Config model") from error  # noqa: TRY003


def check_plugin(directory: Path) -> PluginManifest:
    """Import the entry and validate exports and configuration without calling the factory."""

    manifest = read_manifest(directory)
    module = load_module(directory, manifest)
    plugin_factory(module, manifest)
    plugin_config(module, manifest)
    return manifest


def main() -> None:
    """Check one plugin directory in a child process and report the outcome as JSON."""

    import json

    try:
        manifest = check_plugin(Path(sys.argv[1]))
    except Exception as error:
        message = (
            str(error)
            if isinstance(error, PluginConfigError)
            else f"import or validation failed: {type(error).__name__}"
        )
        print(json.dumps({"error": message}))
        return
    print(json.dumps({"manifest": manifest.model_dump(mode="json")}))


if __name__ == "__main__":
    main()
