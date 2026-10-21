"""P1-2 任务调度：按模型分组、遵守依赖、手动优先、可暂停。"""
import itertools

from app import tasks

_ids = itertools.count()


def T(type_, owner="", project="p1", status="queued", batch=False):
    return {"id": f"t{next(_ids)}", "type": type_, "owner_id": owner, "project_id": project, "status": status,
            "params": {"_batch": "b"} if batch else {}}


def run_all(queue, group=""):
    """模拟单 GPU 依次执行，返回执行顺序与模型切换次数。"""
    order, switches = [], 0
    queue = [dict(t) for t in queue]
    while (t := tasks.pick_next(queue, group)) is not None:
        g = tasks.task_group(t["type"])
        if g != "cpu":
            if g != group:
                switches += 1
            group = g
        order.append(t)
        t["status"] = "done"
        queue = [x for x in queue if x["status"] != "done"]
    return order, switches


def test_batch_keyframes_then_videos_interleaved():
    q = []
    for i in range(13):                       # 用户逐镜头交替点了「分镜图」「视频」
        q += [T("keyframe", f"s{i}", batch=True), T("video", f"s{i}", batch=True)]
    order, switches = run_all(q, group="")
    assert switches == 2                      # 空闲→出图 1 次 + 出图→视频 1 次
    kinds = [t["type"] for t in order]
    assert kinds == ["keyframe"] * 13 + ["video"] * 13


def test_dependency_video_waits_for_its_keyframe():
    q = [T("keyframe", "s1"), T("video", "s1"), T("keyframe", "s2"), T("video", "s2")]
    order, switches = run_all(q, group="video")     # 显存里是视频模型，也不能先跑 s1 视频
    assert [t["type"] for t in order] == ["keyframe", "keyframe", "video", "video"]
    assert switches == 2


def test_stage_order_inside_project():
    q = [T("portrait", "c1"), T("video", "s9"), T("keyframe", "s1"), T("dub", "s9"), T("assemble", "p1:assemble")]
    order, _ = run_all(q, group="video")
    kinds = [t["type"] for t in order]
    assert kinds.index("portrait") < kinds.index("keyframe") < kinds.index("video") < kinds.index("dub") < kinds.index("assemble")


def test_other_project_runs_with_current_model():
    q = [T("keyframe", "a1", project="A"), T("video", "b1", project="B"), T("keyframe", "a2", project="A")]
    order, switches = run_all(q, group="image")
    assert [t["owner_id"] for t in order] == ["a1", "a2", "b1"] and switches == 1


def test_manual_beats_batch():
    q = [T("video", f"s{i}", batch=True) for i in range(3)] + [T("portrait", "c1", project="p2")]
    order, _ = run_all(q, group="video")
    assert order[0]["type"] == "portrait"


def test_llm_barrier_keeps_fifo():
    q = [T("keyframe", "s1"), T("storyboard", "p1"), T("keyframe", "s2")]
    order, _ = run_all(q, group="")
    assert [t["owner_id"] for t in order] == ["s1", "p1", "s2"]


def test_paused_tasks_skipped_and_only_block_same_owner():
    q = [T("video", "s1", status="paused"), T("dub", "s2"), T("dub", "s1")]
    order, _ = run_all(q)
    assert [t["owner_id"] for t in order] == ["s2"]

