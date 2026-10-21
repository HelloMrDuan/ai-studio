"""一键成片：剧本 → 提取 → 参考图 →（音色）→ 分镜表 → 分镜图 → 动态分镜预览 → 视频 →（配音）→ 合成。

状态存在 projects.autopilot（JSON）。推进方式：每当本项目的任务全部结束（任务完成钩子 / 点继续 / 服务启动），
advance() 检查当前步骤的结果：成功就进入下一步（或在审核点停下等用户），失败先自动重试一次，再失败就停下报错。
advance() 是同步函数（只读写数据库、提交任务），在事件循环里天然不会并发。
"""
from __future__ import annotations

import logging
import time

from . import db, voice
from .tasks import AFTER_HOOKS, queue

log = logging.getLogger("studio.autopilot")

STAGES = [
    ("beats", "节拍表"), ("script", "剧本"), ("review", "剧本审稿"),
    ("extract", "角色与场景"), ("refs", "定妆照/场景/道具图"), ("voices", "角色音色"),
    ("storyboard", "分镜表"), ("keyframes", "分镜图"), ("animatic", "动态分镜预览"), ("videos", "镜头视频"),
    ("dub", "配音"), ("assemble", "合成成片"),
]
LABEL = dict(STAGES)
ORDER = [k for k, _ in STAGES]
DEFAULT_STOPS = ["review", "storyboard", "animatic"]
DEFAULT_OPTIONS = {"quality": "draft", "turbo": True, "mode": "auto", "keyframe_count": 1, "subtitles": True,
                   "final_quality": "final", "stops": DEFAULT_STOPS,
                   "beats": True, "review": True}
ACTIVE_SQL = "SELECT * FROM tasks WHERE project_id=? AND status IN ('queued','running','cancelling','paused')"
# 估算剩余时间用的默认单任务耗时（秒）；实际以最近完成任务的平均用时为准
DEFAULT_COST = {"beats": 30, "script_review": 90, "script": 60, "extract": 40, "portrait": 45, "face_ref": 30, "location_image": 35, "prop_image": 30,
                "voice_sample": 150, "storyboard": 90, "keyframe": 45, "animatic": 20, "video": 150, "cloud_video": 150, "dub": 70,
                "assemble": 60}


def _shots(pid: str) -> list[dict]:
    from .pipeline import shots_of
    return shots_of(pid)


def _get(pid: str) -> tuple[dict, dict]:
    project = db.get("projects", pid)
    if not project:
        raise ValueError("项目不存在")
    return project, dict(project.get("autopilot") or {})


def _save(pid: str, ap: dict) -> dict:
    ap["updated_at"] = time.time()
    db.update("projects", pid, {"autopilot": ap})
    return ap


def _use_voice(project: dict) -> bool:
    return bool(project.get("dub_mode")) and voice.tts_available()


# ---------------- 每一步：提交任务 & 检查结果 ----------------
def _submit_stage(project: dict, stage: str, ap: dict) -> list[dict]:
    """提交本步需要的任务；返回任务列表（空 = 本步无事可做，直接检查结果）。"""
    from .pipeline import characters_of, submit_batch
    pid, opt, tag = project["id"], ap["options"], {"_autopilot": ap["run"]}

    def one(type_: str, title: str, params: dict | None = None) -> list[dict]:
        return [queue.submit(type_, project_id=pid, title=title, owner_type="project", owner_id=f"{pid}:{type_}",
                             params={**(params or {}), **tag})]

    if stage == "beats":
        if not opt.get("beats", True) or project.get("beats") or (project.get("script") or "").strip():
            return []
        return one("beats", "节拍表")
    if stage == "script":
        return [] if (project.get("script") or "").strip() else one("script", "生成剧本")
    if stage == "review":
        from .writers import reviewed
        if not opt.get("review", True) or reviewed(project):
            return []
        return one("script_review", "剧本审稿")
    if stage == "extract":
        return [] if characters_of(pid) else one("extract", "提取角色与场景")
    if stage == "refs":
        return submit_batch(pid, "portraits", count=1, extra=tag)[0]
    if stage == "voices":
        return submit_batch(pid, "voices", extra=tag)[0] if _use_voice(project) else []
    if stage == "storyboard":
        return [] if _shots(pid) else one("storyboard", "生成分镜表")
    if stage == "keyframes":
        return submit_batch(pid, "keyframes", count=int(opt.get("keyframe_count") or 1), extra=tag)[0]
    if stage == "animatic":
        return one("animatic", "动态分镜预览", {"tts": _use_voice(project)})
    if stage == "videos":
        return submit_batch(pid, "videos", quality=opt.get("quality") or "draft", turbo=bool(opt.get("turbo", True)),
                            mode=opt.get("mode") or "auto", extra=tag)[0]
    if stage == "dub":       # 固定音色模式下视频任务已自动排了配音；这里补漏（只配还没配过的）
        return submit_batch(pid, "dub", extra=tag)[0] if _use_voice(project) else []
    if stage == "assemble":
        return one("assemble", "合成成片", {"subtitles": bool(opt.get("subtitles", True)),
                                         "quality": opt.get("final_quality") or "final"})
    raise ValueError(f"未知步骤：{stage}")


def _check_stage(project: dict, stage: str, ap: dict) -> tuple[bool, str]:
    """本步产出是否齐全。返回 (是否通过, 说明)。"""
    from .pipeline import characters_of, dub_lines, exists
    pid = project["id"]
    if stage in {"beats", "review"}:
        return True, ""
    if stage == "script":
        return bool((project.get("script") or "").strip()), "剧本为空"
    if stage == "extract":
        return bool(characters_of(pid)), "没有提取到角色"
    if stage == "refs":
        miss = [c["name"] for c in characters_of(pid) if not exists(c.get("ref_image"))]
        return not miss, f"角色缺定妆照：{'、'.join(miss)}"
    if stage == "voices":
        if not _use_voice(project):
            return True, ""
        miss = [c["name"] for c in characters_of(pid) if not exists(c.get("voice_ref"))]
        if miss:
            ap.setdefault("notes", []).append(f"角色「{'、'.join(miss)}」没有音色样本，其台词不配音")
        return True, ""
    if stage == "storyboard":
        return bool(_shots(pid)), "没有生成分镜"
    if stage == "keyframes":
        miss = [str(s["sort"] + 1) for s in _shots(pid) if not exists(s.get("keyframe"))]
        return not miss, f"镜头缺分镜图：#{'、#'.join(miss)}"
    if stage == "animatic":
        return True, ""
    if stage == "videos":
        miss = [str(s["sort"] + 1) for s in _shots(pid) if not exists(s.get("video"))]
        return not miss, f"镜头缺视频：#{'、#'.join(miss)}"
    if stage == "dub":
        if not _use_voice(project):
            return True, ""
        miss = []
        for s in _shots(pid):
            if not dub_lines(s)[0]:
                continue
            a = db.query("SELECT meta FROM assets WHERE path=? LIMIT 1", [s["video"]])
            if not (a and (a[0].get("meta") or {}).get("dub")):
                miss.append(str(s["sort"] + 1))
        return not miss, f"镜头没配上音：#{'、#'.join(miss)}"
    if stage == "assemble":
        return bool(project.get("final_video")) and project.get("final_video") != ap.get("final_before"), "成片没有生成"
    return True, ""


def _failed_tasks(ap: dict) -> list[dict]:
    ids = ap.get("tasks") or []
    if not ids:
        return []
    marks = ",".join("?" for _ in ids)
    return db.query(f"SELECT * FROM tasks WHERE id IN ({marks}) AND status='failed'", ids)


# ---------------- 推进 ----------------
def advance(pid: str) -> dict:
    project, ap = _get(pid)
    if ap.get("status") != "running":
        return ap
    if db.query(ACTIVE_SQL, [pid]):
        return ap                                       # 本项目还有任务在跑/排队/暂停，等它们结束
    for _ in range(len(ORDER) * 3):                    # 跳过无事可做的步骤；上限防止死循环
        stage = ap["stage"]
        if not ap.get("submitted"):
            ap["submitted"], ap["stage_started_at"] = True, time.time()
            ap["message"] = f"正在：{LABEL[stage]}"
            tasks = _submit_stage(project, stage, ap)
            ap["tasks"] = [t["id"] for t in tasks]
            ap["did_work"] = bool(tasks) or (stage == "review" and any(
                h["stage"] == "script" and h["tasks"] for h in ap.get("history") or []))   # 关了审稿也在这里停下看剧本
            if tasks:
                log.info("一键成片 %s：%s 提交 %d 个任务", pid, LABEL[stage], len(tasks))
                return _save(pid, ap)
        project = db.get("projects", pid) or project
        failed = _failed_tasks(ap)
        ok, why = _check_stage(project, stage, ap)
        if failed or not ok:
            reason = f"{len(failed)} 个任务失败（{failed[0]['title']}：{failed[0]['message'][:80]}）" if failed else why
            retries = ap.setdefault("retries", {})
            if retries.get(stage, 0) < 1:              # 自动重试一次（批量只补缺失的）
                retries[stage] = retries.get(stage, 0) + 1
                ap["submitted"] = False
                ap.setdefault("notes", []).append(f"{LABEL[stage]}：{reason}，已自动重试")
                continue
            ap.update(status="failed", message=f"{LABEL[stage]}失败：{reason}。可以到对应页面修正后点「重试当前步骤」")
            log.warning("一键成片 %s 停在 %s：%s", pid, stage, reason)
            return _save(pid, ap)
        ap.setdefault("history", []).append({"stage": stage, "seconds": round(time.time() - ap["stage_started_at"], 1),
                                             "tasks": len(ap.get("tasks") or [])})
        approved = ap.setdefault("approved", [])
        if stage in (ap["options"].get("stops") or []) and ap.get("did_work") and stage not in approved:
            ap.update(status="waiting", message=f"请审核「{LABEL[stage]}」，可以修改，确认后点「继续」")
            return _save(pid, ap)
        i = ORDER.index(stage)
        if i + 1 >= len(ORDER):
            ap.update(status="done", message="成片已生成", finished_at=time.time())
            return _save(pid, ap)
        ap.update(stage=ORDER[i + 1], submitted=False, tasks=[], did_work=False)
    ap.update(status="failed", message="流程推进异常，请点「重试当前步骤」")
    return _save(pid, ap)


def start(pid: str, options: dict | None = None, from_stage: str = "") -> dict:
    project, ap = _get(pid)
    if ap.get("status") == "running":
        return ap
    opt = {**DEFAULT_OPTIONS, **{k: v for k, v in (options or {}).items() if v is not None}}
    stage = from_stage if from_stage in ORDER else ORDER[0]
    if not (project.get("source_text") or "").strip() and not (project.get("script") or "").strip():
        raise ValueError("请先填写创意/小说/剧本素材")
    ap = {"run": db.new_id(), "status": "running", "stage": stage, "submitted": False, "options": opt,
          "started_at": time.time(), "final_before": project.get("final_video") or "", "history": [], "notes": [],
          "approved": [], "retries": {}, "tasks": []}
    _save(pid, ap)
    return advance(pid)


def resume(pid: str) -> dict:
    """继续：审核点 → 进入下一步；失败/已停止 → 重新执行当前步骤（批量只补缺失）。"""
    _, ap = _get(pid)
    st = ap.get("status")
    if st == "waiting":
        ap.setdefault("approved", []).append(ap["stage"])
    elif st in {"failed", "stopped"}:
        ap["submitted"] = False
        ap.setdefault("retries", {}).pop(ap.get("stage"), None)
    else:
        return ap
    ap.update(status="running", message="继续执行")
    _save(pid, ap)
    return advance(pid)


def stop(pid: str) -> dict:
    """停止一键成片：取消本次流程排队中的任务（正在运行的那个会跑完）。"""
    _, ap = _get(pid)
    if ap.get("status") not in {"running", "waiting"}:
        return ap
    ap.update(status="stopped", message="已停止，可点「重试当前步骤」接着做")
    _save(pid, ap)
    for t in db.query("SELECT * FROM tasks WHERE project_id=? AND status IN ('queued','paused')", [pid]):
        if (t.get("params") or {}).get("_autopilot") == ap.get("run"):
            queue.cancel(t["id"])
    return ap


# ---------------- 进度与预计剩余时间 ----------------
def _avg_cost(type_: str) -> float:
    rows = db.query("SELECT result FROM tasks WHERE type=? AND status='done' ORDER BY updated_at DESC LIMIT 20", [type_])
    vals = [float((r.get("result") or {}).get("elapsed") or 0) for r in rows]
    vals = [v for v in vals if v > 0]
    return sum(vals) / len(vals) if vals else DEFAULT_COST.get(type_, 60)


def status(pid: str) -> dict:
    from .pipeline import characters_of, dub_lines, exists, locations_of, props_of
    project, ap = _get(pid)
    if not ap:
        return {"status": "none", "stages": [{"key": k, "label": v} for k, v in STAGES], "options": DEFAULT_OPTIONS}
    shots = _shots(pid)
    use_voice = _use_voice(project)
    active = db.query(ACTIVE_SQL, [pid])
    cur = ap.get("stage")
    idx = ORDER.index(cur) if cur in ORDER else 0
    # 剩余工作量：当前步骤剩余的排队任务 + 后续步骤预计的任务数
    left: dict[str, int] = {}
    for t in active:
        left[t["type"]] = left.get(t["type"], 0) + 1
    future = ORDER[idx + 1:] if ap.get("submitted") else ORDER[idx:]
    n_shots = len(shots) or max(3, int(project.get("target_seconds") or 60) // 5)
    for st in future:
        if st == "script" and not (project.get("script") or "").strip():
            left["script"] = left.get("script", 0) + 1
        elif st == "review" and ap["options"].get("review", True):
            left["script_review"] = left.get("script_review", 0) + 1
        elif st == "extract" and not characters_of(pid):
            left["extract"] = left.get("extract", 0) + 1
        elif st == "refs":
            chars = characters_of(pid) or [{}] * 2
            left["portrait"] = left.get("portrait", 0) + sum(1 for c in chars if not exists(c.get("ref_image")))
            left["location_image"] = left.get("location_image", 0) + sum(1 for x in locations_of(pid) if not exists(x.get("ref_image")))
            left["prop_image"] = left.get("prop_image", 0) + sum(1 for x in props_of(pid) if not exists(x.get("ref_image")))
        elif st == "voices" and use_voice:
            left["voice_sample"] = left.get("voice_sample", 0) + sum(1 for c in (characters_of(pid) or [{}] * 2)
                                                                     if not exists(c.get("voice_ref")))
        elif st == "storyboard" and not shots:
            left["storyboard"] = left.get("storyboard", 0) + 1
        elif st == "keyframes":
            left["keyframe"] = left.get("keyframe", 0) + (sum(1 for s in shots if not exists(s.get("keyframe"))) if shots else n_shots)
        elif st == "animatic":
            left["animatic"] = left.get("animatic", 0) + 1
        elif st == "videos":
            left["video"] = left.get("video", 0) + (sum(1 for s in shots if not exists(s.get("video"))) if shots else n_shots)
        elif st == "dub" and use_voice and "videos" in future:
            left["dub"] = left.get("dub", 0) + (sum(1 for s in shots if dub_lines(s)[0]) if shots else n_shots // 2)
        elif st == "assemble":
            left["assemble"] = left.get("assemble", 0) + 1
    eta = sum(_avg_cost(k) * v for k, v in left.items())
    stages = []
    for i, (k, label) in enumerate(STAGES):
        if k in {"voices", "dub"} and not use_voice:
            state = "skip"
        elif ap.get("status") == "done" or i < idx:
            state = "done"
        else:
            state = "current" if i == idx else "todo"
        stages.append({"key": k, "label": label, "state": state, "stop": k in (ap["options"].get("stops") or [])})
    shot_left = {"keyframes": sum(1 for s in shots if not exists(s.get("keyframe"))),
                 "videos": sum(1 for s in shots if not exists(s.get("video")))}
    return {**{k: ap.get(k) for k in ("status", "stage", "message", "options", "notes", "history", "started_at",
                                      "finished_at", "run")},
            "stage_label": LABEL.get(cur, ""), "stages": stages, "active": len(active), "shots": len(shots),
            "shots_left": shot_left, "eta_seconds": round(eta), "remaining": left}


def _on_task_done(task: dict) -> None:
    pid = task.get("project_id")
    project = db.get("projects", pid) if pid else None
    if project and (project.get("autopilot") or {}).get("status") == "running":
        advance(pid)


def resume_all() -> None:
    """服务启动：还在「进行中」的一键成片继续推进（中断的任务已被标为失败，会自动重试一次）。"""
    for p in db.query("SELECT id, autopilot FROM projects"):
        if (p.get("autopilot") or {}).get("status") == "running":
            try:
                advance(p["id"])
            except Exception as exc:  # noqa: BLE001
                log.error("恢复一键成片失败 %s：%s", p["id"], exc)


AFTER_HOOKS.append(_on_task_done)
