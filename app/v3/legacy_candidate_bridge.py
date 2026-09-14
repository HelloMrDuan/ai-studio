from __future__ import annotations

import asyncio
import secrets
from pathlib import Path
from typing import Any

from temporalio.client import Client

from app.config import Settings
from app.models import TaskStatus
from app.services.media_generation_pipeline import MediaGenerationPipeline
from app.v3.generation_executor import ReferenceAssetStore
from app.v3.resource_store import ResourceStore, ResourceStoreError
from app.v3.workflow.contracts import ProductionStep, ProductionWorkflowInput
from app.v3.workflow.domain_executor import WorkflowPayloadStore
from app.v3.workflow.temporal import ProductionWorkflow


class LegacyCandidateV3Bridge:
    """Keep the original workbench candidate UI while replacing shot generation.

    The archived workbench still creates target assets, prompt assets and manual
    adoption records. For shot image/video tasks only, this bridge swaps the
    producer to the validated V3 Temporal materialization path. TaskStore and
    candidate rows are mirrored back so the original UI remains unchanged.
    """

    def __init__(self, settings: Settings, legacy: Any) -> None:
        self.settings = settings
        self.legacy = legacy
        self.payloads = WorkflowPayloadStore(settings.data_dir)
        self.references = ReferenceAssetStore(settings.data_dir)
        self.resources = ResourceStore(settings.data_dir)
        self.temporal_address = "127.0.0.1:7233"
        self.temporal_namespace = "default"
        self.task_queue = "xiaoduan-v3-production"
        self._tasks: set[asyncio.Task[Any]] = set()
        self.original_execute = legacy.director_workbench_execute_candidate
        self.original_publish = legacy._studio_publish_confirmed_shot_candidate

    @staticmethod
    def _status_value(value: Any) -> str:
        return str(getattr(value, "value", value) or "").strip().lower()

    def _url_for_path(self, path: Path) -> str:
        root = Path(self.settings.data_dir).resolve()
        resolved = path.resolve()
        if resolved != root and root not in resolved.parents:
            raise ValueError("V3 生成结果不在平台数据目录内")
        return "/files/" + resolved.relative_to(root).as_posix()

    def _shot_id(self, target: dict[str, Any]) -> str:
        metadata = target.get("metadata") if isinstance(target.get("metadata"), dict) else {}
        source = target.get("source") if isinstance(target.get("source"), dict) else {}
        return str(metadata.get("shot_id") or source.get("shot_id") or "").strip()

    @staticmethod
    def _logical_key(shot_id: str, kind: str) -> str:
        return f"studio-v3:shot:{shot_id}:{kind}"

    def _prompt_text(self, project_id: str, asset_id: str) -> str:
        if not asset_id:
            raise ValueError("当前镜头缺少已确认的提示词资产")
        text = self.legacy.director.production.read_text_asset(project_id, asset_id)
        text = str(text or "").strip()
        if not text:
            raise ValueError("当前镜头提示词为空")
        return text

    def _asset_path(self, project_id: str, asset_id: str) -> Path:
        url = self.legacy.director.production.asset_url(project_id, asset_id)
        if not url:
            raise ValueError("项目资产没有可用文件")
        path = self.legacy.assets.resolve_asset_url(url)
        if not path.is_file() or path.stat().st_size <= 0:
            raise FileNotFoundError(f"项目资产文件不存在：{asset_id}")
        return path

    def _candidate_reference_ids(self, project_id: str, target: dict[str, Any]) -> list[str]:
        target_entities = {str(item) for item in target.get("entity_ids") or [] if str(item)}
        preferred_roles = {
            "character_reference", "character_turnaround", "character_consistency",
            "scene_reference", "location_reference", "prop_reference", "item_reference",
        }
        rows = []
        for item in self.legacy.director.production.list_assets(project_id, active_only=True):
            if str(item.get("asset_type") or "").upper() != "IMAGE":
                continue
            if self._status_value(item.get("status")) != "ready":
                continue
            if self._status_value(item.get("dependency_state")) == "stale":
                continue
            role = str(item.get("asset_role") or "")
            if role not in preferred_roles:
                continue
            entities = {str(value) for value in item.get("entity_ids") or [] if str(value)}
            if target_entities and entities and not (target_entities & entities):
                continue
            rows.append(item)

        # FaceID runtime profile currently accepts one canonical reference. Keep
        # the selection deterministic and never silently drop a stronger match.
        rows.sort(
            key=lambda item: (
                0 if target_entities & {str(v) for v in item.get("entity_ids") or [] if str(v)} else 1,
                0 if str(item.get("asset_role") or "") in {"character_reference", "character_turnaround"} else 1,
                -int(item.get("version") or 0),
                str(item.get("asset_id") or ""),
            )
        )
        if not rows:
            raise ValueError(
                "当前镜头没有已采用的参考图。请先在②角色或③画面设计中生成并采用人物/场景参考图，再生成分镜画面。"
            )
        item = rows[0]
        asset_id = str(item.get("asset_id") or "")
        path = self._asset_path(project_id, asset_id)
        ref_id = f"legacy:{project_id}:{asset_id}"
        entity_id = next(iter(target_entities), "")
        self.references.import_file(ref_id, path, entity_id=entity_id)
        return [ref_id]

    def _mirror_adopted_first_frame(
        self,
        project_id: str,
        shot_id: str,
        first_frame_asset_id: str,
    ) -> str:
        logical_key = self._logical_key(shot_id, "image")
        adopted = self.resources.adopted(project_id, logical_key)
        if adopted is not None:
            return logical_key

        # The user may have adopted the keyframe before this V3 bridge existed.
        # Mirror that explicit adoption into V3 rather than regenerating it or
        # silently substituting another image.
        path = self._asset_path(project_id, first_frame_asset_id)
        ref_id = f"legacy-adopted:{project_id}:{first_frame_asset_id}"
        self.references.import_file(ref_id, path)
        artifact_ref = f"artifact://image/legacy_{first_frame_asset_id}"
        record = self.resources.create_candidate(
            project_id,
            logical_key=logical_key,
            generation_task_id=f"legacy-adoption:{first_frame_asset_id}",
            provider_id="legacy-workbench-import",
            model_id="user-adopted-image",
            reference_ids=[ref_id],
            metadata={
                "artifact_ref": artifact_ref,
                "artifact_path": str(path),
                "media_kind": "image",
                "legacy_asset_id": first_frame_asset_id,
                "mirrored_user_adoption": True,
            },
        )
        record = self.resources.set_audit_result(
            project_id,
            str(record["resource_id"]),
            passed=True,
            audit={"source": "original-workbench", "manual_adoption_mirror": True},
        )
        self.resources.adopt(project_id, str(record["resource_id"]))
        return logical_key

    def _append_candidate(
        self,
        *,
        project_id: str,
        target: dict[str, Any],
        payload: dict[str, Any],
        task: dict[str, Any],
        output_asset_type: str,
        dependencies: list[str],
        v3_workflow_id: str,
        v3_logical_key: str,
        v3_step_key: str,
    ) -> dict[str, Any]:
        rows = self.legacy._wb_load_candidates(project_id)
        candidate = {
            "candidate_id": "dcand_" + secrets.token_hex(10),
            "project_id": project_id,
            "target_asset_id": str(target.get("asset_id") or ""),
            "target_logical_key": str(target.get("logical_key") or ""),
            "target_contract_artifact_id": str(target.get("contract_artifact_id") or ""),
            "capability": str(payload.get("capability") or ""),
            "mode": str(payload.get("mode") or ""),
            "processor": str(payload.get("processor") or ""),
            "task_id": str(task.get("task_id") or ""),
            "status": self.legacy._wb_task_status(task) or "queued",
            "progress": int(task.get("progress") or 0),
            "message": str(task.get("message") or ""),
            "error": str(task.get("error") or ""),
            "output_files": [str(x) for x in task.get("output_files") or [] if str(x).strip()],
            "output_asset_type": output_asset_type,
            "dependency_asset_ids": list(dict.fromkeys(x for x in dependencies if x)),
            "prompt_asset_id": str(payload.get("prompt_asset_id") or ""),
            "params": payload.get("params") if isinstance(payload.get("params"), dict) else {},
            "confirmed_asset_id": "",
            "created_at": self.legacy._wb_now(),
            "updated_at": self.legacy._wb_now(),
            "producer": "v3_temporal",
            "v3_workflow_id": v3_workflow_id,
            "v3_logical_key": v3_logical_key,
            "v3_step_idempotency_key": v3_step_key,
            "v3_resource_id": "",
        }
        rows.append(candidate)
        self.legacy._wb_save_candidates(project_id, rows)
        return candidate

    def _update_candidate(self, project_id: str, candidate_id: str, **updates: Any) -> None:
        rows = self.legacy._wb_load_candidates(project_id)
        for row in rows:
            if str(row.get("candidate_id") or "") == candidate_id:
                row.update(updates)
                row["updated_at"] = self.legacy._wb_now()
                break
        self.legacy._wb_save_candidates(project_id, rows)

    async def _run_v3(
        self,
        *,
        project_id: str,
        candidate_id: str,
        task_id: str,
        request: ProductionWorkflowInput,
        logical_key: str,
        step_key: str,
    ) -> None:
        try:
            self.legacy.store.update(
                task_id,
                status=TaskStatus.running,
                progress=5,
                message="正在通过新版工作流生成候选",
            )
            client = await Client.connect(self.temporal_address, namespace=self.temporal_namespace)
            handle = await client.start_workflow(
                ProductionWorkflow.run,
                request,
                id=request.workflow_id,
                task_queue=self.task_queue,
            )
            result = await handle.result()
            if result.status != "completed":
                raise RuntimeError(result.message or result.error_code or "新版工作流生成失败")

            versions = self.resources.list_versions(project_id, logical_key)
            resource = next(
                (
                    item for item in reversed(versions)
                    if str(item.get("generation_task_id") or "") == step_key
                ),
                None,
            )
            if resource is None:
                raise RuntimeError("新版工作流完成但没有找到对应候选资源")
            metadata = resource.get("metadata") if isinstance(resource.get("metadata"), dict) else {}
            path = Path(str(metadata.get("artifact_path") or ""))
            if not path.is_file() or path.stat().st_size <= 0:
                raise FileNotFoundError("新版工作流候选文件不存在")
            output_url = self._url_for_path(path)
            self.legacy.store.update(
                task_id,
                status=TaskStatus.completed,
                progress=100,
                message="候选生成完成，等待你预览并采用",
                output_files=[output_url],
                error=None,
            )
            self._update_candidate(
                project_id,
                candidate_id,
                status="completed",
                progress=100,
                message="候选生成完成，等待你预览并采用",
                output_files=[output_url],
                v3_resource_id=str(resource.get("resource_id") or ""),
            )
        except Exception as exc:
            message = f"新版工作流生成失败：{type(exc).__name__}: {exc}"
            try:
                self.legacy.store.update(
                    task_id,
                    status=TaskStatus.failed,
                    progress=100,
                    message="候选生成失败",
                    error=message,
                )
            except Exception:
                pass
            self._update_candidate(
                project_id,
                candidate_id,
                status="failed",
                progress=100,
                message="候选生成失败",
                error=message,
            )

    async def execute_candidate(self, project_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        capability = str(payload.get("capability") or "").strip().lower()
        target_asset_id = str(payload.get("target_asset_id") or "").strip()
        if capability not in {"image", "video"} or not target_asset_id:
            return await self.original_execute(project_id, payload)

        target = self.legacy.director.production.get_asset(project_id, target_asset_id)
        if capability == "image":
            payload = MediaGenerationPipeline().prepare_candidate(self.legacy.director.production, project_id, payload)
        shot_id = self._shot_id(target)
        if not shot_id:
            # Non-shot image/video tools keep their mature original implementation.
            return await self.original_execute(project_id, payload)

        prompt_asset_id = str(payload.get("prompt_asset_id") or "").strip()
        prompt = self._prompt_text(project_id, prompt_asset_id)
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        dependencies = [prompt_asset_id]
        task_id = "v3task_" + secrets.token_hex(10)
        workflow_id = "studio-v3-" + secrets.token_hex(12)
        logical_key = self._logical_key(shot_id, capability)
        step_key = f"{workflow_id}:{capability}-generate"

        if capability == "image":
            reference_ids = self._candidate_reference_ids(project_id, target)
            operation = "generation.image.generate_candidate"
            step_payload = {
                "logical_key": logical_key,
                "provider_id": "local-comfyui-image",
                "model_id": "configured-image-workflow",
                "shot_id": shot_id,
                "source_text": prompt,
                "positive_prompt": params.get("positive_prompt", prompt),
                "negative_prompt": params.get("negative_prompt", ""),
                "visual_context": payload.get("visual_context", {}),
                "visual_direction": payload.get("visual_direction", {}),
                "generation_contract_id": payload.get("generation_contract_id", ""),
                "character_appearances": payload.get("character_appearances", []),
                "reference_ids": reference_ids,
                "entity_ids": [str(x) for x in target.get("entity_ids") or [] if str(x)],
                "camera_direction": str((target.get("metadata") or {}).get("camera_direction") or ""),
                "action": str((target.get("metadata") or {}).get("action") or ""),
                "duration_seconds": float((target.get("metadata") or {}).get("duration_seconds") or 3.0),
                "metadata": {
                    "source": "original_workbench_v3_bridge",
                    "legacy_target_asset_id": target_asset_id,
                    "shot_id": shot_id,
                },
            }
            output_asset_type = "IMAGE"
        else:
            first_frame_asset_id = str(payload.get("first_frame_asset_id") or "").strip()
            if not first_frame_asset_id:
                raise ValueError("视频生成必须先选择并采用视频首帧")
            dependencies.append(first_frame_asset_id)
            first_frame_logical_key = self._mirror_adopted_first_frame(
                project_id, shot_id, first_frame_asset_id
            )
            operation = "generation.h3.generate_candidate"
            step_payload = {
                "logical_key": logical_key,
                "provider_id": "local-h3-video",
                "model_id": "minimax-h3",
                "shot_id": shot_id,
                "prompt": prompt,
                "first_frame_logical_key": first_frame_logical_key,
                "entity_ids": [str(x) for x in target.get("entity_ids") or [] if str(x)],
                "width": int(params.get("width") or 768),
                "height": int(params.get("height") or 448),
                "length": int(params.get("length") or 124),
                "steps": int(params.get("steps") or 20),
                "seed": int(params.get("seed") or 0),
                "metadata": {
                    "source": "original_workbench_v3_bridge",
                    "legacy_target_asset_id": target_asset_id,
                    "legacy_first_frame_asset_id": first_frame_asset_id,
                    "shot_id": shot_id,
                },
            }
            output_asset_type = "VIDEO"

        payload_ref = self.payloads.put(
            project_id,
            f"{workflow_id}-{capability}",
            step_payload,
        )
        step = ProductionStep(
            step_id=f"{capability}-generate",
            skill_id="image_direction" if capability == "image" else "video_direction",
            operation=operation,
            payload_ref=payload_ref,
            idempotency_key=step_key,
        )
        request = ProductionWorkflowInput(
            workflow_id=workflow_id,
            project_id=project_id,
            steps=(step,),
        )
        task_record = self.legacy.store.create(
            task_id=task_id,
            module="新版工作流",
            operation="图片候选生成" if capability == "image" else "视频候选生成",
            title=str(target.get("name") or "镜头候选"),
            params={
                "v3_workflow_id": workflow_id,
                "v3_logical_key": logical_key,
                "legacy_target_asset_id": target_asset_id,
            },
            input_files=[],
        )
        task = task_record.model_dump(mode="json")
        candidate = self._append_candidate(
            project_id=project_id,
            target=target,
            payload=payload,
            task=task,
            output_asset_type=output_asset_type,
            dependencies=dependencies,
            v3_workflow_id=workflow_id,
            v3_logical_key=logical_key,
            v3_step_key=step_key,
        )
        background = asyncio.create_task(
            self._run_v3(
                project_id=project_id,
                candidate_id=str(candidate["candidate_id"]),
                task_id=task_id,
                request=request,
                logical_key=logical_key,
                step_key=step_key,
            )
        )
        self._tasks.add(background)
        background.add_done_callback(self._tasks.discard)
        return {
            "candidate": candidate,
            "task": task,
            "producer": "v3_temporal",
            "manual_adoption_required": True,
        }

    def publish_confirmed_shot_candidate(self, **kwargs: Any) -> dict[str, Any]:
        row = kwargs.get("row") if isinstance(kwargs.get("row"), dict) else {}
        result = self.original_publish(**kwargs)
        resource_id = str(row.get("v3_resource_id") or "").strip()
        project_id = str(kwargs.get("project_id") or "").strip()
        if not resource_id or not project_id:
            return result
        try:
            current = self.resources.set_audit_result(
                project_id,
                resource_id,
                passed=True,
                audit={
                    "source": "original_workbench_manual_adoption",
                    "candidate_id": str(row.get("candidate_id") or ""),
                    "manual_confirmation": True,
                },
            )
            adopted = self.resources.adopt(project_id, str(current["resource_id"]))
            result["v3_resource"] = adopted
            result["v3_manual_adoption_mirrored"] = True
        except ResourceStoreError as exc:
            # If the candidate was already mirrored by a repeated confirm call,
            # treat the V3 side as idempotent when it is already adopted.
            logical_key = str(row.get("v3_logical_key") or "")
            adopted = self.resources.adopted(project_id, logical_key) if logical_key else None
            if not adopted or str(adopted.get("resource_id") or "") != resource_id:
                raise RuntimeError(f"原页面已采用，但新版资源同步失败：{exc}") from exc
            result["v3_resource"] = adopted
            result["v3_manual_adoption_mirrored"] = True
        return result

    def install(self) -> None:
        self.legacy.director_workbench_execute_candidate = self.execute_candidate
        self.legacy._studio_publish_confirmed_shot_candidate = self.publish_confirmed_shot_candidate


__all__ = ["LegacyCandidateV3Bridge"]
