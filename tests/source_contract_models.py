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

"""Captured model graphs shared by worker and neutral-upgrade acceptance tests."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field, field_serializer, model_serializer

from powercontext.sources import Source, SourceMaterialization
from powercontext.sources.definitions import AdapterSourceDefinition


class CapturedAudit(BaseModel):
    model_config = ConfigDict(title="AuditSource", extra="forbid")

    memory_evidence: str
    summary: str


class ChildSource(Source):
    model_config = ConfigDict(extra="forbid")

    content: str
    children: list[ChildSource] = Field(default_factory=list)


class CapturedBundle(BaseModel):
    model_config = ConfigDict(extra="forbid")

    child: ChildSource
    memory_evidence: str


class BundleSource(Source):
    model_config = ConfigDict(extra="forbid")

    child: ChildSource
    children: list[ChildSource]
    by_name: dict[str, ChildSource]
    optional: ChildSource | None
    choice: ChildSource | CapturedAudit
    bundle: CapturedBundle


class AuditSource(Source):
    model_config = ConfigDict(extra="forbid")

    audit: CapturedAudit


class MemoryEvidenceDeclaration(BaseModel):
    """Ordinary captured data sharing the library declaration's class name."""

    model_config = ConfigDict(extra="forbid")

    detail: str


class CollisionSource(Source):
    model_config = ConfigDict(extra="forbid")

    captured: MemoryEvidenceDeclaration


class SerializedChildSource(Source):
    model_config = ConfigDict(extra="forbid")

    content: str

    @model_serializer(mode="wrap")
    def serialize_capture(self, handler):
        value = handler(self)
        value["content"] = value["content"].strip()
        return value


class SerializedBundleSource(Source):
    child: SerializedChildSource


class AliasedChildSource(Source):
    model_config = ConfigDict(
        extra="forbid",
        serialize_by_alias=True,
        alias_generator=lambda name: "evidence" if name == "memory_evidence" else name,
    )

    content: str


class AliasedBundleSource(Source):
    child: AliasedChildSource


class AliasedCapture(BaseModel):
    model_config = ConfigDict(extra="forbid")

    text: str = Field(alias="body")


class DefaultAliasSource(Source):
    captured: AliasedCapture = AliasedCapture(body="Captured evidence.")

    @field_serializer("captured")
    def serialize_capture(self, value: AliasedCapture):
        return value.model_dump(mode="json", by_alias=True)


@dataclass(frozen=True)
class _CapturedAdapter:
    name: str
    input_class: type[Source]
    source_class: type[Source]

    async def resolve(self, value: Source, /) -> Source:
        return value

    async def read(self, source: Source, /) -> object:
        return source.model_dump(mode="json")


def source_contract_case(name: str) -> tuple[AdapterSourceDefinition[Source, Source, object], Source]:
    audit = CapturedAudit(memory_evidence="captured evidence", summary="An audit report.")
    child = ChildSource(name="child", materialization=SourceMaterialization.CAPTURED, content="Child evidence.")
    nested = child.model_copy(update={"children": [child]})
    if name == "nested":
        source = BundleSource(
            name="bundle",
            materialization=SourceMaterialization.CAPTURED,
            child=nested,
            children=[child],
            by_name={"child": child},
            optional=child,
            choice=audit,
            bundle=CapturedBundle(child=child, memory_evidence="captured bundle evidence"),
        )
    elif name == "title_collision":
        source = AuditSource(name="audit", materialization=SourceMaterialization.CAPTURED, audit=audit)
    elif name == "reference_collision":
        source = CollisionSource(
            name="collision",
            materialization=SourceMaterialization.CAPTURED,
            captured=MemoryEvidenceDeclaration(detail="Captured declaration-shaped data."),
        )
    elif name == "custom_serializer":
        source = SerializedBundleSource(
            name="serialized-bundle",
            materialization=SourceMaterialization.CAPTURED,
            child=SerializedChildSource(
                name="serialized-child", materialization=SourceMaterialization.CAPTURED, content="Child evidence."
            ),
        )
    elif name == "aliased_declaration":
        source = AliasedBundleSource(
            name="aliased-bundle",
            materialization=SourceMaterialization.CAPTURED,
            child=AliasedChildSource(
                name="aliased-child", materialization=SourceMaterialization.CAPTURED, content="Child evidence."
            ),
        )
    elif name == "aliased_default":
        source = DefaultAliasSource(name="aliased-default", materialization=SourceMaterialization.CAPTURED)
    else:
        raise ValueError(name)
    adapter = _CapturedAdapter(name=f"contract-{name}", input_class=type(source), source_class=type(source))
    return AdapterSourceDefinition(adapter), source
