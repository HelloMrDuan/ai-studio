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
BEATS = {"logline": "十年后的雨夜重逢", "beats": [
    {"time": "0-3秒", "name": "钩子", "content": "陈默盯着一张泛黄的旧照片，站台广播报出一个熟悉的名字", "emotion": "紧张"},
    {"time": "3-40秒", "name": "冲突升级", "content": "林晚出现却假装不认识他", "emotion": "错愕"},
    {"time": "40-60秒", "name": "结尾卡点", "content": "林晚转身时掉出同一张照片", "emotion": "震撼"}]}
GOOD_REVIEW = {"scores": {"hook": 88, "conflict": 85, "rhythm": 84, "logic": 86, "character": 82, "dialogue": 85},
               "issues": [], "summary": "合格"}
LLM_EXTRA = {}      # system 提示词 → 返回内容（str 或 callable(messages)）；各测试按需设置


def _writer_defaults():
    from app import writers
    return {writers.BEATS_SYSTEM: json.dumps(BEATS, ensure_ascii=False),
            writers.REVIEW_SYSTEM: json.dumps(GOOD_REVIEW, ensure_ascii=False)}


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
        extra = {**_writer_defaults(), **LLM_EXTRA}
        if system in extra:
            r = extra[system]
            return r(messages) if callable(r) else r
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


def test_animatic(client, project):
    pid = project
    shots = client.get(f"/api/projects/{pid}").json()["shots"]
    long_line = "这是一句非常非常长的台词，" * 6
    client.patch(f"/api/shots/{shots[0]['id']}", json={"duration": 2, "dialogue": [{"speaker": "林晚", "line": long_line, "emotion": ""}]})
    t = wait(client, client.post(f"/api/projects/{pid}/animatic", json={}).json()["id"])
    r = t["result"]
    shots = client.get(f"/api/projects/{pid}").json()["shots"]
    assert r["shots"] == len(shots) and len(r["timing"]) == len(shots)
    assert abs(r["seconds"] - sum(s["duration"] for s in shots)) < 0.2        # 无试听声音时严格按镜头时长
    assert r["over"] == [1] and r["timing"][0]["estimated"]
    from app import pipeline, media
    path = pipeline.absolute(r["path"])
    info = asyncio.run(media.probe(path))
    assert abs(info["duration"] - r["seconds"]) < 0.3 and info["has_audio"]
    snap = client.get(f"/api/projects/{pid}").json()
    assert snap["assets"][pid][0]["kind"] == "animatic"
    # 调顺序后再生成，预览按新顺序
    ids = [s["id"] for s in shots][::-1]
    client.post(f"/api/projects/{pid}/shots/reorder", json={"ids": ids})
    r2 = wait(client, client.post(f"/api/projects/{pid}/animatic", json={}).json()["id"])["result"]
    assert [x["shot_id"] for x in r2["timing"]] == ids


def test_animatic_tts(client, project, monkeypatch):
    synth = []

    async def fake_synth(items, log_cb=None):
        synth.append(items)
        for it in items:
            await asyncio.to_thread(media_file, Path(it["out"]), 4.0, False)

    monkeypatch.setattr(voice, "tts_available", lambda: True)
    monkeypatch.setattr(voice, "synthesize", fake_synth)
    from app import db, pipeline
    snap = client.get(f"/api/projects/{project}").json()
    lin = next(c for c in snap["characters"] if c["name"] == "林晚")
    ref = pipeline.project_dir(project, "voices") / "lin_test.wav"
    media_file(ref, 3.0, False)
    db.update("characters", lin["id"], {"voice_ref": pipeline.rel(ref)})
    long_shot = next(s for s in snap["shots"] if s["duration"] == 2)
    r = wait(client, client.post(f"/api/projects/{project}/animatic", json={"tts": True}).json()["id"])["result"]
    assert r["tts"] and synth
    t = next(x for x in r["timing"] if x["shot_id"] == long_shot["id"])
    assert not t["estimated"] and t["over"] and abs(t["duration"] - 4.6) < 0.1     # 画面跟着试听声音延长
    n = len(synth)
    wait(client, client.post(f"/api/projects/{project}/animatic", json={"tts": True}).json()["id"])
    assert len(synth) == n                                                            # 台词没改 → 用缓存，不再合成


def ap_wait(client, pid, timeout=120):
    import time
    end = time.time() + timeout
    while time.time() < end:
        st = client.get(f"/api/projects/{pid}/autopilot").json()
        if st["status"] != "running" and not st["active"]:
            return st
        time.sleep(0.2)
    raise AssertionError(f"一键成片超时：{st}")


def test_autopilot_unattended(client):
    pid = client.post("/api/projects", json={"title": "一键", "source_text": "雨夜车站重逢", "style": "cinematic"}).json()["id"]
    st = client.post(f"/api/projects/{pid}/autopilot/start", json={"stops": []}).json()
    assert st["status"] == "running" and st["stage"] == "beats" and st["eta_seconds"] > 0
    st = ap_wait(client, pid)
    assert st["status"] == "done", st
    snap = client.get(f"/api/projects/{pid}").json()
    assert snap["project"]["final_video"] and all(s["keyframe"] and s["video"] for s in snap["shots"])
    assert any(a["kind"] == "animatic" for a in snap["assets"][pid])
    done = [h["stage"] for h in st["history"]]
    assert done[:3] == ["beats", "script", "review"] and done[-1] == "assemble" and "animatic" in done
    assert all(x["state"] in {"done", "skip"} for x in st["stages"])
    # 自动化任务带标记、批量任务带批次号
    tasks = client.get(f"/api/tasks?project_id={pid}&limit=500").json()
    assert all(t["params"].get("_autopilot") == st["run"] for t in tasks)


def test_autopilot_review_stops_and_retry(client, monkeypatch):
    pid = client.post("/api/projects", json={"title": "审核", "source_text": "雨夜车站重逢", "style": "cinematic"}).json()["id"]
    client.post(f"/api/projects/{pid}/autopilot/start", json={})
    st = ap_wait(client, pid)
    assert st["status"] == "waiting" and st["stage"] == "review"
    client.patch(f"/api/projects/{pid}", json={"script": "（用户改过的剧本）雨夜，车站。"})
    client.post(f"/api/projects/{pid}/autopilot/continue")
    st = ap_wait(client, pid)
    assert st["status"] == "waiting" and st["stage"] == "storyboard"
    # 审核分镜时删掉一个镜头，后面就少做一个
    shots = client.get(f"/api/projects/{pid}").json()["shots"]
    client.delete(f"/api/shots/{shots[-1]['id']}")
    client.post(f"/api/projects/{pid}/autopilot/continue")
    st = ap_wait(client, pid)
    assert st["status"] == "waiting" and st["stage"] == "animatic"
    # 视频第一次全部失败 → 自动重试一次后成功
    fails = {"n": 0}
    real = comfy.run

    async def flaky(workflow, out_dir, progress=None):
        if any(n["class_type"] == "SaveVideo" for n in workflow.values()) and fails["n"] < 1:
            fails["n"] += 1
            raise RuntimeError("模拟编码器异常")
        return await real(workflow, out_dir, progress)

    monkeypatch.setattr(comfy, "run", flaky)
    client.post(f"/api/projects/{pid}/autopilot/continue")
    st = ap_wait(client, pid)
    assert st["status"] == "done", st
    assert any("自动重试" in n for n in st["notes"]) and fails["n"] == 1
    snap = client.get(f"/api/projects/{pid}").json()
    assert snap["project"]["script"].startswith("（用户改过的剧本）") and len(snap["shots"]) == len(shots) - 1


def test_autopilot_stop_and_fail(client, monkeypatch):
    pid = client.post("/api/projects", json={"title": "失败", "source_text": "雨夜车站重逢", "style": "cinematic"}).json()["id"]
    real = comfy.run

    async def broken(workflow, out_dir, progress=None):
        if any(n["class_type"] == "SaveVideo" for n in workflow.values()):
            raise RuntimeError("模拟 ComfyUI 崩溃")
        return await real(workflow, out_dir, progress)

    monkeypatch.setattr(comfy, "run", broken)
    client.post(f"/api/projects/{pid}/autopilot/start", json={"stops": []})
    st = ap_wait(client, pid)
    assert st["status"] == "failed" and st["stage"] == "videos" and "模拟 ComfyUI 崩溃" in st["message"]
    monkeypatch.setattr(comfy, "run", real)
    client.post(f"/api/projects/{pid}/autopilot/continue")
    assert ap_wait(client, pid)["status"] == "done"
    st = client.post(f"/api/projects/{pid}/autopilot/stop").json()
    assert st["status"] == "done"            # 已完成的不受停止影响


# ---------------- P1-4 失败自动恢复 ----------------
def test_diagnose_rules():
    from app.recovery import diagnose, explain
    assert diagnose("video", "ComfyUI 执行失败：KSampler: CUDA out of memory. Tried to allocate").kind == "oom"
    assert diagnose("keyframe", "httpx.ConnectError: All connection attempts failed").kind == "comfy_down"
    assert diagnose("script", "All connection attempts failed").kind == "llm_down"
    d = diagnose("video", "ComfyUI 拒绝工作流：{'type': 'value_not_in_list'}")
    assert d.kind == "config" and not d.retry
    assert not diagnose("keyframe", "请先生成角色的定妆照").retry
    assert not diagnose("assemble", "OSError: [Errno 28] No space left on device").retry
    assert "preflight" in explain("video", "value_not_in_list") and "原始错误" in explain("video", "value_not_in_list")


def _shot_video_task(client, pid):
    import time
    shot = client.get(f"/api/projects/{pid}").json()["shots"][0]
    tid = client.post(f"/api/shots/{shot['id']}/video", json={"quality": "final", "turbo": False}).json()["id"]
    for _ in range(300):
        t = next(x for x in client.get("/api/tasks?limit=500").json() if x["id"] == tid)
        if t["status"] in {"done", "failed", "cancelled"}:
            return t
        time.sleep(0.1)
    raise AssertionError(t)


def test_recover_oom_degrades(client, project, monkeypatch):
    real, seen = comfy.run, []

    async def oom_twice(workflow, out_dir, progress=None):
        if any(n["class_type"] == "SaveVideo" for n in workflow.values()):
            seen.append(json.dumps(workflow))
            if len(seen) <= 2:
                raise RuntimeError("ComfyUI 执行失败：SamplerCustom: CUDA out of memory")
        return await real(workflow, out_dir, progress)

    monkeypatch.setattr(comfy, "run", oom_twice)
    t = _shot_video_task(client, project)
    assert t["status"] == "done", t["message"]
    assert len(t["result"]["recovered"]) == 2 and "自动恢复 2 次" in t["message"]
    assert t["params"]["_degraded"] and t["params"]["quality"] == "draft" and t["params"]["turbo"] is True


def test_recover_comfy_crash_restarts(client, project, monkeypatch):
    from app import recovery
    import httpx
    restarts, calls = [], {"n": 0}
    real = comfy.run

    async def crash_once(workflow, out_dir, progress=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("All connection attempts failed")
        return await real(workflow, out_dir, progress)

    async def fake_restart(reason=""):
        restarts.append(reason)
        return True

    monkeypatch.setattr(comfy, "run", crash_once)
    monkeypatch.setattr(recovery, "restart_comfy", fake_restart)
    t = _shot_video_task(client, project)
    assert t["status"] == "done" and len(restarts) == 1 and "ComfyUI 已重启" in t["result"]["recovered"][0]


def test_recover_gives_up_with_advice(client, project, monkeypatch):
    calls = {"n": 0}

    async def bad(workflow, out_dir, progress=None):
        calls["n"] += 1
        raise RuntimeError("ComfyUI 拒绝工作流：value_not_in_list unet_name")

    monkeypatch.setattr(comfy, "run", bad)
    t = _shot_video_task(client, project)
    assert t["status"] == "failed" and calls["n"] == 1                         # 配置错误不重试
    assert t["message"].startswith("模型文件或节点缺失") and t["result"]["error_kind"] == "config"

    async def always_oom(workflow, out_dir, progress=None):
        calls["n"] += 1
        raise RuntimeError("CUDA out of memory")

    calls["n"] = 0
    monkeypatch.setattr(comfy, "run", always_oom)
    t = _shot_video_task(client, project)
    assert t["status"] == "failed" and calls["n"] == 3 and t["message"].startswith("显存/内存不足")   # 1 次 + 自动 2 次


def test_ensure_comfy_before_use(client, project, monkeypatch):
    from app import recovery
    from app.gpu import gpu
    state = {"alive": False, "restarts": 0}

    async def alive():
        return state["alive"]

    async def fake_restart(reason=""):
        state["restarts"] += 1
        state["alive"] = True
        return True

    monkeypatch.setattr(gpu, "comfy_alive", alive)
    monkeypatch.setattr(recovery, "restart_comfy", fake_restart)
    t = _shot_video_task(client, project)
    assert t["status"] == "done" and state["restarts"] == 1


def test_service_restart_requeues():
    from app import db
    from app.tasks import queue
    t = db.insert("tasks", {"type": "animatic", "project_id": "", "title": "中断", "status": "running", "params": {}})
    queue.recover_interrupted()
    t = db.get("tasks", t["id"])
    assert t["status"] == "queued" and t["params"]["_restarts"] == 1
    db.update("tasks", t["id"], {"status": "running", "params": {"_restarts": 2}})
    queue.recover_interrupted()
    assert db.get("tasks", t["id"])["status"] == "failed"


def test_continuity(client, monkeypatch):
    """P1-6：连续镜头用上一镜最后一帧做首帧、等上一镜先出视频；尾帧图进入首尾帧模式；上一镜重做后提示需重生成。"""
    from app import pipeline, tasks
    board = json.loads(json.dumps(BOARD))
    board["shots"][1]["continuous"] = True
    board["shots"][1]["shot_size"] = "近景"                        # 景别跨度大的不接尾帧（chain_mismatch），这里保持同景别
    board["shots"][0]["continuous"] = True                       # 第一镜即使写 true 也不算连续
    monkeypatch.setitem(BOARD, "shots", board["shots"])
    pid = client.post("/api/projects", json={"title": "衔接", "source_text": "雨夜车站重逢"}).json()["id"]
    wait(client, client.post(f"/api/projects/{pid}/script/generate").json()["id"])
    wait(client, client.post(f"/api/projects/{pid}/extract").json()["id"])
    wait(client, client.post(f"/api/projects/{pid}/storyboard/generate").json()["id"])
    shots = client.get(f"/api/projects/{pid}").json()["shots"]
    assert [s["continuous"] for s in shots] == [0, 1]
    a, b = shots
    assert pipeline.video_after(b) == {"_after": a["id"]} and pipeline.video_after(a) == {}
    # 调度：后一镜视频等前一镜视频
    t_a = {"id": "1", "type": "video", "owner_id": a["id"], "project_id": pid, "status": "queued", "params": {}}
    t_b = {"id": "2", "type": "video", "owner_id": b["id"], "project_id": pid, "status": "queued",
           "params": {"_after": a["id"]}}
    assert tasks.blocked(t_b, [t_a]) and not tasks.blocked({**t_b, "params": {}}, [t_a])

    client.post(f"/api/projects/{pid}/batch/keyframes", json={"only_missing": True, "count": 1})
    drain(client, pid)
    # 尾帧图：以分镜图为基础画结束姿态
    r = client.post(f"/api/shots/{a['id']}/end_frame", json={"instruction": ""})
    assert r.status_code == 200
    wait(client, r.json()["id"])
    a = client.get(f"/api/projects/{pid}").json()["shots"][0]
    assert pipeline.exists(a["end_frame"])

    CALLS.clear()
    client.post(f"/api/projects/{pid}/batch/videos", json={"only_missing": True, "quality": "draft"})
    drain(client, pid)
    vids = [w for w in CALLS if any(n["class_type"] == "SaveVideo" for n in w.values())]
    assert len(vids) == 2
    first = vids[0]["10"]["inputs"]
    assert "last_frame" in first and vids[0]["12"]["inputs"]["image"] == Path(a["end_frame"]).name   # 第一镜：首尾帧
    second = vids[1]["10"]["inputs"]
    chained = vids[1]["11"]["inputs"]["image"]
    assert chained.startswith(f"chain_{b['id']}") and "紧接上一个镜头" in second["prompt"]
    snap = client.get(f"/api/projects/{pid}").json()
    vb = next(x for x in snap["assets"][b["id"]] if x["kind"] == "video")
    assert vb["meta"]["chain_from"] == snap["shots"][0]["video"] and vb["meta"]["notes"]
    # 关掉连续 → 用本镜分镜图
    client.patch(f"/api/shots/{b['id']}", json={"continuous": 0})
    CALLS.clear()
    wait(client, client.post(f"/api/shots/{b['id']}/video", json={"quality": "draft"}).json()["id"])
    assert CALLS[-1]["11"]["inputs"]["image"] == Path(snap["shots"][1]["keyframe"]).name
    # 上传尾帧 / 选用 / 删除
    from PIL import Image
    img = Path(get_settings().data_dir) / "ef.png"
    Image.new("RGB", (32, 18), (9, 9, 9)).save(img)
    with img.open("rb") as f:
        up = client.post(f"/api/shots/{b['id']}/upload?target_field=end_frame", files={"file": ("ef.png", f, "image/png")})
    assert up.status_code == 200 and up.json()["end_frame"]
    ef = next(x for x in client.get(f"/api/projects/{pid}").json()["assets"][b["id"]] if x["kind"] == "end_frame")
    client.delete(f"/api/assets/{ef['id']}")
    assert client.get(f"/api/projects/{pid}").json()["shots"][1]["end_frame"] == ""
