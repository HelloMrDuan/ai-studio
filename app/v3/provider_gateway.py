from __future__ import annotations

from dataclasses import dataclass

from .contracts import Capability, ProviderModelSpec


class ProviderResolutionError(RuntimeError):
    pass


@dataclass(frozen=True)
class ProviderSelection:
    spec: ProviderModelSpec
    required_capabilities: frozenset[Capability]


class ProviderRegistry:
    """Capability-driven provider selection.

    Local and remote providers are peers. Explicit provider/model selections
    never silently fall back to another provider.
    """

    def __init__(self, specs: list[ProviderModelSpec] | tuple[ProviderModelSpec, ...] = ()) -> None:
        self._specs: dict[tuple[str, str], ProviderModelSpec] = {}
        for spec in specs:
            self.register(spec)

    def register(self, spec: ProviderModelSpec) -> None:
        key = (spec.provider_id, spec.model_id)
        if key in self._specs:
            raise ValueError(f"duplicate provider model: {spec.identity}")
        self._specs[key] = spec

    def list(self) -> tuple[ProviderModelSpec, ...]:
        return tuple(self._specs.values())

    @staticmethod
    def _supports(spec: ProviderModelSpec, required: set[Capability]) -> bool:
        return required.issubset(spec.capabilities)

    def resolve(
        self,
        required_capabilities: set[Capability],
        *,
        provider_id: str | None = None,
        model_id: str | None = None,
    ) -> ProviderSelection:
        required = set(required_capabilities)
        if not required:
            raise ProviderResolutionError("at least one capability is required")
        if (provider_id is None) != (model_id is None):
            raise ProviderResolutionError("provider_id and model_id must be selected together")

        if provider_id is not None and model_id is not None:
            spec = self._specs.get((provider_id, model_id))
            if spec is None:
                raise ProviderResolutionError(f"selected provider model not registered: {provider_id}:{model_id}")
            if not spec.enabled:
                raise ProviderResolutionError(f"selected provider model disabled: {spec.identity}")
            if not spec.healthy:
                raise ProviderResolutionError(f"selected provider model unhealthy: {spec.identity}")
            missing = required - spec.capabilities
            if missing:
                names = ", ".join(sorted(item.value for item in missing))
                raise ProviderResolutionError(
                    f"selected provider model {spec.identity} lacks required capabilities: {names}"
                )
            return ProviderSelection(spec=spec, required_capabilities=frozenset(required))

        eligible = [
            spec
            for spec in self._specs.values()
            if spec.enabled and spec.healthy and self._supports(spec, required)
        ]
        if not eligible:
            names = ", ".join(sorted(item.value for item in required))
            raise ProviderResolutionError(f"no provider model satisfies capabilities: {names}")
        eligible.sort(key=lambda item: (item.priority, item.provider_id, item.model_id))
        return ProviderSelection(spec=eligible[0], required_capabilities=frozenset(required))

    def assert_reference_budget(self, selection: ProviderSelection, reference_count: int) -> None:
        if reference_count < 0:
            raise ValueError("reference_count must be non-negative")
        if reference_count <= 1:
            return
        if Capability.multi_reference not in selection.spec.capabilities:
            raise ProviderResolutionError(
                f"provider model {selection.spec.identity} does not support multi-reference generation"
            )
        limit = selection.spec.max_references
        if limit is not None and reference_count > limit:
            raise ProviderResolutionError(
                f"provider model {selection.spec.identity} supports at most {limit} references"
            )
