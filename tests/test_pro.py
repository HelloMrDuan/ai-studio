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
