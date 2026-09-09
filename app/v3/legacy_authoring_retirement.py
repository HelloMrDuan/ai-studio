from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def retire_legacy_authoring_jobs(settings: Any) -> dict[str, Any]:
    """Stop persisted authoring jobs created by the retired multi-turn driver.

    Old ①-④ jobs expose ``turn_count`` in the workbench and may survive a web
    process restart as queued/running JSON. They must never resume after the
    single-pass runtime is installed. Media jobs are intentionally untouched:
    only active job records carrying the legacy authoring ``turn_count`` field
    are retired.
    """

    root = Path(settings.data_dir) / "studio_jobs"
    result = {"scanned": 0, "retired": 0, "job_ids": []}
    if not root.is_dir():
        return result

    for path in root.glob("*.json"):
        result["scanned"] += 1
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if str(data.get("status") or "").strip().lower() not in {"queued", "running"}:
            continue
        if "turn_count" not in data:
            continue
        try:
            turn_count = int(data.get("turn_count") or 0)
        except Exception:
            turn_count = 0
        if turn_count < 1:
            continue

        data["status"] = "failed"
        data["failure_kind"] = "legacy_auto_advance_retired"
        data["message"] = "旧版多轮自动推进任务已停止"
        data["error"] = (
            "当前版本已切换为①-④单次生产模式；该旧任务不会恢复或继续调用模型。"
            "请在作品页面重新执行当前阶段。"
        )
        data["finished_at"] = _utcnow()
        data["updated_at"] = _utcnow()
        data["legacy_turn_count"] = turn_count
        temp = path.with_suffix(".tmp")
        temp.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temp.replace(path)
        result["retired"] += 1
        job_id = str(data.get("job_id") or path.stem).strip()
        if job_id:
            result["job_ids"].append(job_id)

    return result


__all__ = ["retire_legacy_authoring_jobs"]
