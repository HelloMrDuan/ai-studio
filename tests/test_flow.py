"""端到端流程测试：模拟文字模型和 ComfyUI，真实跑任务队列、数据库和 ffmpeg 合成。"""
import json
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import comfy, llm, prompts, workflows

SCRIPT = {"title": "预知手机", "synopsis": "外卖小哥捡到能预知未来的手机",
          "script": "## 场景1：街头 · 夜\n△阿杰骑车送餐。\n阿杰：（惊讶）这手机怎么回事？\n## 场景2：天台 · 夜\n△小雨出现。\n小雨：你也看到了？"}
EXTRACT = {"characters": [
    {"name": "阿杰", "role": "主角", "gender": "男", "age": "25", "appearance": "短发，黄色外卖服", "voice": "爽朗青年男声"},
    {"name": "小雨", "role": "配角", "gender": "女", "age": "22", "appearance": "黑长直，白色风衣", "voice": "清亮女声"}],
    "locations": [{"name": "街头", "description": "雨夜霓虹街道"}, {"name": "天台", "description": "城市天台"}]}
BOARD = {"shots": [
    {"scene": "街头", "location": "街头", "shot_size": "全景", "camera_angle": "平视", "camera_move": "跟拍",
     "characters": ["阿杰"], "description": "阿杰骑电动车穿过雨夜街道", "action": "车灯划过", "dialogue": [],
     "sound": "雨声", "duration": 3},
    {"scene": "天台", "location": "天台", "shot_size": "中景", "camera_angle": "平视", "camera_move": "固定",
     "characters": ["阿杰", "小雨"], "description": "阿杰在画面左侧，小雨在右侧", "action": "两人对视",
     "dialogue": [{"speaker": "小雨", "line": "你也看到了？", "emotion": "紧张"}], "sound": "", "duration": 3}]}

CALLS = {"llm": [], "comfy": []}


def make_video(path: Path, seconds: float = 1.0) -> None:
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=size=320x180:rate=24:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True)


@pytest.fixture(scope="module")
def client():
    async def fake_complete(messages, **kw):
        system = messages[0]["content"]
        CALLS["llm"].append(system[:10])
        if system == prompts.SCRIPT_SYSTEM:
            return json.dumps(SCRIPT, ensure_ascii=False)
        if system == prompts.EXTRACT_SYSTEM:
            return "```json\n" + json.dumps(EXTRACT, ensure_ascii=False) + "\n```"
        return json.dumps(BOARD, ensure_ascii=False)

    async def fake_upload(path):
        return Path(path).name

    async def fake_run(workflow, out_dir, progress=None):
        CALLS["comfy"].append(workflow)
        out_dir.mkdir(parents=True, exist_ok=True)
        if progress:
            await progress(50, "采样")
        if any(n["class_type"] == "SaveVideo" for n in workflow.values()):
            target = out_dir / f"v{len(CALLS['comfy'])}.mp4"
            make_video(target)
        else:
            from PIL import Image
            target = out_dir / f"i{len(CALLS['comfy'])}.png"
            Image.new("RGB", (64, 36), (200, 100, 50)).save(target)
        return [target]

    llm._complete = fake_complete
    comfy.upload_image = fake_upload
    comfy.run = fake_run
    from app.main import app
    with TestClient(app) as c:
        yield c


def wait(client, task_id, timeout=60):
    end = time.time() + timeout
    while time.time() < end:
        t = next(x for x in client.get("/api/tasks?limit=500").json() if x["id"] == task_id)
        if t["status"] in {"done", "failed", "cancelled"}:
            assert t["status"] == "done", t["message"]
            return t
        time.sleep(0.2)
    raise AssertionError("任务超时")


def test_full_flow(client):
    assert client.get("/").status_code == 200
    assert "app.js" in client.get("/").text
    assert client.get("/static/app.js").status_code == 200
    p = client.post("/api/projects", json={"title": "", "source_text": "外卖小哥捡到预知手机", "style": "manju"}).json()
    pid = p["id"]
    wait(client, client.post(f"/api/projects/{pid}/script/generate").json()["id"])
    snap = client.get(f"/api/projects/{pid}").json()
    assert snap["project"]["title"] == "预知手机" and "场景1" in snap["project"]["script"]

    wait(client, client.post(f"/api/projects/{pid}/extract").json()["id"])
    snap = client.get(f"/api/projects/{pid}").json()
    assert [c["name"] for c in snap["characters"]] == ["阿杰", "小雨"]
    assert len(snap["locations"]) == 2

    r = client.post(f"/api/projects/{pid}/batch/portraits", json={"count": 2}).json()
    assert r["submitted"] == 4
    for t in client.get(f"/api/tasks?project_id={pid}&active=1").json():
        wait(client, t["id"])
    snap = client.get(f"/api/projects/{pid}").json()
    assert all(c["ref_image"] for c in snap["characters"])
    assert len(snap["assets"][snap["characters"][0]["id"]]) == 2

    wait(client, client.post(f"/api/projects/{pid}/storyboard/generate").json()["id"])
    snap = client.get(f"/api/projects/{pid}").json()
    shots = snap["shots"]
    assert len(shots) == 2 and len(shots[1]["character_ids"]) == 2 and shots[1]["location_id"]

    # 多人镜头：两个角色参考图 + 场景图都应作为参考图传入，提示词里按 <imageN> 指代
    plan = client.get(f"/api/shots/{shots[1]['id']}/plan").json()
    assert len(plan["keyframe"]["refs"]) == 3
    assert "<image2>" in plan["keyframe"]["prompt"] and "共有 2 个主要人物" in plan["keyframe"]["prompt"]
    CALLS["comfy"].clear()
    wait(client, client.post(f"/api/shots/{shots[1]['id']}/keyframe", json={"count": 2}).json()["id"])
    wf = CALLS["comfy"][0]
    enc = next(n for n in wf.values() if n["class_type"] == "TextEncodeQwenImage21")["inputs"]
    assert {"images.image_1", "images.image_2", "images.image_3"} <= set(enc)
    assert len(CALLS["comfy"]) == 2

    r = client.post(f"/api/projects/{pid}/batch/keyframes", json={"only_missing": True}).json()
    assert r["submitted"] == 1
    for t in client.get(f"/api/tasks?project_id={pid}&active=1").json():
        wait(client, t["id"])

    # 选择候选、改图、编辑台词
    snap = client.get(f"/api/projects/{pid}").json()
    cands = [a for a in snap["assets"][shots[1]["id"]] if a["kind"] == "keyframe"]
    client.post("/api/assets/select", json={"asset_id": cands[-1]["id"]})
    assert client.get(f"/api/projects/{pid}").json()["shots"][1]["keyframe"] == cands[-1]["path"]
    wait(client, client.post(f"/api/shots/{shots[1]['id']}/keyframe/edit", json={"instruction": "小雨微笑"}).json()["id"])
    client.patch(f"/api/shots/{shots[0]['id']}", json={"dialogue": [{"speaker": "阿杰", "line": "出发！", "emotion": ""}]})

    CALLS["comfy"].clear()
    r = client.post(f"/api/projects/{pid}/batch/videos", json={"quality": "draft"}).json()
    assert r["submitted"] == 2
    for t in client.get(f"/api/tasks?project_id={pid}&active=1").json():
        wait(client, t["id"])
    vw = CALLS["comfy"][0]
    node = next(n for n in vw.values() if n["class_type"] == "MiniMaxH3ImageToVideo")["inputs"]
    assert node["length"] == workflows.h3_length(3) and "first_frame" in node
    snap = client.get(f"/api/projects/{pid}").json()
    assert all(s["video"] for s in snap["shots"])

    # 新增、排序、删除镜头
    new = client.post(f"/api/projects/{pid}/shots", json={"after_id": shots[0]["id"], "description": "插入"}).json()
    ids = [s["id"] for s in client.get(f"/api/projects/{pid}").json()["shots"]]
    assert ids[1] == new["id"]
    client.post(f"/api/projects/{pid}/shots/reorder", json={"ids": list(reversed(ids))})
    client.delete(f"/api/shots/{new['id']}")
    client.post(f"/api/projects/{pid}/shots/reorder", json={"ids": [shots[0]["id"], shots[1]["id"]]})

    if shutil.which("ffmpeg"):
        t = wait(client, client.post(f"/api/projects/{pid}/assemble", json={"subtitles": True, "quality": "draft"}).json()["id"], 120)
        assert t["result"]["seconds"] > 1.5
        snap = client.get(f"/api/projects/{pid}").json()
        final = client.get("/media/" + snap["project"]["final_video"])
        assert final.status_code == 200 and len(final.content) > 1000

    status = client.get("/api/system/status").json()
    assert any(not m["ok"] for m in status["models"])
    assert client.get("/api/facefusion/processors").json()["processors"]["face_swapper"]
    assert client.delete(f"/api/projects/{pid}").json()["ok"]


def test_workflow_shapes():
    wf = workflows.image_workflow("测试", "9:16", ["a.png", "b.png"])
    assert wf["6"]["inputs"]["width"] == 928
    r2v = workflows.video_r2v_workflow("x", ["a.png", "b.png"], "16:9", 5)
    assert "ref_images.ref_image_1" in r2v["10"]["inputs"]
    assert workflows.h3_length(5) % 17 == 5


def test_storyboard_validator_fixes_names_and_duration():
    v = prompts.make_storyboard_validator(["阿杰", "小雨"])
    rows = v({"shots": [{"description": "x", "characters": ["阿杰（主角）"], "shot_size": "大特写",
                         "dialogue": [{"speaker": "小雨", "line": "一" * 40}], "duration": 2}]})
    assert rows[0]["characters"] == ["阿杰", "小雨"]
    assert rows[0]["shot_size"] == "特写" and rows[0]["duration"] >= 11
