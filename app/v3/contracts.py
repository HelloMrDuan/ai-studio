from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, model_validator


class Capability(str, Enum):
    text = "text"
    vision = "vision"
    structured_output = "structured_output"
    image_generation = "image_generation"
    video_generation = "video_generation"
    tts = "tts"
    music = "music"
    image_reference = "image_reference"
    identity_reference = "identity_reference"
    multi_reference = "multi_reference"
    first_frame = "first_frame"
    last_frame = "last_frame"
    first_last_frame = "first_last_frame"
    mask = "mask"
    controlnet = "controlnet"
    ip_adapter = "ip_adapter"


class ProviderTransport(str, Enum):
    remote_api = "remote_api"
    local_http = "local_http"
    local_openai_compatible = "local_openai_compatible"
    local_comfyui = "local_comfyui"
    local_worker = "local_worker"
    internal_service = "internal_service"


class EntityKind(str, Enum):
    character = "character"
    creature = "creature"
    prop = "prop"
    location = "location"
    environment = "environment"
    vehicle = "vehicle"
    important_item = "important_item"


class ResourceState(str, Enum):
    generating = "generating"
    generated = "generated"
    audit_failed = "audit_failed"
    candidate_ready = "candidate_ready"
    adopted = "adopted"
    superseded = "superseded"
    failed = "failed"


class ResolutionSource(str, Enum):
    user_override = "user_override"
    script_explicit = "script_explicit"
    context_inferred = "context_inferred"
    project_default = "project_default"
    generated_canonical = "generated_canonical"


class SkillSpec(BaseModel):
    skill_id: str = Field(min_length=1)
    version: str = Field(min_length=1)
    domain: str = Field(min_length=1)
    input_schema: str = Field(min_length=1)
    output_schema: str = Field(min_length=1)
    required_capabilities: set[Capability] = Field(default_factory=set)
    allowed_operations: set[str] = Field(default_factory=set)
    owned_fields: set[str] = Field(default_factory=set)
    dependencies: tuple[str, ...] = ()
    optional_dependencies: tuple[str, ...] = ()


class CreativePlan(BaseModel):
    requested_skills: tuple[str, ...]
    execution_order: tuple[str, ...]


class ProviderModelSpec(BaseModel):
    provider_id: str = Field(min_length=1)
    model_id: str = Field(min_length=1)
    transport: ProviderTransport
    capabilities: set[Capability]
    enabled: bool = True
    healthy: bool = True
    priority: int = 100
    base_url: str | None = None
    secret_ref: str | None = None
    max_references: int | None = Field(default=None, ge=1)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def identity(self) -> str:
        return f"{self.provider_id}:{self.model_id}"


class ResolvedField(BaseModel):
    value: str = Field(min_length=1)
    source: ResolutionSource
    confidence: float = Field(ge=0.0, le=1.0)
    locked: bool = True
    evidence_refs: tuple[str, ...] = ()


class VisualEntity(BaseModel):
    entity_id: str = Field(min_length=1)
    kind: EntityKind
    display_name: str = Field(min_length=1)
    identity: dict[str, Any] = Field(default_factory=dict)
    shot_state: dict[str, Any] = Field(default_factory=dict)
    canonical_reference_ids: list[str] = Field(default_factory=list)
    revision: int = 1


class ResourceVersion(BaseModel):
    resource_id: str = Field(min_length=1)
    version: int = Field(ge=1)
    state: ResourceState
    parent_version: int | None = Field(default=None, ge=1)
    generation_task_id: str | None = None
    provider_id: str | None = None
    model_id: str | None = None
    prompt_contract_version: str | None = None
    continuity_version: int | None = Field(default=None, ge=1)
    reference_ids: tuple[str, ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_parent(self) -> "ResourceVersion":
        if self.parent_version is not None and self.parent_version >= self.version:
            raise ValueError("parent_version must be older than version")
        return self
