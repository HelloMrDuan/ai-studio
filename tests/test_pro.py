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
