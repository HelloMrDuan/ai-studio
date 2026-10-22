"""阶段 2~4 功能测试（模型全部模拟）。"""
import json

from test_features import BOARD, LLM_EXTRA, client, drain, project  # noqa: F401
from test_flow import wait

from app import prompts, writers


def test_writers_room(client, monkeypatch):
    pid = client.post("/api/projects", json={"title": "编剧室", "source_text": "雨夜车站重逢", "genre": "suspense"}).json()["id"]
    assert client.patch(f"/api/projects/{pid}", json={"genre": "nope"}).status_code == 400
    # 节拍表
    wait(client, client.post(f"/api/projects/{pid}/beats").json()["id"])
    st = client.get(f"/api/projects/{pid}/writer").json()
    assert st["genre"] == "suspense" and st["beats"][0]["name"] == "钩子" and st["logline"]
    client.put(f"/api/projects/{pid}/beats", json={"beats": st["beats"][:2] + [{"time": "50-60秒", "name": "卡点", "content": "照片背面写着她的名字"}]})
    # 剧本按题材 + 节拍写
    seen = {}

    def script_cb(messages):
        seen["user"] = messages[1]["content"]
        return json.dumps({"title": "", "synopsis": "重逢", "script": "## 场景1：车站 · 夜\n△陈默看照片。\n陈默：（低声）是你吗？"}, ensure_ascii=False)
    monkeypatch.setitem(LLM_EXTRA, prompts.SCRIPT_SYSTEM, script_cb)
    wait(client, client.post(f"/api/projects/{pid}/script/generate").json()["id"])
    assert "题材模板：悬疑" in seen["user"] and "照片背面写着她的名字" in seen["user"]
    # 审稿：第一轮 60 分 → 自动改稿 → 第二轮 86 分
    reviews = [{"scores": {k: 60 for k, _, _ in writers.DIMENSIONS}, "summary": "钩子太弱",
                "issues": [{"dim": "hook", "where": "场景1", "problem": "开头平淡", "suggestion": "开场先给照片特写和广播"}]},
               {"scores": {k: 86 for k, _, _ in writers.DIMENSIONS}, "issues": [], "summary": "好多了"}]
    monkeypatch.setitem(LLM_EXTRA, writers.REVIEW_SYSTEM, lambda m: json.dumps(reviews.pop(0) if reviews else reviews_default, ensure_ascii=False))
    reviews_default = {"scores": {k: 90 for k, _, _ in writers.DIMENSIONS}, "issues": []}
    rewrite_seen = {}

    def rewrite_cb(messages):
        rewrite_seen["user"] = messages[1]["content"]
        return json.dumps({"script": "## 场景1：车站 · 夜\n△特写：泛黄的照片。广播响起林晚的名字。\n陈默：（猛地抬头）林晚？", "changes": ["强化开场钩子"]}, ensure_ascii=False)
    monkeypatch.setitem(LLM_EXTRA, writers.REWRITE_SYSTEM, rewrite_cb)
    r = wait(client, client.post(f"/api/projects/{pid}/script/review", json={}).json()["id"])["result"]
    assert r["before"] == 60 and r["total"] == 86 and r["rounds"] == 1 and r["passed"]
    assert "开场先给照片特写和广播" in rewrite_seen["user"]
    snap = client.get(f"/api/projects/{pid}").json()
    assert "广播响起" in snap["project"]["script"]
    st = client.get(f"/api/projects/{pid}/writer").json()
    labels = [v["label"] for v in st["versions"]]
    assert "AI 生成" in labels and "自动改稿 第1轮" in labels and st["last"]["total"] == 86
    first = labels.index("AI 生成")
    client.post(f"/api/projects/{pid}/script/versions/{first}/restore")
    assert "广播" not in client.get(f"/api/projects/{pid}").json()["project"]["script"]
    assert writers.reviewed(client.get(f"/api/projects/{pid}").json()["project"]) is False


def test_polish_suggestions(client, project, monkeypatch):
    pid = project
    shots = client.get(f"/api/projects/{pid}").json()["shots"]
    long_shot = shots[0]
    client.patch(f"/api/shots/{long_shot['id']}", json={"duration": 3, "dialogue": [
        {"speaker": "陈默", "line": "十年了，你还好吗？我每天都会来这个车站等你回来。", "emotion": ""}]})
    client.patch("/api/characters/" + client.get(f"/api/projects/{pid}").json()["characters"][0]["id"],
                 json={"speech": "话少，句子短", "catchphrase": "嗯"})
    got = {}

    def polish_cb(messages):
        got["user"] = messages[1]["content"]
        return json.dumps({"shots": [{"index": 1, "lines": [{"speaker": "陈默", "line": "十年了。你还好吗？", "emotion": "压低声音"}], "reason": "精简到时长内"},
                                     {"index": 99, "lines": [{"speaker": "x", "line": "越界"}]}]}, ensure_ascii=False)
    monkeypatch.setitem(LLM_EXTRA, writers.POLISH_SYSTEM, polish_cb)
    assert 1 in client.get(f"/api/projects/{pid}/writer").json()["overlong"]
    r = wait(client, client.post(f"/api/projects/{pid}/polish", json={}).json()["id"])["result"]
    assert r["suggestions"] == 1 and "话少，句子短" in got["user"] and "字数上限 10" in got["user"]
    sug = client.get(f"/api/projects/{pid}/suggestions?source=polish").json()
    assert len(sug) == 1 and sug[0]["new_value"][0]["line"] == "十年了。你还好吗？" and sug[0]["label"].startswith("镜头 #1")
    out = client.post(f"/api/suggestions/{sug[0]['id']}/accept").json()
    assert out["changed"] and not out["conflict"]
    assert client.get(f"/api/projects/{pid}").json()["shots"][0]["dialogue"][0]["emotion"] == "压低声音"
    assert client.get(f"/api/projects/{pid}/suggestions").json() == []
    # 再打磨一次 → 拒绝
    wait(client, client.post(f"/api/projects/{pid}/polish", json={}).json()["id"])
    assert client.get(f"/api/projects/{pid}/suggestions").json() == []          # 结果与当前一致，不产生建议


def test_director_rules_and_notes(client, monkeypatch):
    from app import director
    pid = client.post("/api/projects", json={"title": "导演", "source_text": "x"}).json()["id"]
    c1 = client.post(f"/api/projects/{pid}/characters", json={"name": "甲"}).json()["id"]
    c2 = client.post(f"/api/projects/{pid}/characters", json={"name": "乙"}).json()["id"]
    loc = client.post(f"/api/projects/{pid}/locations", json={"name": "客厅"}).json()["id"]
    base = {"location_id": loc, "scene": "客厅", "character_ids": [c1, c2], "camera_angle": "平视", "camera_move": "固定"}
    rows = [
        {**base, "shot_size": "中景", "description": "甲在画面左侧，乙在画面右侧", "duration": 2,
         "dialogue": [{"speaker": "甲", "line": "你今天为什么这么晚才回来，我等了你一个晚上", "emotion": ""}]},
        {**base, "shot_size": "中景", "description": "甲在画面右侧，乙在画面左侧", "duration": 4,
         "dialogue": [{"speaker": "乙", "line": "加班", "emotion": ""}]},
        {**base, "shot_size": "全景", "description": "两人对视", "duration": 4, "dialogue": [{"speaker": "甲", "line": "真的？", "emotion": ""}]},
        {**base, "shot_size": "近景", "description": "乙低头", "duration": 3, "character_ids": [c2]},
    ]
    ids = [client.post(f"/api/projects/{pid}/shots", json=r).json()["id"] for r in rows]
    issues = client.get(f"/api/projects/{pid}/director/check").json()["issues"]
    rules = {x["rule"] for x in issues}
    assert {"establish", "size", "axis", "duration", "reverse"} <= rules, issues
    # LLM 导演：表演 + 转场 + 参数修改
    def director_cb(messages):
        assert "【规则检查】" in messages[1]["content"] and "越轴" in messages[1]["content"]
        return json.dumps({"summary": "节奏偏平", "shots": [
            {"index": 1, "performance": "甲双手抱胸，先瞪乙再别过脸", "transition": "硬切", "intensity": 6,
             "changes": {"shot_size": "全景"}, "reason": "先交代空间"},
            {"index": 2, "performance": "乙避开视线看向地面", "transition": "叠化", "intensity": 4,
             "changes": {"camera_angle": "过肩", "shot_size": "近景"}, "reason": "正反打"},
            {"index": 9, "performance": "越界"}]}, ensure_ascii=False)
    monkeypatch.setitem(LLM_EXTRA, director.DIRECTOR_SYSTEM, director_cb)
    r = wait(client, client.post(f"/api/projects/{pid}/director").json()["id"])["result"]
    assert r["suggestions"] >= 6 and r["issues"] == len(issues)
    sug = client.get(f"/api/projects/{pid}/suggestions").json()
    fields = {(s["target_id"], s["field"]) for s in sug}
    assert (ids[0], "performance") in fields and (ids[1], "transition") in fields and (ids[1], "camera_angle") in fields
    assert any(s["source"] == "rules" for s in sug)            # LLM 没处理的规则问题补成规则建议
    assert client.post(f"/api/projects/{pid}/suggestions/bulk", json={"action": "accept"}).json()["done"] == len(sug)
    shots = client.get(f"/api/projects/{pid}").json()["shots"]
    assert shots[0]["shot_size"] == "全景" and shots[1]["transition"] == "叠化" and shots[0]["performance"].startswith("甲双手")
    left = {x["rule"] for x in client.get(f"/api/projects/{pid}/director/check").json()["issues"] if x["level"] == "warn"}
    assert not left - {"axis"}, left                          # 可自动修的规则问题已清零（轴线需人工改描述）
    from app import pipeline
    snap = client.get(f"/api/projects/{pid}").json()
    plan = client.get(f"/api/shots/{ids[0]}/plan").json()
    assert "甲双手抱胸" in json.dumps(plan, ensure_ascii=False)


def test_art_department(client, project, monkeypatch):
    from test_features import CALLS
    from app import pipeline
    pid = project
    snap = client.get(f"/api/projects/{pid}").json()
    chen = next(c for c in snap["characters"] if c["name"] == "陈默")
    assert chen["ref_image"] and chen["face_image"]
    # 三视图 + 表情表
    wait(client, client.post(f"/api/characters/{chen['id']}/sheet", json={"views": ["back"], "expressions": ["悲伤"]}).json()["id"])
    chen = next(c for c in client.get(f"/api/projects/{pid}").json()["characters"] if c["name"] == "陈默")
    assert chen["sheet"]["back"] and chen["sheet"]["expr"]["悲伤"]
    shot = client.get(f"/api/projects/{pid}").json()["shots"][0]
    client.patch(f"/api/shots/{shot['id']}", json={"character_ids": [chen["id"]], "description": "陈默背对镜头站在站台边，雨夜",
                                                   "dialogue": [{"speaker": "陈默", "line": "我回来了", "emotion": "哽咽"}]})
    plan = client.get(f"/api/shots/{shot['id']}/plan").json()
    labels = json.dumps(plan, ensure_ascii=False)
    assert "背面" in labels and "悲伤" in labels
    # 场景变体：画面里有「雨」自动用雨天变体
    loc = next(l for l in client.get(f"/api/projects/{pid}").json()["locations"])
    if not loc["ref_image"]:
        wait(client, client.post(f"/api/locations/{loc['id']}/image", json={"count": 1}).json()["id"])
    wait(client, client.post(f"/api/locations/{loc['id']}/variant", json={"variant": "雨天"}).json()["id"])
    client.patch(f"/api/shots/{shot['id']}", json={"location_id": loc["id"]})
    assert "雨天" in json.dumps(client.get(f"/api/shots/{shot['id']}/plan").json(), ensure_ascii=False)
    # 多套造型：按场次指定
    o = client.post(f"/api/characters/{chen['id']}/outfits", json={"name": "西装", "appearance": "深蓝色西装白衬衫"}).json()
    wait(client, client.post(f"/api/outfits/{o['id']}/image").json()["id"])
    r = client.post(f"/api/projects/{pid}/outfits/assign", json={"character_id": chen["id"], "outfit_id": o["id"], "scene": shot["scene"]}).json()
    assert r["shots"] >= 1
    plan = client.get(f"/api/shots/{shot['id']}/plan").json()
    assert "西装" in json.dumps(plan, ensure_ascii=False) and "深蓝色西装白衬衫" in plan["keyframe"]["prompt"]
    # 画风锁定：画风参考图 + 风格 LoRA
    from PIL import Image
    from app.config import get_settings
    img = get_settings().data_dir / "style.png"
    Image.new("RGB", (32, 32), (200, 100, 50)).save(img)
    with img.open("rb") as f:
        client.post(f"/api/projects/{pid}/style_ref", files={"file": ("style.png", f, "image/png")})
    client.patch(f"/api/projects/{pid}", json={"style_lora": "my_style.safetensors", "style_lora_strength": 0.6})
    plan = client.get(f"/api/shots/{shot['id']}/plan").json()
    assert "画风参考" in plan["keyframe"]["prompt"]
    CALLS.clear()
    wait(client, client.post(f"/api/shots/{shot['id']}/keyframe", json={"count": 1}).json()["id"])
    wf = CALLS[0]
    assert wf["7"]["class_type"] == "LoraLoaderModelOnly" and wf["7"]["inputs"]["strength_model"] == 0.6 and wf["4"]["inputs"]["model"] == ["7", 0]
    client.patch(f"/api/projects/{pid}", json={"style_lora": ""})
    # 素材库：存入 → 新项目导入（含造型、三视图）
    lib = client.post("/api/library", json={"kind": "character", "id": chen["id"]}).json()
    assert lib["images"]["ref_image"].startswith("library/") and lib["images"]["outfits"][0]["name"] == "西装"
    pid2 = client.post("/api/projects", json={"title": "第二集", "source_text": "x"}).json()["id"]
    assert client.post(f"/api/projects/{pid2}/library/import", json={"ids": [lib["id"]]}).json()["imported"] == 1
    s2 = client.get(f"/api/projects/{pid2}").json()
    c2 = s2["characters"][0]
    assert c2["name"] == "陈默" and c2["locked"] and pipeline.exists(c2["ref_image"]) and c2["sheet"]["back"]
    assert s2["outfits"][0]["name"] == "西装"
    assert any(x["id"] == lib["id"] for x in client.get("/api/library?kind=character").json())


def test_board_tools(client, project):
    from PIL import Image
    from test_features import CALLS
    from app import board, pipeline
    from app.config import get_settings
    pid = project
    snap = client.get(f"/api/projects/{pid}").json()
    chen = next(c for c in snap["characters"] if c["name"] == "陈默")
    shot = snap["shots"][0]
    # 站位 + 姿势参考
    client.patch(f"/api/shots/{shot['id']}", json={"character_ids": [chen["id"]], "positions": {chen["id"]: "左"}})
    img = get_settings().data_dir / "pose.png"
    Image.new("RGB", (40, 60), (10, 10, 10)).save(img)
    with img.open("rb") as f:
        assert client.post(f"/api/shots/{shot['id']}/pose", files={"file": ("pose.png", f, "image/png")}).json()["pose_ref"]
    plan = client.get(f"/api/shots/{shot['id']}/plan").json()["keyframe"]
    assert "「陈默」位于画面左侧三分之一处" in plan["prompt"] and "姿势参考" in plan["prompt"] and plan["refs"][-1].endswith(".png")
    # 局部重绘：只替换框选区域，其余像素不变
    k = get_settings().data_dir / "kf.png"
    Image.new("RGB", (400, 200), (255, 255, 255)).save(k)
    with k.open("rb") as f:
        client.post(f"/api/shots/{shot['id']}/upload", files={"file": ("kf.png", f, "image/png")})
    CALLS.clear()
    wait(client, client.post(f"/api/shots/{shot['id']}/keyframe/region",
                             json={"instruction": "把杯子换成红色", "box": [0.5, 0.5, 0.25, 0.25]}).json()["id"])
    wf = CALLS[0]
    lat = next(n for n in wf.values() if n["class_type"] == "EmptyLatentImage")["inputs"]
    assert lat["width"] % 16 == 0 and lat["width"] > 400   # 裁切区放大生成
    cur = client.get(f"/api/projects/{pid}").json()["shots"][0]
    out = Image.open(pipeline.absolute(cur["keyframe"])).convert("RGB")
    assert out.size == (400, 200)
    assert out.getpixel((10, 10)) == (255, 255, 255)          # 框外不动
    assert out.getpixel((250, 125)) != (255, 255, 255)       # 框内已替换
    # 多版本对比
    cmp = client.get(f"/api/shots/{shot['id']}/compare").json()["items"]
    assert len(cmp) >= 2 and any(x["current"] and x["edit"] == "把杯子换成红色" and x["region"] for x in cmp)
    inner, outer = board.crop_box(1000, 500, [0.9, 0.9, 0.5, 0.5])
    assert outer[2] <= 1000 and outer[3] <= 500 and inner[0] == 900


def test_video_tools(client, project, monkeypatch, tmp_path):
    import json as _j
    from test_features import CALLS
    from app import pipeline, videox
    from app.config import get_settings
    pid = project
    shot = client.get(f"/api/projects/{pid}").json()["shots"][0]
    client.patch(f"/api/shots/{shot['id']}", json={"camera_move": "摇镜", "camera_dir": "左"})
    prompt = client.get(f"/api/shots/{shot['id']}/plan").json()["video"]["prompt"]
    assert "[左摇]" in prompt and "向左" in prompt
    # 运镜 LoRA：串在模型链末端
    (get_settings().data_dir / "camera_loras.json").write_text(_j.dumps({"摇镜": "pan.safetensors"}), "utf-8")
    if not client.get(f"/api/projects/{pid}").json()["shots"][0]["keyframe"]:
        wait(client, client.post(f"/api/shots/{shot['id']}/keyframe", json={"count": 1}).json()["id"])
    CALLS.clear()
    wait(client, client.post(f"/api/shots/{shot['id']}/video", json={}).json()["id"])
    wf = next(w for w in CALLS if any(n["class_type"] == "SaveVideo" for n in w.values()))
    assert wf["8"]["inputs"]["lora_name"] == "pan.safetensors"
    assert wf["20"]["inputs"]["model"] == (["9", 0] if "9" in wf else ["8", 0])   # 草稿有推理缓存时缓存在最外层
    # 视频延长：尾帧续写 + 拼接，时长变长
    cur = client.get(f"/api/projects/{pid}").json()["shots"][0]
    before = asyncio_run(pipeline.media.probe(pipeline.absolute(cur["video"])))["duration"]
    t = wait(client, client.post(f"/api/shots/{shot['id']}/video/extend", json={"seconds": 3, "instruction": "他转身离开"}).json()["id"])
    assert t["status"] == "done", t
    cur = client.get(f"/api/projects/{pid}").json()["shots"][0]
    after = asyncio_run(pipeline.media.probe(pipeline.absolute(cur["video"])))["duration"]
    assert after > before + 2 and cur["duration"] >= after - 0.2
    cmp = client.get(f"/api/shots/{shot['id']}/compare?kind=video").json()["items"]
    assert any(x["mode"] == "extend" and x["current"] for x in cmp)
    # 动作参考：没配置工作流时明确报错；配置后按占位符填充
    assert client.post(f"/api/shots/{shot['id']}/video/motion").status_code == 400
    wfdir = get_settings().data_dir / "workflows"
    wfdir.mkdir(exist_ok=True)
    (wfdir / "motion.json").write_text(_j.dumps({"1": {"class_type": "LoadImage", "inputs": {"image": "{{IMAGE}}"}},
                                                 "2": {"class_type": "LoadVideo", "inputs": {"file": "{{VIDEO}}", "w": "{{WIDTH}}"}},
                                                 "9": {"class_type": "SaveVideo", "inputs": {"t": "提示：{{PROMPT}}"}}}), "utf-8")
    with pipeline.absolute(cur["video"]).open("rb") as f:
        assert client.post(f"/api/shots/{shot['id']}/motion", files={"file": ("m.mp4", f, "video/mp4")}).json()["motion_ref"]
    CALLS.clear()
    t = wait(client, client.post(f"/api/shots/{shot['id']}/video/motion", json={}).json()["id"])
    assert t["status"] == "done", t
    assert isinstance(CALLS[0]["2"]["inputs"]["w"], int) and CALLS[0]["9"]["inputs"]["t"].startswith("提示：")
    assert videox.fill({"a": ["{{X}}", "x{{X}}"]}, {"X": 3}) == {"a": [3, "x3"]}
    (wfdir / "motion.json").unlink()
    (get_settings().data_dir / "camera_loras.json").unlink()


def asyncio_run(coro):
    import asyncio
    return asyncio.new_event_loop().run_until_complete(coro)


def test_vlm_review(client, project, monkeypatch):
    import httpx
    from app import vlm
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "vlm_base_url", "http://vlm.test/v1")
    monkeypatch.setattr(s, "vlm_model", "qwen-vl")
    assert vlm.provider() == "api" and vlm.auto_enabled() and client.get("/api/meta").json()["vlm"]
    seen = []
    answers = [{"score": 3, "people": 2, "issues": [{"type": "anatomy", "severity": "high", "detail": "左手六根手指"}],
                "fix": "陈默双手自然下垂，五指正常"}]
    real_post = httpx.AsyncClient.post

    async def fake_post(self, url, *a, **kw):
        if "vlm.test" not in str(url):
            return await real_post(self, url, *a, **kw)
        body = kw["json"]
        seen.append(body)
        content = body["messages"][1]["content"]
        assert content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,") and "应有画面" in content[-1]["text"]
        ans = answers.pop(0) if answers else {"score": 9, "people": 1, "issues": [], "fix": ""}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(ans, ensure_ascii=False)}}]},
                              request=httpx.Request("POST", url))
    monkeypatch.setattr(httpx.AsyncClient, "post", fake_post)
    pid = project
    shot = client.get(f"/api/projects/{pid}").json()["shots"][0]
    # 出图后即时审：第一张六指 → 自动重抽 → 选合格的那张
    wait(client, client.post(f"/api/shots/{shot['id']}/keyframe", json={"count": 1, "auto_select": True}).json()["id"])
    cmp = client.get(f"/api/shots/{shot['id']}/compare").json()["items"][-2:]
    assert cmp[0]["qa_ok"] is False and "六根手指" in cmp[0]["qa"][0] and cmp[1]["qa_ok"]
    # 整片审片（视频抽帧网格）+ 报告
    wait(client, client.post(f"/api/shots/{shot['id']}/video", json={}).json()["id"])
    answers.append({"score": 4, "issues": [{"type": "flicker", "severity": "high", "detail": "第3帧人脸变成另一个人"}], "fix": "人物保持同一张脸"})
    n0 = len(seen)
    r = wait(client, client.post(f"/api/projects/{pid}/review", json={}).json()["id"])
    snap = client.get(f"/api/projects/{pid}").json()
    rep = snap["project"]["review_report"]
    assert rep["reviewed"] >= 1 and 1 in rep["bad"] and rep["issues"]["flicker"] == 1
    rv = snap["shots"][0]["review"]
    assert rv["target"] == "video" and not rv["ok"] and len(seen) > n0
    # 一键返工：问题写进提示词
    tasks = client.post(f"/api/projects/{pid}/review/rework", json={}).json()["tasks"]
    assert len(tasks) == 1
    t = next(x for x in client.get("/api/tasks?limit=500").json() if x["id"] == tasks[0])
    assert t["type"] == "video" and "第3帧人脸变成另一个人" in t["params"]["prompt"] and "同一张脸" in t["params"]["prompt"]
    wait(client, tasks[0])
    # 音画核对
    sh = {"dialogue": [{"speaker": "陈默", "line": "我回来了，你还好吗？"}]}
    assert vlm.dialogue_match(sh, "我回来了你还好吗") is None
    assert vlm.dialogue_match(sh, "今天天气不错")["type"] == "audio"
    assert vlm.normalize({"score": "12", "issues": [{"type": "x", "detail": "d"}]})["score"] == 10


def test_color_grade(client, project, tmp_path):
    import subprocess
    import time as _t
    from app import grade, pipeline

    def solid(path, color):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=c={color}:s=160x90:d=1:r=24",
                        "-f", "lavfi", "-i", "sine=d=1", "-shortest", "-pix_fmt", "yuv420p", str(path)], check=True)
        return path
    a, b, c = solid(tmp_path / "a.mp4", "0x806040"), solid(tmp_path / "b.mp4", "0x4060a0"), solid(tmp_path / "c.mp4", "0x7a5a44")
    shots = [{"id": "1", "sort": 0, "scene": "S1"}, {"id": "2", "sort": 1, "scene": "S1"}, {"id": "3", "sort": 2, "scene": "S1"}]
    t0 = _t.time()
    gp = asyncio_run(grade.plan([a, b, c], shots, tmp_path / "w", strength=1.0, base_shot="1"))
    assert _t.time() - t0 < 20
    assert gp["before"] > 15 and gp["after"] < gp["before"] / 3, gp
    assert gp["cubes"][0] is None and gp["cubes"][1].read_text().startswith('TITLE')
    # 风格 LUT 对所有镜头生效；黑白 LUT 把彩色变灰
    fn = grade.style_fn("黑白")
    r, g, b2 = fn(0.8, 0.2, 0.1)
    assert abs(r - g) < 1e-6 and abs(g - b2) < 1e-6
    # 成片：开启调色统一
    pid = project
    shot = client.get(f"/api/projects/{pid}").json()["shots"][0]
    if not shot["video"]:
        wait(client, client.post(f"/api/shots/{shot['id']}/video", json={}).json()["id"])
    client.patch(f"/api/projects/{pid}", json={"grade": {"enabled": True, "lut": "电影", "deflicker": True}})
    wait(client, client.post(f"/api/projects/{pid}/assemble", json={}).json()["id"])
    fin = [x for x in client.get(f"/api/projects/{pid}").json()["assets"][pid] if x["kind"] == "final"][0]
    assert fin["meta"]["grade"]["lut"] == "电影" and pipeline.exists(fin["path"])


def test_packaging(client, project):
    from app import package, pipeline
    pid = project
    shots = client.get(f"/api/projects/{pid}").json()["shots"][:2]
    if not shots[0]["keyframe"]:
        wait(client, client.post(f"/api/shots/{shots[0]['id']}/keyframe", json={"count": 1}).json()["id"])
    for s in shots:
        if not s["video"]:
            wait(client, client.post(f"/api/shots/{s['id']}/video", json={}).json()["id"])
    client.patch(f"/api/shots/{shots[1]['id']}", json={"transition": "叠化", "dialogue": [{"speaker": "陈默", "line": "【真相】只有一个"}]})
    snap = client.get(f"/api/projects/{pid}").json()
    vids = [s for s in snap["shots"] if s["video"]]
    durs = [asyncio_run(pipeline.media.probe(pipeline.absolute(s["video"])))["duration"] for s in vids]
    client.patch(f"/api/projects/{pid}", json={"grade": {}, "post": {
        "title_card": {"enabled": True, "seconds": 2, "subtitle": "第 1 集"}, "end_card": {"enabled": True, "seconds": 2},
        "subtitle": {"preset": "短剧", "keywords": "只有"}}})
    wait(client, client.post(f"/api/projects/{pid}/assemble", json={}).json()["id"])
    snap = client.get(f"/api/projects/{pid}").json()
    fin = [x for x in snap["assets"][pid] if x["kind"] == "final"][0]
    total = asyncio_run(pipeline.media.probe(pipeline.absolute(fin["path"])))["duration"]
    # 片头 2s + 片尾 2s + 正片，减去转场重叠（片头→正片叠化、#2 叠化、正片→片尾黑场）
    assert fin["meta"]["cards"] == 2 and fin["meta"]["transitions"] >= 3
    assert sum(durs) + 4 - 2.5 < total < sum(durs) + 4, (total, durs)
    assert snap["project"]["cover"] and pipeline.exists(snap["project"]["cover"])
    srt = (pipeline.project_dir(pid, "final") / "subtitles.srt").read_text("utf-8")
    assert "【真相】只有一个" in srt
    # ASS：关键词花字 + 逐字高亮
    tl = [(0.0, 3.0, [{"speaker": "陈默", "line": "【真相】只有一个"}], None)]
    ass = package.build_ass(tl, 768, 1344, {"preset": "短剧", "keywords": "只有"})
    assert "\\c&H0000D7FF" in ass and "真相" in ass and "【" not in ass
    ass = package.build_ass(tl, 768, 1344, {"karaoke": True})
    assert ass.count("\\kf") == 6
    # 配音时间优先
    lt = package.line_timing(10, 5, [{"line": "a"}, {"line": "b"}], [{"j": 1, "start": 1.0, "dur": 2.0}])
    assert lt[1] == (11.0, 13.0, {"line": "b"}) and lt[0][0] == 10 and lt[0][2] == {"line": "a"}   # 未配音的句子仍有字幕
    # 手动封面：指定镜头 + 竖版
    r = client.post(f"/api/projects/{pid}/cover", json={"shot_id": vids[0]["id"], "title": "重生之我是\n总裁", "aspect": "9:16"}).json()
    from PIL import Image
    assert Image.open(pipeline.absolute(r["cover"])).size == (1080, 1920)


def test_sound_post(client, project, monkeypatch, tmp_path):
    import asyncio
    import shutil
    import subprocess
    from pathlib import Path
    from app import comfy, pipeline, sound, voice

    def tone(path, freq, d, vol=0.5):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"sine=f={freq}:d={d}", "-af", f"volume={vol}",
                        "-ar", "48000", str(path)], check=True)
        return path
    pid = project
    snap = client.get(f"/api/projects/{pid}").json()
    shots = snap["shots"][:2]
    for s in shots:
        if not s["video"]:
            wait(client, client.post(f"/api/shots/{s['id']}/video", json={}).json()["id"])
    # 音效库：按文件名标签匹配镜头的「环境音」描述
    for name in ("雨声_环境.wav", "关门声.wav", "脚步_走廊.wav"):
        tone(sound.sfx_dir() / name, 300, 1.0, 0.3)
    m = sound.match_sfx("窗外雨声、远处关门声")
    assert [x["name"] for x in m] == ["雨声_环境.wav", "关门声.wav"] and m[0]["loop"]
    assert client.get("/api/sfx").json()[0]["tags"]
    client.patch(f"/api/shots/{shots[0]['id']}", json={"sound": "雨声", "narration": "那一夜，他终于等到了她。",
                                                       "dialogue": [{"speaker": "旁白", "line": "三年前"}]})
    # 解说词（LLM）：不覆盖已有
    from test_features import LLM_EXTRA
    monkeypatch.setitem(LLM_EXTRA, sound.NARR_SYSTEM, lambda m: json.dumps(
        {"lines": [{"n": 1, "text": "不该覆盖"}, {"n": 2, "text": "她没有回头。"}]}, ensure_ascii=False))
    assert wait(client, client.post(f"/api/projects/{pid}/narration/write", json={}).json()["id"])["result"]["written"] == 1
    snap = client.get(f"/api/projects/{pid}").json()
    assert snap["shots"][0]["narration"].startswith("那一夜") and snap["shots"][1]["narration"] == "她没有回头。"
    # 旁白配音：上传旁白音色 → 合成（TTS 模拟）
    monkeypatch.setattr(voice, "tts_available", lambda: True)

    async def fake_synth(items, log_cb=None):
        for it in items:
            await asyncio.to_thread(tone, Path(it["out"]), 600, 1.2)
    monkeypatch.setattr(voice, "synthesize", fake_synth)
    ref = tone(tmp_path / "narr.wav", 500, 2)
    with open(ref, "rb") as fh:
        client.post(f"/api/projects/{pid}/narration/voice", files={"file": ("narr.wav", fh, "audio/wav")})
    r = wait(client, client.post(f"/api/projects/{pid}/narration", json={}).json()["id"])
    assert r["status"] == "done" and r["result"]["lines"] == 3, r
    snap = client.get(f"/api/projects/{pid}").json()
    narr = [x for x in snap["assets"][shots[0]["id"]] if x["kind"] == "narration"][0]
    assert [t["j"] for t in narr["meta"]["timing"]] == [0, 1] and narr["meta"]["timing"][1]["start"] > 1.2
    # 配乐：情绪分段 + ACE-Step 工作流（ComfyUI 模拟出音频）
    wf = sound.music_workflow("sad, piano", 12, 7)
    assert {n["class_type"] for n in wf.values()} >= {"TextEncodeAceStepAudio", "EmptyAceStepLatentAudio", "VAEDecodeAudio"}
    segs = sound.mood_segments([{"id": "a", "scene": "1", "description": "她哭着告别"}, {"id": "b", "scene": "1"},
                                {"id": "c", "scene": "2", "description": "他当众反击打脸"}], [6, 6, 10])
    assert [g["mood"] for g in segs] == ["悲伤", "燃"] and segs[0]["shots"] == ["a", "b"]
    real = comfy.run

    async def fake_music(workflow, out_dir, progress=None):
        if not any(n["class_type"] == "SaveAudio" for n in workflow.values()):
            return await real(workflow, out_dir, progress)
        out_dir.mkdir(parents=True, exist_ok=True)
        secs = next(n for n in workflow.values() if n["class_type"] == "EmptyAceStepLatentAudio")["inputs"]["seconds"]
        return [await asyncio.to_thread(tone, out_dir / "bgm_00001_.flac", 220, secs, 0.6)]
    monkeypatch.setattr(comfy, "run", fake_music)
    r = wait(client, client.post(f"/api/projects/{pid}/bgm", json={}).json()["id"])
    assert r["status"] == "done", r
    snap = client.get(f"/api/projects/{pid}").json()
    assert snap["project"]["bgm"] and pipeline.exists(snap["project"]["bgm"])
    # 成片：旁白 + 音效 + 生成的 BGM（闪避）+ 响度标准化到 -14 LUFS
    client.patch(f"/api/projects/{pid}", json={"grade": {}, "post": {"sound": {"bgm_volume": 0.3}}})
    wait(client, client.post(f"/api/projects/{pid}/assemble", json={}).json()["id"])
    snap = client.get(f"/api/projects/{pid}").json()
    fin = [x for x in snap["assets"][pid] if x["kind"] == "final"][0]
    info = fin["meta"]["sound"]
    assert info["narration"] >= 1 and info["sfx"] >= 1 and info["bgm"] == "生成" and info["layers"] == ["bgm", "narr", "sfx"], info
    lufs = asyncio_run(sound.measure_lufs(pipeline.absolute(fin["path"])))
    assert abs(lufs - sound.LUFS) < 1.5, lufs
    srt = (pipeline.project_dir(pid, "final") / "subtitles.srt").read_text("utf-8")
    assert "那一夜" in srt and "三年前" in srt
    shutil.rmtree(sound.sfx_dir(), ignore_errors=True)


def test_enhance(client, project, monkeypatch):
    import subprocess
    from app import comfy, enhance, pipeline
    from test_features import CALLS
    pid = project
    shot = client.get(f"/api/projects/{pid}").json()["shots"][0]
    if not shot["video"]:
        wait(client, client.post(f"/api/shots/{shot['id']}/video", json={}).json()["id"])
    assert enhance.target_size(768, 1344, 1080) == (1080, 1920) and enhance.target_size(320, 180, 1080) == (1920, 1080)
    # 实测：草稿 480×864 超分成 1080×1944（非标准 9:16）→ 对齐标准尺寸；明显不标准的画幅保持比例
    assert enhance.target_size(480, 864, 1080) == (1080, 1920) and enhance.target_size(480, 864, 2160) == (2160, 3840)
    assert enhance.target_size(1000, 500, 1080) == (2160, 1080)
    # 补帧：没装 RIFE 时用 ffmpeg 运动补偿插帧
    monkeypatch.setattr(enhance, "rife_available", lambda: False)
    r = wait(client, client.post(f"/api/shots/{shot['id']}/interpolate", json={"fps": 30}).json()["id"])
    assert r["result"]["method"] == "ffmpeg"
    snap = client.get(f"/api/projects/{pid}").json()
    cur = next(s for s in snap["shots"] if s["id"] == shot["id"])
    info = asyncio_run(enhance.video_info(pipeline.absolute(cur["video"])))
    assert abs(info["fps"] - 30) < 0.01, info
    # RIFE：倍数补帧后重采样到目标帧率
    monkeypatch.setattr(enhance, "rife_available", lambda: True)
    n0 = len(CALLS)
    wait(client, client.post(f"/api/shots/{shot['id']}/interpolate", json={"fps": 60}).json()["id"])
    wf = CALLS[n0]
    assert wf["3"]["class_type"] == "RIFE VFI" and wf["3"]["inputs"]["multiplier"] == 2      # 30 → ×2 → 60
    cur = next(s for s in client.get(f"/api/projects/{pid}").json()["shots"] if s["id"] == shot["id"])
    assert abs(asyncio_run(enhance.video_info(pipeline.absolute(cur["video"])))["fps"] - 60) < 0.01
    # 超分（Real-ESRGAN 脚本模拟成 ffmpeg 缩放）
    monkeypatch.setattr(enhance, "esrgan_available", lambda: True)

    async def fake_esrgan(src, out, width, height, model, ctx):
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(src), "-vf", f"scale={width}:{height}", "-c:a", "copy", str(out)], check=True)
    monkeypatch.setattr(enhance, "run_esrgan", fake_esrgan)
    r = wait(client, client.post(f"/api/shots/{shot['id']}/upscale", json={"target": "1080p"}).json()["id"])
    assert r["result"]["size"] == "1920x1080"
    snap = client.get(f"/api/projects/{pid}").json()
    a = next(x for x in snap["assets"][shot["id"]] if x["path"] == next(s for s in snap["shots"] if s["id"] == shot["id"])["video"])
    assert a["meta"]["enhanced"] == ["补帧 30fps", "补帧 60fps", "超分 1080p"] and a["meta"]["upscale"]["to"] == "1920x1080"
    # 批量：已超分的跳过
    assert client.post(f"/api/projects/{pid}/enhance", json={"kind": "upscale"}).json()["submitted"] == \
        sum(1 for s in snap["shots"] if s["video"]) - 1
    drain(client, pid)
    # 成片：1080p · 30fps
    client.patch(f"/api/projects/{pid}", json={"grade": {}, "post": {"fps": 30, "sound": {"loudnorm": False}}})
    wait(client, client.post(f"/api/projects/{pid}/assemble", json={"quality": "1080p"}).json()["id"])
    fin = [x for x in client.get(f"/api/projects/{pid}").json()["assets"][pid] if x["kind"] == "final"][0]
    info = asyncio_run(enhance.video_info(pipeline.absolute(fin["path"])))
    assert fin["meta"]["fps"] == 30 and abs(info["fps"] - 30) < 0.01 and min(info["width"], info["height"]) == 1080, info


def test_export_project(client, project, tmp_path):
    import subprocess
    import sys
    import zipfile
    import xml.etree.ElementTree as ET
    from app import db, pipeline
    pid = project
    snap = client.get(f"/api/projects/{pid}").json()
    for s in snap["shots"][:2]:
        if not s["video"]:
            wait(client, client.post(f"/api/shots/{s['id']}/video", json={}).json()["id"])
    snap = client.get(f"/api/projects/{pid}").json()
    s0 = next(s for s in snap["shots"] if s["video"])
    # 模拟配音分轨（对白 + 环境音）
    stems = {}
    for k in ("speech", "ambient"):
        p = pipeline.project_dir(pid, "dub") / f"{k}_test.wav"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", "sine=d=2", str(p)], check=True)
        stems[k] = pipeline.rel(p)
    row = db.query("SELECT id, meta FROM assets WHERE path=?", [s0["video"]])[0]
    db.update("assets", row["id"], {"meta": {**(row["meta"] or {}), "stems": stems}})
    client.patch(f"/api/shots/{s0['id']}", json={"dialogue": [{"speaker": "陈默", "line": "是你吗"}]})
    r = wait(client, client.post(f"/api/projects/{pid}/export", json={"fps": 30}).json()["id"])
    tr = r["result"]["tracks"]
    assert tr["dialogue"] == 1 and tr["ambient"] == 1 and tr["subs"] >= 1, tr
    zp = pipeline.absolute(r["result"]["path"])
    with zipfile.ZipFile(zp) as z:
        names = z.namelist()
        draft_name = next(n for n in names if n.endswith("draft_content.json"))
        draft = json.loads(z.read(draft_name))
        fcp = z.read(next(n for n in names if n.endswith(".fcpxml"))).decode()
        edl = z.read(next(n for n in names if n.endswith(".edl"))).decode()
        z.extractall(tmp_path / "x")
    assert any(n.startswith("media/v01_") for n in names) and "修复素材路径.bat" in names
    kinds = {t["name"]: t["type"] for t in draft["tracks"]}
    assert kinds["画面"] == "video" and kinds["对白"] == "audio" and kinds["环境音"] == "audio" and kinds["字幕"] == "text"
    vt = next(t for t in draft["tracks"] if t["type"] == "video")["segments"]
    assert vt[0]["volume"] == 0.0                                       # 有分轨的镜头画面静音
    ends = [sg["target_timerange"]["start"] + sg["target_timerange"]["duration"] for sg in vt]
    assert all(abs(vt[i + 1]["target_timerange"]["start"] - ends[i]) <= 1 for i in range(len(vt) - 1))  # 首尾相接
    assert abs(ends[-1] - draft["duration"]) <= 1
    mat_ids = {m["id"] for k in ("videos", "audios", "texts") for m in draft["materials"][k]}
    assert all(sg["material_id"] in mat_ids for t in draft["tracks"] for sg in t["segments"])
    root = ET.fromstring(fcp.split("\n", 2)[2])
    assert len(root.findall(".//spine/asset-clip")) == len(vt)
    assert edl.count("FROM CLIP NAME") == len(vt)
    subprocess.run([sys.executable, str(tmp_path / "x" / "fix_paths.py")], check=True, capture_output=True)
    fixed = (tmp_path / "x" / draft_name).read_text("utf-8")
    assert "__MEDIA_ROOT__" not in fixed and (tmp_path / "x" / "media").as_posix() in fixed


def test_series(client, monkeypatch):
    from PIL import Image
    from app import db, pipeline, series
    from test_features import LLM_EXTRA
    sid = client.post("/api/series", json={"title": "重生之逆袭", "source_text": "长篇小说……" * 50, "genre": "revenge"}).json()["id"]
    plan = {"bible": "林晚：被陷害的千金，冷静隐忍。陈默：她的青梅竹马。",
            "episodes": [{"title": "雨夜", "synopsis": "林晚被赶出家门", "source": "雨夜，林晚被继母赶出家门……", "hook": "陌生人递来一把伞"},
                         {"title": "重逢", "synopsis": "陈默出现", "source": "撑伞的人竟是陈默……", "hook": "他手里有她母亲的遗书"},
                         {"title": "遗书", "synopsis": "真相初现", "source": "遗书里写着……", "hook": "继母的电话打来"}]}
    monkeypatch.setitem(LLM_EXTRA, series.PLAN_SYSTEM, lambda m: json.dumps(plan, ensure_ascii=False))
    r = wait(client, client.post(f"/api/series/{sid}/plan", json={"seconds": 90}).json()["id"])
    assert r["result"]["episodes"] == 3 and r["result"]["created"] == 3
    st = client.get(f"/api/series/{sid}").json()
    assert [e["episode"] for e in st["episodes"]] == [1, 2, 3] and st["series"]["bible"].startswith("林晚")
    ep1, ep2 = st["episodes"][0]["id"], st["episodes"][1]["id"]
    # 第一集定好角色（带定妆图、服装）→ 新一集继承，文件复制到新项目
    img = pipeline.project_dir(ep1, "images") / "lin.png"
    Image.new("RGB", (32, 32), (200, 50, 50)).save(img)
    c = db.insert("characters", {"project_id": ep1, "name": "林晚", "ref_image": pipeline.rel(img), "locked": 1,
                                 "sheet": {"front": pipeline.rel(img)}})
    db.insert("outfits", {"project_id": ep1, "character_id": c["id"], "name": "白裙", "ref_image": pipeline.rel(img)})
    client.patch(f"/api/projects/{ep1}", json={"style": "anime", "post": {"subtitle": {"preset": "短剧"}}})
    ep4 = client.post(f"/api/series/{sid}/episodes", json={"source_text": "第四集", "copy_from": ep1}).json()
    assert ep4["episode"] == 4 and ep4["copied"]["characters"] == 1 and ep4["copied"]["outfits"] == 1
    p4 = client.get(f"/api/projects/{ep4['id']}").json()
    ch = p4["characters"][0]
    assert ch["ref_image"].startswith(ep4["id"] + "/") and pipeline.exists(ch["ref_image"]) and ch["sheet"]["front"] == ch["ref_image"]
    assert p4["project"]["style"] == "anime" and p4["project"]["post"]["subtitle"]["preset"] == "短剧"
    o = db.query("SELECT * FROM outfits WHERE project_id=?", [ep4["id"]])[0]
    assert o["character_id"] == ch["id"]
    # 前情摘要 → 写进第 2 集的剧本提示词
    db.update("projects", ep1, {"script": "## 场景1\n林晚被赶出家门。陌生人递来一把伞。"})
    monkeypatch.setitem(LLM_EXTRA, series.RECAP_SYSTEM, lambda m: json.dumps({"recap": "林晚被继母赶出家门。", "last_hook": "陌生人递伞"}, ensure_ascii=False))
    assert client.post(f"/api/series/{sid}/recap", json={}).json()["submitted"] == 3
    drain(client, ep2)
    assert "陌生人递伞" in client.get(f"/api/projects/{ep2}").json()["project"]["recap"]
    seen = {}

    def script_cb(messages):
        seen["user"] = messages[1]["content"]
        return json.dumps({"title": "", "synopsis": "重逢", "script": "## 场景1\n陈默撑伞。"}, ensure_ascii=False)
    monkeypatch.setitem(LLM_EXTRA, prompts.SCRIPT_SYSTEM, script_cb)
    wait(client, client.post(f"/api/projects/{ep2}/script/generate").json()["id"])
    u = seen["user"]
    assert "第 2 集" in u and "【前情摘要】" in u and "陌生人递伞" in u and "他手里有她母亲的遗书" in u and "青梅竹马" in u
    # 批量一键成片：没有素材的集报错但不影响其它集
    empty = client.post(f"/api/series/{sid}/episodes", json={}).json()
    r = client.post(f"/api/series/{sid}/autopilot", json={"episode_ids": [empty["id"]]}).json()
    assert empty["id"] in r["errors"] and not r["started"]
    assert client.get("/api/series").json()[0]["episodes"] == 5
    # 删除系列：各集保留为独立项目
    client.delete(f"/api/series/{sid}")
    assert client.get(f"/api/projects/{ep2}").json()["project"]["series_id"] == ""


def _boxes_clip(path, xs, seconds=2.0, move=0):
    import subprocess
    args = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"color=c=0x101010:s=320x180:r=24:d={seconds}"]
    chain, last = [], "[0]"
    for i, x in enumerate(xs):
        args += ["-f", "lavfi", "-i", f"color=c=white:s=36x70:r=24:d={seconds}"]
        xe = f"{x}+{move}*t" if move else str(x)
        chain.append(f"{last}[{i + 1}]overlay=x='{xe}':y=55:shortest=1[v{i}]")
        last = f"[v{i}]"
    args += ["-filter_complex", ";".join(chain), "-map", last, "-c:v", "libx264", "-pix_fmt", "yuv420p", str(path)]
    subprocess.run(args, check=True)


def test_reframe_analyze(tmp_path):
    from app import reframe
    one = tmp_path / "one.mp4"
    _boxes_clip(one, [240])
    p = asyncio_run(reframe.analyze(one, {}, "9:16"))
    assert p["mode"] == "crop" and p["horizontal"] and p["center"] > 0.65 and not p["centers"], p
    f = reframe.crop_filter(p, 320, 180)
    w, h, x, _ = f[5:].split(":")
    assert int(w) == int(320 * p["frac"]) // 2 * 2 and int(h) == 180 and 240 + 36 <= int(x) + int(w) and int(x) <= 240
    three = tmp_path / "three.mp4"
    _boxes_clip(three, [8, 142, 276])
    assert asyncio_run(reframe.analyze(three, {}, "9:16"))["mode"] == "pad"
    moving = tmp_path / "move.mp4"
    _boxes_clip(moving, [20], seconds=3.0, move=80)
    pm = asyncio_run(reframe.analyze(moving, {}, "9:16"))
    assert pm["mode"] == "crop" and len(pm["centers"]) >= 3 and pm["centers"][-1][1] > pm["centers"][0][1] + 0.3, pm
    assert "if(lt(t," in reframe.crop_filter(pm, 320, 180)
    # 同画幅不处理；横 → 1:1 裁左右；手动位置优先
    assert asyncio_run(reframe.analyze(one, {}, "16:9"))["mode"] == "none"
    d = reframe.decide(p, {"center": 0.1}, "smart")
    assert d["mode"] == "crop" and reframe.crop_filter(d, 320, 180).endswith(":0:0")
    assert reframe.decide(p, {"mode": "pad"}, "smart")["mode"] == "pad"
    assert reframe.decide(p, None, "pad")["mode"] == "pad"
    # 所有滤镜都能被 ffmpeg 执行
    import subprocess
    for plan in (p, pm, {**p, "mode": "pad"}):
        vf = reframe.filter_for(plan, 480, 864) + ",scale=480:864:force_original_aspect_ratio=decrease,pad=480:864:(ow-iw)/2:(oh-ih)/2"
        out = tmp_path / "o.mp4"
        subprocess.run(["ffmpeg", "-y", "-v", "error", "-i", str(moving), "-vf", vf, "-t", "1", str(out)], check=True)
        assert asyncio_run(reframe.media.probe(out))["width"] == 480


def test_reframe_assemble_and_clone(client, project):
    from app import pipeline
    pid = project
    snap = client.get(f"/api/projects/{pid}").json()
    for s in snap["shots"][:2]:
        if not s["video"]:
            wait(client, client.post(f"/api/shots/{s['id']}/video", json={}).json()["id"])
    snap = client.get(f"/api/projects/{pid}").json()
    before = snap["project"]["final_video"]
    target = "9:16" if snap["project"]["aspect_ratio"] != "9:16" else "16:9"
    plan = client.post(f"/api/projects/{pid}/reframe/plan", json={"aspect": target}).json()
    assert plan["shots"] and all(x["mode"] in ("crop", "pad") for x in plan["shots"]), plan
    sid = plan["shots"][0]["shot_id"]
    client.patch(f"/api/projects/{pid}", json={"post": {**(snap["project"].get("post") or {}),
                                                         "reframe": {"mode": "smart", "shots": {sid: {"mode": "pad"}}}}})
    plan2 = client.post(f"/api/projects/{pid}/reframe/plan", json={"aspect": target}).json()
    assert plan2["shots"][0]["mode"] == "pad"
    assert client.post(f"/api/projects/{pid}/reframe/plan", json={"aspect": "2:1"}).status_code == 400
    r = wait(client, client.post(f"/api/projects/{pid}/assemble", json={"aspect": target, "quality": "draft"}).json()["id"])
    assert r["status"] == "done", r
    assert r["result"]["aspect"] == target
    info = asyncio_run(pipeline.media.probe(pipeline.absolute(r["result"]["path"])))
    w, h = pipeline.VIDEO_SIZES["draft"][target]
    assert (info["width"], info["height"]) == (w, h)
    p2 = client.get(f"/api/projects/{pid}").json()
    assert p2["project"]["final_video"] == before                    # 换画幅版本不替换当前成片
    fin = next(a for a in p2["assets"][pid] if a["path"] == r["result"]["path"])
    assert fin["meta"]["aspect"] == target and fin["meta"]["reframe"]["pad"] >= 1
    # 重新出图：复制成新画幅项目
    c = client.post(f"/api/projects/{pid}/clone", json={"aspect": target}).json()
    assert c["aspect_ratio"] == target and c["copied"]["shots"] == len(snap["shots"])
    cs = client.get(f"/api/projects/{c['id']}").json()
    new_chars = {x["id"] for x in cs["characters"]}
    new_locs = {x["id"] for x in cs["locations"]}
    assert all(not s["keyframe"] and not s["video"] for s in cs["shots"])
    assert all(set(s["character_ids"]) <= new_chars for s in cs["shots"]) and any(s["character_ids"] for s in cs["shots"])
    assert all(not s["location_id"] or s["location_id"] in new_locs for s in cs["shots"])
    assert cs["project"]["script"] == snap["project"]["script"]


def test_cloud_parallel_pick():
    from app import tasks
    mk = lambda i, typ, eng="", st="queued", pid="p", owner="", params=None: {  # noqa: E731
        "id": str(i), "type": typ, "status": st, "project_id": pid, "owner_id": owner or f"s{i}",
        "params": {"engine": eng, **(params or {})}}
    rows = [mk(1, "cloud_video", "hailuo", "running"), mk(2, "cloud_video", "hailuo"), mk(3, "cloud_video", "hailuo"),
            mk(4, "cloud_video", "kling"), mk(5, "video"), mk(6, "cloud_video", "kling", params={"_after": "s5"})]
    got = [t["id"] for t in tasks.pick_parallel(rows, {"cloud_video:hailuo": 1}, 2)]
    assert got == ["2", "4"], got                         # 海螺通道只剩 1 个名额；6 要等上一镜（本地视频）
    assert tasks.pick_next(rows, "video")["id"] == "5"   # 主队列不会挑云端任务
    rows2 = [mk(1, "keyframe", st="running"), mk(2, "cloud_video", "kling")]
    assert tasks.pick_parallel(rows2, {}, 3) == []        # 同项目分镜图没做完，视频不抢跑
    from app import recovery
    assert recovery.diagnose("cloud_video", "ConnectTimeout").retry
    assert not recovery.diagnose("cloud_video", "可灵生成失败：内容涉及敏感信息").retry
    assert recovery.diagnose("cloud_video", "Connection refused").kind == "cloud"   # 不会去重启 ComfyUI


def test_cloud_video(client, project, monkeypatch, tmp_path):
    import base64
    import hashlib
    import hmac
    import httpx
    from app import cloud, db, pipeline
    from app.config import get_settings
    from test_features import media_file
    s = get_settings()
    keep = {k: getattr(s, k) for k in ("minimax_api_key", "kling_access_key", "kling_secret_key", "cloud_concurrency")}
    s.minimax_api_key, s.kling_access_key, s.kling_secret_key, s.cloud_concurrency = "mm-key", "ak", "sk", 3
    monkeypatch.setattr(cloud, "POLL", 0.05)
    clip = tmp_path / "cloud.mp4"
    media_file(clip, 6.0)
    data = clip.read_bytes()
    calls = {"mm_submit": 0, "kl_submit": 0, "polls": {}}

    def handle(req: httpx.Request) -> httpx.Response:
        u = req.url
        if u.host == "cdn.test":
            return httpx.Response(200, content=data)
        if u.host == "api.minimaxi.com":
            assert req.headers["authorization"] == "Bearer mm-key"
            if u.path == "/v1/video_generation":
                body = json.loads(req.content)
                assert body["first_frame_image"].startswith("data:image/jpeg;base64,") and body["duration"] in (6, 10)
                calls["mm_submit"] += 1
                return httpx.Response(200, json={"task_id": f"mm{calls['mm_submit']}", "base_resp": {"status_code": 0}})
            if u.path == "/v1/query/video_generation":
                tid = u.params["task_id"]
                n = calls["polls"][tid] = calls["polls"].get(tid, 0) + 1
                st = "Processing" if n < 2 else "Success"
                return httpx.Response(200, json={"task_id": tid, "status": st, "file_id": "f1", "base_resp": {"status_code": 0}})
            if u.path == "/v1/files/retrieve":
                return httpx.Response(200, json={"file": {"download_url": "https://cdn.test/a.mp4"}, "base_resp": {"status_code": 0}})
        if u.host == "api-beijing.klingai.com":
            tok = req.headers["authorization"].split()[1]
            h, b, sig = tok.split(".")
            good = base64.urlsafe_b64encode(hmac.new(b"sk", f"{h}.{b}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
            assert sig == good and json.loads(base64.urlsafe_b64decode(b + "=="))["iss"] == "ak"
            if req.method == "POST":
                body = json.loads(req.content)
                assert body["model_name"] and body["duration"] in ("5", "10") and ("image" in body or "aspect_ratio" in body)
                calls["kl_submit"] += 1
                return httpx.Response(200, json={"code": 0, "data": {"task_id": f"k{calls['kl_submit']}", "task_status": "submitted"}})
            tid = u.path.rsplit("/", 1)[-1]
            n = calls["polls"][tid] = calls["polls"].get(tid, 0) + 1
            if n < 2:
                return httpx.Response(200, json={"code": 0, "data": {"task_status": "processing"}})
            return httpx.Response(200, json={"code": 0, "data": {"task_status": "succeed",
                                                                 "task_result": {"videos": [{"url": "https://cdn.test/k.mp4"}]}}})
        return httpx.Response(404, json={})

    monkeypatch.setattr(cloud, "_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(handle)))
    try:
        pid = project
        meta = client.get("/api/meta").json()["cloud"]
        assert meta["engines"]["hailuo"]["ready"] and meta["engines"]["kling"]["ready"]
        snap = client.get(f"/api/projects/{pid}").json()
        for x in snap["shots"][:2]:
            if not x["keyframe"]:
                wait(client, client.post(f"/api/shots/{x['id']}/keyframe", json={"count": 1}).json()["id"])
        snap = client.get(f"/api/projects/{pid}").json()
        shots = [x for x in snap["shots"] if x["keyframe"]]
        assert len(shots) >= 2
        client.patch(f"/api/shots/{shots[0]['id']}", json={"duration": 4})
        t = client.post(f"/api/shots/{shots[0]['id']}/video", json={"engine": "hailuo"}).json()
        assert t["type"] == "cloud_video"
        r = wait(client, t["id"])
        assert r["result"]["engine"] == "hailuo" and calls["mm_submit"] == 1
        cur = client.get(f"/api/projects/{pid}").json()
        sh = next(x for x in cur["shots"] if x["id"] == shots[0]["id"])
        a = next(x for x in cur["assets"][sh["id"]] if x["path"] == sh["video"])
        assert a["meta"]["engine"] == "hailuo" and a["meta"]["remote_id"] == "mm1"
        assert abs(asyncio_run(pipeline.media.probe(pipeline.absolute(sh["video"])))["duration"] - 4) < 0.3   # 裁到镜头时长
        # 重试 / 重启：已有远端任务号时只查询、不重复提交
        t2 = cloud.queue.submit("cloud_video", project_id=pid, title="resume", owner_type="shot", owner_id=shots[1]["id"],
                                params={"shot_id": shots[1]["id"], "engine": "kling", "_remote": "image2video/kOLD"})
        r2 = wait(client, t2["id"])
        assert calls["kl_submit"] == 0 and r2["result"]["remote_id"] == "image2video/kOLD"
        # 镜头级引擎 + 项目「混合」：批量时一部分镜头走云端、其余本地，同时排队
        assert client.patch(f"/api/projects/{pid}", json={"video_engine": "bogus"}).status_code == 400
        client.patch(f"/api/projects/{pid}", json={"video_engine": "mix", "cloud_share": 0.5})
        client.patch(f"/api/shots/{shots[0]['id']}", json={"engine": "local"})
        client.post(f"/api/projects/{pid}/batch/videos", json={"only_missing": False})
        rows = [x for x in client.get(f"/api/tasks?project_id={pid}&active=1").json()]
        types = {x["owner_id"]: (x["type"], (x["params"] or {}).get("engine")) for x in rows}
        assert types[shots[0]["id"]][0] == "video"
        assert any(tp == "cloud_video" for tp, _ in types.values()) and any(tp == "video" for tp, _ in types.values())
        drain(client, pid)
        assert calls["kl_submit"] + calls["mm_submit"] >= 2
        # 没配置 Key 的引擎回落本地；指定未配置的引擎报错
        s.minimax_api_key = ""
        assert cloud.engine_for({"engine": "hailuo"}, {"video_engine": "local"}) == "local"
        assert client.post(f"/api/shots/{shots[0]['id']}/video", json={"engine": "hailuo"}).status_code == 400
    finally:
        for k, v in keep.items():
            setattr(s, k, v)
        client.patch(f"/api/projects/{project}", json={"video_engine": "local"})
        for x in client.get(f"/api/projects/{project}").json()["shots"]:
            if x.get("engine"):
                client.patch(f"/api/shots/{x['id']}", json={"engine": ""})


def test_ops_stats_cleanup_backup(client, project):
    import os
    import time as _t
    from app import db, ops, pipeline
    pid = project
    st = client.get("/api/ops/stats?days=30").json()
    assert st["total"]["done"] > 0 and any(t["success_rate"] is not None and t["label"] for t in st["types"])
    assert st["daily"] and "p90" in st["types"][0]
    assert client.get(f"/api/ops/stats?project_id={pid}").json()["total"]["tasks"] > 0
    snap = client.get(f"/api/projects/{pid}").json()
    shot = snap["shots"][0]
    wait(client, client.post(f"/api/shots/{shot['id']}/keyframe", json={"count": 2}).json()["id"])
    snap = client.get(f"/api/projects/{pid}").json()
    shot = next(x for x in snap["shots"] if x["id"] == shot["id"])
    kfs = [a for a in snap["assets"][shot["id"]] if a["kind"] == "keyframe"]
    refs = ops.referenced(pid)
    unused = [a["path"] for a in kfs if a["path"] != shot["keyframe"] and a["path"] not in refs]
    assert unused
    # 选中的超分版引用原版：原版不能被清理
    if not any(x["video"] for x in snap["shots"]):
        wait(client, client.post(f"/api/shots/{snap['shots'][1]['id']}/video", json={}).json()["id"])
        snap = client.get(f"/api/projects/{pid}").json()
    vs = next(x for x in snap["shots"] if x["video"])
    up = pipeline.project_dir(pid, "videos") / "up_test.mp4"
    import shutil as _sh
    _sh.copy2(pipeline.absolute(vs["video"]), up)
    orig = vs["video"]
    a = pipeline.add_asset(pid, "video", up, "shot", vs["id"], {"source": orig})
    db.update("shots", vs["id"], {"video": a["path"]})
    # 孤儿文件 + 中断留下的临时目录
    orphan = pipeline.project_dir(pid, "keyframes") / "orphan.png"
    orphan.write_bytes(b"x" * 1000)
    work = pipeline.project_dir(pid, "final") / "work_dead"
    work.mkdir(exist_ok=True)
    (work / "n0000.mp4").write_bytes(b"0" * 500)
    old = _t.time() - 7200
    for p in (orphan, work, work / "n0000.mp4"):
        os.utime(p, (old, old))
    pv = client.post(f"/api/projects/{pid}/cleanup/preview", json={}).json()
    paths = {i["path"]: i["cat"] for i in pv["items"]}
    assert all(paths.get(u) == "candidates" for u in unused), (unused, paths)
    assert shot["keyframe"] not in paths and orig not in paths and a["path"] not in paths
    assert paths.get(pipeline.rel(orphan)) == "orphans" and paths.get(pipeline.rel(work)) == "temp"
    assert pv["summary"]["candidates"]["files"] >= 1 and pv["bytes"] > 0
    kept = client.post(f"/api/projects/{pid}/cleanup/preview", json={"keep": 5}).json()
    assert not any(i["cat"] == "candidates" and i["kind"] == "keyframe" and i["path"] in unused for i in kept["items"])
    r = client.post(f"/api/projects/{pid}/cleanup", json={}).json()
    assert r["deleted"] == len(pv["items"]) and r["freed"] > 0
    assert not any(pipeline.exists(u) for u in unused) and not orphan.exists() and not work.exists()
    assert pipeline.exists(shot["keyframe"]) and pipeline.exists(orig)
    assert not db.query("SELECT id FROM assets WHERE path=?", [unused[0]])
    # 备份 → 恢复为新项目
    t = wait(client, client.post(f"/api/projects/{pid}/backup", json={}).json()["id"])
    name = t["result"]["name"]
    disk = client.get("/api/ops/disk").json()
    assert any(b["name"] == name for b in disk["backups"]) and any(p["id"] == pid and p["bytes"] > 0 for p in disk["projects"])
    assert client.get(f"/api/backups/{name}").status_code == 200
    rs = client.post(f"/api/backups/{name}/restore").json()
    nid = rs["id"]
    src = client.get(f"/api/projects/{pid}").json()
    dst = client.get(f"/api/projects/{nid}").json()
    assert len(dst["shots"]) == len(src["shots"]) and dst["project"]["script"] == src["project"]["script"]
    chars = {c["id"] for c in dst["characters"]}
    assert chars and not chars & {c["id"] for c in src["characters"]}
    for x in dst["shots"]:
        assert set(x["character_ids"]) <= chars
        for k in ("keyframe", "video"):
            if x[k]:
                assert x[k].startswith(nid + "/") and pipeline.exists(x[k])
    assert all(a["path"].startswith(nid + "/") for lst in dst["assets"].values() for a in lst)
    sv = next(x for x in dst["shots"] if x["video"])
    va = next(a for a in dst["assets"][sv["id"]] if a["path"] == sv["video"])
    assert va["owner_id"] == sv["id"]
    # 上传 zip 恢复；坏文件报错
    zp = ops.backup_dir() / name
    with zp.open("rb") as f:
        up2 = client.post("/api/projects/restore?name=导入副本", files={"file": (name, f, "application/zip")}).json()
    assert up2["title"] == "导入副本" and up2["id"] not in (pid, nid)
    assert client.post("/api/projects/restore", files={"file": ("x.zip", b"not a zip", "application/zip")}).status_code == 400
    assert client.delete(f"/api/backups/{name}").json()["ok"]
    for x in (nid, up2["id"]):
        client.delete(f"/api/projects/{x}")


def test_aspects_auto_and_per_shot(client, project):
    import subprocess
    from app import cloud, enhance, pipeline, reframe, workflows
    from test_features import CALLS
    # 8 种画幅尺寸齐全（视频 / 图像宽高都是 16 的倍数）
    meta = client.get("/api/meta").json()
    assert len(meta["aspects"]) == 8 and meta["aspect_info"]["21:9"][0] == "宽银幕"
    for a in meta["aspects"]:
        sizes = [workflows.IMAGE_SIZES[a], workflows.VIDEO_SIZES["draft"][a], workflows.VIDEO_SIZES["final"][a]]
        assert all(v % 16 == 0 for wh in sizes for v in wh), a
        assert a in enhance.EXPORT_SIZES["1080p"] and a in reframe.ASPECTS
    # 智能画幅
    r = client.post("/api/projects", json={"title": "抖音短剧", "source_text": "x", "aspect_ratio": "auto"}).json()
    assert r["aspect_ratio"] == "9:16" and "抖音" in r["aspect_reason"]
    r2 = client.post("/api/projects", json={"title": "史诗", "source_text": "一部宽银幕电影大片", "aspect_ratio": "auto"}).json()
    assert r2["aspect_ratio"] == "21:9"
    r3 = client.patch(f"/api/projects/{r2['id']}", json={"aspect_ratio": "auto", "title": "穿搭", "source_text": "发小红书"}).json()
    assert r3["aspect_ratio"] == "3:4" and r3["aspect_reason"]
    assert workflows.suggest_aspect("")[0] == "9:16"
    assert client.post("/api/projects", json={"aspect_ratio": "5:4"}).status_code == 400
    assert cloud.nearest_aspect("21:9", ("16:9", "9:16", "1:1")) == "16:9"
    assert cloud.nearest_aspect("2:3", ("16:9", "9:16", "1:1")) == "9:16"
    # 镜头级画幅：出图 / 出视频按镜头画幅
    snap = client.get(f"/api/projects/{project}").json()
    pa = snap["project"]["aspect_ratio"]
    shot = snap["shots"][0]
    assert client.patch(f"/api/shots/{shot['id']}", json={"aspect": "2:1"}).status_code == 400
    assert client.patch(f"/api/shots/{shot['id']}", json={"aspect": "1:1"}).json()["aspect"] == "1:1"
    n = len(CALLS)
    wait(client, client.post(f"/api/shots/{shot['id']}/keyframe", json={}).json()["id"])
    dims = [(nd["inputs"].get("width"), nd["inputs"].get("height")) for wf in CALLS[n:] for nd in wf.values()]
    assert workflows.IMAGE_SIZES["1:1"] in dims, dims
    n = len(CALLS)
    wait(client, client.post(f"/api/shots/{shot['id']}/video", json={}).json()["id"])
    dims = [(nd["inputs"].get("width"), nd["inputs"].get("height")) for wf in CALLS[n:] for nd in wf.values()]
    assert workflows.VIDEO_SIZES["draft"]["1:1"] in dims or workflows.VIDEO_SIZES["final"]["1:1"] in dims, dims
    # 成片合成：画幅不一致的镜头自动重构图，输出仍是项目画幅
    snap = client.get(f"/api/projects/{project}").json()
    for s in snap["shots"]:
        if not s["video"]:
            wait(client, client.post(f"/api/shots/{s['id']}/video", json={}).json()["id"])
    sq = pipeline.project_dir(project, "videos") / "square_test.mp4"
    subprocess.run(["ffmpeg", "-nostdin", "-y", "-loglevel", "error", "-f", "lavfi", "-i", "testsrc=size=320x320:rate=24:d=2",
                    "-f", "lavfi", "-i", "sine=frequency=330:duration=2", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", str(sq)], check=True)
    old_video = client.get(f"/api/projects/{project}").json()["shots"][0]["video"]
    client.patch(f"/api/shots/{shot['id']}", json={"video": pipeline.rel(sq)})
    try:
        r = wait(client, client.post(f"/api/projects/{project}/assemble", json={"quality": "draft"}).json()["id"])
        assert r["status"] == "done", r
        assert r["result"]["aspect"] == pa
        info = asyncio_run(pipeline.media.probe(pipeline.absolute(r["result"]["path"])))
        assert (info["width"], info["height"]) == workflows.VIDEO_SIZES["draft"][pa]
        fin = next(a for a in client.get(f"/api/projects/{project}").json()["assets"][project] if a["path"] == r["result"]["path"])
        rf = fin["meta"]["reframe"]
        assert rf["crop"] + rf["pad"] >= 1, rf
    finally:
        client.patch(f"/api/shots/{shot['id']}", json={"aspect": "", "video": old_video})


def test_canvas_layout(client, project):
    pid = project
    snap = client.get(f"/api/projects/{pid}").json()
    assert snap["project"]["canvas"] == {}
    sid = snap["shots"][0]["id"]
    c = client.put(f"/api/projects/{pid}/canvas", json={"nodes": {f"shot:{sid}": {"x": 100.26, "y": "-50"}, "script": {"x": 0, "y": 0}},
                                                       "viewport": {"x": 10, "y": 20, "z": 99},
                                                       "notes": [{"text": "雨再大一点", "x": 5, "y": 6, "color": "nope"}]}).json()
    assert c["nodes"][f"shot:{sid}"] == {"x": 100.3, "y": -50.0} and c["viewport"]["z"] == 4
    assert c["notes"][0]["color"] == "yellow" and c["notes"][0]["id"] and c["notes"][0]["text"] == "雨再大一点"
    # 节点按 key 合并，null 删除；便签整体替换
    c = client.put(f"/api/projects/{pid}/canvas", json={"nodes": {"script": None, "final": {"x": 900, "y": float("1e9")}}}).json()
    assert "script" not in c["nodes"] and f"shot:{sid}" in c["nodes"] and c["nodes"]["final"]["y"] == 1e6
    assert len(c["notes"]) == 1
    assert client.get(f"/api/projects/{pid}").json()["project"]["canvas"]["nodes"]["final"]["x"] == 900
    assert client.put(f"/api/projects/{pid}/canvas", json={"notes": [{}] * 301}).status_code == 400
    # 自动排列：清空位置，便签保留
    r = client.post(f"/api/projects/{pid}/canvas/reset").json()
    assert r["nodes"] == {} and len(r["notes"]) == 1
    # 复制为新画幅项目不带画布布局（节点 id 已变）
    cl = client.post(f"/api/projects/{pid}/clone", json={"aspect": "1:1"}).json()
    assert client.get(f"/api/projects/{cl['id']}").json()["project"]["canvas"] == {}
    assert client.get("/static/canvas.js").status_code == 200


def test_snapshots_and_branches(client, project):
    from app import db
    pid = project
    snap = client.get(f"/api/projects/{pid}").json()
    shots = snap["shots"]
    assert len(shots) >= 2 and snap["branches"] == []
    s0, s1 = shots[0], shots[1]
    # 存档点 → 改内容 / 删镜头 → 回退
    sp = client.post(f"/api/projects/{pid}/snapshots", json={"title": "定稿前"}).json()
    assert sp["meta"]["shots"] == len(shots) and "data" not in sp
    client.patch(f"/api/shots/{s0['id']}", json={"description": "改坏了"})
    client.delete(f"/api/shots/{s1['id']}")
    client.patch(f"/api/projects/{pid}", json={"synopsis": "新简介"})
    assert len(client.get(f"/api/projects/{pid}").json()["shots"]) == len(shots) - 1
    tid = db.insert("tasks", {"project_id": pid, "type": "keyframe", "status": "queued", "title": "x", "params": {}})["id"]
    assert client.post(f"/api/snapshots/{sp['id']}/restore").status_code == 409       # 有任务排队时不让回退
    db.execute("DELETE FROM tasks WHERE id=?", [tid])
    r = client.post(f"/api/snapshots/{sp['id']}/restore").json()
    assert r["ok"] and r["backup"]["auto"] == 1
    after = client.get(f"/api/projects/{pid}").json()
    assert [x["id"] for x in after["shots"]] == [x["id"] for x in shots]
    assert after["shots"][0]["description"] == s0["description"] and after["project"]["synopsis"] == snap["project"]["synopsis"]
    lst = client.get(f"/api/projects/{pid}/snapshots").json()
    assert {x["title"] for x in lst} >= {"定稿前"} and any(x["auto"] for x in lst)
    # 回退前的自动存档可以再回去（撤销回退）
    client.post(f"/api/snapshots/{r['backup']['id']}/restore")
    assert client.get(f"/api/projects/{pid}").json()["shots"][0]["description"] == "改坏了"
    client.post(f"/api/snapshots/{sp['id']}/restore")
    client.patch(f"/api/snapshots/{sp['id']}", json={"title": "定稿"})
    assert client.delete(f"/api/snapshots/{lst[-1]['id']}").json()["ok"]
    # 镜头分支：不进正式镜头列表；出图后采用 → 写回原镜头，候选合并
    br = client.post(f"/api/shots/{s0['id']}/branch", json={"keep_media": False}).json()
    assert br["branch_of"] == s0["id"] and not br["keyframe"]
    cur = client.get(f"/api/projects/{pid}").json()
    assert br["id"] not in [x["id"] for x in cur["shots"]] and cur["branches"][0]["id"] == br["id"]
    client.patch(f"/api/shots/{br['id']}", json={"description": "另一版：俯拍雨夜"})
    wait(client, client.post(f"/api/shots/{br['id']}/keyframe", json={}).json()["id"])
    kf = client.get(f"/api/projects/{pid}").json()["branches"][0]["keyframe"]
    assert kf
    n_assets = len(cur["assets"].get(s0["id"], []))
    assert client.post(f"/api/shots/{s0['id']}/adopt").status_code == 400
    got = client.post(f"/api/shots/{br['id']}/adopt").json()
    assert got["id"] == s0["id"] and got["keyframe"] == kf and got["description"] == "另一版：俯拍雨夜"
    fin = client.get(f"/api/projects/{pid}").json()
    assert fin["branches"] == [] and len(fin["assets"][s0["id"]]) > n_assets
    assert [x["id"] for x in fin["shots"]] == [x["id"] for x in shots]
    # 删除原镜头时分支一起删
    b2 = client.post(f"/api/shots/{s1['id']}/branch", json={}).json()
    assert b2["keyframe"] == s1["keyframe"]
    client.post(f"/api/snapshots/{sp['id']}/restore")


def test_video_cache_and_optional_nodes(client, monkeypatch):
    import asyncio
    from app import comfy, videox, workflows
    from app.config import get_settings
    s = get_settings()
    monkeypatch.setattr(s, "video_cache", 0.2); monkeypatch.setattr(s, "video_cache_final", 0.0)
    # 草稿 8 步：插入 EasyCache，采样器接到缓存节点
    wf = workflows.video_i2v_workflow("p", "a.png", "16:9", 5, quality="draft", turbo=True, seed=1)
    assert wf["9"]["class_type"] == "EasyCache" and wf["9"]["inputs"]["reuse_threshold"] == 0.2
    assert wf["20"]["inputs"]["model"] == ["9", 0] and wf["9"]["inputs"]["model"] == ["7", 0]
    # 成品默认关；显式传入可开；4 步 Turbo 参考生视频不加
    assert "9" not in workflows.video_i2v_workflow("p", "a.png", "16:9", 5, quality="final", turbo=False, seed=1)
    assert "9" in workflows.video_i2v_workflow("p", "a.png", "16:9", 5, quality="final", turbo=False, seed=1, cache=0.1)
    assert "9" not in workflows.video_r2v_workflow("p", ["a.png"], "16:9", 5, quality="draft", turbo=True, seed=1)
    # 运镜 LoRA 插在缓存之前，缓存保持在最外层
    videox.chain_lora(wf, "push.safetensors")
    assert wf["8"]["inputs"]["model"] == ["7", 0] and wf["9"]["inputs"]["model"] == ["8", 0]
    assert wf["20"]["inputs"]["model"] == ["9", 0] and wf["23"]["inputs"]["model"] == ["8", 0]
    # 旧版 ComfyUI 没有 EasyCache：自动去掉并把引用接回上游
    monkeypatch.setattr(comfy, "_has_node", {"EasyCache": False})
    out = asyncio.run(comfy.strip_missing_optional(json.loads(json.dumps(wf))))
    assert "9" not in out and out["20"]["inputs"]["model"] == ["8", 0]
    monkeypatch.setattr(comfy, "_has_node", {"EasyCache": True})
    assert "9" in asyncio.run(comfy.strip_missing_optional(json.loads(json.dumps(wf))))


def test_subtitle_retry_and_chain_guard(client, project, monkeypatch):
    """实测问题：台词被烧成字幕多给一次机会；上一镜人物走样时不接它的尾帧；场景文字不算字幕。"""
    from app import pipeline, qa
    from test_flow import wait as wait_task
    calls = []

    def fake(results):
        async def check(target, **kw):
            calls.append(kw.get("lines"))
            return results[min(len(calls) - 1, len(results) - 1)]
        return check

    sub = {"ok": False, "issues": [{"type": "text", "subtitle": True, "detail": "台词被烧成了字幕：十年了"}]}
    face = {"ok": False, "issues": [{"type": "face", "detail": "「陈默」的脸不像"}]}
    monkeypatch.setattr(qa, "available", lambda: True)
    monkeypatch.setattr(qa, "check", fake([sub, sub, {"ok": True, "issues": []}]))
    a, b = client.get(f"/api/projects/{project}").json()["shots"][:2]
    wait_task(client, client.post(f"/api/shots/{a['id']}/keyframe", json={"count": 1}).json()["id"])
    calls.clear()
    wait_task(client, client.post(f"/api/shots/{a['id']}/video", json={"quality": "draft"}).json()["id"])
    video_calls = [c for c in calls if c]
    assert len(video_calls) == 3 and video_calls[0] == pipeline.shot_lines(a)          # 字幕：2 次 + 额外 1 次
    snap = client.get(f"/api/projects/{project}").json()
    assert "画面中不出现任何字幕" in next(x for x in snap["assets"][a["id"]] if x["kind"] == "video")["meta"]["prompt"]
    calls.clear(); monkeypatch.setattr(qa, "check", fake([face]))
    wait_task(client, client.post(f"/api/shots/{a['id']}/video", json={"quality": "draft"}).json()["id"])
    assert len(calls) == 2                                                               # 人脸问题：不加次数
    # 上一镜（a）当前视频人脸不合格 → b 连续镜头不接尾帧
    client.patch(f"/api/shots/{b['id']}", json={"continuous": 1})
    b = next(s for s in client.get(f"/api/projects/{project}").json()["shots"] if s["id"] == b["id"])
    assert pipeline.chain_source(b) is None and "人物走样" in pipeline.chain_blocked(pipeline.prev_shot(b))
    assert pipeline.shot_lines({"dialogue": [{"line": "快走"}], "narration": "那一夜"}) == ["快走", "那一夜"]


def test_qa_text_rules(monkeypatch):
    import importlib.util
    import pytest
    pytest.importorskip("cv2")
    spec = importlib.util.spec_from_file_location("qa_check", Path(__file__).resolve().parents[1] / "scripts" / "qa_check.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    seq = iter([[("POLICE", 0.9, 0.5, 0.5)], [("POLICE", 0.9, 0.5, 0.5)], [("快走", 0.9, 0.8, 0.5)]])
    monkeypatch.setattr(m, "ocr", lambda img: next(seq))
    r = m.check_text([0, 0, 0], None, ["警察在找我，快走！"])
    assert r["flagged"] and r["subtitle"] == ["快走"]
    seq = iter([[("POLICE", 0.9, 0.5, 0.5)]] * 3)
    assert not m.check_text([0, 0, 0], None, ["快走"])["flagged"]


def test_chain_mismatch():
    from app import pipeline
    p = {"sort": 0, "shot_size": "近景", "character_ids": ["a"]}
    assert pipeline.chain_mismatch(p, {"shot_size": "全景", "character_ids": ["a"]})          # 跳两级
    assert pipeline.chain_mismatch(p, {"shot_size": "中景", "character_ids": ["a", "b"]})     # 换人又换景别
    assert not pipeline.chain_mismatch(p, {"shot_size": "中景", "character_ids": ["a"]})
    assert not pipeline.chain_mismatch(p, {"shot_size": "近景", "character_ids": ["b"]})


def test_chain_layout(client, project, monkeypatch):
    """实测：#3「两人交握的手部」接了上一镜的脸部特写尾帧 → 整段沿用上一镜构图。人物布局对不上时改用本镜分镜图。"""
    from app import pipeline, qa
    from test_flow import wait as wait_task
    lm = qa.layout_mismatch
    # 4 个实测接续失败的镜头（上一镜尾帧 a vs 本镜分镜图 b）
    assert "人数" in lm({"a": [{"cx": .66, "s": .464}], "b": [{"cx": .37, "s": .224}, {"cx": .75, "s": .202}]})
    assert "人数" in lm({"a": [{"cx": .5, "s": .373}], "b": [{"cx": .4, "s": .062}, {"cx": .55, "s": .06}]})
    assert "不是同一个人" in lm({"a": [{"cx": .48, "s": .196}], "b": [{"cx": .48, "s": .158}], "sims": [[0.05]]})
    assert "景别" in lm({"a": [{"cx": .42, "s": .366}], "b": [{"cx": .45, "s": .157}]})
    assert "位置" in lm({"a": [{"cx": .2, "s": .3}], "b": [{"cx": .7, "s": .3}]})
    # 同一构图（首尾帧基本不变）、无脸空镜、质检不可用 → 照常接
    assert not lm({"a": [{"cx": .54, "s": .119}, {"cx": .42, "s": .114}], "b": [{"cx": .54, "s": .123}, {"cx": .43, "s": .118}],
                   "sims": [[0.7, 0.1], [0.1, 0.6]]})
    assert not lm({"a": [], "b": []}) and not lm(None)

    layout = {"a": [{"cx": .5, "s": .46}], "b": [{"cx": .3, "s": .22}, {"cx": .7, "s": .2}]}

    async def face_layout(a, b, style="cinematic"):
        return layout

    async def check(target, **kw):
        return {"ok": True, "issues": []}
    monkeypatch.setattr(qa, "available", lambda: True)
    monkeypatch.setattr(qa, "check", check)
    monkeypatch.setattr(qa, "face_layout", face_layout)
    a, b = client.get(f"/api/projects/{project}").json()["shots"][:2]
    client.patch(f"/api/shots/{b['id']}", json={"continuous": 1, "shot_size": a["shot_size"], "character_ids": a["character_ids"]})
    for s in (a, b):
        wait_task(client, client.post(f"/api/shots/{s['id']}/keyframe", json={"count": 1}).json()["id"])
    wait_task(client, client.post(f"/api/shots/{a['id']}/video", json={"quality": "draft"}).json()["id"])

    def b_video():
        wait_task(client, client.post(f"/api/shots/{b['id']}/video", json={"quality": "draft"}).json()["id"])
        snap = client.get(f"/api/projects/{project}").json()
        return [x for x in snap["assets"][b["id"]] if x["kind"] == "video"][0]["meta"]
    m = b_video()
    assert not m["chain_from"] and any("人数/构图不同" in n for n in m["notes"])
    layout = {"a": [{"cx": .5, "s": .3}], "b": [{"cx": .52, "s": .28}]}
    m = b_video()
    assert m["chain_from"] and any("最后一帧" in n for n in m["notes"])


def test_art_clause_subject():
    """实测 P2-2：「周屿侧身站在栏杆旁，侧脸对着镜头，惊讶地愣住」——后两句省略了主语，也要算周屿的。"""
    from app import art
    one = {"description": "周屿侧身站在栏杆旁，侧脸对着镜头，惊讶地愣住"}
    assert art.emotion(one, "周屿") == "惊讶" and art.orientation(one, "周屿") == "side"
    two = {"description": "苏晴背对镜头站着，肩膀微微颤抖。周屿走上前，开心地笑了"}
    assert art.orientation(two, "苏晴", ["周屿"]) == "back" and art.emotion(two, "苏晴", ["周屿"]) == "恐惧"
    assert art.orientation(two, "周屿", ["苏晴"]) is None and art.emotion(two, "周屿", ["苏晴"]) == "开心"
    assert art.emotion({"description": "两人对视，沉默不语"}, "周屿", ["苏晴"]) is None


def test_region_context_and_margin(tmp_path):
    """实测 P2-4：只框一张脸时裁切太小，模型画成白底人像 → 裁切至少为短边 40%；边缘对不上能被检测出来。"""
    from PIL import Image
    from app import board
    inner, outer = board.crop_box(720, 1280, [0.16, 0.25, 0.28, 0.16])
    assert outer[2] - outer[0] >= 288 and outer[3] - outer[1] >= 288
    assert outer[0] <= inner[0] and outer[2] >= inner[2]
    inner, outer = board.crop_box(720, 1280, [0.0, 0.0, 0.1, 0.05])      # 贴左上角：往右下多扩
    assert outer[:2] == (0, 0) and outer[2] >= 288 and outer[3] >= 288
    crop = tmp_path / "c.png"; same = tmp_path / "s.png"; other = tmp_path / "o.png"
    im = Image.new("RGB", (300, 300), (30, 60, 90)); im.save(crop)
    im2 = im.copy(); im2.paste((250, 250, 250), (100, 100, 200, 200)); im2.save(same)      # 只改了框内
    Image.new("RGB", (600, 600), (240, 240, 240)).save(other)                              # 整张重画
    box_in, box_out = (100, 100, 200, 200), (0, 0, 300, 300)
    assert board.margin_diff(crop, same, box_in, box_out) < 5
    assert board.margin_diff(crop, other, box_in, box_out) > board.REGION_MAX_DIFF


def test_region_words():
    from app import board
    assert board.region_words([0.1, 0.1, 0.2, 0.2]).startswith("画面上部左侧")
    assert board.region_words([0.4, 0.7, 0.2, 0.2]).startswith("画面下部中间")
    assert "不要新增人物" in board.region_prompt("加一只纸鹤", [], board.region_words([0.6, 0.4, 0.3, 0.2]))


def test_camera_mismatch_rules():
    import importlib.util
    import pytest
    pytest.importorskip("cv2")
    spec = importlib.util.spec_from_file_location("qa_check", Path(__file__).resolve().parents[1] / "scripts" / "qa_check.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    # 实测数值（RTX 4090D，8 种运镜）：推镜 zoom +0.52，拉镜 -0.39，右摇 tx -0.18，左移 tx +0.18，固定 ≈0
    assert m.camera_mismatch("推镜", "", {"zoom": 0.517, "tx": -0.16, "ty": 0.01}) == ""
    assert "没有变近" in m.camera_mismatch("推镜", "", {"zoom": -0.2, "tx": 0, "ty": 0})
    assert m.camera_mismatch("拉镜", "", {"zoom": -0.386, "tx": 0, "ty": 0}) == ""
    assert m.camera_mismatch("摇镜", "右", {"zoom": 0.056, "tx": -0.179, "ty": 0}) == ""
    assert m.camera_mismatch("移镜", "左", {"zoom": -0.186, "tx": 0.18, "ty": 0.04}) == ""
    assert "横向" in m.camera_mismatch("移镜", "左", {"zoom": 0, "tx": -0.2, "ty": 0})
    assert m.camera_mismatch("固定", "", {"zoom": 0.001, "tx": -0.004, "ty": 0.001}) == ""
    assert "明显运动" in m.camera_mismatch("固定", "", {"zoom": 0.3, "tx": 0, "ty": 0})
    assert m.camera_mismatch("升降", "上", {"zoom": 0.3, "tx": 0, "ty": -0.2}) == ""     # 不可靠的不判


def test_video_prompt_keeps_light():
    """实测：首帧黄昏的镜头，提示词没写时段 → 第二帧变成白天。"""
    from app import prompts
    loc = {"name": "学校天台", "description": "老旧的教学楼顶层，地面为粗糙的水泥地。背景是黄昏时分的城市天际线，光线为强烈的逆光"}
    shot = {"description": "苏晴站在天台上", "shot_size": "中景", "camera_angle": "平视", "camera_move": "固定"}
    p = prompts.video_prompt({"style": "cinematic"}, shot, {}, loc)
    assert "光线与时段：背景是黄昏时分的城市天际线，光线为强烈的逆光" in p and "保持首帧的光线、时段" in p
    assert prompts.light_text({"description": "深夜，雨中的站台"}, loc).startswith("深夜")
    assert prompts.light_text({"description": "两人对视"}, {"name": "教室", "description": "宽敞的教室"}) == ""


def test_region_paste_removes_connected_change(tmp_path):
    """实测 P2-4：「让她举手拿纸鹤」框在胸前，模型整图里已去掉原来垂着的手臂，
    但只贴框内会把旧手臂留下 → 与框内改动相连的框外变化一起贴回；整体偏移时不扩。"""
    from PIL import Image, ImageDraw
    from app import board
    base = Image.new("RGB", (400, 600), (90, 90, 90))
    ImageDraw.Draw(base).rectangle([100, 300, 130, 480], fill=(230, 230, 230))      # 原来垂着的手臂（框外）
    patch = Image.new("RGB", (400, 600), (90, 90, 90))
    ImageDraw.Draw(patch).rectangle([110, 200, 220, 290], fill=(250, 250, 250))     # 框内新画的手和纸鹤
    ImageDraw.Draw(patch).rectangle([100, 285, 130, 300], fill=(230, 230, 230))     # 与改动相连
    bp, pp = tmp_path / "b.png", tmp_path / "p.png"
    base.save(bp); patch.save(pp)
    inner = (100, 190, 240, 300)
    out = board.feather_paste(bp, pp, inner, (0, 0, 400, 600), tmp_path / "o.png", grow=True)
    assert Image.open(out).getpixel((115, 420))[0] < 120                          # 旧手臂已去掉
    out2 = board.feather_paste(bp, pp, inner, (0, 0, 400, 600), tmp_path / "o2.png")
    assert Image.open(out2).getpixel((115, 420))[0] > 200                         # 不扩时旧手臂残留
    shifted = Image.new("RGB", (400, 600), (200, 200, 200))                        # 整张都变了 → 不扩
    shifted.save(pp)
    assert board.change_mask(base, shifted, inner) is None


def test_light_changes_and_lock_fallback(tmp_path, monkeypatch):
    """实测 P2-5：H3 图生视频黄昏首帧 5 秒内变成白天 → 生成后拉回首帧光线；镜头本身要求光线变化时不锁。"""
    import asyncio
    from app import prompts, qa
    assert prompts.light_changes({"description": "她按下开关，房间的灯亮起"})
    assert not prompts.light_changes({"description": "黄昏的天台，她站着不动"})
    v = tmp_path / "v.mp4"
    v.write_bytes(b"x" * 10)
    monkeypatch.setattr(qa.get_settings(), "qa_python", tmp_path / "missing-python")
    out, info = asyncio.run(qa.light_lock(v))          # 环境不可用 → 原样返回
    assert out == v and info == {} and v.exists()


def test_light_anchor_rules():
    """固定机位小动作 → 首帧兼作尾帧锁光线；有运镜、大动作、要求光线变化时不锚定。"""
    from app import prompts
    assert prompts.light_anchor({"camera_move": "固定", "description": "黄昏天台，苏晴望着远方轻声说话"})
    assert prompts.light_anchor({"description": "两人对坐聊天"})
    assert not prompts.light_anchor({"camera_move": "推镜", "description": "苏晴站着"})
    assert not prompts.light_anchor({"camera_move": "固定", "description": "苏晴转身离开天台"})
    assert not prompts.light_anchor({"camera_move": "固定", "description": "房间的灯突然亮起"})
    p = prompts.video_prompt({"style": "cinematic"}, {"description": "苏晴站着", "_anchor": True}, {}, None)
    assert "回到与首帧相近" in p and "指定的尾帧" not in p


def test_comfy_fit_inputs(monkeypatch):
    """实测 P3-2：新版 RIFE VFI 多了 dtype/batch_size 必填项 → 按节点实际定义补默认值、去掉不认识的输入。"""
    import asyncio
    from app import comfy
    spec = {"input": {"required": {"frames": ["IMAGE"], "multiplier": ["INT", {"default": 2}],
                                   "dtype": [["float32", "float16"], {"default": "float32"}],
                                   "batch_size": ["INT", {"default": 1}], "mode": [["a", "b"]]},
                      "optional": {"states": ["X"]}}}

    async def fake(name):
        return spec
    monkeypatch.setattr(comfy, "node_spec", fake)
    wf = {"3": {"class_type": "RIFE VFI", "inputs": {"frames": ["2", 0], "multiplier": 3, "old_flag": True}},
          "4": {"class_type": "Other", "inputs": {"x": 1}}}
    out = asyncio.run(comfy.fit_inputs(wf, ("RIFE VFI",)))
    assert out["3"]["inputs"] == {"frames": ["2", 0], "multiplier": 3, "dtype": "float32", "batch_size": 1, "mode": "a"}
    assert out["4"]["inputs"] == {"x": 1}


def test_empty_shot_has_no_people():
    """实测：空镜头「桌上一杯咖啡」+ 近景 → 景别提示写着「人物胸部以上」被画出一个人；21:9 公路远景视频中途切到车内司机。"""
    from app import prompts
    empty = {"description": "咖啡馆窗边的木桌上放着一杯冒热气的咖啡", "shot_size": "近景"}
    assert not prompts.has_people(empty)
    assert "人物" not in prompts.size_text(empty, False)
    kp = prompts.keyframe_prompt({"style": "cinematic"}, empty, [], None, {}, None)
    assert "没有人物" in kp and "胸部以上" not in kp
    vp = prompts.video_prompt({"style": "cinematic"}, empty, {}, None)
    assert "不出现任何人物" in vp
    girl = {"description": "樱花树下的少女全身像", "shot_size": "全景"}
    assert prompts.has_people(girl)
    assert "不出现任何人物" not in prompts.video_prompt({"style": "cinematic"}, girl, {}, None)
