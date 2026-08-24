from __future__ import annotations

import asyncio
import difflib
import hashlib
import json
import re
import secrets
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, Field

from app.config import Settings
from app.services.gemma import GemmaService
from app.services.skill_runtime import (
    apply_asset_completion,
    compact_contract,
    empty_runtime_state,
    normalize_contract,
    source_sha256 as skill_contract_source_sha256,
    update_runtime_state,
)
from app.services.production_assets import ProductionAssetService


STAGE_ORDER = ("01", "02", "03", "04")
STAGE_SKILLS = {
    "01": "chuanzhang-chuangzuo-v1",
    "02": 'ai-studio-character-design',
    "03": 'ai-studio-visual-design',
    "04": "chuanzhang-fenjing-biaoqing",
}
WORKFLOW_SKILL = "chuanzhang-ai-shijie-workflow"
ALLOWED_SOURCE_SUFFIXES = {".md", ".txt", ".json", ".yaml", ".yml"}


class DirectorProjectCreate(BaseModel):
    title: str = Field(default="未命名导演项目", max_length=120)


class DirectorMessageRequest(BaseModel):
    text: str = Field(min_length=1, max_length=20000)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _clean_text(value: Any) -> str:
    return str(value or "").strip()


def _extract_json_object(text: str) -> dict[str, Any]:
    raw = _clean_text(text)
    if not raw:
        raise ValueError("LLM 返回空内容")

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    fenced = re.sub(r"^```(?:json)?\s*", "", raw, flags=re.I)
    fenced = re.sub(r"\s*```$", "", fenced)
    try:
        parsed = json.loads(fenced)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass

    start = raw.find("{")
    end = raw.rfind("}")
    if start >= 0 and end > start:
        parsed = json.loads(raw[start : end + 1])
        if isinstance(parsed, dict):
            return parsed

    raise ValueError("LLM 输出不是可解析 JSON 对象")


class DirectorService:
    def __init__(self, settings: Settings, llm: GemmaService) -> None:
        self.settings = settings
        self.llm = llm
        self.skill_root = Path(settings.director_skill_root)
        self.projects_dir = Path(settings.director_projects_dir)
        self.manifest_path = Path(settings.director_manifest_path)
        self.projects_dir.mkdir(parents=True, exist_ok=True)
        self.skill_contracts_dir = (
            self.projects_dir.parent / "director_skill_contracts"
        )
        self.skill_contracts_dir.mkdir(parents=True, exist_ok=True)
        self.production = ProductionAssetService(settings.data_dir)
        self._locks: dict[str, asyncio.Lock] = {}
        self._llm_context_window_cache: int | None = None
        self._tokenize_endpoint_available: bool | None = None

    def _lock(self, project_id: str) -> asyncio.Lock:
        lock = self._locks.get(project_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[project_id] = lock
        return lock

    def _manifest(self) -> dict[str, Any]:
        return json.loads(self.manifest_path.read_text(encoding="utf-8"))

    def _skill_dir(self, skill_name: str) -> Path:
        root = (self.skill_root / "skills" / skill_name).resolve()
        expected = (self.skill_root / "skills").resolve()
        if expected not in root.parents:
            raise RuntimeError("非法技能目录")
        return root

    def _skill_md(self, skill_name: str) -> str:
        path = self._skill_dir(skill_name) / "SKILL.md"
        if not path.is_file():
            raise FileNotFoundError(f"缺少技能入口：{path}")
        return path.read_text(encoding="utf-8")

    def _available_files(self, skill_name: str) -> list[str]:
        base = self._skill_dir(skill_name)
        result: list[str] = []
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if path.name == "SKILL.md":
                continue
            if path.suffix.lower() not in ALLOWED_SOURCE_SUFFIXES:
                continue
            rel = path.relative_to(base).as_posix()
            if any(part.startswith(".") for part in Path(rel).parts):
                continue
            result.append(rel)
        return sorted(result)

    def _read_source_file(self, skill_name: str, relative: str) -> str:
        base = self._skill_dir(skill_name).resolve()
        path = (base / relative).resolve()
        if base not in path.parents:
            raise ValueError(f"非法引用路径：{relative}")
        if not path.is_file():
            raise FileNotFoundError(f"技能引用不存在：{relative}")
        if path.suffix.lower() not in ALLOWED_SOURCE_SUFFIXES:
            raise ValueError(f"不允许读取的技能引用类型：{relative}")
        return path.read_text(encoding="utf-8")

    def source_status(self) -> dict[str, Any]:
        manifest = self._manifest()
        checks: dict[str, bool] = {}
        for skill in [WORKFLOW_SKILL, *STAGE_SKILLS.values()]:
            checks[skill] = (
                self._skill_dir(skill) / "SKILL.md"
            ).is_file()
        return {
            "ready": all(checks.values()),
            "manifest": manifest,
            "checks": checks,
            "skill_runtime": {
                "enabled": True,
                "contract_schema": "skill_contract_v2",
                "runtime_schema": "skill_runtime_state_v2",
                "completion_gate": "production_asset_system",
                "production_asset_graph": True,
                "task_output_binding": True,
                "asset_versioning": True,
                "entity_registry": True,
            },
        }

    def _project_path(self, project_id: str) -> Path:
        if not re.fullmatch(r"[a-f0-9]{24}", project_id):
            raise ValueError("非法 project_id")
        return self.projects_dir / f"{project_id}.json"

    def _save_project(self, project: dict[str, Any]) -> None:
        path = self._project_path(project["project_id"])
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(project, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temp.replace(path)

    def get_project(self, project_id: str) -> dict[str, Any]:
        path = self._project_path(project_id)
        if not path.is_file():
            raise FileNotFoundError(f"导演项目不存在：{project_id}")
        return json.loads(path.read_text(encoding="utf-8"))

    def list_projects(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self.projects_dir.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                continue
            items.append(data)
        items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
        return items

    def create_project(self, title: str) -> dict[str, Any]:
        project_id = secrets.token_hex(12)
        now = _utcnow()
        project = {
            "project_id": project_id,
            "title": _clean_text(title) or "未命名导演项目",
            "status": "active",
            "current_stage": "01",
            "completed_stages": [],
            "confirmed_outputs": {},
            "stage_state": {
                stage: {
                    "internal_step": "",
                    "stage_memory": "",
                    "stage_ready": False,
                    "handoff": "",
                    "next_expected_action": "",
                    "last_required_files": [],
                    "approved_steps": [],
                    "last_control_action": "",
                    "native_plan": {},
                    "skill_contract": {},
                    "skill_runtime": empty_runtime_state(),
                    "last_native_target": {},
                    "last_skill_runtime_control": {},
                }
                for stage in STAGE_ORDER
            },
            "history": [],
            "created_at": now,
            "updated_at": now,
        }
        self._save_project(project)
        self.production.ensure_project(project_id, project["title"])
        return project

    def _history_context(
        self,
        project: dict[str, Any],
        stage: str,
        max_chars: int = 9000,
    ) -> str:
        records = [
            item for item in project.get("history", [])
            if item.get("stage") == stage
        ]
        selected: list[str] = []
        total = 0
        for item in reversed(records):
            line = (
                f"{item.get('role', '')}: "
                f"{_clean_text(item.get('content'))}"
            )
            if total + len(line) > max_chars:
                break
            selected.append(line)
            total += len(line)
        selected.reverse()
        return "\n\n".join(selected)

    def _prior_handoffs(
        self,
        project: dict[str, Any],
        max_chars: int = 12000,
    ) -> str:
        blocks = []
        total = 0
        for stage in reversed(project.get("completed_stages", [])):
            data = project.get("confirmed_outputs", {}).get(stage, {})
            handoff = _clean_text(data.get("handoff"))
            if not handoff:
                continue
            block = f"[已确认阶段 {stage}]\n{handoff}"
            if blocks and total + len(block) > max_chars:
                break
            if not blocks and len(block) > max_chars:
                block = block[:max_chars] + "\n[handoff 超出本轮上下文预算]"
            blocks.append(block)
            total += len(block)
        blocks.reverse()
        return "\n\n".join(blocks)

    def _compact_stage_state(
        self,
        stage_state: dict[str, Any],
    ) -> dict[str, Any]:
        plan = stage_state.get("native_plan") or {}
        audit = stage_state.get("last_artifact_audit") or {}
        return {
            "internal_step": _clean_text(
                stage_state.get("internal_step")
            ),
            "stage_memory": _clean_text(
                stage_state.get("stage_memory")
            )[:2400],
            "stage_ready": bool(
                stage_state.get("stage_ready")
            ),
            "next_expected_action": _clean_text(
                stage_state.get("next_expected_action")
            ),
            "last_required_files": list(
                stage_state.get("last_required_files") or []
            )[:8],
            "approved_steps": list(
                stage_state.get("approved_steps") or []
            )[-24:],
            "last_control_action": _clean_text(
                stage_state.get("last_control_action")
            ),
            "native_plan": {
                "mode": _clean_text(plan.get("mode")),
                "steps": list(plan.get("steps") or [])[:24],
                "current_index": plan.get("current_index", -1),
                "source_sha256": _clean_text(
                    plan.get("source_sha256")
                ),
            },
            "last_artifact_audit": {
                "valid": bool(audit.get("valid")),
                "reason": _clean_text(
                    audit.get("reason")
                )[:600],
                "missing": list(audit.get("missing") or [])[:8],
            },
            "skill_runtime": {
                "selected_output_group_ids": list(
                    (stage_state.get("skill_runtime") or {}).get(
                        "selected_output_group_ids"
                    ) or []
                )[:8],
                "active_requirement_ids": list(
                    (stage_state.get("skill_runtime") or {}).get(
                        "active_requirement_ids"
                    ) or []
                )[:16],
                "completion": (
                    (stage_state.get("skill_runtime") or {}).get(
                        "completion"
                    ) or {}
                ),
            },
        }

    def _llm_server_root(self) -> str:
        base = _clean_text(
            getattr(self.llm, "base_url", "")
        ).rstrip("/")
        if base.endswith("/v1"):
            return base[:-3]
        return base

    @staticmethod
    def _fallback_token_estimate(text: str) -> int:
        if not text:
            return 0
        cjk = sum(
            1
            for char in text
            if (
                "\\u3400" <= char <= "\\u4dbf"
                or "\\u4e00" <= char <= "\\u9fff"
                or "\\uf900" <= char <= "\\ufaff"
            )
        )
        other = max(0, len(text) - cjk)
        return cjk + (other + 2) // 3 + 32

    async def _runtime_context_window(self) -> int:
        cached = getattr(
            self,
            "_llm_context_window_cache",
            None,
        )
        if isinstance(cached, int) and cached >= 2048:
            return cached

        root = self._llm_server_root()
        if root:
            try:
                async with httpx.AsyncClient(
                    timeout=4,
                    trust_env=False,
                ) as client:
                    response = await client.get(
                        f"{root}/props"
                    )
                    response.raise_for_status()
                    payload = response.json()
                value = int(
                    (
                        payload.get(
                            "default_generation_settings",
                            {},
                        )
                        .get("n_ctx", 0)
                    )
                    or 0
                )
                if value >= 2048:
                    self._llm_context_window_cache = value
                    return value
            except Exception:
                pass

        fallback = 8192
        self._llm_context_window_cache = fallback
        return fallback

    async def _count_prompt_tokens(
        self,
        *,
        system_prompt: str,
        messages: list[dict[str, str]],
    ) -> tuple[int, str]:
        # llama.cpp /tokenize gives the active model tokenizer.  We
        # intentionally add a fixed chat-template reserve afterwards,
        # because /tokenize sees raw text rather than the rendered
        # chat template.
        blocks = [f"<SYSTEM>\\n{system_prompt}"]
        for item in messages:
            blocks.append(
                f"<{_clean_text(item.get('role')).upper()}>\\n"
                f"{_clean_text(item.get('content'))}"
            )
        raw = "\\n\\n".join(blocks)

        root = self._llm_server_root()
        endpoint_state = getattr(
            self,
            "_tokenize_endpoint_available",
            None,
        )
        if root and endpoint_state is not False:
            try:
                async with httpx.AsyncClient(
                    timeout=6,
                    trust_env=False,
                ) as client:
                    response = await client.post(
                        f"{root}/tokenize",
                        json={
                            "content": raw,
                            "add_special": False,
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                tokens = payload.get("tokens")
                if isinstance(tokens, list):
                    self._tokenize_endpoint_available = True
                    # Conservative reserve for role wrappers /
                    # generation prompt produced by the active chat
                    # template.
                    return (
                        len(tokens)
                        + 96
                        + 12 * len(messages),
                        "llama_tokenize",
                    )
            except Exception:
                self._tokenize_endpoint_available = False

        return (
            self._fallback_token_estimate(raw)
            + 128
            + 12 * len(messages),
            "fallback_estimate",
        )

    async def _llm_call_budget(
        self,
        *,
        phase: str,
        system_prompt: str,
        messages: list[dict[str, str]],
        requested_output_tokens: int,
        minimum_output_tokens: int,
        safety_tokens: int = 192,
    ) -> dict[str, Any]:
        context_window = await self._runtime_context_window()
        prompt_tokens, estimator = await self._count_prompt_tokens(
            system_prompt=system_prompt,
            messages=messages,
        )
        available = (
            context_window
            - prompt_tokens
            - max(64, int(safety_tokens))
        )
        if available < minimum_output_tokens:
            raise RuntimeError(
                f"{phase}: 上下文预算不足；"
                f"prompt_tokens≈{prompt_tokens}, "
                f"context_window={context_window}, "
                f"available_output≈{available}, "
                f"minimum_output={minimum_output_tokens}, "
                f"estimator={estimator}"
            )
        output_tokens = min(
            max(1, int(requested_output_tokens)),
            available,
        )
        return {
            "phase": phase,
            "context_window": context_window,
            "prompt_tokens": prompt_tokens,
            "requested_output_tokens": int(
                requested_output_tokens
            ),
            "output_tokens": int(output_tokens),
            "safety_tokens": int(safety_tokens),
            "estimator": estimator,
        }

    @staticmethod
    def _markdown_section_catalog(
        content: str,
    ) -> tuple[str, list[dict[str, Any]]]:
        lines = content.splitlines()
        heading_rows: list[tuple[int, int, str]] = []
        for index, line in enumerate(lines):
            match = re.match(
                r"^(#{1,6})\\s+(.+?)\\s*$",
                line,
            )
            if match:
                heading_rows.append(
                    (
                        index,
                        len(match.group(1)),
                        match.group(2).strip(),
                    )
                )

        if not heading_rows:
            return content, []

        preamble = "\\n".join(
            lines[:heading_rows[0][0]]
        ).strip()
        sections: list[dict[str, Any]] = []

        for position, (
            start,
            level,
            title,
        ) in enumerate(heading_rows):
            end = len(lines)
            for later_start, later_level, _ in (
                heading_rows[position + 1 :]
            ):
                if later_level <= level:
                    end = later_start
                    break

            parent_titles: list[str] = []
            for prior_start, prior_level, prior_title in reversed(
                heading_rows[:position]
            ):
                if prior_level < level:
                    parent_titles.append(prior_title)
                    level = prior_level
                    if level == 1:
                        break
            parent_titles.reverse()
            path = " > ".join(
                [*parent_titles, title]
            )
            body = "\\n".join(
                lines[start:end]
            ).strip()
            sections.append(
                {
                    "id": f"s{position:03d}",
                    "title": title,
                    "path": path,
                    "content": body,
                    "chars": len(body),
                }
            )
        return preamble, sections

    async def _select_reference_sections(
        self,
        *,
        skill_name: str,
        skill_md: str,
        relative: str,
        content: str,
        native_target: dict[str, Any],
        user_text: str,
        stage_state: dict[str, Any],
        route_reason: str,
    ) -> tuple[list[str], dict[str, Any]]:
        preamble, sections = self._markdown_section_catalog(
            content
        )
        if not sections:
            return [content], {
                "file": relative,
                "mode": "whole_file_no_headings",
                "selected_section_ids": [],
            }

        catalog = [
            {
                "id": item["id"],
                "path": item["path"],
                "chars": item["chars"],
            }
            for item in sections
        ]

        system_prompt = """你是 Agent Skill 引用章节选择器，不执行用户任务。
生产 SKILL.md 已经选择了当前 reference 文件；你只需从该 reference 的 Markdown 章节目录中选出本轮原生目标真正需要的最少章节。

规则：
1. 不改变生产 SKILL.md 的步骤顺序。
2. 只能返回 SECTION_CATALOG 中已有 section id。
3. 按重要性排序，最必要的在前。
4. 不为了“全面”选择无关章节。
5. 章节选择只能依据 SKILL.md、NATIVE_TARGET、当前项目控制记忆、路由理由和用户当前消息，不使用代码关键词表。
6. 最多返回 6 个 section id。
返回严格 JSON：
{"section_ids":["s000"],"reason":"一句话"}"""

        user_prompt = f"""CURRENT_SKILL={skill_name}
REFERENCE_FILE={relative}

=== SKILL.md ===
{skill_md}

=== NATIVE_TARGET ===
{json.dumps(native_target, ensure_ascii=False)}

=== COMPACT_STAGE_STATE ===
{json.dumps(self._compact_stage_state(stage_state), ensure_ascii=False)}

=== ROUTE_REASON ===
{route_reason}

=== CURRENT_USER_MESSAGE ===
{user_text}

=== SECTION_CATALOG ===
{json.dumps(catalog, ensure_ascii=False)}
"""
        _, parsed, _ = await self._structured_json_call(
            phase="reference_section_selector",
            messages=[{
                "role": "user",
                "content": user_prompt,
            }],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=500,
            contract=(
                '{"section_ids":["s000"],'
                '"reason":"一句话"}'
            ),
        )

        requested = parsed.get("section_ids", [])
        if not isinstance(requested, list):
            raise ValueError(
                "reference_section_selector.section_ids "
                "必须是数组"
            )

        section_map = {
            item["id"]: item for item in sections
        }
        selected: list[str] = []
        normalized: list[str] = []
        for raw in requested[:6]:
            section_id = _clean_text(raw)
            if (
                section_id
                and section_id in section_map
                and section_id not in normalized
            ):
                normalized.append(section_id)
                selected.append(
                    section_map[section_id]["content"]
                )

        if not selected:
            # Empty selection is allowed only when the routed file
            # turns out not to be needed for this exact native target.
            return [], {
                "file": relative,
                "mode": "section_selected",
                "selected_section_ids": [],
                "reason": _clean_text(parsed.get("reason")),
                "preamble_chars": len(preamble),
            }

        if preamble:
            selected.insert(0, preamble)

        return selected, {
            "file": relative,
            "mode": "section_selected",
            "selected_section_ids": normalized,
            "reason": _clean_text(parsed.get("reason")),
            "preamble_chars": len(preamble),
        }

    async def _build_reference_context(
        self,
        *,
        skill_name: str,
        skill_md: str,
        required_files: list[str],
        native_target: dict[str, Any],
        user_text: str,
        stage_state: dict[str, Any],
        route_reason: str,
        max_chars: int,
    ) -> tuple[list[str], dict[str, Any]]:
        raw_files: list[tuple[str, str]] = []
        raw_total = 0
        for relative in required_files:
            content = self._read_source_file(
                skill_name,
                relative,
            )
            raw_files.append((relative, content))
            raw_total += len(content)

        # Small routed references remain exact and whole.  Large sets
        # are reduced only at Markdown section boundaries; SKILL.md is
        # never truncated here.
        whole_threshold = min(
            max(3500, max_chars // 2),
            7000,
        )
        blocks: list[str] = []
        metadata: list[dict[str, Any]] = []
        used_chars = 0

        for relative, content in raw_files:
            if (
                raw_total <= max_chars
                and len(content) <= whole_threshold
            ):
                pieces = [content]
                info = {
                    "file": relative,
                    "mode": "whole_file",
                    "selected_section_ids": [],
                }
            else:
                pieces, info = (
                    await self._select_reference_sections(
                        skill_name=skill_name,
                        skill_md=skill_md,
                        relative=relative,
                        content=content,
                        native_target=native_target,
                        user_text=user_text,
                        stage_state=stage_state,
                        route_reason=route_reason,
                    )
                )

            accepted: list[str] = []
            for piece in pieces:
                piece = _clean_text(piece)
                if not piece:
                    continue
                projected = (
                    used_chars
                    + len(piece)
                    + len(relative)
                    + 16
                )
                if projected > max_chars:
                    continue
                accepted.append(piece)
                used_chars = projected

            if accepted:
                blocks.append(
                    f"### {relative}\\n"
                    + "\\n\\n".join(accepted)
                )

            info = dict(info)
            info["included"] = bool(accepted)
            info["included_chars"] = sum(
                len(item) for item in accepted
            )
            metadata.append(info)

        return blocks, {
            "raw_reference_chars": raw_total,
            "included_reference_chars": used_chars,
            "reference_budget_chars": max_chars,
            "files": metadata,
        }

    def _skill_source_sha256(self, skill_md: str) -> str:
        return hashlib.sha256(
            skill_md.encode("utf-8")
        ).hexdigest()

    def _skill_contract_path(
        self,
        skill_name: str,
        source_hash: str,
    ) -> Path:
        safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", skill_name)
        return self.skill_contracts_dir / f"{safe}-{source_hash}.json"

    def _load_skill_contract_cache(
        self,
        *,
        skill_name: str,
        skill_md: str,
    ) -> dict[str, Any] | None:
        source_hash = skill_contract_source_sha256(skill_md)
        path = self._skill_contract_path(skill_name, source_hash)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            return None
        if (
            _clean_text(data.get("schema_version")) != "skill_contract_v2"
            or _clean_text(data.get("skill_name")) != skill_name
            or _clean_text(data.get("source_sha256")) != source_hash
        ):
            return None
        return data

    def _save_skill_contract_cache(
        self,
        contract: dict[str, Any],
    ) -> None:
        skill_name = _clean_text(contract.get("skill_name"))
        source_hash = _clean_text(contract.get("source_sha256"))
        if not skill_name or not source_hash:
            return
        path = self._skill_contract_path(skill_name, source_hash)
        temp = path.with_suffix(".tmp")
        temp.write_text(
            json.dumps(contract, ensure_ascii=False, indent=2) + "\
",
            encoding="utf-8",
        )
        temp.replace(path)

    async def _compile_skill_contract(
        self,
        *,
        skill_name: str,
        skill_md: str,
    ) -> dict[str, Any]:
        """Compile SKILL.md into a system execution contract.

        This is control-plane compilation, not business-output review. Every
        extracted rule must carry an exact SKILL.md source quote. If contract
        compilation cannot ground terminal outputs, runtime degrades to
        native_only instead of inventing a blocker.
        """
        system_prompt = """你是 Agent Skill -> 系统执行契约编译器。
只读取给定 SKILL.md，把其中明确存在的“最终输出形态/交付物”和“会影响完成条件的条件规则”整理成固定 JSON。
你不执行 Skill，不评价内容质量，不添加规则，不使用外部常识。

关键规则：
1. source_quote 必须逐字连续复制自 SKILL.md。
2. 只提取会决定‘当前 Skill 是否真正完成’的内容。方法论、质量建议、示例说明、负面词表、参考文件路由不要变成完成门。
3. output_groups 表示互斥或可选的最终输出形态。每个 artifact 必须是用户真正应该拿到的交付物，而不是‘准备做/执行步骤/目标/说明’。
4. 每个 artifact 还要映射到平台通用资产协议：asset_type 只能是 TEXT/STRUCTURED_DATA/IMAGE/VIDEO/AUDIO/FILE/ENTITY/COLLECTION；asset_role 是从 SKILL 原文概括出的稳定角色名；materialization 只能是 text/structured/task_output/external_file/entity/collection；producer_capability 只能是 director/image/video/facefusion/external/none。
5. cardinality_min/max 只根据 SKILL 明确数量要求填写；未明确时 min=1,max=null。file_extension 只用于文本/文件交付，默认 .md。
6. literal_marker 只有 SKILL.md 的输出模板明确要求一个可稳定识别的原文字面标记时才填写，并且必须逐字存在于 SKILL.md；否则留空。
7. conditional_requirements 只记录在特定输入状态下必须对用户执行/说明的规则。
8. 如果 SKILL.md 没有明确最终输出形态，completion_mode=native_only，output_groups=[]。
9. 不要把 sequential workflow 的每一步都当成终态 artifact；这里只编译最终交付契约。
10. 如果 Skill 只要求生成提示词/文本，不得擅自把 IMAGE/VIDEO 当成完成条件；只有 Skill 明确要求真实媒体产物时才用 task_output。

严格 JSON：
{
  "completion_mode":"native_only|artifact_gate",
  "output_groups":[{
    "name":"输出形态名",
    "source_quote":"SKILL.md逐字依据",
    "artifacts":[{
      "name":"交付物名",
      "required":true,
      "source_quote":"SKILL.md逐字依据",
      "literal_marker":"可选的SKILL.md逐字输出标记",
      "asset_type":"TEXT",
      "asset_role":"稳定资产角色",
      "materialization":"text",
      "producer_capability":"director",
      "cardinality_min":1,
      "cardinality_max":null,
      "file_extension":".md"
    }]
  }],
  "conditional_requirements":[{
    "name":"规则名",
    "source_quote":"SKILL.md逐字依据",
    "activation_description":"何时适用",
    "required_behavior":"适用时必须实际做什么"
  }],
  "reason":"一句话"
}"""
        user_prompt = f"""CURRENT_SKILL={skill_name}

=== SKILL.md ===
{skill_md}
"""
        try:
            _, raw, _ = await self._structured_json_call(
                phase="skill_contract_compile",
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=system_prompt,
                temperature=0.0,
                max_tokens=2200,
                contract=(
                    '{"completion_mode":"native_only|artifact_gate",'
                    '"output_groups":[],"conditional_requirements":[],'
                    '"reason":"一句话"}'
                ),
            )
            contract = normalize_contract(
                skill_name=skill_name,
                skill_md=skill_md,
                raw=raw,
            )
        except Exception as exc:
            contract = normalize_contract(
                skill_name=skill_name,
                skill_md=skill_md,
                raw={
                    "completion_mode": "native_only",
                    "output_groups": [],
                    "conditional_requirements": [],
                    "reason": "contract compile fallback: " + str(exc)[:300],
                },
            )
        self._save_skill_contract_cache(contract)
        return contract

    async def _ensure_skill_contract(
        self,
        *,
        skill_name: str,
        skill_md: str,
        stage_state: dict[str, Any],
    ) -> dict[str, Any]:
        source_hash = skill_contract_source_sha256(skill_md)
        current = stage_state.get("skill_contract") or {}
        if (
            _clean_text(current.get("schema_version")) == "skill_contract_v2"
            and _clean_text(current.get("source_sha256")) == source_hash
        ):
            return current
        cached = self._load_skill_contract_cache(
            skill_name=skill_name,
            skill_md=skill_md,
        )
        contract = cached or await self._compile_skill_contract(
            skill_name=skill_name,
            skill_md=skill_md,
        )
        stage_state["skill_contract"] = contract
        runtime_state = stage_state.get("skill_runtime") or empty_runtime_state()
        if _clean_text(runtime_state.get("contract_source_sha256")) != source_hash:
            stage_state["skill_runtime"] = empty_runtime_state()
        return contract

    async def get_skill_contract(self, skill_name: str) -> dict[str, Any]:
        if skill_name not in {WORKFLOW_SKILL, *STAGE_SKILLS.values()}:
            raise ValueError(f"未知 Director Skill：{skill_name}")
        skill_md = self._skill_md(skill_name)
        cached = self._load_skill_contract_cache(
            skill_name=skill_name,
            skill_md=skill_md,
        )
        return cached or await self._compile_skill_contract(
            skill_name=skill_name,
            skill_md=skill_md,
        )

    def project_skill_runtime(self, project_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        stage = _clean_text(project.get("current_stage"))
        state = (project.get("stage_state") or {}).get(stage, {}) or {}
        return {
            "project_id": project_id,
            "status": project.get("status"),
            "current_stage": stage,
            "skill": STAGE_SKILLS.get(stage, ""),
            "skill_contract": state.get("skill_contract") or {},
            "skill_runtime": state.get("skill_runtime") or empty_runtime_state(),
        }

    def _ensure_project_production_history(
        self, project: dict[str, Any]
    ) -> None:
        project_id = project["project_id"]
        self.production.ensure_project(
            project_id, _clean_text(project.get("title"))
        )
        changed = False
        for index, item in enumerate(project.get("history") or []):
            if item.get("role") != "assistant":
                continue
            content = _clean_text(item.get("content"))
            stage = _clean_text(item.get("stage"))
            if not content or stage not in STAGE_SKILLS:
                continue
            turn_id = _clean_text(item.get("turn_id")) or f"legacy{index:04d}"
            asset = self.production.materialize_turn_output(
                project_id,
                stage=stage,
                skill=STAGE_SKILLS[stage],
                turn_id=turn_id,
                content=content,
                native_target=item.get("native_target") or {"kind":"legacy_history","name":""},
            )
            if not _clean_text(item.get("production_asset_id")):
                item["production_asset_id"] = asset["asset_id"]
                changed = True
        if changed:
            project["updated_at"] = _utcnow()
            self._save_project(project)

    def project_production(self, project_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        self._ensure_project_production_history(project)
        graph = self.production.ensure_project(
            project_id, _clean_text(project.get("title"))
        )
        return {
            "project_id": project_id,
            "status": project.get("status"),
            "current_stage": project.get("current_stage"),
            "production": graph,
            "stage_status": {
                stage: self.production.stage_status(project_id, stage)
                for stage in STAGE_ORDER
            },
        }

    def _prior_asset_manifest(
        self,
        project: dict[str, Any],
        max_chars: int = 5000,
    ) -> str:
        return self.production.context_manifest(
            project["project_id"],
            stages=list(project.get("completed_stages") or []),
            max_chars=max_chars,
        )

    def refresh_production_completion(self, project_id: str) -> dict[str, Any]:
        project = self.get_project(project_id)
        stage = _clean_text(project.get("current_stage"))
        if stage not in STAGE_SKILLS:
            return self.project_skill_runtime(project_id)
        state = (project.get("stage_state") or {}).get(stage, {}) or {}
        contract = state.get("skill_contract") or {}
        runtime_state = state.get("skill_runtime") or empty_runtime_state()
        if not contract:
            return self.project_skill_runtime(project_id)
        readiness = self.production.contract_asset_readiness(
            project_id, stage, contract
        )
        runtime_state = apply_asset_completion(
            contract=contract,
            runtime_state=runtime_state,
            control_runtime=state.get("last_skill_runtime_control") or {},
            native_target=state.get("last_native_target") or {},
            native_plan=state.get("native_plan") or {},
            asset_readiness=readiness,
        )
        self.production.ensure_contract_placeholders(
            project_id,
            stage=stage,
            skill=STAGE_SKILLS[stage],
            contract=contract,
            runtime_state=runtime_state,
        )
        state["skill_runtime"] = runtime_state
        state["stage_ready"] = bool(
            (runtime_state.get("completion") or {}).get("ready")
        )
        if not state["stage_ready"]:
            state["handoff"] = ""
        project["updated_at"] = _utcnow()
        self._save_project(project)
        return {
            "project_id": project_id,
            "current_stage": stage,
            "stage_ready": state["stage_ready"],
            "skill_runtime": runtime_state,
            "production_stage_status": self.production.stage_status(project_id, stage),
        }


    def _validate_native_plan(
        self,
        *,
        skill_md: str,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        """Validate and canonicalize native step labels against SKILL.md.

        Persisted step names are always exact substrings of the current
        SKILL.md. A planner may occasionally concatenate a Markdown list
        item's following explanation onto the item label; in that narrow case
        we deterministically keep only the exact source label. We never
        invent, translate, rename or fuzzy-match a step.
        """
        mode = _clean_text(plan.get("mode"))
        if mode not in {"sequential", "dynamic"}:
            raise ValueError(
                f"非法 native_plan.mode：{mode or '<empty>'}"
            )

        source_sha256 = self._skill_source_sha256(skill_md)
        if mode == "dynamic":
            return {
                "mode": "dynamic",
                "steps": [],
                "current_index": -1,
                "reason": _clean_text(plan.get("reason")),
                "source_sha256": source_sha256,
            }

        raw_steps = plan.get("steps")
        if not isinstance(raw_steps, list):
            raise ValueError("native_plan.steps 必须是数组")
        if not 2 <= len(raw_steps) <= 24:
            raise ValueError(
                "sequential native_plan 步骤数必须在 2-24 之间"
            )

        def markdown_label_candidates(
            source: str,
            start: int,
        ) -> list[tuple[int, str]]:
            result: list[tuple[int, str]] = []
            offset = 0
            for line in source.splitlines(keepends=True):
                line_start = offset
                offset += len(line)
                if line_start + len(line) <= start:
                    continue
                stripped = line.strip()
                if not stripped:
                    continue

                variants: list[str] = [stripped]

                value = re.sub(r"^#{1,6}\s+", "", stripped).strip()
                if value != stripped:
                    variants.append(value)

                value = re.sub(
                    r"^(?:\d{1,3}[.)]|[-+*])\s+",
                    "",
                    stripped,
                ).strip()
                if value != stripped:
                    variants.append(value)

                for value in list(variants):
                    cleaned = value.strip()
                    cleaned = re.sub(
                        r"^\*\*(.+?)\*\*\s*$",
                        r"\1",
                        cleaned,
                    ).strip()
                    cleaned = re.sub(
                        r"^__(.+?)__\s*$",
                        r"\1",
                        cleaned,
                    ).strip()
                    if cleaned and cleaned not in variants:
                        variants.append(cleaned)

                seen: set[str] = set()
                for value in variants:
                    value = _clean_text(value)
                    if not value or value in seen:
                        continue
                    seen.add(value)
                    if len(re.sub(r"\s+", "", value)) < 4:
                        continue
                    local = line.find(value)
                    if local < 0:
                        continue
                    result.append((line_start + local, value))
            return result

        def canonical_step(
            raw_name: str,
            start: int,
        ) -> tuple[int, str]:
            name = _clean_text(raw_name)
            if not name:
                raise ValueError("native_plan 含空步骤")

            try:
                return skill_md.index(name, start), name
            except ValueError:
                pass

            normalized = re.sub(r"\s+", " ", name).strip()
            matches: list[tuple[int, str]] = []
            for position, candidate in markdown_label_candidates(
                skill_md,
                start,
            ):
                c_norm = re.sub(r"\s+", " ", candidate).strip()
                if (
                    normalized.startswith(c_norm)
                    and len(normalized) > len(c_norm)
                ):
                    matches.append((position, candidate))

            if matches:
                matches.sort(
                    key=lambda item: (-len(item[1]), item[0])
                )
                return matches[0]

            raise ValueError(
                "native_plan 步骤必须逐字来自当前 SKILL.md，"
                f"且保持原文顺序：{name}"
            )

        steps: list[str] = []
        cursor = 0
        for raw_step in raw_steps:
            position, name = canonical_step(
                _clean_text(raw_step),
                cursor,
            )
            if name in steps:
                raise ValueError(
                    f"native_plan 含重复步骤：{name}"
                )
            steps.append(name)
            cursor = position + len(name)

        return {
            "mode": "sequential",
            "steps": steps,
            "current_index": -1,
            "reason": _clean_text(plan.get("reason")),
            "source_sha256": source_sha256,
        }

    async def _structured_json_call(
        self,
        *,
        phase: str,
        messages: list[dict[str, str]],
        system_prompt: str,
        temperature: float,
        max_tokens: int,
        contract: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        initial_budget = await self._llm_call_budget(
            phase=phase,
            system_prompt=system_prompt,
            messages=messages,
            requested_output_tokens=max_tokens,
            minimum_output_tokens=min(
                160,
                max(80, int(max_tokens)),
            ),
        )
        result = await self.llm.chat(
            messages=messages,
            system_prompt=system_prompt,
            temperature=temperature,
            max_tokens=initial_budget["output_tokens"],
        )
        result = dict(result)
        result["context_budget"] = initial_budget
        raw = _clean_text(result.get("content"))
        try:
            parsed = _extract_json_object(raw)
            return result, parsed, False
        except Exception as first_error:
            repair_system = """你是严格 JSON 结构修复器，不执行原任务。
你的唯一工作是把 RAW_OUTPUT 修复为满足 JSON_CONTRACT 的一个合法 JSON 对象。

规则：
1. 只输出 JSON 对象，不要 Markdown，不要解释。
2. 不新增 RAW_OUTPUT 中不存在的业务结论；只修复结构、转义、围栏、前后缀和缺失的 JSON 语法。
3. 如果 RAW_OUTPUT 已包含所需语义，保留原意。
4. 如果某个非关键说明字段确实缺失，使用空字符串、空数组或 false；不得编造业务内容。
5. 必须严格满足 JSON_CONTRACT。"""

            raw_for_repair = raw
            if len(raw_for_repair) > 12000:
                raw_for_repair = (
                    raw_for_repair[:6000]
                    + "\n...[middle omitted by transport repair]...\n"
                    + raw_for_repair[-6000:]
                )

            repair_prompt = f"""PHASE={phase}

=== JSON_CONTRACT ===
{contract}

=== RAW_OUTPUT ===
{raw_for_repair}
"""
            repair_messages = [{
                "role": "user",
                "content": repair_prompt,
            }]
            repair_requested = min(
                max(max_tokens, 600),
                1800,
            )
            repair_budget = await self._llm_call_budget(
                phase=f"{phase}_json_repair",
                system_prompt=repair_system,
                messages=repair_messages,
                requested_output_tokens=repair_requested,
                minimum_output_tokens=160,
            )
            repair_result = await self.llm.chat(
                messages=repair_messages,
                system_prompt=repair_system,
                temperature=0.0,
                max_tokens=repair_budget["output_tokens"],
            )
            repaired_raw = _clean_text(
                repair_result.get("content")
            )
            try:
                parsed = _extract_json_object(repaired_raw)
            except Exception as second_error:
                raw_sha = hashlib.sha256(
                    raw.encode("utf-8")
                ).hexdigest()[:16]
                repaired_sha = hashlib.sha256(
                    repaired_raw.encode("utf-8")
                ).hexdigest()[:16]
                raise ValueError(
                    f"{phase}: LLM 结构化 JSON 修复失败；"
                    f"raw_len={len(raw)}, raw_sha={raw_sha}, "
                    f"repair_len={len(repaired_raw)}, "
                    f"repair_sha={repaired_sha}"
                ) from second_error

            merged = dict(result)
            merged["structured_json_repaired"] = True
            merged["structured_json_phase"] = phase
            merged["structured_json_repair_model"] = (
                repair_result.get("model")
            )
            return merged, parsed, True


    async def _audit_native_plan(
        self,
        *,
        skill_name: str,
        skill_md: str,
        project: dict[str, Any],
        stage: str,
        user_text: str,
        plan: dict[str, Any],
    ) -> dict[str, Any]:
        system_prompt = """你是 Agent Skill 原生步骤计划审计器，不执行创作任务。
核对 CANDIDATE_PLAN 是否忠实、完整地覆盖当前 SKILL.md 对当前任务明确规定的有限顺序执行流程。

审计规则：
1. SKILL.md 是唯一步骤权威。
2. sequential 计划必须包含所有适用的正式顺序步骤，不得遗漏、增加、重排、改名。
3. dynamic 只有在 SKILL.md 对当前任务确实没有明确有限顺序流程时才有效。
4. reference 不参与决定步骤顺序。
5. 不执行创作任务。

返回严格 JSON：
{"valid":true,"reason":"一句话","missing_or_wrong":["如有问题列出原文步骤名或问题；无则空数组"]}"""

        user_prompt = f"""CURRENT_SKILL={skill_name}

=== SKILL.md ===
{skill_md}

=== CURRENT_USER_MESSAGE（仅用于适用路线，不得提供步骤名称） ===
{user_text}

=== CANDIDATE_PLAN ===
{json.dumps(plan, ensure_ascii=False)}
"""
        result, parsed, _ = await self._structured_json_call(
            phase="native_plan_audit",
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=600,
            contract=(
                '{"valid":true,"reason":"一句话",'
                '"missing_or_wrong":[]}'
            ),
        )
        return {
            "valid": bool(parsed.get("valid")),
            "reason": _clean_text(parsed.get("reason")),
            "missing_or_wrong": parsed.get(
                "missing_or_wrong",
                [],
            ),
        }



    async def _extract_native_plan(
        self,
        *,
        skill_name: str,
        skill_md: str,
        project: dict[str, Any],
        stage: str,
        user_text: str,
    ) -> dict[str, Any]:
        """Best-effort native plan discovery.

        native_plan is navigation metadata, not a production gate.
        If the Skill does not expose a clean finite sequence, or if extraction
        is ambiguous, fall back to skill-driven dynamic execution instead of
        blocking the user's production turn.
        """
        source_sha256 = self._skill_source_sha256(skill_md)

        def dynamic(reason: str) -> dict[str, Any]:
            return {
                "mode": "dynamic",
                "steps": [],
                "current_index": -1,
                "reason": _clean_text(reason) or "当前 Skill 使用动态原生执行",
                "source_sha256": source_sha256,
                "audit": {
                    "valid": True,
                    "reason": "native_plan 仅作导航；无法稳定提取有限顺序时自动使用 dynamic，不阻塞生产执行",
                },
            }

        system_prompt = """你是 Agent Skill 的轻量步骤导航解析器，不执行创作任务。

目标：
- 只有当当前 SKILL.md 明确存在“有限、连续、需要逐步推进”的正式步骤时，才返回 sequential。
- 如果 SKILL.md 更像方法说明、规则集合、字段顺序、输出模板、条件分支或动态工作流，返回 dynamic。

要求：
1. SKILL.md 是唯一来源。
2. 不创造步骤，不翻译步骤名，不把正文说明、字段链、示例或输出格式当步骤。
3. sequential 的每个 steps 元素必须是 SKILL.md 中能逐字找到的单个步骤标签。
4. 不确定时优先 dynamic。dynamic 不是失败，是正常执行模式。
5. 不输出创作内容。

返回严格 JSON：
{"mode":"sequential|dynamic","steps":["原文步骤标签"],"reason":"一句话"}"""

        user_prompt = f"""CURRENT_SKILL={skill_name}

=== CURRENT SKILL.md ===
{skill_md}

=== CURRENT TASK CONTEXT ===
{user_text}
"""

        try:
            result, parsed, _ = await self._structured_json_call(
                phase="native_plan_extract",
                messages=[{"role": "user", "content": user_prompt}],
                system_prompt=system_prompt,
                temperature=0.0,
                max_tokens=900,
                contract=(
                    '{"mode":"sequential|dynamic",'
                    '"steps":["原文步骤标签"],'
                    '"reason":"一句话"}'
                ),
            )
        except Exception as exc:
            return dynamic(
                "native_plan 解析不可用，按当前 Skill dynamic 执行："
                + type(exc).__name__
            )

        if not isinstance(parsed, dict):
            return dynamic("native_plan 返回格式不稳定，按当前 Skill dynamic 执行")

        mode = _clean_text(parsed.get("mode"))
        if mode != "sequential":
            return dynamic(
                _clean_text(parsed.get("reason"))
                or "当前 Skill 未定义明确有限顺序步骤"
            )

        raw_steps = parsed.get("steps")
        if not isinstance(raw_steps, list) or not raw_steps:
            return dynamic("未提取到明确有限顺序步骤")

        # Lightweight exact-source check only. Failure means dynamic fallback,
        # never a production error.
        try:
            plan = self._validate_native_plan(
                skill_md=skill_md,
                plan={
                    "mode": "sequential",
                    "steps": raw_steps,
                    "reason": _clean_text(parsed.get("reason")),
                },
            )
        except Exception:
            return dynamic("步骤标签无法稳定逐字对应当前 SKILL.md，自动使用 dynamic")

        # Full-plan audit is advisory. Any ambiguity falls back to dynamic.
        try:
            audit = await self._audit_native_plan(
                skill_name=skill_name,
                skill_md=skill_md,
                project=project,
                stage=stage,
                user_text=user_text,
                plan=plan,
            )
        except Exception:
            return dynamic("步骤完整性审计不可用，自动使用 dynamic")

        if not bool((audit or {}).get("valid")):
            return dynamic(
                _clean_text((audit or {}).get("reason"))
                or "当前 Skill 更适合 dynamic 执行"
            )

        plan["audit"] = {
            "valid": True,
            "reason": _clean_text((audit or {}).get("reason"))
            or "当前 Skill 存在明确有限顺序步骤",
        }
        return plan

    async def _align_native_plan_index(
        self,
        *,
        skill_name: str,
        skill_md: str,
        plan: dict[str, Any],
        current_step: str,
    ) -> int:
        if plan.get("mode") != "sequential":
            return -1

        steps = plan.get("steps") or []
        if not current_step:
            return -1

        for index, name in enumerate(steps):
            if current_step == name:
                return index

        system_prompt = """你是 Agent Skill 步骤对齐器，不执行创作任务。
将 CURRENT_STEP 对齐到 PLAN_STEPS 中语义上同一个原生步骤。
只允许返回一个已有步骤索引；如果无法确定则返回 -1。
禁止推断下一步，禁止改写计划，禁止根据常识补步骤。
返回严格 JSON：
{"index":0,"reason":"一句话"}"""

        user_prompt = f"""CURRENT_SKILL={skill_name}

=== SKILL.md ===
{skill_md}

=== PLAN_STEPS ===
{json.dumps(steps, ensure_ascii=False)}

=== CURRENT_STEP ===
{current_step}
"""

        result, parsed, _ = await self._structured_json_call(
            phase="native_plan_align",
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=300,
            contract='{"index":0,"reason":"一句话"}',
        )
        try:
            index = int(parsed.get("index", -1))
        except Exception:
            index = -1

        if index < 0 or index >= len(steps):
            raise RuntimeError(
                "当前内部步骤无法与 SKILL.md 原生计划安全对齐："
                f"{current_step}"
            )
        return index



    async def _ensure_native_plan(
        self,
        *,
        skill_name: str,
        skill_md: str,
        project: dict[str, Any],
        stage: str,
        stage_state: dict[str, Any],
        user_text: str,
    ) -> dict[str, Any]:
        """Return a usable plan without ever blocking Skill execution."""
        source_sha256 = self._skill_source_sha256(skill_md)
        existing = stage_state.get("native_plan")

        # Dynamic cache from the same Skill source is safe to reuse directly.
        if (
            isinstance(existing, dict)
            and existing.get("source_sha256") == source_sha256
            and existing.get("mode") == "dynamic"
        ):
            return existing

        # Sequential cache is only navigation metadata. Revalidate softly;
        # stale or malformed cache is discarded rather than raising.
        if (
            isinstance(existing, dict)
            and existing.get("source_sha256") == source_sha256
            and existing.get("mode") == "sequential"
        ):
            try:
                checked = self._validate_native_plan(
                    skill_md=skill_md,
                    plan=existing,
                )
                existing = dict(existing)
                existing["steps"] = checked.get("steps") or []
                existing["source_sha256"] = source_sha256
                if existing["steps"]:
                    return existing
            except Exception:
                pass

        # Rebuild once. _extract_native_plan itself always returns a usable
        # sequential or dynamic plan and never turns plan ambiguity into a
        # production failure.
        try:
            plan = await self._extract_native_plan(
                skill_name=skill_name,
                skill_md=skill_md,
                project=project,
                stage=stage,
                user_text=user_text,
            )
        except Exception as exc:
            plan = {
                "mode": "dynamic",
                "steps": [],
                "current_index": -1,
                "reason": (
                    "native_plan 辅助层异常，已绕过并继续当前 Skill："
                    + type(exc).__name__
                ),
                "source_sha256": source_sha256,
                "audit": {
                    "valid": True,
                    "reason": "native_plan 不作为生产阻断条件",
                },
            }

        if plan.get("mode") not in {"sequential", "dynamic"}:
            plan = {
                "mode": "dynamic",
                "steps": [],
                "current_index": -1,
                "reason": "native_plan 非法结果已自动降级 dynamic",
                "source_sha256": source_sha256,
                "audit": {
                    "valid": True,
                    "reason": "native_plan 不作为生产阻断条件",
                },
            }

        stage_state["native_plan"] = plan
        return plan

    def _native_target(
        self,
        *,
        plan: dict[str, Any],
        previous_step: str,
        control_event: dict[str, Any],
    ) -> dict[str, Any]:
        action = _clean_text(control_event.get("action"))
        if plan.get("mode") != "sequential":
            return {
                "kind": "skill_driven",
                "index": -1,
                "name": "",
            }

        steps = list(plan.get("steps") or [])
        current_index = int(plan.get("current_index", -1))

        if action == "advance":
            if previous_step and current_index < 0:
                raise RuntimeError(
                    "已有内部步骤但 native_plan 未完成对齐，"
                    "拒绝猜测下一步"
                )
            target_index = current_index + 1
            if target_index >= len(steps):
                return {
                    "kind": "complete_stage",
                    "index": len(steps),
                    "name": "",
                }
            return {
                "kind": "step",
                "index": target_index,
                "name": steps[target_index],
            }

        if current_index >= 0:
            return {
                "kind": "step",
                "index": current_index,
                "name": steps[current_index],
            }

        if steps:
            return {
                "kind": "step",
                "index": 0,
                "name": steps[0],
            }

        return {
            "kind": "skill_driven",
            "index": -1,
            "name": "",
        }

    async def _classify_control_action(
        self,
        *,
        skill_name: str,
        skill_md: str,
        stage_state: dict[str, Any],
        user_text: str,
    ) -> dict[str, Any]:
        previous_step = _clean_text(stage_state.get("internal_step"))
        next_expected = _clean_text(
            stage_state.get("next_expected_action")
        )

        if not previous_step:
            return {
                "action": "other",
                "confidence": 1.0,
                "reason": "当前阶段尚无已输出内部步骤",
            }

        # V2.35.1: these are product/workflow control commands, not business
        # semantics.  They must not depend on an LLM classifier: a literal
        # user approval must always mean advance.
        normalized = re.sub(
            r"[\\s，。！？!?、；;：:（）()【】\\[\\]<>《》“”‘’]+",
            "",
            _clean_text(user_text),
        ).lower()
        if normalized in {
            "通过", "确认", "继续", "下一步", "确认通过",
            "通过继续", "继续下一步", "ok", "okay",
        }:
            return {
                "action": "advance",
                "confidence": 1.0,
                "reason": "产品工作流明确批准指令（确定性解析）",
            }
        if normalized in {"自检", "检查当前结果", "检查结果"}:
            return {
                "action": "self_check",
                "confidence": 1.0,
                "reason": "产品工作流明确自检指令（确定性解析）",
            }

        system_prompt = """你是 Agent Skill 控制动作解析器，不执行创作任务。
必须根据当前生产技能 SKILL.md、上一内部步骤、技能给出的 next_expected_action 和用户本轮消息，判断用户对上一内部步骤的控制意图。
禁止使用代码业务关键词表；只做语义判断。
action 只能是：
- advance：用户接受/确认上一内部步骤，希望按技能进入下一内部步骤
- revise：用户要求修改、补充、重做上一内部步骤
- self_check：用户要求按技能对上一内部步骤自检/审查
- other：上述都不是，按普通任务消息处理

返回严格 JSON，不要 Markdown：
{"action":"advance|revise|self_check|other","confidence":0.0,"reason":"一句话"}"""

        user_prompt = f"""CURRENT_SKILL={skill_name}

=== SKILL.md ===
{skill_md}

=== PREVIOUS_INTERNAL_STEP ===
{previous_step}

=== NEXT_EXPECTED_ACTION ===
{next_expected or "<none>"}

=== USER_MESSAGE ===
{user_text}
"""

        result, parsed, _ = await self._structured_json_call(
            phase="control_action",
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            temperature=0.0,
            max_tokens=300,
            contract=(
                '{"action":"advance|revise|self_check|other",'
                '"confidence":0.0,"reason":"一句话"}'
            ),
        )
        action = _clean_text(parsed.get("action"))
        if action not in {
            "advance",
            "revise",
            "self_check",
            "other",
        }:
            raise ValueError(
                f"非法导演控制动作：{action or '<empty>'}"
            )

        try:
            confidence = float(parsed.get("confidence", 0.0))
        except Exception:
            confidence = 0.0

        return {
            "action": action,
            "confidence": max(0.0, min(1.0, confidence)),
            "reason": _clean_text(parsed.get("reason")),
        }

    def _actual_input_manifest(
        self,
        *,
        project: dict[str, Any],
        stage: str,
        user_text: str,
    ) -> dict[str, Any]:
        current = _clean_text(user_text)

        user_records = [
            _clean_text(item.get("content"))
            for item in project.get("history", [])
            if (
                item.get("stage") == stage
                and item.get("role") == "user"
                and _clean_text(item.get("content"))
            )
        ]
        if current:
            user_records.append(current)

        upstream: list[dict[str, Any]] = []
        for prior_stage in STAGE_ORDER:
            if prior_stage == stage:
                break
            confirmed = (
                project.get("confirmed_outputs", {})
                .get(prior_stage, {})
            )
            handoff = _clean_text(confirmed.get("handoff"))
            if not handoff:
                continue
            audit = confirmed.get("handoff_audit") or {}
            upstream.append({
                "stage": prior_stage,
                "skill": _clean_text(confirmed.get("skill")),
                "handoff_sha256": hashlib.sha256(
                    handoff.encode("utf-8")
                ).hexdigest(),
                "handoff_chars": len(handoff),
                "contract_version": _clean_text(
                    audit.get("contract_version")
                ),
                "provenance_verified": bool(
                    audit.get("provenance_verified")
                ),
            })

        # IMPORTANT:
        # This manifest is an inventory, not a second copy of text context.
        # Raw user text and confirmed handoff text are supplied exactly once
        # in dedicated authoritative prompt sections.
        return {
            "schema_version": "actual_input_manifest_v2",
            "current_stage": stage,
            "current_user_message_sha256": (
                hashlib.sha256(
                    current.encode("utf-8")
                ).hexdigest()
                if current
                else ""
            ),
            "current_user_message_chars": len(current),
            "current_stage_user_message_count": len(user_records),
            "confirmed_upstream_handoffs": upstream,
            "external_assets": [],
            "external_assets_authoritative": True,
            "confirmed_upstream_handoffs_are_project_context": True,
            "external_assets_empty_does_not_invalidate_upstream_text": True,
            "generated_assistant_history_is_input": False,
            "skill_text_is_input": False,
            "manifest_contains_raw_user_text": False,
            "manifest_contains_raw_handoff_text": False,
        }

    def _actual_input_text_context(
        self,
        *,
        project: dict[str, Any],
        stage: str,
        user_text: str,
        upstream_handoffs: str,
        max_user_chars: int = 2200,
    ) -> dict[str, str]:
        user_messages: list[str] = []
        current = _clean_text(user_text)

        for item in project.get("history", []):
            if (
                item.get("stage") == stage
                and item.get("role") == "user"
            ):
                text = _clean_text(item.get("content"))
                if text:
                    user_messages.append(text)
        if current:
            user_messages.append(current)

        # Preserve newest user-authored text while bounding repeated
        # regression/test instructions.  This is only user text; generated
        # assistant history is deliberately excluded.
        selected: list[str] = []
        used = 0
        for text in reversed(user_messages):
            block = f"user: {text}"
            if selected and used + len(block) > max_user_chars:
                break
            if not selected and len(block) > max_user_chars:
                block = (
                    block[:max_user_chars]
                    + "\n[用户文本仅因上下文预算做了显式节选]"
                )
            selected.append(block)
            used += len(block)
        selected.reverse()

        return {
            "current_stage_user_text": "\n\n".join(selected),
            "confirmed_upstream_text": _clean_text(
                upstream_handoffs
            ),
        }




    def _project_origin_brief(
        self,
        *,
        project: dict[str, Any],
        stage: str,
        user_text: str,
    ) -> str:
        """Return the earliest substantive Stage01 user brief as immutable text.

        This is raw user-authored text, not a model summary. It is kept
        separate from generated history so later stages cannot accidentally
        replace the user's original premise with generated reinterpretation.
        """
        candidates: list[str] = []
        for item in project.get("history", []):
            if (
                item.get("stage") == "01"
                and item.get("role") == "user"
            ):
                text = _clean_text(item.get("content"))
                if text:
                    candidates.append(text)

        if not candidates and stage == "01":
            current = _clean_text(user_text)
            if current:
                candidates.append(current)

        for text in candidates:
            if len(text) >= 12:
                return text
        return candidates[0] if candidates else ""

    @staticmethod
    def _deterministic_output_check(
        *,
        content: str,
        native_target: dict[str, Any],
        control: dict[str, Any],
    ) -> dict[str, Any]:
        """Mechanical protocol checks only; no business/content semantics."""
        issues: list[str] = []
        text = _clean_text(content)
        if not text:
            issues.append("empty_content")

        target_kind = _clean_text(native_target.get("kind"))
        target_name = _clean_text(native_target.get("name"))
        internal_step = _clean_text(control.get("internal_step"))
        stage_ready = bool(control.get("stage_ready"))

        if target_kind == "step":
            if internal_step != target_name:
                issues.append(
                    "native_step_mismatch:"
                    f"{target_name}->{internal_step}"
                )
            if stage_ready:
                issues.append("step_cannot_mark_stage_ready")
        elif target_kind == "complete_stage":
            if not stage_ready:
                issues.append("complete_stage_must_be_ready")

        return {"valid": not issues, "issues": issues}


    def _handoff_consumer_skill(
        self,
        stage: str,
    ) -> tuple[str, str]:
        index = STAGE_ORDER.index(stage)
        if index + 1 < len(STAGE_ORDER):
            consumer_stage = STAGE_ORDER[index + 1]
            return (
                consumer_stage,
                STAGE_SKILLS[consumer_stage],
            )
        return ("workflow", WORKFLOW_SKILL)

    def _latest_stage_output(
        self,
        project: dict[str, Any],
        stage: str,
    ) -> str:
        for item in reversed(project.get("history", [])):
            if (
                item.get("stage") == stage
                and item.get("role") == "assistant"
            ):
                content = _clean_text(item.get("content"))
                if content:
                    return content
        return ""

    def _stage_evidence_records(
        self,
        project: dict[str, Any],
        stage: str,
        transient_content: str = "",
    ) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        sequence = 0
        for item in project.get("history", []):
            if (
                item.get("stage") != stage
                or item.get("role") != "assistant"
            ):
                continue
            content = _clean_text(item.get("content"))
            if not content:
                continue
            sequence += 1
            target = item.get("native_target") or {}
            records.append({
                "evidence_id": f"E{sequence:03d}",
                "content": content,
                "sha256": hashlib.sha256(
                    content.encode("utf-8")
                ).hexdigest(),
                "target_kind": _clean_text(
                    target.get("kind")
                ),
                "target_name": _clean_text(
                    target.get("name")
                ),
            })

        transient = _clean_text(transient_content)
        if (
            transient
            and not any(
                record["content"] == transient
                for record in records
            )
        ):
            sequence += 1
            records.append({
                "evidence_id": f"E{sequence:03d}",
                "content": transient,
                "sha256": hashlib.sha256(
                    transient.encode("utf-8")
                ).hexdigest(),
                "target_kind": "current_result",
                "target_name": "",
            })

        return records

    def _stage_evidence_catalog(
        self,
        records: list[dict[str, Any]],
        excerpt_chars: int = 520,
    ) -> str:
        blocks: list[str] = []
        for record in records:
            content = record["content"]
            excerpt = content
            if len(excerpt) > excerpt_chars:
                excerpt = (
                    excerpt[:excerpt_chars]
                    + "\n[目录预览截断]"
                )
            blocks.append(
                " | ".join([
                    record["evidence_id"],
                    (
                        record["target_name"]
                        or record["target_kind"]
                        or "stage_output"
                    ),
                    record["sha256"][:12],
                ])
                + "\n"
                + excerpt
            )
        return "\n\n".join(blocks)

    def _handoff_candidate_records(
        self,
        records: list[dict[str, Any]],
        max_records: int = 8,
    ) -> list[dict[str, Any]]:
        if not records:
            return []

        unique: list[dict[str, Any]] = []
        seen_sha: set[str] = set()
        for record in records:
            digest = _clean_text(record.get("sha256"))
            if not digest or digest in seen_sha:
                continue
            seen_sha.add(digest)
            unique.append(record)

        if len(unique) <= max_records:
            return unique

        # Generic boundary strategy: preserve early-established context and
        # latest concrete outputs without any business-semantic routing.
        chosen_indexes: list[int] = []
        head_count = min(3, len(unique))
        tail_count = min(
            max_records - head_count,
            len(unique) - head_count,
        )

        for index in range(head_count):
            chosen_indexes.append(index)

        tail_start = max(
            head_count,
            len(unique) - tail_count,
        )
        for index in range(tail_start, len(unique)):
            if index not in chosen_indexes:
                chosen_indexes.append(index)

        # If capacity remains, fill from the middle in chronological order.
        for index in range(head_count, tail_start):
            if len(chosen_indexes) >= max_records:
                break
            if index not in chosen_indexes:
                chosen_indexes.append(index)

        chosen_indexes.sort()
        return [unique[index] for index in chosen_indexes]

    def _build_verbatim_handoff(
        self,
        *,
        project: dict[str, Any],
        stage: str,
        consumer_skill: str,
        transient_content: str = "",
        max_chars: int = 6200,
    ) -> tuple[str, dict[str, Any]]:
        records = self._stage_evidence_records(
            project,
            stage,
            transient_content=transient_content,
        )
        candidates = self._handoff_candidate_records(
            records,
            max_records=8,
        )
        if not candidates:
            return "", {
                "valid": False,
                "reason": "上一阶段没有可交接的真实 assistant 产出",
                "missing": ["stage assistant evidence"],
                "consumer_skill": consumer_skill,
                "contract_version": "verbatim_evidence_v1",
                "provenance_verified": False,
                "evidence_count": 0,
                "provenance": [],
            }

        header = (
            "【跨阶段原始证据包】\n"
            "以下内容逐字来自上一阶段已经持久化的 assistant 产出；"
            "平台不做事实改写。下游 Skill 只能依据这些原始证据继续工作，"
            "证据中没有出现的信息视为缺失，不得补造。\n"
        )

        blocks: list[str] = []
        provenance: list[dict[str, Any]] = []
        used = len(header)

        for record in candidates:
            label = (
                record.get("target_name")
                or record.get("target_kind")
                or "stage_output"
            )
            prefix = (
                f"\n【{record['evidence_id']} | "
                f"target={label} | "
                f"sha256={record['sha256']}】\n"
            )
            content = record["content"]
            block = prefix + content + "\n"

            if used + len(block) <= max_chars:
                blocks.append(block)
                used += len(block)
                provenance.append({
                    "evidence_id": record["evidence_id"],
                    "source_sha256": record["sha256"],
                    "mode": "full",
                    "included_chars": len(content),
                    "target_kind": record.get(
                        "target_kind",
                        "",
                    ),
                    "target_name": record.get(
                        "target_name",
                        "",
                    ),
                })

        # Generic fallback for unusually large single records.  The prefix is
        # still an exact source substring and the truncation marker is clearly
        # platform metadata rather than evidence.
        if not provenance:
            record = min(
                candidates,
                key=lambda item: len(item["content"]),
            )
            label = (
                record.get("target_name")
                or record.get("target_kind")
                or "stage_output"
            )
            prefix = (
                f"\n【{record['evidence_id']} | "
                f"target={label} | "
                f"sha256={record['sha256']} | excerpt】\n"
            )
            room = max(
                200,
                max_chars
                - len(header)
                - len(prefix)
                - 64,
            )
            excerpt = record["content"][:room]
            blocks.append(
                prefix
                + excerpt
                + "\n[平台因上下文预算仅保留该证据的逐字前缀]\n"
            )
            provenance.append({
                "evidence_id": record["evidence_id"],
                "source_sha256": record["sha256"],
                "mode": "prefix",
                "included_chars": len(excerpt),
                "included_sha256": hashlib.sha256(
                    excerpt.encode("utf-8")
                ).hexdigest(),
                "target_kind": record.get(
                    "target_kind",
                    "",
                ),
                "target_name": record.get(
                    "target_name",
                    "",
                ),
            })

        handoff = (header + "".join(blocks)).strip()

        # Deterministic verification against the real stage evidence ledger.
        by_id = {
            record["evidence_id"]: record
            for record in records
        }
        verification_errors: list[str] = []
        for item in provenance:
            source = by_id.get(item["evidence_id"])
            if not source:
                verification_errors.append(
                    f"missing evidence_id={item['evidence_id']}"
                )
                continue
            if (
                source["sha256"]
                != item["source_sha256"]
            ):
                verification_errors.append(
                    f"sha mismatch evidence_id={item['evidence_id']}"
                )
                continue
            if item["mode"] == "full":
                if source["content"] not in handoff:
                    verification_errors.append(
                        f"full source not embedded evidence_id={item['evidence_id']}"
                    )
            elif item["mode"] == "prefix":
                length = int(item["included_chars"])
                excerpt = source["content"][:length]
                if excerpt not in handoff:
                    verification_errors.append(
                        f"prefix not embedded evidence_id={item['evidence_id']}"
                    )
                if hashlib.sha256(
                    excerpt.encode("utf-8")
                ).hexdigest() != item.get(
                    "included_sha256"
                ):
                    verification_errors.append(
                        f"prefix sha mismatch evidence_id={item['evidence_id']}"
                    )

        verified = not verification_errors
        return handoff, {
            "valid": bool(handoff) and verified,
            "reason": (
                "跨阶段交接由真实阶段产出逐字确定性构建，未经过 LLM 事实改写"
                if verified
                else "跨阶段原始证据包确定性校验失败"
            ),
            "missing": verification_errors,
            "consumer_skill": consumer_skill,
            "contract_version": "verbatim_evidence_v1",
            "provenance_verified": verified,
            "evidence_count": len(provenance),
            "selected_evidence_ids": [
                item["evidence_id"]
                for item in provenance
            ],
            "bundle_sha256": hashlib.sha256(
                handoff.encode("utf-8")
            ).hexdigest(),
            "provenance": provenance,
        }

    async def _compile_stage_handoff(
        self,
        *,
        project: dict[str, Any],
        stage: str,
        source_skill: str,
        source_skill_md: str,
        consumer_skill: str,
        consumer_skill_md: str,
        final_content: str,
        draft_handoff: str,
        audit_feedback: dict[str, Any] | None = None,
    ) -> tuple[str, dict[str, Any]]:
        # V2.13.8 deliberately does not ask the LLM to rewrite stage facts.
        # The downstream skill receives a bounded verbatim evidence bundle.
        return self._build_verbatim_handoff(
            project=project,
            stage=stage,
            consumer_skill=consumer_skill,
            transient_content=final_content,
            max_chars=6200,
        )

    async def _compile_and_audit_stage_handoff(
        self,
        *,
        project: dict[str, Any],
        stage: str,
        source_skill: str,
        source_skill_md: str,
        consumer_skill: str,
        consumer_skill_md: str,
        final_content: str,
        draft_handoff: str,
    ) -> tuple[str, dict[str, Any]]:
        handoff, audit = await self._compile_stage_handoff(
            project=project,
            stage=stage,
            source_skill=source_skill,
            source_skill_md=source_skill_md,
            consumer_skill=consumer_skill,
            consumer_skill_md=consumer_skill_md,
            final_content=final_content,
            draft_handoff=draft_handoff,
        )
        if not (
            audit.get("valid")
            and audit.get("provenance_verified")
        ):
            raise RuntimeError(
                "跨阶段原始证据包确定性校验失败："
                f"{audit.get('missing')}"
            )
        return handoff, audit

    async def _ensure_incoming_handoff(
        self,
        *,
        project: dict[str, Any],
        stage: str,
    ) -> dict[str, Any]:
        index = STAGE_ORDER.index(stage)
        if index == 0:
            return {
                "checked": False,
                "refreshed": False,
                "reason": "首阶段无上游交接",
            }

        previous_stage = STAGE_ORDER[index - 1]
        if previous_stage not in project.get(
            "completed_stages",
            [],
        ):
            raise RuntimeError(
                f"当前阶段 {stage} 的上游阶段 "
                f"{previous_stage} 尚未确认"
            )

        confirmed = (
            project.get("confirmed_outputs", {})
            .get(previous_stage, {})
        )
        old_handoff = _clean_text(
            confirmed.get("handoff")
        )
        existing_audit = (
            confirmed.get("handoff_audit")
            or {}
        )
        consumer_skill = STAGE_SKILLS[stage]

        if (
            bool(existing_audit.get("valid"))
            and bool(
                existing_audit.get(
                    "provenance_verified"
                )
            )
            and _clean_text(
                existing_audit.get(
                    "contract_version"
                )
            ) == "verbatim_evidence_v1"
            and _clean_text(
                existing_audit.get("consumer_skill")
            ) == consumer_skill
            and old_handoff
        ):
            return {
                "checked": True,
                "refreshed": False,
                "previous_stage": previous_stage,
                "consumer_skill": consumer_skill,
                "handoff_chars": len(old_handoff),
                "audit": existing_audit,
            }

        source_skill = STAGE_SKILLS[previous_stage]
        source_skill_md = self._skill_md(
            source_skill
        )
        consumer_skill_md = self._skill_md(
            consumer_skill
        )
        final_content = self._latest_stage_output(
            project,
            previous_stage,
        )

        handoff, audit = (
            await self._compile_and_audit_stage_handoff(
                project=project,
                stage=previous_stage,
                source_skill=source_skill,
                source_skill_md=source_skill_md,
                consumer_skill=consumer_skill,
                consumer_skill_md=consumer_skill_md,
                final_content=final_content,
                draft_handoff=old_handoff,
            )
        )

        confirmed["handoff"] = handoff
        confirmed["handoff_audit"] = audit
        confirmed["refreshed_at"] = _utcnow()
        project["confirmed_outputs"][
            previous_stage
        ] = confirmed

        previous_state = (
            project.get("stage_state", {})
            .get(previous_stage, {})
        )
        previous_state["handoff"] = handoff
        previous_state["last_handoff_audit"] = audit

        project["updated_at"] = _utcnow()
        self._save_project(project)

        return {
            "checked": True,
            "refreshed": True,
            "previous_stage": previous_stage,
            "consumer_skill": consumer_skill,
            "old_handoff_chars": len(old_handoff),
            "handoff_chars": len(handoff),
            "audit": audit,
        }


    async def _plan_references(
        self,
        *,
        skill_name: str,
        skill_md: str,
        project: dict[str, Any],
        user_text: str,
        control_event: dict[str, Any] | None = None,
        native_target: dict[str, Any] | None = None,
    ) -> tuple[list[str], str]:
        allowed = self._available_files(skill_name)
        allowed_text = "\n".join(f"- {item}" for item in allowed) or "- <none>"

        system_prompt = """你是 Agent Skill 引用路由器，不负责回答用户任务。
必须以当前 SKILL.md 原文为唯一业务路由依据，不得凭关键词表或自行发明规则。
只允许选择 ALLOWED_FILES 中真实存在的文件。
SKILL.md 明确要求/按需要求读取引用时，按用户当前任务和项目状态选择必要文件。
不要为了“更全面”加载无关文件。
返回严格 JSON，不要 Markdown，不要解释：
{"required_files":["relative/path.md"],"reason":"一句话说明"}
required_files 可以为空，最多 8 个。"""

        user_prompt = f"""CURRENT_SKILL={skill_name}

=== SKILL.md（完整） ===
{skill_md}

=== ALLOWED_FILES ===
{allowed_text}

=== PROJECT_CONTROL_STATE ===
{json.dumps(self._compact_stage_state(project.get("stage_state", {}).get(project["current_stage"], {})), ensure_ascii=False)}

=== CONTROL_EVENT ===
{json.dumps(control_event or {"action": "other"}, ensure_ascii=False)}

=== NATIVE_TARGET ===
{json.dumps(native_target or {"kind": "skill_driven"}, ensure_ascii=False)}

=== PRIOR_CONFIRMED_HANDOFFS ===
{self._prior_handoffs(project, max_chars=2000) or "<none>"}

=== USER_MESSAGE ===
{user_text}
"""

        result, parsed, _ = await self._structured_json_call(
            phase="reference_router",
            messages=[{"role": "user", "content": user_prompt}],
            system_prompt=system_prompt,
            temperature=0.1,
            max_tokens=900,
            contract=(
                '{"required_files":["relative/path.md"],'
                '"reason":"一句话说明"}'
            ),
        )
        requested = parsed.get("required_files", [])
        if not isinstance(requested, list):
            raise ValueError("required_files 必须是数组")
        if len(requested) > 8:
            raise ValueError("required_files 超过 8 个")

        allowed_set = set(allowed)
        normalized: list[str] = []
        for item in requested:
            rel = _clean_text(item).replace("\\", "/")
            if not rel:
                continue
            if rel not in allowed_set:
                raise ValueError(f"LLM 选择了不存在的技能引用：{rel}")
            if rel not in normalized:
                normalized.append(rel)

        return normalized, _clean_text(parsed.get("reason"))

    def _build_execution_user_prompt(
        self,
        *,
        source_blocks: list[str],
        required_files: list[str],
        route_reason: str,
        control_event: dict[str, Any],
        native_plan: dict[str, Any],
        native_target: dict[str, Any],
        approved_steps: list[str],
        actual_input_manifest: dict[str, Any],
        prior_handoffs: str,
        compact_stage_state: dict[str, Any],
        history_context: str,
        user_text: str,
    ) -> tuple[str, dict[str, Any]]:
        source_text = "\n".join(source_blocks)
        manifest_text = json.dumps(
            actual_input_manifest,
            ensure_ascii=False,
        )
        control_text = json.dumps(
            control_event,
            ensure_ascii=False,
        )
        plan_text = json.dumps(
            native_plan,
            ensure_ascii=False,
        )
        target_text = json.dumps(
            native_target,
            ensure_ascii=False,
        )
        approved_text = json.dumps(
            approved_steps,
            ensure_ascii=False,
        )
        state_text = json.dumps(
            compact_stage_state,
            ensure_ascii=False,
        )
        handoff_text = _clean_text(prior_handoffs)
        history_text = _clean_text(history_context)
        user_message = _clean_text(user_text)

        prompt = f"""=== SOURCE FILES ===
{source_text}

=== ROUTE DECISION ===
required_files={json.dumps(required_files, ensure_ascii=False)}
reason={route_reason}

=== CONTROL_EVENT ===
{control_text}

=== NATIVE_PLAN ===
{plan_text}

=== NATIVE_TARGET ===
{target_text}

=== APPROVED_NATIVE_STEPS ===
{approved_text}

=== ACTUAL_INPUT_MANIFEST (METADATA ONLY) ===
{manifest_text}

=== AUTHORITATIVE PREVIOUS CONFIRMED STAGE TEXT ===
{handoff_text or "<none>"}

=== GENERATED CURRENT-STAGE HISTORY NOTICE ===
The CURRENT STAGE HISTORY below is generated history, not proof that any external input exists.

=== CURRENT STAGE CONTROL MEMORY ===
{state_text}

=== RECENT CURRENT-STAGE HISTORY ===
{history_text or "<none>"}

=== AUTHORITATIVE CURRENT USER MESSAGE ===
{user_message}
"""

        # These diagnostics make accidental context duplication observable.
        manifest_contains_handoff = bool(
            handoff_text
            and handoff_text in manifest_text
        )
        manifest_contains_user = bool(
            user_message
            and user_message in manifest_text
        )
        pack_meta = {
            "schema_version": "single_context_pack_v1",
            "source_chars": len(source_text),
            "manifest_chars": len(manifest_text),
            "upstream_handoff_chars": len(handoff_text),
            "history_chars": len(history_text),
            "current_user_chars": len(user_message),
            "manifest_contains_raw_handoff": (
                manifest_contains_handoff
            ),
            "manifest_contains_raw_current_user": (
                manifest_contains_user
            ),
            "upstream_handoff_sha256": (
                hashlib.sha256(
                    handoff_text.encode("utf-8")
                ).hexdigest()
                if handoff_text
                else ""
            ),
            "current_user_sha256": (
                hashlib.sha256(
                    user_message.encode("utf-8")
                ).hexdigest()
                if user_message
                else ""
            ),
        }
        if manifest_contains_handoff:
            raise RuntimeError(
                "single_context_pack: ACTUAL_INPUT_MANIFEST "
                "意外重复包含完整上游 handoff"
            )
        if manifest_contains_user:
            raise RuntimeError(
                "single_context_pack: ACTUAL_INPUT_MANIFEST "
                "意外重复包含当前用户原文"
            )
        return prompt, pack_meta


    async def message(
        self,
        project_id: str,
        user_text: str,
    ) -> dict[str, Any]:
        """Execute the current production Skill without business guard/judge layers."""
        async with self._lock(project_id):
            project = self.get_project(project_id)
            if project.get("status") != "active":
                raise RuntimeError("导演项目已完成，不能继续写入")
            self._ensure_project_production_history(project)

            stage = _clean_text(project.get("current_stage"))
            if stage not in STAGE_SKILLS:
                raise RuntimeError(f"非法当前阶段：{stage}")

            skill_name = STAGE_SKILLS[stage]
            skill_md = self._skill_md(skill_name)

            incoming_handoff_refresh = await self._ensure_incoming_handoff(
                project=project,
                stage=stage,
            )

            stage_state = project["stage_state"][stage]
            stage_state.setdefault("approved_steps", [])
            stage_state.setdefault("last_control_action", "")
            stage_state.setdefault("native_plan", {})
            stage_state.setdefault("skill_contract", {})
            stage_state.setdefault("skill_runtime", empty_runtime_state())
            skill_contract = await self._ensure_skill_contract(
                skill_name=skill_name,
                skill_md=skill_md,
                stage_state=stage_state,
            )
            previous_step = _clean_text(stage_state.get("internal_step"))

            control_event = await self._classify_control_action(
                skill_name=skill_name,
                skill_md=skill_md,
                stage_state=stage_state,
                user_text=user_text,
            )
            native_plan = await self._ensure_native_plan(
                skill_name=skill_name,
                skill_md=skill_md,
                project=project,
                stage=stage,
                stage_state=stage_state,
                user_text=user_text,
            )
            native_target = self._native_target(
                plan=native_plan,
                previous_step=previous_step,
                control_event=control_event,
            )

            required_files, route_reason = await self._plan_references(
                skill_name=skill_name,
                skill_md=skill_md,
                project=project,
                user_text=user_text,
                control_event=control_event,
                native_target=native_target,
            )
            actual_input_manifest = self._actual_input_manifest(
                project=project,
                stage=stage,
                user_text=user_text,
            )
            origin_brief = self._project_origin_brief(
                project=project,
                stage=stage,
                user_text=user_text,
            )
            compact_stage_state = self._compact_stage_state(stage_state)

            limit = int(self.settings.director_source_context_max_chars)
            reference_budget_chars = min(9000, max(3500, limit - len(skill_md)))

            system_prompt = f"""你正在执行“船长AI视界”生产技能 {skill_name}。

业务来源只有：
1. 当前完整 SKILL.md；
2. SKILL.md 本轮需要的 reference；
3. 用户当前输入、项目最初需求、已经确认的上游原始交接。

平台只负责阶段编排、真实输入清单、上下文预算、Skill Contract、项目资产登记/版本/关系和持久化，不增加额外业务审核、禁词表、事实审稿器或二次语义裁判。
ACTUAL_INPUT_MANIFEST 只描述真实外部输入是否存在；不要把示例、模板或历史文字当成用户上传的附件。
SKILL_CONTRACT 是系统从当前 SKILL.md 编译出的“最终交付/条件规则索引”，来源仍然只有 SKILL.md，不是新增业务规则。
如果 SKILL_RUNTIME_STATUS 显示缺少当前 Skill 明确要求的终态产物，本轮应直接完成该产物，而不是只解释目标、步骤或下一步计划。

当前只执行阶段 {stage}/04。
NATIVE_TARGET.kind=step 时完成当前原生步骤本身；complete_stage 时按 Skill 收口本阶段并补齐 Skill Contract 中仍缺的最终产物；skill_driven 时按完整 Skill 动态执行。
输出用户真正需要的当前 Skill 生产内容，不输出平台内部字段、JSON 控制数据或运行状态。"""

            content_budget = None
            user_prompt = ""
            reference_context_meta: dict[str, Any] = {}
            context_pack_meta: dict[str, Any] = {}
            history_context = ""
            prior_handoffs = ""
            prior_assets = ""

            pack_attempts = (
                {"history_chars":1600,"reference_chars":reference_budget_chars,"handoff_chars":6000},
                {"history_chars":700,"reference_chars":min(reference_budget_chars,6500),"handoff_chars":5500},
                {"history_chars":0,"reference_chars":min(reference_budget_chars,5000),"handoff_chars":5000},
                {"history_chars":0,"reference_chars":min(reference_budget_chars,3600),"handoff_chars":3800},
            )

            for attempt_index, attempt in enumerate(pack_attempts):
                history_chars = int(attempt["history_chars"])
                ref_chars = int(attempt["reference_chars"])
                handoff_chars = int(attempt["handoff_chars"])
                history_context = (
                    self._history_context(project, stage, max_chars=history_chars)
                    if history_chars > 0 else ""
                )
                prior_handoffs = self._prior_handoffs(project, max_chars=handoff_chars)
                prior_assets = self._prior_asset_manifest(project, max_chars=min(4500, handoff_chars))
                reference_blocks, reference_context_meta = await self._build_reference_context(
                    skill_name=skill_name,
                    skill_md=skill_md,
                    required_files=required_files,
                    native_target=native_target,
                    user_text=user_text,
                    stage_state=stage_state,
                    route_reason=route_reason,
                    max_chars=ref_chars,
                )
                source_blocks = [f"### SKILL.md\n{skill_md}", *reference_blocks]
                user_prompt = f"""=== SOURCE FILES ===
{chr(10).join(source_blocks)}

=== ROUTE DECISION ===
required_files={json.dumps(required_files, ensure_ascii=False)}
reason={route_reason}

=== NATIVE_TARGET ===
{json.dumps(native_target, ensure_ascii=False)}

=== CONTROL_EVENT ===
{json.dumps(control_event, ensure_ascii=False)}

=== ACTUAL_INPUT_MANIFEST ===
{json.dumps(actual_input_manifest, ensure_ascii=False)}

=== AUTHORITATIVE PREVIOUS CONFIRMED STAGE TEXT ===
{prior_handoffs or "<none>"}

=== PREVIOUS CONFIRMED PRODUCTION ASSET MANIFEST ===
{prior_assets or "<none>"}

=== SKILL_CONTRACT (COMPILED FROM CURRENT SKILL.md) ===
{json.dumps(compact_contract(skill_contract), ensure_ascii=False)}

=== SKILL_RUNTIME_STATUS ===
{json.dumps(stage_state.get("skill_runtime") or empty_runtime_state(), ensure_ascii=False)}

=== CURRENT STAGE CONTROL MEMORY ===
{json.dumps(compact_stage_state, ensure_ascii=False)}

=== RECENT CURRENT-STAGE HISTORY ===
{history_context or "<none>"}

=== ORIGINAL PROJECT REQUEST ===
{origin_brief or "<none>"}

=== CURRENT USER MESSAGE ===
{_clean_text(user_text)}

Execute the current production Skill directly. Do not add platform-specific review rules that are not in the Skill or user request.
"""
                context_pack_meta = {
                    "schema_version":"director_orchestrator_context_v1",
                    "attempt_index":attempt_index,
                    "skill_chars":len(skill_md),
                    "reference_chars":sum(len(x) for x in reference_blocks),
                    "upstream_handoff_chars":len(prior_handoffs),
                    "upstream_asset_manifest_chars":len(prior_assets),
                    "history_chars":len(history_context),
                    "current_user_chars":len(_clean_text(user_text)),
                    "origin_brief_chars":len(origin_brief),
                }
                try:
                    content_budget = await self._llm_call_budget(
                        phase="director_orchestrator_content",
                        system_prompt=system_prompt,
                        messages=[{"role":"user","content":user_prompt}],
                        requested_output_tokens=3200,
                        minimum_output_tokens=800,
                        safety_tokens=256,
                    )
                    context_pack_meta.update({
                        "prompt_tokens":content_budget["prompt_tokens"],
                        "output_tokens":content_budget["output_tokens"],
                        "context_window":content_budget["context_window"],
                    })
                    break
                except RuntimeError:
                    content_budget = None


            if content_budget is None:
                # V2.27.3: context budget is a runtime packing concern, not a
                # business gate. Keep the current Skill authoritative and
                # progressively remove auxiliary context instead of stopping
                # the production turn.
                emergency_system_prompt = f"""执行当前生产 Skill：{skill_name}。
当前阶段：{stage}/04。

规则：
1. 当前 Skill 是业务来源；只执行当前阶段。
2. 使用已经确认的上游事实和当前用户输入，不补造缺失事实。
3. 不输出平台内部 JSON、Runtime、审计或调度字段。
4. 当前输入要求与 Skill 冲突时，以用户明确要求和 Skill 的真实适用规则执行。
5. 上下文压缩只影响历史/引用广度/平台辅助元数据，不把缺失内容假装成已知内容。"""

                emergency_attempts = (
                    {
                        "reference_chars": 1400,
                        "handoff_chars": 1800,
                        "origin_chars": 1000,
                        "include_manifest": True,
                        "minimum_output_tokens": 384,
                    },
                    {
                        "reference_chars": 700,
                        "handoff_chars": 1200,
                        "origin_chars": 700,
                        "include_manifest": True,
                        "minimum_output_tokens": 320,
                    },
                    {
                        "reference_chars": 0,
                        "handoff_chars": 900,
                        "origin_chars": 500,
                        "include_manifest": False,
                        "minimum_output_tokens": 256,
                    },
                    {
                        "reference_chars": 0,
                        "handoff_chars": 0,
                        "origin_chars": 0,
                        "include_manifest": False,
                        "minimum_output_tokens": 192,
                    },
                )

                for emergency_index, emergency in enumerate(
                    emergency_attempts
                ):
                    emergency_ref_chars = int(
                        emergency["reference_chars"]
                    )
                    emergency_handoff_chars = int(
                        emergency["handoff_chars"]
                    )
                    emergency_origin_chars = int(
                        emergency["origin_chars"]
                    )

                    emergency_refs: list[str] = []
                    emergency_ref_meta: dict[str, Any] = {
                        "mode": "omitted_for_context_budget",
                        "reference_budget_chars": 0,
                    }
                    if (
                        emergency_ref_chars > 0
                        and required_files
                    ):
                        try:
                            (
                                emergency_refs,
                                emergency_ref_meta,
                            ) = await self._build_reference_context(
                                skill_name=skill_name,
                                skill_md=skill_md,
                                required_files=required_files,
                                native_target=native_target,
                                user_text=user_text,
                                stage_state=stage_state,
                                route_reason=route_reason,
                                max_chars=emergency_ref_chars,
                            )
                        except Exception:
                            emergency_refs = []
                            emergency_ref_meta = {
                                "mode": "reference_pack_unavailable",
                                "reference_budget_chars": 0,
                            }

                    emergency_handoff = (
                        self._prior_handoffs(
                            project,
                            max_chars=emergency_handoff_chars,
                        )
                        if emergency_handoff_chars > 0
                        else ""
                    )
                    emergency_origin = (
                        origin_brief[:emergency_origin_chars]
                        if emergency_origin_chars > 0
                        else ""
                    )
                    emergency_manifest = (
                        json.dumps(
                            actual_input_manifest,
                            ensure_ascii=False,
                        )
                        if bool(emergency["include_manifest"])
                        else "<omitted for context budget>"
                    )

                    emergency_sources = [
                        f"### CURRENT SKILL.md\n{skill_md}",
                        *emergency_refs,
                    ]
                    emergency_user_prompt = f"""=== CURRENT SKILL SOURCE ===
{chr(10).join(emergency_sources)}

=== CURRENT TARGET ===
{json.dumps(native_target, ensure_ascii=False)}

=== CURRENT USER INPUT ===
{_clean_text(user_text)}

=== CONFIRMED UPSTREAM FACTS ===
{emergency_handoff or "<none>"}

=== ORIGINAL PROJECT REQUEST (BOUNDED) ===
{emergency_origin or "<omitted>"}

=== ACTUAL INPUT MANIFEST ===
{emergency_manifest}

直接执行当前 Skill 的当前任务。不要解释上下文压缩，不要输出平台状态。
"""

                    try:
                        emergency_budget = await self._llm_call_budget(
                            phase="director_orchestrator_content_soft_pack",
                            system_prompt=emergency_system_prompt,
                            messages=[{
                                "role": "user",
                                "content": emergency_user_prompt,
                            }],
                            requested_output_tokens=2400,
                            minimum_output_tokens=int(
                                emergency[
                                    "minimum_output_tokens"
                                ]
                            ),
                            safety_tokens=128,
                        )
                    except RuntimeError:
                        continue

                    system_prompt = emergency_system_prompt
                    user_prompt = emergency_user_prompt
                    content_budget = emergency_budget
                    reference_context_meta = emergency_ref_meta
                    prior_handoffs = emergency_handoff
                    prior_assets = ""
                    history_context = ""
                    context_pack_meta = {
                        "schema_version":
                            "director_orchestrator_context_v2_soft_pack",
                        "mode": "full_skill_soft_pack",
                        "attempt_index": emergency_index,
                        "skill_chars": len(skill_md),
                        "reference_chars": sum(
                            len(x) for x in emergency_refs
                        ),
                        "upstream_handoff_chars":
                            len(emergency_handoff),
                        "upstream_asset_manifest_chars": 0,
                        "history_chars": 0,
                        "origin_brief_chars":
                            len(emergency_origin),
                        "current_user_chars":
                            len(_clean_text(user_text)),
                        "prompt_tokens":
                            emergency_budget["prompt_tokens"],
                        "output_tokens":
                            emergency_budget["output_tokens"],
                        "context_window":
                            emergency_budget["context_window"],
                        "soft_fallback": True,
                    }
                    break

            if content_budget is None:
                # Last-resort compact Skill execution. The contract was
                # compiled from the current SKILL.md and is already part of
                # the platform's Skill Runtime. This path is explicit in
                # metadata and is never presented as "full Skill source".
                contract_text = json.dumps(
                    compact_contract(skill_contract),
                    ensure_ascii=False,
                )
                compact_system_prompt = f"""执行阶段 {stage}/04 的当前生产 Skill {skill_name}。
当前模型上下文不足以同时容纳完整 Skill 文本和生产输出空间。
使用当前 Skill 已编译的 Skill Contract、当前用户输入和已确认上游事实继续执行。
不得创造 Contract 或输入中不存在的交付要求，不得输出平台内部字段。"""

                compact_handoff = self._prior_handoffs(
                    project,
                    max_chars=1000,
                )
                compact_user_prompt = f"""=== CURRENT SKILL CONTRACT ===
{contract_text}

=== CURRENT TARGET ===
{json.dumps(native_target, ensure_ascii=False)}

=== CURRENT USER INPUT ===
{_clean_text(user_text)}

=== CONFIRMED UPSTREAM FACTS ===
{compact_handoff or "<none>"}

基于以上真实来源完成当前 Skill 当前应交付内容。
"""
                compact_budget = None
                for compact_minimum in (192, 128, 96):
                    try:
                        compact_budget = await self._llm_call_budget(
                            phase="director_orchestrator_content_contract_pack",
                            system_prompt=compact_system_prompt,
                            messages=[{
                                "role": "user",
                                "content": compact_user_prompt,
                            }],
                            requested_output_tokens=1800,
                            minimum_output_tokens=compact_minimum,
                            safety_tokens=96,
                        )
                        break
                    except RuntimeError:
                        compact_budget = None

                if compact_budget is not None:
                    system_prompt = compact_system_prompt
                    user_prompt = compact_user_prompt
                    content_budget = compact_budget
                    prior_handoffs = compact_handoff
                    prior_assets = ""
                    history_context = ""
                    reference_context_meta = {
                        "mode": "contract_fallback",
                        "reference_budget_chars": 0,
                    }
                    context_pack_meta = {
                        "schema_version":
                            "director_orchestrator_context_v2_soft_pack",
                        "mode": "skill_contract_fallback",
                        "skill_chars": len(skill_md),
                        "contract_chars": len(contract_text),
                        "reference_chars": 0,
                        "upstream_handoff_chars":
                            len(compact_handoff),
                        "upstream_asset_manifest_chars": 0,
                        "history_chars": 0,
                        "current_user_chars":
                            len(_clean_text(user_text)),
                        "prompt_tokens":
                            compact_budget["prompt_tokens"],
                        "output_tokens":
                            compact_budget["output_tokens"],
                        "context_window":
                            compact_budget["context_window"],
                        "soft_fallback": True,
                    }

            if content_budget is None:
                # This is no longer a policy/validation failure: there is
                # physically not enough model context even for the compact
                # current-Skill contract plus the current user input.
                raise RuntimeError(
                    "director_orchestrator_content: 当前模型上下文过小，"
                    "连 Skill Contract + 当前输入的最小执行包也无法容纳；"
                    "请提高模型 context window。"
                )

            execution_result = await self.llm.chat(
                messages=[{"role":"user","content":user_prompt}],
                system_prompt=system_prompt,
                temperature=0.50,
                max_tokens=content_budget["output_tokens"],
            )
            execution_result = dict(execution_result)
            execution_result["context_budget"] = content_budget
            content = _clean_text(execution_result.get("content"))
            if not content:
                raise RuntimeError("Director 当前 Skill 返回空内容")

            control_system_prompt = """你是 Agent Skill 平台控制元数据编译器。
只根据已经生成的 EXECUTION_CONTENT、完整 SKILL.md、SKILL_CONTRACT、ACTUAL_INPUT_MANIFEST、CONTROL_EVENT、NATIVE_PLAN、NATIVE_TARGET 和上轮控制状态生成 JSON 元数据。
不能改写、评价、审核或否决 EXECUTION_CONTENT，也不能新增创作事实。

系统现在有 Skill Runtime。你只做结构登记：
1. NATIVE_TARGET.kind=step：internal_step=NATIVE_TARGET.name。
2. output group / artifact / requirement 的 ID 只能来自 SKILL_CONTRACT。
3. artifact_receipts 只有在 EXECUTION_CONTENT 中已经实际出现该交付物时才能登记；不能把“准备生成、目标、执行步骤、下一步、说明”登记成最终产物。evidence_quote 必须逐字连续来自 EXECUTION_CONTENT，并尽量包含交付物本体。
4. conditional requirement 是否激活，只按 SKILL_CONTRACT 的 source_quote + ACTUAL_INPUT_MANIFEST + 当前真实任务判断；不得发明新条件。已满足时给 requirement_receipts，evidence_quote 必须逐字来自 EXECUTION_CONTENT。
5. dynamic/skill_driven 模式下，只有本轮已实际完成所选输出形态的终态交付，stage_complete_claim 才为 true。
6. sequential 模式的最终完成点由 NATIVE_TARGET=complete_stage 决定，stage_complete_claim 仅作记录。
7. production_entities 只做实体抽取，不评价内容。只有 EXECUTION_CONTENT 明确出现的人物/场景/镜头/片段/其他可复用实体才登记；name 和 evidence_quote 必须逐字来自正文，entity_type 使用稳定通用类别（character/scene/shot/clip/generic），metadata 只放正文直接可读出的结构信息。
8. stage_memory 只保存下一轮必要控制事实。handoff 不负责重写事实，平台完成阶段后另做逐字证据交接。

只输出严格 JSON：
{
  "internal_step":"步骤名",
  "stage_memory":"紧凑事实记忆",
  "handoff":"",
  "next_expected_action":"下一步用户动作",
  "skill_runtime":{
    "selected_output_group_ids":[],
    "active_requirement_ids":[],
    "artifact_receipts":[{"artifact_id":"A001","evidence_quote":"正文逐字片段"}],
    "requirement_receipts":[{"requirement_id":"R001","evidence_quote":"正文逐字片段"}],
    "stage_complete_claim":false
  },
  "production_entities":[{"entity_type":"character","name":"正文逐字名称","evidence_quote":"正文逐字片段","metadata":{}}]
}"""

            control_user_prompt = f"""CURRENT_SKILL={skill_name}

=== SKILL.md ===
{skill_md}

=== SKILL_CONTRACT ===
{json.dumps(compact_contract(skill_contract), ensure_ascii=False)}

=== ACTUAL_INPUT_MANIFEST ===
{json.dumps(actual_input_manifest, ensure_ascii=False)}

=== CONTROL_EVENT ===
{json.dumps(control_event, ensure_ascii=False)}

=== NATIVE_PLAN ===
{json.dumps(native_plan, ensure_ascii=False)}

=== NATIVE_TARGET ===
{json.dumps(native_target, ensure_ascii=False)}

=== PREVIOUS_STAGE_STATE ===
{json.dumps(compact_stage_state, ensure_ascii=False)}

=== PREVIOUS_SKILL_RUNTIME ===
{json.dumps(stage_state.get("skill_runtime") or empty_runtime_state(), ensure_ascii=False)}

=== EXECUTION_CONTENT ===
{content}
"""
            control_result, control, control_repaired = await self._structured_json_call(
                phase="director_orchestrator_control",
                messages=[{"role":"user","content":control_user_prompt}],
                system_prompt=control_system_prompt,
                temperature=0.0,
                max_tokens=1800,
                contract=(
                    '{"internal_step":"步骤名","stage_memory":"紧凑事实记忆",'
                    '"handoff":"","next_expected_action":"下一步用户动作",'
                    '"skill_runtime":{"selected_output_group_ids":[],'
                    '"active_requirement_ids":[],"artifact_receipts":[],'
                    '"requirement_receipts":[],"stage_complete_claim":false},'
                    '"production_entities":[]}'
                ),
            )

            target_kind = _clean_text(native_target.get("kind"))
            if target_kind == "step":
                control["internal_step"] = _clean_text(native_target.get("name"))
                control["handoff"] = ""
            elif target_kind == "complete_stage":
                steps = native_plan.get("steps") or []
                if steps:
                    control["internal_step"] = _clean_text(steps[-1])

            turn_id = secrets.token_hex(8)
            runtime_control = control.get("skill_runtime") or {}
            runtime_state = update_runtime_state(
                contract=skill_contract,
                previous=stage_state.get("skill_runtime") or empty_runtime_state(),
                content=content,
                control_runtime=runtime_control,
                native_target=native_target,
                native_plan=native_plan,
                turn_id=turn_id,
            )

            # Every real Director output becomes a persisted project asset.
            # Contract receipts are then materialized into versioned assets;
            # media completion can only be satisfied by real READY task/file
            # assets, never by a text claim.
            parent_asset_ids = []
            for prev_stage in project.get("completed_stages") or []:
                confirmed = (project.get("confirmed_outputs") or {}).get(prev_stage) or {}
                parent_asset_ids.extend(confirmed.get("production_asset_ids") or [])
            turn_asset = self.production.materialize_turn_output(
                project_id,
                stage=stage,
                skill=skill_name,
                turn_id=turn_id,
                content=content,
                native_target=native_target,
                parent_asset_ids=parent_asset_ids[-32:],
            )
            entities = self.production.record_control_entities(
                project_id,
                stage=stage,
                skill=skill_name,
                content=content,
                turn_id=turn_id,
                raw_entities=control.get("production_entities") or [],
                turn_asset_id=turn_asset["asset_id"],
            )
            runtime_state, _ = self.production.materialize_contract_receipts(
                project_id,
                stage=stage,
                skill=skill_name,
                contract=skill_contract,
                runtime_state=runtime_state,
                turn_asset_id=turn_asset["asset_id"],
            )
            asset_readiness = self.production.contract_asset_readiness(
                project_id, stage, skill_contract
            )
            runtime_state = apply_asset_completion(
                contract=skill_contract,
                runtime_state=runtime_state,
                control_runtime=runtime_control,
                native_target=native_target,
                native_plan=native_plan,
                asset_readiness=asset_readiness,
            )
            self.production.ensure_contract_placeholders(
                project_id,
                stage=stage,
                skill=skill_name,
                contract=skill_contract,
                runtime_state=runtime_state,
            )
            control["stage_ready"] = bool(
                (runtime_state.get("completion") or {}).get("ready")
            )
            if not control["stage_ready"]:
                control["handoff"] = ""

            runtime_check = self._deterministic_output_check(
                content=content,
                native_target=native_target,
                control=control,
            )
            if not runtime_check.get("valid"):
                raise RuntimeError(
                    "Director 机械协议校验失败；本轮未写入："
                    + json.dumps(runtime_check, ensure_ascii=False)
                )

            handoff_audit = {
                "valid": True,
                "reason": "当前阶段尚未完成，不生成跨阶段交接",
                "missing": [],
                "consumer_skill": "",
            }
            if bool(control.get("stage_ready")):
                consumer_stage, consumer_skill = self._handoff_consumer_skill(stage)
                consumer_skill_md = self._skill_md(consumer_skill)
                compiled_handoff, handoff_audit = await self._compile_and_audit_stage_handoff(
                    project=project,
                    stage=stage,
                    source_skill=skill_name,
                    source_skill_md=skill_md,
                    consumer_skill=consumer_skill,
                    consumer_skill_md=consumer_skill_md,
                    final_content=content,
                    draft_handoff="",
                )
                handoff_audit["consumer_stage"] = consumer_stage
                control["handoff"] = compiled_handoff

            stage_memory = _clean_text(control.get("stage_memory"))
            if len(stage_memory) > 7000:
                stage_memory = stage_memory[:7000]
            handoff = _clean_text(control.get("handoff"))
            if len(handoff) > 12000:
                handoff = handoff[:12000]

            project["history"].append({
                "stage":stage,"role":"user","content":user_text,"created_at":_utcnow(),
            })
            project["history"].append({
                "stage":stage,"role":"assistant","content":content,
                "model":execution_result.get("model"),
                "required_files":required_files,"native_target":native_target,
                "turn_id": turn_id,
                "skill_runtime_completion": runtime_state.get("completion") or {},
                "production_asset_id": turn_asset.get("asset_id"),
                "production_entity_ids": [x.get("entity_id") for x in entities],
                "created_at":_utcnow(),
            })

            approved_steps = list(stage_state.get("approved_steps", []))
            if (
                control_event.get("action") == "advance"
                and previous_step
                and previous_step not in approved_steps
            ):
                approved_steps.append(previous_step)
            if native_plan.get("mode") == "sequential" and native_target.get("kind") == "step":
                native_plan["current_index"] = int(native_target["index"])

            stage_state.update({
                "internal_step":_clean_text(control.get("internal_step")),
                "stage_memory":stage_memory,
                "stage_ready":bool(control.get("stage_ready")),
                "handoff":handoff,
                "next_expected_action":_clean_text(control.get("next_expected_action")),
                "last_required_files":required_files,
                "approved_steps":approved_steps,
                "last_control_action":_clean_text(control_event.get("action")),
                "last_runtime_check":runtime_check,
                "last_handoff_audit":handoff_audit,
                "native_plan":native_plan,
                "skill_contract": skill_contract,
                "skill_runtime": runtime_state,
                "last_native_target": native_target,
                "last_skill_runtime_control": runtime_control,
            })
            for stale_key in (
                "last_artifact_audit","last_input_audit","last_branch_audit","last_skill_branch"
            ):
                stage_state.pop(stale_key, None)

            project["updated_at"] = _utcnow()
            self._save_project(project)

            return {
                "project_id":project_id,
                "stage":stage,
                "skill":skill_name,
                "content":content,
                "control":stage_state,
                "required_files":required_files,
                "route_reason":route_reason,
                "control_event":control_event,
                "native_plan":native_plan,
                "native_target":native_target,
                "actual_input_manifest":actual_input_manifest,
                "skill_contract": skill_contract,
                "skill_runtime": runtime_state,
                "production": {
                    "turn_asset": turn_asset,
                    "entity_ids": [x.get("entity_id") for x in entities],
                    "stage_status": self.production.stage_status(project_id, stage),
                },
                "runtime_check":runtime_check,
                "handoff_audit":handoff_audit,
                "incoming_handoff_refresh":incoming_handoff_refresh,
                "execution_transport":{
                    "production_calls":1,
                    "business_audit_calls":0,
                    "semantic_guard_calls":0,
                    "repair_calls":0,
                    "control_compiler_calls":1,
                    "content_mode":"plain_text",
                    "control_mode":"structured_json",
                    "control_json_repaired":bool(control_repaired),
                    "context_budget":execution_result.get("context_budget", content_budget),
                    "reference_context":reference_context_meta,
                    "context_pack":context_pack_meta,
                },
                "model":execution_result.get("model"),
                "control_model":control_result.get("model"),
            }


    async def confirm_stage(self, project_id: str) -> dict[str, Any]:
        async with self._lock(project_id):
            project = self.get_project(project_id)
            if project.get("status") != "active":
                raise RuntimeError("导演项目已经完成")

            stage = _clean_text(project.get("current_stage"))
            state = project.get("stage_state", {}).get(stage, {})
            runtime_state = state.get("skill_runtime") or empty_runtime_state()
            contract = state.get("skill_contract") or {}
            if contract:
                runtime_state = apply_asset_completion(
                    contract=contract,
                    runtime_state=runtime_state,
                    control_runtime=state.get("last_skill_runtime_control") or {},
                    native_target=state.get("last_native_target") or {},
                    native_plan=state.get("native_plan") or {},
                    asset_readiness=self.production.contract_asset_readiness(
                        project_id, stage, contract
                    ),
                )
                state["skill_runtime"] = runtime_state
                state["stage_ready"] = bool(
                    (runtime_state.get("completion") or {}).get("ready")
                )
            completion = runtime_state.get("completion") or {}
            if completion.get("ready") is not True:
                raise RuntimeError(
                    "阶段 Skill Runtime 尚未完成，不能确认："
                    + json.dumps({
                        "reason": completion.get("reason"),
                        "missing_artifact_ids": completion.get("missing_artifact_ids") or [],
                        "missing_requirement_ids": completion.get("missing_requirement_ids") or [],
                    }, ensure_ascii=False)
                )
            if not state.get("stage_ready"):
                raise RuntimeError(
                    f"阶段 {stage} 系统完成状态未同步，不能越级确认"
                )

            handoff = _clean_text(state.get("handoff"))
            if not handoff:
                raise RuntimeError(
                    f"阶段 {stage} 缺少真实 handoff，不能确认"
                )

            handoff_audit = (
                state.get("last_handoff_audit")
                or {}
            )
            if not (
                bool(handoff_audit.get("valid"))
                and bool(
                    handoff_audit.get(
                        "provenance_verified"
                    )
                )
                and _clean_text(
                    handoff_audit.get(
                        "contract_version"
                    )
                ) == "verbatim_evidence_v1"
            ):
                raise RuntimeError(
                    f"阶段 {stage} HANDOFF 尚未通过逐字证据交接校验，"
                    "不能确认"
                )

            production_assets = self.production.list_assets(
                project_id, stage=stage, active_only=True
            )
            production_asset_ids = [
                x["asset_id"] for x in production_assets
                if _clean_text(x.get("status")) == "ready"
                and _clean_text(x.get("dependency_state")) != "stale"
            ]
            project["confirmed_outputs"][stage] = {
                "skill": STAGE_SKILLS[stage],
                "handoff": handoff,
                "handoff_audit": handoff_audit,
                "skill_contract_source_sha256": _clean_text(
                    (state.get("skill_contract") or {}).get("source_sha256")
                ),
                "artifacts": (runtime_state.get("artifact_registry") or {}),
                "requirements": (runtime_state.get("requirement_registry") or {}),
                "completion": completion,
                "production_asset_ids": production_asset_ids,
                "production_stage_status": self.production.stage_status(project_id, stage),
                "confirmed_at": _utcnow(),
            }
            if stage not in project["completed_stages"]:
                project["completed_stages"].append(stage)

            index = STAGE_ORDER.index(stage)
            if index == len(STAGE_ORDER) - 1:
                project["status"] = "completed"
            else:
                project["current_stage"] = STAGE_ORDER[index + 1]

            project["updated_at"] = _utcnow()
            self._save_project(project)
            return project
