"""P2-3 导演审校：镜头语言规则引擎 + LLM 导演批注。

规则（确定性，秒出结果）：景别交替、场景建立镜头、对话正反打、轴线（左右站位翻转）、动作接剪（跳切）、
台词与时长、连续同向运镜。能自动修的给出「修改建议」，其余给出提示。
LLM 导演：逐镜表演指导（表情/动作节拍/视线）、情绪曲线 → 镜头时长、转场方式，并给出镜头参数修改建议。
所有修改都进入「修改建议」，逐条接受 / 拒绝。
"""
from __future__ import annotations

import re
import time
from typing import Any

from fastapi import APIRouter, HTTPException

from . import db, llm, prompts, suggest
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")
TRANSITIONS = ["硬切", "叠化", "闪白", "黑场", "匹配剪辑", "划像", "推移"]
SIZE_ORDER = prompts.SHOT_SIZES                         # 远景 全景 中景 近景 特写
WIDE = {"远景", "全景"}
CHARS_PER_SEC = 4.0


def _speech(shot: dict) -> int:
    return sum(len(re.sub(r"[\s，。！？、；：,.!?;:…—“”\"'（）()]", "", d.get("line") or "")) for d in shot.get("dialogue") or [])


def _scene_key(s: dict) -> str:
    return (s.get("location_id") or "") + "|" + (s.get("scene") or "")


def _sides(shot: dict, names: dict[str, str]) -> dict[str, str]:
    """从画面描述里解析每个角色在画面左/右（用于轴线检查）。"""
    out = {}
    for clause in re.split(r"[，。；,;！!？?\n]", shot.get("description") or ""):
        for cid, name in names.items():
            if name and name in clause:
                if re.search(r"画面左|左侧|左边|左方", clause):
                    out[cid] = "左"
                elif re.search(r"画面右|右侧|右边|右方", clause):
                    out[cid] = "右"
    return out


def _neighbor_size(size: str, prefer_closer: bool = True) -> str:
    i = SIZE_ORDER.index(size) if size in SIZE_ORDER else 2
    j = i + 1 if (prefer_closer and i + 1 < len(SIZE_ORDER)) or i == 0 else i - 1
    return SIZE_ORDER[j]


def check(shots: list[dict], chars: list[dict]) -> list[dict]:
    """返回问题列表：{rule, label, shots:[序号], level: warn|info, message, fix:{shot_id, field, value}|None}"""
    names = {c["id"]: c["name"] for c in chars}
    issues: list[dict] = []

    def add(rule, label, idx, msg, fix=None, level="warn"):
        issues.append({"rule": rule, "label": label, "shots": [i + 1 for i in idx], "level": level, "message": msg,
                       "fix": fix})

    for i, s in enumerate(shots):
        prev = shots[i - 1] if i else None
        same_scene = prev is not None and _scene_key(prev) == _scene_key(s)
        # 1 场景建立镜头
        if not same_scene:
            rest = [x for x in shots[i:] if _scene_key(x) == _scene_key(s)]
            if len(rest) >= 3 and s.get("shot_size") not in WIDE and not any(x.get("shot_size") in WIDE for x in rest[:2]):
                add("establish", "建立镜头", [i], f"场景「{s.get('scene') or '新场景'}」开头没有全景/远景交代环境，观众不知道人在哪",
                    {"shot_id": s["id"], "field": "shot_size", "value": "全景"})
        if prev is not None and same_scene:
            # 2 景别交替 / 动作接剪
            if prev.get("shot_size") == s.get("shot_size") and prev.get("camera_angle") == s.get("camera_angle"):
                if s.get("continuous"):
                    add("jumpcut", "动作接剪", [i - 1, i], "连续动作的两个镜头景别和角度完全相同，剪在一起会像跳帧（跳切）",
                        {"shot_id": s["id"], "field": "shot_size", "value": _neighbor_size(s.get("shot_size") or "中景")})
                elif set(prev.get("character_ids") or []) == set(s.get("character_ids") or []):
                    add("size", "景别交替", [i - 1, i], f"相邻两镜同为「{s.get('shot_size')}」「{s.get('camera_angle')}」且人物相同，画面缺少变化",
                        {"shot_id": s["id"], "field": "shot_size", "value": _neighbor_size(s.get("shot_size") or "中景")})
            # 3 轴线：同一角色左右站位翻转（环绕/摇镜除外）
            if prev.get("camera_move") not in {"环绕"} and s.get("camera_move") not in {"环绕"}:
                a, b = _sides(prev, names), _sides(s, names)
                flipped = [names[c] for c in a if c in b and a[c] != b[c]]
                if flipped and len(set(prev.get("character_ids") or []) & set(s.get("character_ids") or [])) >= 2:
                    add("axis", "轴线", [i - 1, i], f"「{'、'.join(flipped)}」上一镜在画面{a[next(c for c in a if names[c] == flipped[0])]}，"
                        f"这一镜到了另一侧，可能越轴（180° 规则），观众会搞不清谁在看谁")
        # 4 台词与时长
        need = _speech(s) / CHARS_PER_SEC + 1 if s.get("dialogue") else 0
        if need and (s.get("duration") or 0) < need - 0.05:
            add("duration", "台词时长", [i], f"台词约需 {need:.1f} 秒，镜头只有 {s.get('duration')} 秒，会被截断",
                {"shot_id": s["id"], "field": "duration", "value": round(min(15.0, need + 0.5), 1)})
        if not s.get("dialogue") and (s.get("duration") or 0) > 8 and s.get("camera_move") == "固定":
            add("static", "节奏", [i], f"无台词的固定镜头长达 {s.get('duration')} 秒，容易拖沓", 
                {"shot_id": s["id"], "field": "duration", "value": 5.0}, level="info")
    # 5 对话正反打：同场景两人对话，连续 3 个以上「两人同框」的对话镜头
    run: list[int] = []
    for i, s in enumerate(shots + [{}]):
        two = (s.get("dialogue") and len(s.get("character_ids") or []) >= 2 and s.get("camera_angle") != "过肩"
               and s.get("shot_size") in {"全景", "中景"})
        if two and (not run or _scene_key(shots[run[-1]]) == _scene_key(s)):
            run.append(i)
            continue
        if len(run) >= 3:
            mid = shots[run[1]]
            add("reverse", "正反打", run, f"连续 {len(run)} 个两人同框的对话镜头，建议改成过肩正反打（说话人近景）",
                {"shot_id": mid["id"], "field": "camera_angle", "value": "过肩"})
        run = [i] if two else []
    # 6 连续同向运镜
    for i in range(2, len(shots)):
        mv = {shots[k].get("camera_move") for k in (i - 2, i - 1, i)}
        if len(mv) == 1 and next(iter(mv)) not in {"固定", "跟拍"}:
            add("move", "运镜", [i - 2, i - 1, i], f"连续 3 个镜头都是「{shots[i].get('camera_move')}」，运镜单调", level="info")
    return issues


# ---------------- LLM 导演 ----------------
DIRECTOR_SYSTEM = f"""你是经验丰富的影视导演，正在审校一份 AI 短剧分镜表。逐镜头给出导演批注：
1. performance 表演指导（必填，每镜一句话）：表情、动作节拍（先…再…）、视线方向（看向谁/哪里），要具体可演。
2. transition 本镜头与上一镜之间的转场（第 1 镜填硬切）：只能是 {TRANSITIONS}。默认硬切；时间跳跃用叠化或黑场，回忆/惊吓用闪白，形状或动作呼应用匹配剪辑。
3. 根据情绪曲线调整节奏：高潮、紧张处镜头更短更快，抒情处可略长；给出 intensity（情绪强度 1~10）。
4. 只在确实需要时修改镜头参数（changes，可选字段：shot_size、camera_angle、camera_move、duration、description、action），
   遵守镜头语言：180° 轴线、视线匹配、景别交替、对话正反打、场景开头建立镜头、动作接剪。【规则检查】里的问题要优先解决。
   shot_size 只能是 {prompts.SHOT_SIZES}；camera_angle 只能是 {prompts.CAMERA_ANGLES}；camera_move 只能是 {prompts.CAMERA_MOVES}。
5. reason 用一句话说明为什么这样改。
只输出 JSON：{{"shots":[{{"index":1,"performance":"","transition":"硬切","intensity":5,"changes":{{}},"reason":""}}],"summary":"整体意见"}}"""


def director_user(project: dict, shots: list[dict], chars: list[dict], locs: list[dict], issues: list[dict]) -> str:
    from .writers import bio_text, genre_text
    names = {c["id"]: c["name"] for c in chars}
    lnames = {l["id"]: l["name"] for l in locs}
    rows = []
    for i, s in enumerate(shots, 1):
        lines = "；".join(f"{d.get('speaker')}：{d.get('line')}" for d in s.get("dialogue") or [])
        rows.append(f"{i}. [{lnames.get(s.get('location_id'), s.get('scene') or '')}] {s.get('shot_size')}/{s.get('camera_angle')}/"
                    f"{s.get('camera_move')} {s.get('duration')}秒{' 连续动作' if s.get('continuous') else ''}；"
                    f"人物：{'、'.join(names.get(c, '') for c in s.get('character_ids') or []) or '无'}；画面：{s.get('description')}；"
                    f"动作：{s.get('action') or '无'}；台词：{lines or '无'}")
    probs = "\n".join(f"- 镜头 {'、'.join(map(str, x['shots']))}【{x['label']}】{x['message']}" for x in issues) or "（无）"
    return (f"{genre_text(project)}\n\n【人设卡】\n{bio_text(chars) or '（无）'}\n\n【规则检查】\n{probs}\n\n【分镜表】\n" + "\n".join(rows))


def _validate(n: int):
    def validate(data: Any) -> dict:
        rows = data.get("shots") if isinstance(data, dict) else None
        if not isinstance(rows, list) or not rows:
            raise ValueError("shots 必须是非空数组")
        out = []
        for r in rows:
            try:
                idx = int(r.get("index"))
            except (TypeError, ValueError, AttributeError):
                continue
            if not 1 <= idx <= n:
                continue
            ch = r.get("changes") if isinstance(r.get("changes"), dict) else {}
            changes: dict[str, Any] = {}
            for key, opts in (("shot_size", prompts.SHOT_SIZES), ("camera_angle", prompts.CAMERA_ANGLES),
                              ("camera_move", prompts.CAMERA_MOVES)):
                if ch.get(key) in opts:
                    changes[key] = ch[key]
            try:
                if ch.get("duration") is not None:
                    changes["duration"] = round(min(15.0, max(2.0, float(ch["duration"]))), 1)
            except (TypeError, ValueError):
                pass
            for key in ("description", "action"):
                if str(ch.get(key) or "").strip():
                    changes[key] = str(ch[key]).strip()
            tr = str(r.get("transition") or "").strip()
            try:
                intensity = max(1, min(10, int(r.get("intensity") or 5)))
            except (TypeError, ValueError):
                intensity = 5
            out.append({"index": idx, "performance": str(r.get("performance") or "").strip(),
                        "transition": tr if tr in TRANSITIONS else "", "intensity": intensity,
                        "changes": changes, "reason": str(r.get("reason") or "").strip()})
        if not out:
            raise ValueError("没有有效的镜头批注")
        return {"shots": out, "summary": str(data.get("summary") or "").strip()}
    return validate


def rule_suggestions(project_id: str, shots: list[dict], issues: list[dict], batch: str) -> int:
    by_id = {s["id"]: s for s in shots}
    n = 0
    for x in issues:
        f = x.get("fix")
        if not f or f["shot_id"] not in by_id:
            continue
        s = by_id[f["shot_id"]]
        if suggest.add(project_id, "rules", "shots", s["id"], f["field"], s.get(f["field"]), f["value"],
                       f"【{x['label']}】{x['message']}", batch, f"镜头 #{s['sort'] + 1}"):
            n += 1
    return n


@handler("director")
async def task_director(ctx: TaskContext) -> dict:
    from .pipeline import characters_of, locations_of, shots_of
    project = db.get("projects", ctx.project_id)
    shots = shots_of(ctx.project_id)
    if not shots:
        raise ValueError("请先生成分镜表")
    chars, locs = characters_of(ctx.project_id), locations_of(ctx.project_id)
    issues = check(shots, chars)
    batch = f"director-{int(time.time())}"
    await ctx.progress(10, f"规则检查发现 {len(issues)} 个问题，导演审校中…")
    data = await llm.chat_json(DIRECTOR_SYSTEM, director_user(project, shots, chars, locs, issues),
                               validate=_validate(len(shots)), temperature=0.4, max_tokens=12000)
    made = 0
    rule_fields = {(x["fix"]["shot_id"], x["fix"]["field"]) for x in issues if x.get("fix")}
    curve = []
    for r in data["shots"]:
        s = shots[r["index"] - 1]
        curve.append({"index": r["index"], "intensity": r["intensity"]})
        label = f"镜头 #{s['sort'] + 1}"
        if r["performance"] and r["performance"] != (s.get("performance") or ""):
            made += bool(suggest.add(ctx.project_id, "director", "shots", s["id"], "performance", s.get("performance") or "",
                                     r["performance"], "表演指导", batch, label))
        if r["transition"] and r["index"] > 1 and r["transition"] != (s.get("transition") or "硬切"):
            made += bool(suggest.add(ctx.project_id, "director", "shots", s["id"], "transition", s.get("transition") or "硬切",
                                     r["transition"], r["reason"] or "转场", batch, label))
        for field, value in r["changes"].items():
            if value != s.get(field):
                made += bool(suggest.add(ctx.project_id, "director", "shots", s["id"], field, s.get(field), value,
                                         r["reason"] or "导演调整", batch, label))
    # LLM 没处理的可自动修复规则问题，补成规则建议
    covered = {(shots[r["index"] - 1]["id"], f) for r in data["shots"] for f in r["changes"]}
    left = [x for x in issues if x.get("fix") and (x["fix"]["shot_id"], x["fix"]["field"]) not in covered]
    made += rule_suggestions(ctx.project_id, shots, left, batch)
    from .writers import writer_state
    p = db.get("projects", ctx.project_id)
    w = writer_state(p)
    w["director"] = {"at": time.time(), "summary": data["summary"], "curve": curve, "issues": len(issues),
                     "rule_fields": len(rule_fields)}
    db.update("projects", ctx.project_id, {"writer": w})
    return {"suggestions": made, "issues": len(issues), "summary": data["summary"]}


# ---------------- 接口 ----------------
@router.get("/projects/{project_id}/director/check")
async def rules_check(project_id: str) -> dict:
    from .pipeline import characters_of, shots_of
    if not db.get("projects", project_id):
        raise HTTPException(404, "项目不存在")
    return {"issues": check(shots_of(project_id), characters_of(project_id)), "transitions": TRANSITIONS}


@router.post("/projects/{project_id}/director/fix")
async def rules_fix(project_id: str) -> dict:
    """把规则检查里能自动修的问题转成修改建议（不调用文字模型）。"""
    from .pipeline import characters_of, shots_of
    shots = shots_of(project_id)
    n = rule_suggestions(project_id, shots, check(shots, characters_of(project_id)), f"rules-{int(time.time())}")
    return {"suggestions": n}


@router.post("/projects/{project_id}/director")
async def run_director(project_id: str) -> dict:
    if not db.get("projects", project_id):
        raise HTTPException(404, "项目不存在")
    return queue.submit("director", project_id=project_id, title="导演审校", owner_type="project",
                        owner_id=f"{project_id}:director")
