from __future__ import annotations

import hashlib
import json
import mimetypes
import re
import shutil
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .adapters.comfyui import ComfyUIAdapter
from .generation_contract import GenerationContract


class ReferenceAssetError(RuntimeError):
    pass


@dataclass(frozen=True)
class ReferenceAsset:
    reference_id: str
    path: Path
    sha256: str
    mime_type: str
    entity_id: str = ""


class ReferenceAssetStore:
    """Persistent resolver from canonical reference IDs to private local files."""

    schema_version = "xiaoduan_reference_assets_v1"

    def __init__(self, data_dir: Path | str) -> None:
        self.root = Path(data_dir) / "v3" / "reference-assets"
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "index.json"

    def _load(self) -> dict[str, Any]:
        if not self.index_path.is_file():
            return {"schema_version": self.schema_version, "references": {}}
        data = json.loads(self.index_path.read_text(encoding="utf-8"))
        if data.get("schema_version") != self.schema_version:
            raise ReferenceAssetError("unsupported reference asset index")
        return data

    def _save(self, data: dict[str, Any]) -> None:
        tmp = self.index_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.index_path)

    @staticmethod
    def _safe_id(value: str) -> str:
        ref = str(value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9._:-]{3,160}", ref):
            raise ValueError("invalid reference_id")
        return ref

    def import_file(self, reference_id: str, source: Path | str, *, entity_id: str = "") -> ReferenceAsset:
        ref = self._safe_id(reference_id)
        src = Path(source)
        if not src.is_file() or src.stat().st_size <= 0:
            raise FileNotFoundError(f"reference file missing or empty: {src}")
        suffix = src.suffix.lower()
        if suffix not in {".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("reference asset must be png/jpg/jpeg/webp")
        digest = hashlib.sha256(src.read_bytes()).hexdigest()
        target = self.root / f"{hashlib.sha256(ref.encode()).hexdigest()[:24]}{suffix}"
        if src.resolve() != target.resolve():
            shutil.copy2(src, target)
        mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        data = self._load()
        data["references"][ref] = {
            "reference_id": ref,
            "path": str(target),
            "sha256": digest,
            "mime_type": mime,
            "entity_id": str(entity_id or "").strip(),
        }
        self._save(data)
        return self.resolve(ref)

    def resolve(self, reference_id: str) -> ReferenceAsset:
        ref = self._safe_id(reference_id)
        record = self._load()["references"].get(ref)
        if not isinstance(record, dict):
            raise ReferenceAssetError(f"unknown canonical reference: {ref}")
        path = Path(str(record.get("path") or ""))
        if not path.is_file() or path.stat().st_size <= 0:
            raise ReferenceAssetError(f"canonical reference file unavailable: {ref}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != record.get("sha256"):
            raise ReferenceAssetError(f"canonical reference checksum changed: {ref}")
        return ReferenceAsset(
            reference_id=ref,
            path=path,
            sha256=digest,
            mime_type=str(record.get("mime_type") or "application/octet-stream"),
            entity_id=str(record.get("entity_id") or ""),
        )

    def resolve_many(self, reference_ids: tuple[str, ...] | list[str]) -> tuple[ReferenceAsset, ...]:
        return tuple(self.resolve(item) for item in reference_ids)


@dataclass(frozen=True)
class ComfyReferenceBinding:
    reference_index: int
    node_id: str
    input_name: str = "image"


class ComfyWorkflowBindingError(RuntimeError):
    pass


def parse_reference_bindings(raw: Any) -> tuple[ComfyReferenceBinding, ...]:
    if not isinstance(raw, list):
        raise ComfyWorkflowBindingError("provider reference_bindings must be an array")
    result: list[ComfyReferenceBinding] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ComfyWorkflowBindingError("reference binding must be an object")
        result.append(
            ComfyReferenceBinding(
                reference_index=int(item["reference_index"]),
                node_id=str(item["node_id"]),
                input_name=str(item.get("input_name") or "image"),
            )
        )
    return tuple(result)


def bind_comfy_references(
    workflow: dict[str, Any],
    uploaded_names: tuple[str, ...] | list[str],
    bindings: tuple[ComfyReferenceBinding, ...] | list[ComfyReferenceBinding],
) -> dict[str, Any]:
    """Inject every uploaded canonical reference into explicit workflow slots."""
    names = tuple(str(item or "").strip() for item in uploaded_names)
    if any(not item for item in names):
        raise ComfyWorkflowBindingError("uploaded reference name cannot be empty")
    mapping = {item.reference_index: item for item in bindings}
    if set(mapping) != set(range(len(names))):
        raise ComfyWorkflowBindingError(
            f"reference bindings must cover exactly indexes 0..{len(names)-1}; got={sorted(mapping)}"
        )
    compiled = deepcopy(workflow)
    for index, name in enumerate(names):
        binding = mapping[index]
        node = compiled.get(binding.node_id)
        if not isinstance(node, dict) or not isinstance(node.get("inputs"), dict):
            raise ComfyWorkflowBindingError(f"reference binding node missing: {binding.node_id}")
        node["inputs"][binding.input_name] = name
    serialized = json.dumps(compiled, ensure_ascii=False)
    missing = [name for name in names if name not in serialized]
    if missing:
        raise ComfyWorkflowBindingError(f"compiled workflow dropped references: {missing}")
    return compiled


@dataclass(frozen=True)
class GenerationExecutionReceipt:
    prompt_id: str
    provider_id: str
    model_id: str
    provider_reference_ids: tuple[str, ...]
    uploaded_reference_names: tuple[str, ...]


class ReferenceFirstComfyExecutor:
    """Execute a GenerationContract through an explicit reference-capable Comfy workflow."""

    def __init__(self, *, adapter: ComfyUIAdapter, references: ReferenceAssetStore) -> None:
        self.adapter = adapter
        self.references = references

    async def execute(
        self,
        contract: GenerationContract,
        *,
        workflow: dict[str, Any],
        reference_bindings: tuple[ComfyReferenceBinding, ...] | list[ComfyReferenceBinding],
    ) -> GenerationExecutionReceipt:
        if contract.provider_id != self.adapter.spec.provider_id or contract.model_id != self.adapter.spec.model_id:
            raise RuntimeError("generation contract/provider adapter mismatch")
        resolved = self.references.resolve_many(contract.provider_reference_ids)
        uploaded: list[str] = []
        for asset in resolved:
            response = await self.adapter.upload_reference(
                filename=asset.path.name,
                content=asset.path.read_bytes(),
                overwrite=False,
                subfolder="xiaoduan-v3",
            )
            subfolder = str(response.get("subfolder") or "xiaoduan-v3").strip("/")
            name = str(response.get("name") or "").strip()
            uploaded.append(f"{subfolder}/{name}" if subfolder else name)
        compiled = bind_comfy_references(workflow, uploaded, reference_bindings)
        queued = await self.adapter.queue_workflow(compiled)
        return GenerationExecutionReceipt(
            prompt_id=str(queued["prompt_id"]),
            provider_id=contract.provider_id,
            model_id=contract.model_id,
            provider_reference_ids=tuple(contract.provider_reference_ids),
            uploaded_reference_names=tuple(uploaded),
        )

    async def execute_provider_profile(self, contract: GenerationContract) -> GenerationExecutionReceipt:
        """Load the provider's declared workflow/bindings; never guess reference nodes."""
        metadata = self.adapter.spec.metadata
        path = Path(str(metadata.get("reference_workflow_path") or ""))
        if not path.is_file():
            raise ComfyWorkflowBindingError(
                f"reference-capable provider has no usable reference_workflow_path: {path}"
            )
        workflow = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(workflow, dict) or not workflow:
            raise ComfyWorkflowBindingError("reference workflow must be a non-empty Comfy API workflow object")
        bindings = parse_reference_bindings(metadata.get("reference_bindings"))
        return await self.execute(contract, workflow=workflow, reference_bindings=bindings)
