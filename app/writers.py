"""P2-1 编剧室：题材模板、节拍表、人设卡、评分卡审稿 + 自动改稿、台词打磨。

- 节拍表（projects.beats）：先定 3 秒钩子 → 冲突升级 → 反转 → 结尾卡点，确认后剧本按它写。
- 评分卡（projects.writer.reviews）：钩子/冲突/节奏/逻辑/人设/台词 6 项打分 + 修改意见；低于阈值自动改稿（最多 2 轮），
  每一版存入 projects.writer.versions，可对比、可恢复。
- 台词打磨：按镜头时长控制字数（≈4 字/秒）、口语化、贴合人设，结果作为「修改建议」逐条接受 / 拒绝。
"""
from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from . import db, llm, prompts, suggest
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")
CHARS_PER_SEC = 4.0

# ---------------- 题材 / 平台模板 ----------------
GENRES: dict[str, dict[str, str]] = {
    "general": {"label": "通用短剧", "structure": "开端（3 秒钩子）→ 发展（冲突升级）→ 高潮（反转）→ 结尾（情绪落点或悬念）",
                "rhythm": "每 10~15 秒一个情节推进点，不拖沓", "dialogue": "口语化、短句为主，一句台词不超过 20 字",
                "taboo": "不要大段独白和心理描写，不要说教"},
    "vertical": {"label": "竖屏爽剧", "structure": "开场即冲突（被羞辱/被误解/身份反差）→ 连续打脸爽点 → 身份揭晓/逆转 → 结尾留钩子引下一集",
                 "rhythm": "每 5~8 秒一个爽点或信息增量，情绪拉满、节奏极快", "dialogue": "台词短促有力，金句化，反派台词要嚣张、主角台词要克制有分量",
                 "taboo": "不要铺垫过长，前 3 秒必须出现冲突；不要复杂支线"},
    "commentary": {"label": "漫剧解说", "structure": "悬念式开场旁白 → 旁白推进剧情、角色关键台词点睛 → 反转 → 结尾旁白设问",
                   "rhythm": "旁白每句 2~4 秒，画面跟着旁白切换，信息密度高", "dialogue": "以「旁白」为主（speaker 写旁白），解说口吻、第三人称、带情绪起伏；角色台词只保留关键句",
                   "taboo": "不要让旁白重复画面已经表达的内容"},
    "revenge": {"label": "爽文逆袭", "structure": "主角落魄受辱 → 金手指/隐藏身份显露 → 逐级打脸 → 众人震惊、反派崩溃 → 更大的局",
                "rhythm": "压抑 → 释放的循环要快，单次压抑不超过 15 秒", "dialogue": "反派轻蔑挑衅，主角一句话反杀；围观者的惊讶反应要有",
                "taboo": "主角不能一直被动挨打，不要拖延反击"},
    "suspense": {"label": "悬疑", "structure": "异常事件开场 → 线索逐步出现 → 误导 → 真相反转 → 结尾留新的疑点",
                 "rhythm": "信息有节制地释放，每段揭示一个新线索，关键处留白", "dialogue": "台词克制、有潜台词，关键信息用细节和道具表达",
                 "taboo": "不要提前泄底，不要用巧合解决问题"},
    "romance": {"label": "甜宠", "structure": "误会或意外相遇 → 心动细节 → 小冲突/吃醋 → 甜蜜和解或心动瞬间 → 结尾撒糖或留暧昧",
                "rhythm": "情绪细腻，心动瞬间放慢（特写、对视），其余节奏轻快", "dialogue": "轻松俏皮、有来有回，男主台词宠溺但不油腻",
                "taboo": "不要狗血虐心过度，不要低俗"},
    "ancient": {"label": "古风", "structure": "身份/宿命冲突开场 → 权谋或情感纠葛 → 关键抉择 → 反转 → 结尾意境或悬念",
                "rhythm": "张弛有度，仪式感场面可略慢，冲突场面紧凑", "dialogue": "半文半白、典雅简洁，称谓符合古代礼制（本宫、在下、姑娘等），避免现代网络用语",
                "taboo": "不要出现现代物品和现代词汇"},
}
DIMENSIONS = [("hook", "钩子强度", 1.3), ("conflict", "冲突", 1.1), ("rhythm", "节奏", 1.0), ("logic", "逻辑", 1.0),
              ("character", "人设一致", 0.9), ("dialogue", "台词", 1.0)]
DIM_LABEL = {k: v for k, v, _ in DIMENSIONS}
DEFAULT_WRITER = {"threshold": 80, "max_rounds": 2, "auto_review": True}


def genre_of(project: dict) -> dict[str, str]:
    return GENRES.get(project.get("genre") or "general", GENRES["general"])


def genre_text(project: dict) -> str:
    g = genre_of(project)
    return (f"【题材模板：{g['label']}】\n结构：{g['structure']}\n节奏：{g['rhythm']}\n台词风格：{g['dialogue']}\n禁忌：{g['taboo']}")


def beats_text(beats: list[dict]) -> str:
    return "\n".join(f"{i}. [{b.get('time') or ''}] {b.get('name') or ''}：{b.get('content') or ''}"
                     + (f"（情绪：{b['emotion']}）" if b.get("emotion") else "") for i, b in enumerate(beats, 1))


def bio_text(chars: list[dict]) -> str:
    """人设卡：后续剧本改写、分镜台词、台词打磨都引用。"""
    rows = []
    for c in chars:
        bits = [f"{c['name']}（{c.get('role') or ''}{('，' + c['gender']) if c.get('gender') else ''}{('，' + c['age']) if c.get('age') else ''}）"]
        for key, label in (("personality", "性格"), ("motivation", "动机"), ("relations", "关系"),
                           ("speech", "说话方式"), ("catchphrase", "口头禅")):
            if (c.get(key) or "").strip():
                bits.append(f"{label}：{c[key].strip()}")
        rows.append("- " + "；".join(bits))
    return "\n".join(rows)


def writer_state(project: dict) -> dict:
    w = dict(project.get("writer") or {})
    for k, v in DEFAULT_WRITER.items():
        w.setdefault(k, v)
    w.setdefault("reviews", [])
    w.setdefault("versions", [])
    return w


def script_hash(script: str) -> str:
    import hashlib
    return hashlib.md5((script or "").strip().encode()).hexdigest()[:12]


def reviewed(project: dict) -> bool:
    last = writer_state(project).get("last") or {}
    return bool(last) and last.get("hash") == script_hash(project.get("script") or "")


def save_version(project_id: str, script: str, label: str, total: float | None = None) -> None:
    p = db.get("projects", project_id)
    w = writer_state(p)
    if not script.strip() or (w["versions"] and w["versions"][-1]["script"] == script):
        if w["versions"] and total is not None:
            w["versions"][-1]["total"] = total
            db.update("projects", project_id, {"writer": w})
        return
    w["versions"].append({"at": time.time(), "label": label, "script": script, "total": total})
    w["versions"] = w["versions"][-12:]
    db.update("projects", project_id, {"writer": w})


# ---------------- 提示词 ----------------
BEATS_SYSTEM = """你是短剧总编剧，擅长设计让观众停不下来的节拍表。根据素材、题材模板和目标时长设计这一集的节拍表。
要求：
1. 第一个节拍必须是 0~3 秒的「钩子」：一个立刻抓住注意力的画面/冲突/悬念。
2. 中间是冲突升级（至少两次加码），要有一次反转。
3. 最后一个节拍是「结尾卡点」：情绪最高点或悬念，让人想看下一集。
4. time 写时间段（如 0-3秒），所有节拍首尾相接覆盖目标时长；content 写这一拍具体发生什么（可拍摄的画面和关键台词）。
只输出 JSON：{"logline":"一句话卖点","beats":[{"time":"0-3秒","name":"钩子","content":"","emotion":"紧张"}]}"""

REVIEW_SYSTEM = """你是严格的短剧审稿主编。按评分卡给剧本逐项打分（0~100 的整数，80 分以上才算合格），并给出具体可执行的修改意见。
评分项：
- hook 钩子强度：前 3 秒是否立刻有冲突/悬念/强画面
- conflict 冲突：矛盾是否清晰、是否逐步升级
- rhythm 节奏：信息密度、有无拖沓，结尾是否有卡点
- logic 逻辑：因果是否成立、人物行为是否合理
- character 人设一致：人物言行是否符合人设卡，角色是否有辨识度
- dialogue 台词：是否口语化、简短有力，有无书面腔，台词时长是否适合短视频
issues 列出最需要改的问题（最多 6 条），每条写清在哪里（场景/台词）、问题、怎么改。
只输出 JSON：{"scores":{"hook":0,"conflict":0,"rhythm":0,"logic":0,"character":0,"dialogue":0},
"issues":[{"dim":"hook","where":"","problem":"","suggestion":""}],"summary":"一句话总评"}"""

REWRITE_SYSTEM = """你是短剧编剧，根据审稿意见修改剧本。只改有问题的地方，保留原剧本的人物、主线和格式
（「## 场景N：地点 · 时间」、「△画面动作」、「角色名：（情绪）台词」），不要新增主要角色。
只输出 JSON：{"script":"修改后的完整剧本","changes":["改了什么（一句话）"]}"""

POLISH_SYSTEM = f"""你是短剧台词指导。逐个镜头打磨台词：
1. 口语化，去掉书面腔和翻译腔，像真人在这个情境下会说的话；
2. 符合人设卡里的性格和说话方式（口头禅不要每句都用）；
3. 严格控制字数：每个镜头的台词总字数 ≤ 镜头时长 × {CHARS_PER_SEC:.0f} - 2（标点不算），超了就精简，不改变剧情信息；
4. emotion 写清楚语气（如：压低声音、冷笑、哽咽）；
5. 不需要改的镜头不要输出。
只输出 JSON：{{"shots":[{{"index":1,"lines":[{{"speaker":"","line":"","emotion":""}}],"reason":"改了什么"}}]}}"""


def beats_user(project: dict) -> str:
    return (f"{genre_text(project)}\n\n目标时长：约 {project.get('target_seconds') or 60} 秒\n\n素材：\n"
            f"{(project.get('source_text') or '').strip()[:16000]}")


def validate_beats(data: Any) -> dict:
    rows = data.get("beats") if isinstance(data, dict) else data
    if not isinstance(rows, list) or len(rows) < 2:
        raise ValueError("beats 至少要有 2 个节拍")
    beats = [{k: str((b or {}).get(k) or "").strip() for k in ("time", "name", "content", "emotion")}
             for b in rows if isinstance(b, dict) and str(b.get("content") or "").strip()]
    if len(beats) < 2:
        raise ValueError("节拍缺少 content")
    return {"logline": str(data.get("logline") or "").strip() if isinstance(data, dict) else "", "beats": beats}


def total_score(scores: dict) -> float:
    w = sum(x for _, _, x in DIMENSIONS)
    return round(sum(float(scores.get(k) or 0) * x for k, _, x in DIMENSIONS) / w, 1)


def validate_review(data: Any) -> dict:
    if not isinstance(data, dict) or not isinstance(data.get("scores"), dict):
        raise ValueError("缺少 scores")
    scores = {}
    for k, _, _ in DIMENSIONS:
        try:
            scores[k] = max(0, min(100, int(round(float(data["scores"].get(k) or 0)))))
        except (TypeError, ValueError):
            scores[k] = 0
    issues = [{"dim": str(i.get("dim") or ""), "where": str(i.get("where") or ""), "problem": str(i.get("problem") or ""),
               "suggestion": str(i.get("suggestion") or "")} for i in data.get("issues") or [] if isinstance(i, dict)]
    return {"scores": scores, "total": total_score(scores), "issues": issues[:8],
            "summary": str(data.get("summary") or "").strip()}


def validate_rewrite(data: Any) -> dict:
    if not isinstance(data, dict) or len(str(data.get("script") or "").strip()) < 20:
        raise ValueError("缺少完整的 script")
    return {"script": str(data["script"]).strip(), "changes": [str(x) for x in data.get("changes") or []][:10]}


def review_user(project: dict, chars: list[dict]) -> str:
    parts = [genre_text(project), f"目标时长：约 {project.get('target_seconds') or 60} 秒"]
    if chars:
        parts.append("【人设卡】\n" + bio_text(chars))
    if project.get("beats"):
        parts.append("【节拍表】\n" + beats_text(project["beats"]))
    parts.append("【剧本】\n" + (project.get("script") or "")[:20000])
    return "\n\n".join(parts)


def rewrite_user(project: dict, chars: list[dict], review: dict) -> str:
    issues = "\n".join(f"- [{DIM_LABEL.get(i['dim'], i['dim'])}] {i['where']}：{i['problem']} → {i['suggestion']}"
                       for i in review["issues"]) or "- 整体提升钩子和节奏"
    low = "、".join(f"{DIM_LABEL[k]} {v}" for k, v in review["scores"].items() if v < 80) or "无"
    return f"{review_user(project, chars)}\n\n【审稿意见】总分 {review['total']}，低分项：{low}\n{issues}"


def speech_len(lines: list[dict]) -> int:
    import re
    return sum(len(re.sub(r"[\s，。！？、；：,.!?;:…—“”\"'（）()]", "", str(d.get("line") or ""))) for d in lines)


def overlong(shot: dict) -> bool:
    return speech_len(shot.get("dialogue") or []) > max(0, (shot.get("duration") or 0) * CHARS_PER_SEC - 2) \
        and bool(shot.get("dialogue"))


def polish_user(project: dict, chars: list[dict], shots: list[dict]) -> str:
    by_id = {c["id"]: c["name"] for c in chars}
    rows = []
    for i, s in enumerate(shots, 1):
        if not s.get("dialogue"):
            continue
        who = "、".join(by_id.get(x, "") for x in s.get("character_ids") or [])
        lines = "；".join(f"{d.get('speaker')}（{d.get('emotion') or ''}）：{d.get('line')}" for d in s["dialogue"])
        limit = max(0, int(s.get("duration", 0) * CHARS_PER_SEC - 2))
        rows.append(f"镜头{i}（{s.get('duration')}秒，字数上限 {limit}，当前 {speech_len(s['dialogue'])} 字；画面：{s.get('description', '')[:80]}；"
                    f"出镜：{who}）\n  {lines}")
    return (f"{genre_text(project)}\n\n【人设卡】\n{bio_text(chars) or '（无）'}\n\n【镜头台词】\n" + "\n".join(rows))


# ---------------- 任务 ----------------
@handler("beats")
async def task_beats(ctx: TaskContext) -> dict:
    project = db.get("projects", ctx.project_id)
    if not project or not (project.get("source_text") or "").strip():
        raise ValueError("请先填写创意/小说/剧本素材")
    await ctx.progress(10, "设计节拍表…")
    data = await llm.chat_json(BEATS_SYSTEM, beats_user(project), validate=validate_beats, temperature=0.8,
                               max_tokens=4096)
    w = writer_state(project)
    w["logline"] = data["logline"]
    db.update("projects", project["id"], {"beats": data["beats"], "writer": w})
    return {"beats": len(data["beats"])}


async def review_once(ctx: TaskContext, project: dict, chars: list[dict], round_no: int) -> dict:
    await ctx.progress(10 + round_no * 25, f"审稿打分（第 {round_no + 1} 轮）…")
    review = await llm.chat_json(REVIEW_SYSTEM, review_user(project, chars), validate=validate_review,
                                 temperature=0.2, max_tokens=4096)
    review.update(at=time.time(), round=round_no)
    return review


@handler("script_review")
async def task_script_review(ctx: TaskContext) -> dict:
    """评分卡审稿；低于阈值且开启自动改稿时，按意见改稿后再审（最多 max_rounds 轮），保留每一版。"""
    from .pipeline import characters_of
    project = db.get("projects", ctx.project_id)
    if not project or not (project.get("script") or "").strip():
        raise ValueError("请先生成或填写剧本")
    chars = characters_of(project["id"])
    w = writer_state(project)
    auto = bool(ctx.params.get("auto_fix", w["auto_review"]))
    threshold, rounds = float(w["threshold"]), int(w["max_rounds"])
    save_version(project["id"], project["script"], "审稿前")
    review = await review_once(ctx, project, chars, 0)
    history = [review]
    best_script, best = project["script"], review
    fixed = 0
    while auto and review["total"] < threshold and fixed < rounds:
        await ctx.progress(20 + fixed * 25, f"总分 {review['total']} < {threshold:.0f}，按意见改稿（第 {fixed + 1} 轮）…")
        data = await llm.chat_json(REWRITE_SYSTEM, rewrite_user(project, chars, review), validate=validate_rewrite,
                                   temperature=0.7, max_tokens=12000)
        fixed += 1
        project = {**project, "script": data["script"]}
        review = await review_once(ctx, project, chars, fixed)
        review["changes"] = data["changes"]
        history.append(review)
        save_version(project["id"], data["script"], f"自动改稿 第{fixed}轮", review["total"])
        if review["total"] >= best["total"]:
            best_script, best = data["script"], review
    if best_script != db.get("projects", project["id"])["script"]:
        db.update("projects", project["id"], {"script": best_script})
    p = db.get("projects", project["id"])
    w = writer_state(p)
    for v in reversed(w["versions"]):          # 审稿前那一版补上分数，便于对比
        if v["label"] == "审稿前" and v.get("total") is None:
            v["total"] = history[0]["total"]
            break
    w["reviews"] = (w["reviews"] + history)[-20:]
    w["last"] = {**best, "hash": script_hash(best_script)}
    db.update("projects", project["id"], {"writer": w})
    return {"total": best["total"], "rounds": fixed, "passed": best["total"] >= threshold,
            "before": history[0]["total"]}


def _validate_polish(n: int):
    def validate(data: Any) -> list[dict]:
        rows = data.get("shots") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise ValueError("shots 必须是数组")
        out = []
        for r in rows:
            try:
                idx = int(r.get("index"))
            except (TypeError, ValueError, AttributeError):
                continue
            lines = [{"speaker": str(d.get("speaker") or "").strip(), "line": str(d.get("line") or "").strip(),
                      "emotion": str(d.get("emotion") or "").strip()}
                     for d in r.get("lines") or [] if isinstance(d, dict) and str(d.get("line") or "").strip()]
            if 1 <= idx <= n and lines:
                out.append({"index": idx, "lines": lines, "reason": str(r.get("reason") or "").strip()})
        return out
    return validate


@handler("polish")
async def task_polish(ctx: TaskContext) -> dict:
    """台词打磨：生成修改建议（不直接改），用户逐条接受。"""
    from .pipeline import characters_of, shots_of
    project = db.get("projects", ctx.project_id)
    shots = shots_of(ctx.project_id)
    if not [s for s in shots if s.get("dialogue")]:
        raise ValueError("分镜里还没有台词")
    chars = characters_of(ctx.project_id)
    only = set(ctx.params.get("shot_ids") or [])
    targets = [s for s in shots if not only or s["id"] in only]
    await ctx.progress(10, "打磨台词…")
    rows = await llm.chat_json(POLISH_SYSTEM, polish_user(project, chars, targets), validate=_validate_polish(len(targets)),
                               temperature=0.6, max_tokens=8192)
    batch = f"polish-{int(time.time())}"
    names = {c["name"] for c in chars} | {"旁白"}
    made = 0
    for r in rows:
        shot = targets[r["index"] - 1]
        old = shot.get("dialogue") or []
        new = []
        for j, line in enumerate(r["lines"]):
            sp = line["speaker"] if line["speaker"] in names else (old[j]["speaker"] if j < len(old) else line["speaker"])
            new.append({**line, "speaker": sp})
        limit = shot.get("duration", 0) * CHARS_PER_SEC - 2
        reason = r["reason"] or "口语化"
        if speech_len(new) > limit:
            reason += f"（仍超出时长：{speech_len(new)} 字 / {shot['duration']} 秒）"
        if suggest.add(ctx.project_id, "polish", "shots", shot["id"], "dialogue", old, new, reason, batch,
                       f"镜头 #{shot['sort'] + 1} 台词"):
            made += 1
    over = [s["sort"] + 1 for s in shots if overlong(s)]
    return {"suggestions": made, "overlong": over}


# ---------------- 接口 ----------------
def _project(pid: str) -> dict:
    p = db.get("projects", pid)
    if not p:
        raise HTTPException(404, "项目不存在")
    return p


@router.get("/meta/genres")
async def genres() -> dict:
    return {k: v for k, v in GENRES.items()}


@router.get("/projects/{project_id}/writer")
async def writer_status(project_id: str) -> dict:
    return status(_project(project_id))


def status(p: dict) -> dict:
    from .pipeline import shots_of
    project_id = p["id"]
    w = writer_state(p)
    return {"genre": p.get("genre") or "general", "beats": p.get("beats") or [], "logline": w.get("logline", ""),
            "threshold": w["threshold"], "max_rounds": w["max_rounds"], "auto_review": w["auto_review"],
            "last": w.get("last"), "reviews": w["reviews"][-6:], "reviewed": reviewed(p),
            "versions": [{"i": i, "at": v["at"], "label": v["label"], "total": v.get("total"), "chars": len(v["script"])}
                         for i, v in enumerate(w["versions"])],
            "dimensions": [{"key": k, "label": l} for k, l, _ in DIMENSIONS],
            "overlong": [s["sort"] + 1 for s in shots_of(project_id) if overlong(s)]}


@router.post("/projects/{project_id}/beats")
async def gen_beats(project_id: str) -> dict:
    _project(project_id)
    return queue.submit("beats", project_id=project_id, title="节拍表", owner_type="project",
                        owner_id=f"{project_id}:beats")


class BeatsIn(BaseModel):
    beats: list[dict]


@router.put("/projects/{project_id}/beats")
async def put_beats(project_id: str, body: BeatsIn) -> dict:
    _project(project_id)
    beats = [{k: str(b.get(k) or "").strip() for k in ("time", "name", "content", "emotion")} for b in body.beats]
    return db.update("projects", project_id, {"beats": [b for b in beats if b["content"] or b["name"]]})


class ReviewIn(BaseModel):
    auto_fix: bool | None = None


@router.post("/projects/{project_id}/script/review")
async def review_script(project_id: str, body: ReviewIn) -> dict:
    p = _project(project_id)
    if not (p.get("script") or "").strip():
        raise HTTPException(400, "请先生成或填写剧本")
    params = {} if body.auto_fix is None else {"auto_fix": body.auto_fix}
    return queue.submit("script_review", project_id=project_id, title="剧本审稿", owner_type="project",
                        owner_id=f"{project_id}:script_review", params=params)


class WriterCfg(BaseModel):
    threshold: int | None = Field(None, ge=50, le=100)
    max_rounds: int | None = Field(None, ge=0, le=3)
    auto_review: bool | None = None
    genre: str | None = None


@router.patch("/projects/{project_id}/writer")
async def writer_cfg(project_id: str, body: WriterCfg) -> dict:
    p = _project(project_id)
    w = writer_state(p)
    values: dict[str, Any] = {}
    if body.genre is not None:
        if body.genre not in GENRES:
            raise HTTPException(400, "未知题材")
        values["genre"] = body.genre
    for k in ("threshold", "max_rounds", "auto_review"):
        if getattr(body, k) is not None:
            w[k] = getattr(body, k)
    values["writer"] = w
    db.update("projects", project_id, values)
    return await writer_status(project_id)


@router.get("/projects/{project_id}/script/versions/{index}")
async def get_version(project_id: str, index: int) -> dict:
    w = writer_state(_project(project_id))
    if not 0 <= index < len(w["versions"]):
        raise HTTPException(404, "版本不存在")
    return w["versions"][index]


@router.post("/projects/{project_id}/script/versions/{index}/restore")
async def restore_version(project_id: str, index: int) -> dict:
    p = _project(project_id)
    w = writer_state(p)
    if not 0 <= index < len(w["versions"]):
        raise HTTPException(404, "版本不存在")
    save_version(project_id, p.get("script") or "", "恢复前")
    return db.update("projects", project_id, {"script": w["versions"][index]["script"]})


class PolishIn(BaseModel):
    shot_ids: list[str] | None = None


@router.post("/projects/{project_id}/polish")
async def polish(project_id: str, body: PolishIn) -> dict:
    _project(project_id)
    return queue.submit("polish", project_id=project_id, title="台词打磨", owner_type="project",
                        owner_id=f"{project_id}:polish", params={"shot_ids": body.shot_ids or []})
