"""新功能测试：访问密码、道具一致性、自动质检重抽、固定音色配音（模型全部模拟，真实跑 ffmpeg）。"""
import asyncio
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import comfy, llm, prompts, qa, voice
from app.config import get_settings
from test_flow import SCRIPT, wait

EXTRACT = {"characters": [
    {"name": "陈默", "role": "主角", "gender": "男", "age": "30", "appearance": "短发，黑色风衣", "voice": "低沉男声"},
    {"name": "林晚", "role": "配角", "gender": "女", "age": "28", "appearance": "长发，米色大衣", "voice": "温柔女声"}],
    "locations": [{"name": "车站", "description": "雨夜站台"}],
    "props": [{"name": "旧照片", "description": "泛黄的合影，边角卷起"}, {"name": "", "description": "空名字丢弃"}]}
BOARD = {"shots": [
    {"scene": "车站", "location": "车站", "shot_size": "近景", "camera_angle": "平视", "camera_move": "固定",
     "characters": ["陈默"], "props": ["旧照片"], "description": "陈默低头看着手里的旧照片", "action": "手指摩挲照片",
     "dialogue": [{"speaker": "陈默", "line": "十年了，你还好吗？", "emotion": "伤感"},
                  {"speaker": "陈默", "line": "我一直在等你。", "emotion": "平静"}], "sound": "雨声", "duration": 3},
    {"scene": "车站", "location": "车站", "shot_size": "全景", "camera_angle": "平视", "camera_move": "固定",
     "characters": ["陈默", "林晚"], "description": "林晚从站台另一头走来", "action": "",
     "dialogue": [{"speaker": "林晚", "line": "是你吗？", "emotion": "惊讶"}], "sound": "", "duration": 3}]}
CALLS = []


def media_file(path: Path, seconds: float, video: bool = True) -> None:
    args = ["ffmpeg", "-nostdin", "-y", "-loglevel", "error"]
    if video:
        args += ["-f", "lavfi", "-i", f"testsrc=size=320x180:rate=24:d={seconds}"]
    args += ["-f", "lavfi", "-i", f"sine=frequency=330:duration={seconds}"]
    args += (["-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac"] if video else [])
    subprocess.run([*args, str(path)], check=True)


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True, check=True).stdout
    return float(out.strip())


@pytest.fixture(scope="module")
def client():
    async def fake_complete(messages, **kw):
        system = messages[0]["content"]
        if system == prompts.SCRIPT_SYSTEM:
            return json.dumps(SCRIPT, ensure_ascii=False)
        if system == prompts.EXTRACT_SYSTEM:
            return json.dumps(EXTRACT, ensure_ascii=False)
        return json.dumps(BOARD, ensure_ascii=False)

    async def fake_upload(path):
        return Path(path).name

    async def fake_run(workflow, out_dir, progress=None):
        CALLS.append(workflow)
        out_dir.mkdir(parents=True, exist_ok=True)
        if any(n["class_type"] == "SaveVideo" for n in workflow.values()):
            target = out_dir / f"fv{len(CALLS)}.mp4"
            await asyncio.to_thread(media_file, target, 3.0)
        else:
            from PIL import Image
            target = out_dir / f"fi{len(CALLS)}.png"
            Image.new("RGB", (64, 36), (60, 120, 200)).save(target)
        return [target]

    llm._complete = fake_complete
    comfy.upload_image = fake_upload
    comfy.run = fake_run
    from app.main import app
    with TestClient(app) as c:
        yield c


def drain(client, pid):
    for t in client.get(f"/api/tasks?project_id={pid}&active=1").json():
        wait(client, t["id"])


@pytest.fixture(scope="module")
def project(client):
    pid = client.post("/api/projects", json={"title": "重逢", "source_text": "雨夜车站重逢", "style": "cinematic"}).json()["id"]
    wait(client, client.post(f"/api/projects/{pid}/script/generate").json()["id"])
    wait(client, client.post(f"/api/projects/{pid}/extract").json()["id"])
    client.post(f"/api/projects/{pid}/batch/portraits", json={"count": 1})
    drain(client, pid)
    wait(client, client.post(f"/api/projects/{pid}/storyboard/generate").json()["id"])
    return pid


def test_access_password(client):
    s = get_settings()
    s.access_password = "secret-pw"
    try:
        client.cookies.clear()
        assert client.get("/api/projects").status_code == 401
        r = client.get("/", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/login"
        assert client.get("/login").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.post("/api/login", json={"password": "wrong"}).status_code == 401
        assert client.post("/api/login", json={"password": "secret-pw"}).status_code == 200
        assert client.get("/api/projects").status_code == 200
        assert client.get("/api/meta").json().get("auth") is True
        client.post("/api/logout")
        client.cookies.clear()
        assert client.get("/api/projects").status_code == 401
    finally:
        s.access_password = ""
        client.cookies.clear()
    assert client.get("/api/projects").status_code == 200


def test_props(client, project):
    snap = client.get(f"/api/projects/{project}").json()
    assert [p["name"] for p in snap["props"]] == ["旧照片"]
    prop = snap["props"][0]
    assert prop["ref_image"], "批量生成定妆照时应同时生成道具图"
    shots = snap["shots"]
    assert shots[0]["prop_ids"] == [prop["id"]] and shots[1]["prop_ids"] == []
    plan = client.get(f"/api/shots/{shots[0]['id']}/plan").json()
    labels = plan["keyframe"]["labels"]
    assert labels[0] == "角色:陈默" and "场景:车站" in labels and "道具:旧照片" in labels
    idx = labels.index("道具:旧照片") + 1
    assert f"<image{idx}>" in plan["keyframe"]["prompt"] and "旧照片" in plan["keyframe"]["prompt"]
    # 增删改
    extra = client.post(f"/api/projects/{project}/props", json={"name": "雨伞"}).json()
    client.patch(f"/api/shots/{shots[1]['id']}", json={"prop_ids": [extra["id"]]})
    assert client.get(f"/api/projects/{project}").json()["shots"][1]["prop_ids"] == [extra["id"]]
    client.delete(f"/api/props/{extra['id']}")
    snap = client.get(f"/api/projects/{project}").json()
    assert snap["shots"][1]["prop_ids"] == [] and len(snap["props"]) == 1


def test_qa_retry(client, project, monkeypatch):
    calls = []

    async def fake_check(target, **kw):
        calls.append(Path(target).name)
        if len(calls) == 1:
            return {"ok": False, "issues": [{"type": "text", "detail": "画面底部出现字幕"}]}
        return {"ok": True, "issues": [], "faces": {"scores": {"陈默": 0.6}}}

    monkeypatch.setattr(qa, "available", lambda: True)
    monkeypatch.setattr(qa, "check", fake_check)
    shot = client.get(f"/api/projects/{project}").json()["shots"][0]
    r = wait(client, client.post(f"/api/shots/{shot['id']}/keyframe", json={"count": 1}).json()["id"])
    snap = client.get(f"/api/projects/{project}").json()
    kfs = [a for a in snap["assets"][shot["id"]] if a["kind"] == "keyframe"]
    assert len(calls) == 2 and len(kfs) >= 2
    best = next(a for a in kfs if a["meta"]["qa"] and a["meta"]["qa"]["ok"])
    assert snap["shots"][0]["keyframe"] == best["path"]

    calls.clear()
    wait(client, client.post(f"/api/shots/{shot['id']}/video", json={"quality": "draft"}).json()["id"])
    snap = client.get(f"/api/projects/{project}").json()
    vids = [a for a in snap["assets"][shot["id"]] if a["kind"] == "video"]
    vids.sort(key=lambda a: a["meta"]["attempt"])
    assert len(calls) == 2 and [a["meta"]["attempt"] for a in vids] == [1, 2]
    assert snap["shots"][0]["video"] == vids[1]["path"]
    assert qa.summary(vids[0]["meta"]["qa"]) == "画面底部出现字幕"
    assert qa.rank(None) < qa.rank({"ok": False, "issues": [{}]})


def test_dub(client, project, monkeypatch, tmp_path):
    synth = []

    async def fake_synth(items, log_cb=None):
        synth.append(items)
        for it in items:
            await asyncio.to_thread(media_file, Path(it["out"]), 2.0, False)

    async def fake_remove(audio, out_dir):
        out_dir.mkdir(parents=True, exist_ok=True)
        dst = out_dir / "no_vocals.wav"
        shutil.copy(audio, dst)
        return dst

    lips = []

    async def fake_lipsync(video, audio, output, log_cb=None, seed=1247):
        lips.append((await asyncio.to_thread(duration, video), await asyncio.to_thread(duration, audio)))
        shutil.copy(video, output)
        return output

    monkeypatch.setattr(voice, "tts_available", lambda: True)
    monkeypatch.setattr(voice, "separator_available", lambda: True)
    monkeypatch.setattr(voice, "lipsync_available", lambda: True)
    monkeypatch.setattr(voice, "synthesize", fake_synth)
    monkeypatch.setattr(voice, "remove_vocals", fake_remove)
    monkeypatch.setattr(voice, "isolate_vocals", fake_remove)
    monkeypatch.setattr(voice, "lipsync", fake_lipsync)
    snap = client.get(f"/api/projects/{project}").json()
    chen, lin = snap["characters"]
    shot = snap["shots"][0]
    assert client.post(f"/api/shots/{snap['shots'][1]['id']}/dub", json={}).status_code == 400   # 没视频

    # 没有音色样本时不能配音
    r = client.post(f"/api/shots/{shot['id']}/dub", json={})
    assert r.status_code == 400 and "音色样本" in r.json()["detail"]

    # 生成音色样本（模拟 H3 说话的视频）
    wait(client, client.post(f"/api/characters/{chen['id']}/voice").json()["id"])
    chen = client.get(f"/api/projects/{project}").json()["characters"][0]
    assert chen["voice_ref"].endswith(".wav")
    # 上传音色样本
    sample = tmp_path / "lin.mp3"
    media_file(sample, 4.0, video=False)
    with sample.open("rb") as fh:
        r = client.post(f"/api/characters/{lin['id']}/voice/upload", files={"file": ("lin.mp3", fh, "audio/mpeg")})
    assert r.status_code == 200 and r.json()["voice_ref"].endswith("_voice.wav")

    src = shot_video = client.get(f"/api/projects/{project}").json()["shots"][0]["video"]
    wait(client, client.post(f"/api/shots/{shot['id']}/dub", json={"lipsync": "auto"}).json()["id"])
    snap = client.get(f"/api/projects/{project}").json()
    new = snap["shots"][0]["video"]
    assert new != shot_video and "dub_" in new
    meta = next(a for a in snap["assets"][shot["id"]] if a["path"] == new)["meta"]
    assert meta["dub"] and meta["lipsync"] and meta["dub_source"] == src and meta["lines"] == 2
    assert meta["speed"] == 1.25 and any("定格" in n for n in meta["notes"])
    assert len(synth[-1]) == 2 and synth[-1][0]["emo"] == "伤感"
    out = get_settings().media_dir / new
    # 2 句 × 2 秒 / 1.25 + 0.3 + 0.25 + 0.3 = 4.05 秒（原视频 3 秒，定格补齐）
    assert abs(duration(out) - 4.05) < 0.2
    assert abs(lips[-1][0] - lips[-1][1]) < 0.2

    # 重新配音从原视频开始，不叠加
    wait(client, client.post(f"/api/shots/{shot['id']}/dub", json={"lipsync": "off"}).json()["id"])
    snap = client.get(f"/api/projects/{project}").json()
    meta2 = next(a for a in snap["assets"][shot["id"]] if a["path"] == snap["shots"][0]["video"])["meta"]
    assert meta2["dub_source"] == src and not meta2["lipsync"] and len(lips) == 1

    # 配音模式：生成视频后自动配音；多人全景镜头不自动对口型
    client.patch(f"/api/projects/{project}", json={"dub_mode": 1})
    s2 = snap["shots"][1]
    client.post(f"/api/shots/{s2['id']}/keyframe", json={"count": 1})
    drain(client, project)
    res = wait(client, client.post(f"/api/shots/{s2['id']}/video", json={"quality": "draft"}).json()["id"])
    drain(client, project)
    snap = client.get(f"/api/projects/{project}").json()
    meta3 = next(a for a in snap["assets"][s2["id"]] if a["path"] == snap["shots"][1]["video"])["meta"]
    assert meta3.get("dub") and not meta3["lipsync"] and any("多人" in n for n in meta3["notes"])
    plan = client.get(f"/api/shots/{s2['id']}/plan").json()
    assert "是你吗" not in plan["video"]["prompt"]


def test_batch_pause_resume(client, project, monkeypatch):
    pid = project
    fast = comfy.run

    async def slow_run(*a, **kw):
        await asyncio.sleep(0.5)
        return await fast(*a, **kw)

    monkeypatch.setattr(comfy, "run", slow_run)
    r = client.post(f"/api/projects/{pid}/batch/keyframes", json={"only_missing": False, "count": 1}).json()
    assert r["submitted"] >= 2 and r["batch"].startswith("keyframes-")
    paused = client.post("/api/tasks/pause", params={"project_id": pid}).json()["paused"]
    assert paused >= 1
    active = client.get(f"/api/tasks?project_id={pid}&active=1").json()
    assert any(t["status"] == "paused" for t in active)
    assert client.post("/api/tasks/resume", params={"project_id": pid}).json()["resumed"] == paused
    drain(client, pid)
    rows = [t for t in client.get(f"/api/tasks?project_id={pid}&limit=500").json() if t["params"].get("_batch") == r["batch"]]
    assert len(rows) == r["submitted"] and all(t["status"] == "done" for t in rows)
    assert "elapsed" in rows[0]["result"] and rows[0]["message"].startswith("完成")
    st = client.get("/api/system/status").json()
    assert "switches" in st["gpu"] and "paused" in st["queue"]
