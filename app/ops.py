"""P4-5 运维与统计：任务耗时 / 成功率统计、磁盘清理（删除未选用的候选）、项目备份与恢复（zip）。"""
from __future__ import annotations

import asyncio
import json
import shutil
import statistics
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any, Iterable

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel

from . import db
from .config import get_settings
from .tasks import ACTIVE, GROUP, TaskContext, handler, queue

router = APIRouter(prefix="/api")

TYPE_LABELS = {
    "script": "剧本", "extract": "提取角色场景", "storyboard": "分镜脚本", "portrait": "角色定妆", "face_ref": "脸部特写",
    "location_image": "场景图", "prop_image": "道具图", "keyframe": "分镜图", "keyframe_edit": "局部重绘", "end_frame": "尾帧图",
    "video": "镜头视频（本地）", "cloud_video": "镜头视频（云端）", "voice_sample": "音色样本", "dub": "配音对口型",
    "face_fix": "人脸修正", "facefusion": "FaceFusion", "assemble": "合成成片", "animatic": "动态分镜", "narration": "旁白配音",
    "narration_write": "旁白文案", "bgm": "AI 配乐", "upscale": "超分", "interpolate": "补帧", "export": "导出工程",
    "series_plan": "分集规划", "recap": "前情摘要", "backup": "备份项目",
}
PROJECT_TABLES = ("characters", "locations", "props", "outfits", "shots", "assets", "suggestions")
KEEP_LATEST = {"animatic", "export"}        # 这些只保留最新一个
FINAL_KINDS = {"final"}                     # 成片历史：默认不清理


# ---------------- 统计 ----------------
def _pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = min(len(v) - 1, max(0, round(q * (len(v) - 1))))
    return round(v[k], 1)


def stats(days: int = 14, project_id: str = "") -> dict:
    since = time.time() - days * 86400
    sql, args = "SELECT * FROM tasks WHERE status IN ('done','failed','cancelled') AND created_at>=?", [since]
    if project_id:
        sql += " AND project_id=?"
        args.append(project_id)
    rows = db.query(sql, args)
    by_type: dict[str, dict] = {}
    daily: dict[str, dict] = {}
    errors: dict[str, dict] = {}
    groups: dict[str, float] = {}
    per_project: dict[str, dict] = {}
    engines: dict[str, dict] = {}
    recovered = 0
    for t in rows:
        r = t.get("result") or {}
        el = float(r.get("elapsed") or 0) if t["status"] == "done" else 0.0
        d = by_type.setdefault(t["type"], {"type": t["type"], "label": TYPE_LABELS.get(t["type"], t["type"]), "done": 0,
                                           "failed": 0, "cancelled": 0, "_el": []})
        d[t["status"]] += 1
        if t["status"] == "done" and el:
            d["_el"].append(el)
            g = GROUP.get(t["type"], "cpu")
            groups[g] = groups.get(g, 0) + el
        if r.get("recovered"):
            recovered += 1
        day = time.strftime("%Y-%m-%d", time.localtime(t["created_at"]))
        dd = daily.setdefault(day, {"day": day, "done": 0, "failed": 0, "seconds": 0.0})
        if t["status"] in ("done", "failed"):
            dd[t["status"]] += 1
        dd["seconds"] += el
        if t["status"] == "failed":
            k = r.get("error_kind") or "unknown"
            e = errors.setdefault(k, {"kind": k, "count": 0, "sample": "", "types": {}})
            e["count"] += 1
            e["sample"] = e["sample"] or (t.get("message") or "")[:160]
            e["types"][t["type"]] = e["types"].get(t["type"], 0) + 1
        if t.get("project_id"):
            pp = per_project.setdefault(t["project_id"], {"project_id": t["project_id"], "tasks": 0, "failed": 0, "seconds": 0.0})
            pp["tasks"] += 1
            pp["failed"] += t["status"] == "failed"
            pp["seconds"] += el
        if t["type"] == "cloud_video":
            eng = (t.get("params") or {}).get("engine") or "?"
            ee = engines.setdefault(eng, {"engine": eng, "done": 0, "failed": 0, "_el": []})
            if t["status"] in ("done", "failed"):
                ee[t["status"]] += 1
            if el:
                ee["_el"].append(el)
    types = []
    for d in by_type.values():
        el = d.pop("_el")
        fin = d["done"] + d["failed"]
        d.update({"success_rate": round(d["done"] / fin, 3) if fin else None, "avg": round(statistics.mean(el), 1) if el else 0,
                  "p50": _pct(el, 0.5), "p90": _pct(el, 0.9), "total": round(sum(el), 1)})
        types.append(d)
    types.sort(key=lambda x: -x["total"])
    for e in engines.values():
        el = e.pop("_el")
        fin = e["done"] + e["failed"]
        e.update({"success_rate": round(e["done"] / fin, 3) if fin else None, "avg": round(statistics.mean(el), 1) if el else 0})
    titles = {p["id"]: p["title"] for p in db.query("SELECT id, title FROM projects")}
    projects = sorted(({**p, "title": titles.get(p["project_id"], "（已删除）"), "seconds": round(p["seconds"], 1)}
                       for p in per_project.values()), key=lambda x: -x["seconds"])[:20]
    done = sum(d["done"] for d in types)
    failed = sum(d["failed"] for d in types)
    return {"days": days, "total": {"tasks": len(rows), "done": done, "failed": failed,
                                    "cancelled": sum(d["cancelled"] for d in types), "recovered": recovered,
                                    "success_rate": round(done / (done + failed), 3) if done + failed else None,
                                    "seconds": round(sum(d["total"] for d in types), 1)},
            "types": types, "groups": {k: round(v, 1) for k, v in sorted(groups.items(), key=lambda x: -x[1])},
            "daily": [daily[k] | {"seconds": round(daily[k]["seconds"], 1)} for k in sorted(daily)],
            "errors": sorted(errors.values(), key=lambda x: -x["count"]), "projects": projects,
            "engines": list(engines.values())}


# ---------------- 引用分析 ----------------
def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for k, v in value.items():
            yield str(k)
            yield from _strings(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _strings(v)


def referenced(pid: str) -> set[str]:
    """项目里正在使用的文件（各表字段 + 被使用素材的 meta 里引用的文件 + 未完成任务的参数）。"""
    prefix = pid + "/"
    refs: set[str] = set()
    rows = db.query("SELECT * FROM projects WHERE id=?", [pid])
    for t in ("characters", "locations", "props", "outfits", "shots"):
        rows += db.query(f"SELECT * FROM {t} WHERE project_id=?", [pid])
    rows += db.query("SELECT data FROM snapshots WHERE project_id=?", [pid])      # 存档点里用到的文件也保留
    rows += db.query(f"SELECT params FROM tasks WHERE project_id=? AND status IN ({','.join('?' * len(ACTIVE))})", [pid, *ACTIVE])
    for r in rows:
        refs.update(s for s in _strings(r) if s.startswith(prefix))
    assets = {a["path"]: a for a in db.query("SELECT * FROM assets WHERE project_id=?", [pid])}
    todo = [p for p in refs if p in assets]
    while todo:                                  # 闭包：被使用的素材引用的源文件（超分原版、配音分轨……）也保留
        a = assets[todo.pop()]
        for s in _strings(a.get("meta") or {}):
            if s.startswith(prefix) and s not in refs:
                refs.add(s)
                if s in assets:
                    todo.append(s)
    return refs


def _size(p: Path) -> int:
    try:
        return p.stat().st_size if p.is_file() else sum(f.stat().st_size for f in p.rglob("*") if f.is_file())
    except OSError:
        return 0


def plan_cleanup(pid: str, candidates: bool = True, old_finals: bool = False, orphans: bool = True,
                 keep: int = 0, min_age: float = 3600) -> dict:
    from .pipeline import media_root
    root = media_root()
    refs = referenced(pid)
    items: list[dict] = []
    assets = db.query("SELECT * FROM assets WHERE project_id=? ORDER BY created_at DESC", [pid])
    seen: dict[tuple, int] = {}
    latest: set[tuple] = set()
    for a in assets:                              # 按时间倒序
        if a["path"] in refs:
            continue
        kind = a["kind"]
        key = (a["owner_type"], a["owner_id"], kind)
        if kind in FINAL_KINDS:
            akey = (kind, (a.get("meta") or {}).get("aspect") or "")
            if akey not in latest or not old_finals:
                latest.add(akey)                  # 当前成片 + 每种画幅最新的一版永远保留；其余历史版本按选项清理
                continue
            cat = "old_finals"
        elif kind in KEEP_LATEST:
            if key not in latest:
                latest.add(key)
                continue
            cat = "candidates"
        else:
            n = seen.get(key, 0)
            seen[key] = n + 1
            if n < keep:
                continue
            cat = "candidates"
        if cat == "candidates" and not candidates:
            continue
        f = root / a["path"]
        items.append({"path": a["path"], "size": _size(f), "cat": cat, "asset_id": a["id"], "kind": kind})
    pdir = root / pid
    if orphans and pdir.is_dir():
        known = {a["path"] for a in assets} | refs
        now = time.time()
        for d in pdir.glob("*/work_*"):          # 合成中断留下的临时目录
            if d.is_dir() and now - d.stat().st_mtime > min_age:
                items.append({"path": d.relative_to(root).as_posix(), "size": _size(d), "cat": "temp", "asset_id": "", "kind": "dir"})
        for f in pdir.rglob("*"):
            if not f.is_file() or any(p.name.startswith("work_") for p in f.parents):
                continue
            rp = f.relative_to(root).as_posix()
            if rp in known or f.parent.name in ("uploads", "cast") or f.name == "subtitles.srt":
                continue
            if now - f.stat().st_mtime > min_age:
                items.append({"path": rp, "size": f.stat().st_size, "cat": "orphans", "asset_id": "", "kind": "file"})
    summary: dict[str, dict] = {}
    for it in items:
        s = summary.setdefault(it["cat"], {"files": 0, "bytes": 0})
        s["files"] += 1
        s["bytes"] += it["size"]
    return {"items": items, "summary": summary, "bytes": sum(i["size"] for i in items),
            "project_bytes": _size(pdir) if pdir.is_dir() else 0}


def run_cleanup(pid: str, **opts) -> dict:
    from .pipeline import media_root
    if db.query("SELECT id FROM tasks WHERE project_id=? AND status IN ('running','cancelling')", [pid]):
        raise ValueError("项目还有任务在运行，等任务结束再清理")
    plan = plan_cleanup(pid, **opts)
    root = media_root()
    freed = 0
    for it in plan["items"]:
        p = (root / it["path"]).resolve()
        if root not in p.parents or not str(it["path"]).startswith(pid + "/"):
            continue
        if p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
        else:
            p.unlink(missing_ok=True)
        if it["asset_id"]:
            db.execute("DELETE FROM assets WHERE id=?", [it["asset_id"]])
        freed += it["size"]
    return {"deleted": len(plan["items"]), "freed": freed, "summary": plan["summary"]}


def disk_usage() -> dict:
    from .pipeline import media_root
    root = media_root()
    rows = []
    for p in db.query("SELECT id, title, series_id FROM projects ORDER BY updated_at DESC"):
        d = root / p["id"]
        rows.append({"id": p["id"], "title": p["title"], "bytes": _size(d) if d.is_dir() else 0})
    known = {p["id"] for p in rows} | {"toolbox", "library"}
    stray = [d.name for d in root.iterdir() if d.is_dir() and d.name not in known] if root.is_dir() else []
    disk = shutil.disk_usage(root if root.is_dir() else get_settings().data_dir)
    backups = backup_dir()
    return {"projects": sorted(rows, key=lambda x: -x["bytes"]), "media_bytes": sum(r["bytes"] for r in rows),
            "stray_dirs": stray, "free": disk.free, "total": disk.total,
            "backups": [{"name": f.name, "bytes": f.stat().st_size, "mtime": f.stat().st_mtime}
                        for f in sorted(backups.glob("*.zip"), key=lambda f: -f.stat().st_mtime)]}


# ---------------- 备份 / 恢复 ----------------
BACKUP_VERSION = 1


def backup_dir() -> Path:
    d = get_settings().data_dir / "backups"
    d.mkdir(parents=True, exist_ok=True)
    return d


def dump_project(pid: str) -> dict:
    p = db.get("projects", pid)
    if not p:
        raise ValueError("项目不存在")
    data = {"version": BACKUP_VERSION, "created_at": time.time(), "project": p,
            "series": db.get("series", p["series_id"]) if p.get("series_id") else None}
    for t in PROJECT_TABLES:
        data[t] = db.query(f"SELECT * FROM {t} WHERE project_id=?", [pid])
    return data


def build_backup(pid: str, out: Path, slim: bool = False) -> dict:
    """zip：project.json + media/（项目目录下全部文件；slim=True 时跳过未选用的候选）。"""
    from .pipeline import media_root
    data = dump_project(pid)
    root = media_root() / pid
    skip = {i["path"] for i in plan_cleanup(pid, candidates=True, orphans=True, min_age=0)["items"]} if slim else set()
    if slim:
        data["assets"] = [a for a in data["assets"] if a["path"] not in skip]
    n, size = 0, 0
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_suffix(".part")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as z:
        z.writestr("project.json", json.dumps(data, ensure_ascii=False, indent=1))
        if root.is_dir():
            for f in sorted(root.rglob("*")):
                rp = f.relative_to(root.parent).as_posix()
                if not f.is_file() or rp in skip or any(rp.startswith(s + "/") for s in skip) or "/work_" in rp:
                    continue
                comp = zipfile.ZIP_STORED if f.suffix.lower() in {".mp4", ".mov", ".webm", ".png", ".jpg", ".jpeg", ".webp", ".zip", ".mp3", ".m4a"} else zipfile.ZIP_DEFLATED
                z.write(f, "media/" + f.relative_to(root).as_posix(), compress_type=comp)
                n += 1
                size += f.stat().st_size
    tmp.replace(out)
    return {"files": n, "bytes": out.stat().st_size, "media_bytes": size}


def _remap(value: Any, ids: dict[str, str], old: str, new: str) -> Any:
    if isinstance(value, str):
        if value in ids:
            return ids[value]
        if value.startswith(old + "/"):
            return new + value[len(old):]
        return value
    if isinstance(value, dict):
        return {(ids.get(k, k) if isinstance(k, str) else k): _remap(v, ids, old, new) for k, v in value.items()}
    if isinstance(value, list):
        return [_remap(v, ids, old, new) for v in value]
    return value


def restore_backup(zpath: Path, title: str = "") -> dict:
    """导入为新项目（所有 id 重新生成，文件路径和引用一起改），不会覆盖现有项目。"""
    from .pipeline import media_root
    with zipfile.ZipFile(zpath) as z:
        try:
            data = json.loads(z.read("project.json"))
        except KeyError:
            raise ValueError("不是 AI Studio 的项目备份（缺少 project.json）")
        if int(data.get("version") or 0) > BACKUP_VERSION:
            raise ValueError("备份来自更新的版本，请先升级")
        old = data["project"]["id"]
        new = db.new_id()
        ids = {old: new}
        for t in PROJECT_TABLES:
            for r in data.get(t) or []:
                ids[r["id"]] = db.new_id()
        dest = media_root() / new
        dest.mkdir(parents=True, exist_ok=True)
        n = 0
        for info in z.infolist():
            if not info.filename.startswith("media/") or info.is_dir():
                continue
            relp = Path(info.filename[len("media/"):])
            target = (dest / relp).resolve()
            if dest.resolve() not in target.parents:
                continue                       # 防 zip 路径穿越
            target.parent.mkdir(parents=True, exist_ok=True)
            with z.open(info) as src, target.open("wb") as f:
                shutil.copyfileobj(src, f, 1 << 20)
            n += 1
    p = _remap({k: v for k, v in data["project"].items()}, ids, old, new)
    p.update({"id": new, "autopilot": {}})
    if title:
        p["title"] = title
    if p.get("series_id") and not db.get("series", p["series_id"]):
        p.update({"series_id": "", "episode": 0})
    cols = {r[1] for r in db.conn().execute("PRAGMA table_info(projects)").fetchall()}
    db.insert("projects", {k: v for k, v in p.items() if k in cols})
    counts = {}
    for t in PROJECT_TABLES:
        cols = {r[1] for r in db.conn().execute(f"PRAGMA table_info({t})").fetchall()}
        rows = data.get(t) or []
        for r in rows:
            row = _remap(r, ids, old, new)
            row["project_id"] = new
            db.insert(t, {k: v for k, v in row.items() if k in cols})
        counts[t] = len(rows)
    return {"id": new, "title": p["title"], "files": n, "counts": counts}


@handler("backup")
async def task_backup(ctx: TaskContext) -> dict:
    p = db.get("projects", ctx.project_id)
    if not p:
        raise ValueError("项目不存在")
    safe = "".join(ch for ch in (p["title"] or "project") if ch not in '\\/:*?"<>|')[:40]
    out = backup_dir() / f"{safe}_{time.strftime('%Y%m%d_%H%M%S')}{'_精简' if ctx.params.get('slim') else ''}.zip"
    await ctx.progress(10, "打包项目文件…")
    info = await asyncio.to_thread(build_backup, p["id"], out, bool(ctx.params.get("slim")))
    return {"name": out.name, **info}


# ---------------- 接口 ----------------
class CleanupIn(BaseModel):
    candidates: bool = True
    old_finals: bool = False
    orphans: bool = True
    keep: int = 0


class BackupIn(BaseModel):
    slim: bool = False


@router.get("/ops/stats")
async def api_stats(days: int = 14, project_id: str = "") -> dict:
    return await asyncio.to_thread(stats, max(1, min(365, days)), project_id)


@router.get("/ops/disk")
async def api_disk() -> dict:
    return await asyncio.to_thread(disk_usage)


@router.post("/projects/{project_id}/cleanup/preview")
async def api_cleanup_preview(project_id: str, body: CleanupIn) -> dict:
    if not db.get("projects", project_id):
        raise HTTPException(404, "项目不存在")
    plan = await asyncio.to_thread(plan_cleanup, project_id, **body.model_dump())
    plan["items"] = plan["items"][:300]
    return plan


@router.post("/projects/{project_id}/cleanup")
async def api_cleanup(project_id: str, body: CleanupIn) -> dict:
    if not db.get("projects", project_id):
        raise HTTPException(404, "项目不存在")
    try:
        return await asyncio.to_thread(run_cleanup, project_id, **body.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/projects/{project_id}/backup")
async def api_backup(project_id: str, body: BackupIn) -> dict:
    if not db.get("projects", project_id):
        raise HTTPException(404, "项目不存在")
    return queue.submit("backup", project_id=project_id, title="备份项目", owner_type="project",
                        owner_id=f"{project_id}:backup", params=body.model_dump())


@router.get("/backups/{name}")
async def api_backup_file(name: str):
    from fastapi.responses import FileResponse
    f = (backup_dir() / name).resolve()
    if f.parent != backup_dir().resolve() or not f.is_file():
        raise HTTPException(404, "备份不存在")
    return FileResponse(f, filename=name, media_type="application/zip")


@router.delete("/backups/{name}")
async def api_backup_delete(name: str) -> dict:
    f = (backup_dir() / name).resolve()
    if f.parent != backup_dir().resolve() or not f.is_file():
        raise HTTPException(404, "备份不存在")
    f.unlink()
    return {"ok": True}


@router.post("/projects/restore")
async def api_restore(file: UploadFile = File(...), name: str = "") -> dict:
    tmp = Path(tempfile.mkstemp(suffix=".zip", dir=backup_dir())[1])
    try:
        with tmp.open("wb") as f:
            while chunk := await file.read(1 << 20):
                f.write(chunk)
        return await asyncio.to_thread(restore_backup, tmp, name)
    except (ValueError, zipfile.BadZipFile) as e:
        raise HTTPException(400, f"恢复失败：{e}")
    finally:
        tmp.unlink(missing_ok=True)


@router.post("/backups/{name}/restore")
async def api_restore_existing(name: str) -> dict:
    f = (backup_dir() / name).resolve()
    if f.parent != backup_dir().resolve() or not f.is_file():
        raise HTTPException(404, "备份不存在")
    try:
        return await asyncio.to_thread(restore_backup, f)
    except (ValueError, zipfile.BadZipFile) as e:
        raise HTTPException(400, f"恢复失败：{e}")
