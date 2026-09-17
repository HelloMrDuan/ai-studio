"""Xiaoduan Studio V3 domain foundation.

The V3 runtime is intentionally isolated from the legacy Stage01-04 pipeline
while the new architecture is built and validated on the refactor branch.
"""

from .contracts import (
    Capability,
    CreativePlan,
    EntityKind,
    ProviderModelSpec,
    ProviderTransport,
    ResolutionSource,
    ResourceState,
    SkillSpec,
)

__all__ = [
    "Capability",
    "CreativePlan",
    "EntityKind",
    "ProviderModelSpec",
    "ProviderTransport",
    "ResolutionSource",
    "ResourceState",
    "SkillSpec",
]
