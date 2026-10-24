"""P2-2 美术 / 选角：角色三视图 + 表情表、多套造型、场景变体（日/夜/雨/反打）、项目画风锁定、跨项目素材库。

出图时（keyframe_plan）按镜头自动挑参考：
- 角色：分镜指定的造型 → 造型定妆照；描述里是背影/侧身 → 背面/侧面图；台词情绪/表演指导 → 对应表情特写；
- 场景：分镜指定的变体，或画面里出现「夜/雨/黄昏…」时自动用对应变体；
- 画风：项目画风参考图作为最后一张参考，可选风格 LoRA。
"""
from __future__ import annotations

import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, File, HTTPException, UploadFile
from pydantic import BaseModel, Field

from . import db, prompts
from .tasks import TaskContext, handler, queue

router = APIRouter(prefix="/api")
VIEWS = {"side": "侧面", "back": "背面"}
EXPRESSIONS = {"开心": ("笑", "开心", "高兴", "喜", "兴奋", "得意"), "愤怒": ("怒", "生气", "愤", "吼", "咬牙"),
               "悲伤": ("哭", "泪", "悲", "伤心", "哽咽", "难过"), "惊讶": ("惊", "愣", "诧异", "不敢相信"),
               "恐惧": ("怕", "恐", "惧", "慌", "颤抖"), "冷漠": ("冷", "漠", "面无表情", "不屑", "轻蔑")}
VARIANT_PRESETS = {"夜晚": ("夜", "晚上", "深夜"), "雨天": ("雨",), "黄昏": ("黄昏", "傍晚", "夕阳"), "雪天": ("雪",),
                   "清晨": ("清晨", "早晨", "黎明"), "反打": ()}
EXPRESSION_LOOKS = {"开心": "眼睛弯成月牙，嘴角高高上扬露出牙齿，脸颊鼓起，灿烂的笑容",
                    "愤怒": "眉头紧锁下压，怒目圆睁，咬紧牙关，鼻翼张开，面部肌肉紧绷",
                    "悲伤": "眉头内侧上扬并皱起，眼眶泛红噙满泪水，脸颊挂着泪痕，嘴角向下撇",
                    "惊讶": "眉毛高高扬起，眼睛睁得很大，嘴巴张开呈 O 形",
                    "恐惧": "眼睛瞪大、眉头上扬并拧在一起，嘴唇微张发抖，脸色发白，身体微缩",
                    "冷漠": "眼皮半垂，眼神冷淡地斜视，嘴角平直略带不屑，面无表情"}
_BACK = re.compile(r"背影|背对|背身|转身离开|背朝")
_SIDE = re.compile(r"侧脸|侧身|侧面|侧对|侧影")


# ---------------- 按镜头挑参考 ----------------
def _clauses(text: str, name: str, others: list[str] | tuple = ()) -> list[str]:
    """属于该角色的分句：含名字的分句，以及其后省略主语的分句（直到出现别的角色名）。
    只有一个角色的镜头，开头没写名字的分句也算他的。"""
    out, cur = [], not others
    for c in re.split(r"[，。；,;！!？?\n]", text or ""):
        if name in c:
            cur = True
        elif any(o and o in c for o in others):
            cur = False
        if cur and c.strip():
            out.append(c)
    return out


def orientation(shot: dict, name: str, others: list[str] | tuple = ()) -> str | None:
    text = " ".join(_clauses(shot.get("description") or "", name, others))
    if _BACK.search(text):
        return "back"
    if _SIDE.search(text) or shot.get("camera_angle") == "侧面":
        return "side"
    return None


def emotion(shot: dict, name: str, others: list[str] | tuple = ()) -> str | None:
    texts = [d.get("emotion") or "" for d in shot.get("dialogue") or [] if d.get("speaker") == name]
    texts += _clauses(shot.get("performance") or "", name, others)
    texts += _clauses(shot.get("description") or "", name, others)
    joined = " ".join(texts)
    for key, words in EXPRESSIONS.items():
        if any(w in joined for w in words):
            return key
    return None


def dress(shot: dict, char: dict, others: list[str] | tuple = ()) -> dict:
    """返回按本镜头调整过参考图的角色副本（不改数据库）。"""
    from .pipeline import exists
    c = dict(char)
    sheet = c.get("sheet") or {}
    oid = (shot.get("outfits") or {}).get(c["id"])
    outfit = db.get("outfits", oid) if oid else None
    if outfit:
        c["appearance"] = outfit.get("appearance") or c.get("appearance")
        c["_outfit"] = outfit["name"] + (f"——{outfit['appearance']}" if outfit.get("appearance") else "")
        if exists(outfit.get("ref_image")):
            c["ref_image"] = outfit["ref_image"]
    view = orientation(shot, c["name"], others)
    if view and not outfit and exists(sheet.get(view)):
        c["ref_image"], c["_view"] = sheet[view], VIEWS[view]
    emo = emotion(shot, c["name"], others)
    if emo and exists((sheet.get("expr") or {}).get(emo)):
        c["face_image"], c["_expr"] = sheet["expr"][emo], emo
    return c


def place(shot: dict, loc: dict | None) -> dict | None:
    from .pipeline import exists
    if not loc:
        return loc
    variants = loc.get("variants") or {}
    want = shot.get("location_variant") or ""
    if not want:
        text = (shot.get("scene") or "") + (shot.get("description") or "")
        want = next((k for k, words in VARIANT_PRESETS.items() if k in variants and any(w in text for w in words)), "")
    if want and exists(variants.get(want)):
        return {**loc, "ref_image": variants[want], "_variant": want,
                "name": f"{loc['name']}（{want}）"}
    return loc


def style_lines(project: dict, idx: int | None) -> list[str]:
    if not idx:
        return []
    return [f"<image{idx}> 是本片的画风参考：只参考它的画风、色调、笔触和光影质感，不要照搬它的人物和内容。"]


def style_lora(project: dict) -> tuple[str, float] | None:
    name = (project.get("style_lora") or "").strip()
    return (name, float(project.get("style_lora_strength") or 0.8)) if name else None


# ---------------- 生成任务 ----------------
def sheet_prompt(project: dict, char: dict, kind: str, value: str) -> str:
    who = f"<image1> 中的角色「{char['name']}」"
    style = f"画风：{prompts.style_text(project)}。纯浅灰色干净背景，柔和均匀的影棚光，画面中只有这一个人，不要文字和水印。"
    if kind == "view":
        pose = {"side": "身体和脸转向画面右侧的标准侧面全身站姿（90° 侧面），能看清侧脸轮廓",
                "back": "背对镜头的背面全身站姿，能看清发型后侧和服装背面"}[value]
        return f"角色设定图：{who}的{VIEWS[value]}视图，{pose}。脸型、发型发色、体型、服装款式和颜色必须与 <image1> 完全一致。{style}"
    look = EXPRESSION_LOOKS.get(value, value)
    return (f"表情编辑：保持 {who}是同一个人，只改变面部表情为「{value}」——{look}。"
            f"表情要明显、一眼能看出情绪，不要保持原图的平静表情。头肩特写，正脸。"
            f"脸型五官比例、肤色、发型和服装必须与 <image1> 完全一致。{style}")


@handler("char_sheet")
async def task_char_sheet(ctx: TaskContext) -> dict:
    """角色三视图（侧面 / 背面）+ 表情表（6 种）。"""
    from .pipeline import _generate_images, absolute, add_asset, exists, need, project_dir
    char = need("characters", ctx.params.get("character_id"), "角色")
    project = need("projects", char["project_id"], "项目")
    if not exists(char.get("ref_image")):
        raise ValueError("请先生成定妆照")
    items = [("view", v) for v in ctx.params.get("views", list(VIEWS))] + \
            [("expression", e) for e in ctx.params.get("expressions", list(EXPRESSIONS))]
    out_dir = project_dir(project["id"], "characters")
    made = []
    for n, (kind, value) in enumerate(items):
        await ctx.progress(5 + 90 * n // max(1, len(items)), f"{VIEWS.get(value, value)}（{n + 1}/{len(items)}）")
        src = char["ref_image"] if kind == "view" or not exists(char.get("face_image")) else char["face_image"]
        prompt = sheet_prompt(project, char, kind, value)
        files = await _generate_images(ctx, prompt, "3:4" if kind == "view" else "1:1", [absolute(src)], 1, out_dir)
        a = add_asset(project["id"], kind, files[0], "character", char["id"], {"prompt": prompt, kind: value})
        made.append(a["id"])
        cur = db.get("characters", char["id"])
        sheet = dict(cur.get("sheet") or {})
        if kind == "view":
            sheet[value] = a["path"]
        else:
            sheet.setdefault("expr", {})[value] = a["path"]
        db.update("characters", char["id"], {"sheet": sheet})
    return {"assets": made}


@handler("outfit_image")
async def task_outfit_image(ctx: TaskContext) -> dict:
    from .pipeline import _generate_images, absolute, add_asset, exists, need, project_dir
    outfit = need("outfits", ctx.params.get("outfit_id"), "造型")
    char = need("characters", outfit["character_id"], "角色")
    project = need("projects", char["project_id"], "项目")
    src = char.get("face_image") if exists(char.get("face_image")) else char.get("ref_image")
    refs = [absolute(src)] if exists(src) else []
    who = f"<image1> 中的同一个人（脸型五官、发型发色保持一致）" if refs else f"角色{prompts._who(char)}"
    prompt = (f"角色造型定妆照，单人，正面全身站立，{who}，换上新的造型：{outfit.get('appearance') or outfit['name']}。"
              f"纯浅灰色干净背景，柔和均匀的影棚光。画风：{prompts.style_text(project)}。画面中只有这一个人，不要文字和水印。")
    files = await _generate_images(ctx, prompt, "3:4", refs, ctx.params.get("count", 1), project_dir(project["id"], "characters"))
    assets = [add_asset(project["id"], "outfit", f, "outfit", outfit["id"], {"prompt": prompt}) for f in files]
    if not exists(outfit.get("ref_image")):
        db.update("outfits", outfit["id"], {"ref_image": assets[0]["path"]})
    return {"assets": [a["id"] for a in assets]}


def variant_prompt(project: dict, loc: dict, variant: str, note: str) -> str:
    if variant == "反打":
        change = "从相反方向拍摄同一个空间（正反打的反向机位，看向原画面背后的一侧），陈设、材质、色调与 <image1> 一致"
    else:
        change = f"同一个空间、同一机位，变为「{variant}」的时间/天气：{note or variant}，陈设与布局与 <image1> 完全一致，只改变光线、天气和氛围"
    return f"电影场景概念图，空镜，画面中没有任何人物。以 <image1> 的场景「{loc['name']}」为准，{change}。画风：{prompts.style_text(project)}。不要文字和水印。"


@handler("location_variant")
async def task_location_variant(ctx: TaskContext) -> dict:
    from .pipeline import _generate_images, absolute, add_asset, exists, need, project_dir
    loc = need("locations", ctx.params.get("location_id"), "场景")
    project = need("projects", loc["project_id"], "项目")
    if not exists(loc.get("ref_image")):
        raise ValueError("请先生成场景图")
    variant = (ctx.params.get("variant") or "").strip() or "夜晚"
    prompt = variant_prompt(project, loc, variant, ctx.params.get("note") or "")
    files = await _generate_images(ctx, prompt, project["aspect_ratio"], [absolute(loc["ref_image"])], 1,
                                   project_dir(project["id"], "locations"))
    a = add_asset(project["id"], "loc_variant", files[0], "location", loc["id"], {"prompt": prompt, "variant": variant})
    cur = db.get("locations", loc["id"])
    db.update("locations", loc["id"], {"variants": {**(cur.get("variants") or {}), variant: a["path"]}})
    return {"asset": a["id"], "variant": variant}


# ---------------- 选用 / 删除 时维护 JSON 引用 ----------------
def select(asset: dict) -> dict | None:
    m = asset.get("meta") or {}
    if asset["owner_type"] == "character" and asset["kind"] in {"view", "expression"}:
        c = db.get("characters", asset["owner_id"])
        sheet = dict(c.get("sheet") or {})
        if asset["kind"] == "view":
            sheet[m.get("view") or "side"] = asset["path"]
        else:
            sheet.setdefault("expr", {})[m.get("expression") or "开心"] = asset["path"]
        return db.update("characters", c["id"], {"sheet": sheet})
    if asset["owner_type"] == "location" and asset["kind"] == "loc_variant":
        l = db.get("locations", asset["owner_id"])
        return db.update("locations", l["id"], {"variants": {**(l.get("variants") or {}), m.get("variant") or "变体": asset["path"]}})
    if asset["owner_type"] == "outfit":
        return db.update("outfits", asset["owner_id"], {"ref_image": asset["path"]})
    return None


def forget(path: str) -> None:
    """删除素材时清掉 JSON 字段里的引用。"""
    for c in db.query("SELECT * FROM characters WHERE sheet LIKE ?", [f"%{path}%"]):
        sheet = dict(c.get("sheet") or {})
        sheet = {k: v for k, v in sheet.items() if v != path}
        if "expr" in sheet:
            sheet["expr"] = {k: v for k, v in (sheet["expr"] or {}).items() if v != path}
        db.update("characters", c["id"], {"sheet": sheet})
    for l in db.query("SELECT * FROM locations WHERE variants LIKE ?", [f"%{path}%"]):
        db.update("locations", l["id"], {"variants": {k: v for k, v in (l.get("variants") or {}).items() if v != path}})
    db.execute("UPDATE outfits SET ref_image='' WHERE ref_image=?", [path])
    db.execute("UPDATE projects SET style_ref='' WHERE style_ref=?", [path])
    db.execute("UPDATE shots SET pose_ref='' WHERE pose_ref=?", [path])
    db.execute("UPDATE projects SET cover='' WHERE cover=?", [path])
    db.execute("UPDATE shots SET motion_ref='' WHERE motion_ref=?", [path])


# ---------------- 素材库（跨项目复用） ----------------
LIB_FIELDS = {
    "character": ("name", "role", "gender", "age", "appearance", "personality", "voice", "motivation", "relations",
                  "speech", "catchphrase"),
    "location": ("name", "description"),
    "prop": ("name", "description"),
}
LIB_IMAGES = {"character": ("ref_image", "face_image", "voice_ref"), "location": ("ref_image",), "prop": ("ref_image",)}
TABLE = {"character": "characters", "location": "locations", "prop": "props"}


def _copy_into_library(lib_id: str, relpath: str) -> str:
    from .pipeline import absolute, exists, media_root, rel
    if not exists(relpath):
        return ""
    d = media_root() / "library" / lib_id
    d.mkdir(parents=True, exist_ok=True)
    src = absolute(relpath)
    dst = d / src.name
    if not dst.exists():
        shutil.copy2(src, dst)
    return rel(dst)


def save_to_library(kind: str, row: dict) -> dict:
    lib_id = db.new_id()
    data = {k: row.get(k) or "" for k in LIB_FIELDS[kind]}
    images = {k: _copy_into_library(lib_id, row.get(k) or "") for k in LIB_IMAGES[kind]}
    if kind == "character":
        sheet = row.get("sheet") or {}
        images["sheet"] = {k: _copy_into_library(lib_id, v) for k, v in sheet.items() if isinstance(v, str)}
        images["sheet"]["expr"] = {k: _copy_into_library(lib_id, v) for k, v in (sheet.get("expr") or {}).items()}
        images["outfits"] = [{"name": o["name"], "appearance": o.get("appearance") or "",
                              "ref_image": _copy_into_library(lib_id, o.get("ref_image") or "")}
                             for o in db.query("SELECT * FROM outfits WHERE character_id=?", [row["id"]])]
    if kind == "location":
        images["variants"] = {k: _copy_into_library(lib_id, v) for k, v in (row.get("variants") or {}).items()}
    old = db.query("SELECT * FROM library WHERE kind=? AND name=?", [kind, data["name"]])
    if old:       # 同名覆盖为新版本
        db.execute("DELETE FROM library WHERE id=?", [old[0]["id"]])
    return db.insert("library", {"id": lib_id, "kind": kind, "name": data["name"], "data": data, "images": images})


def import_from_library(lib_id: str, project_id: str) -> dict:
    item = db.get("library", lib_id)
    if not item:
        raise ValueError("素材库条目不存在")
    kind, table = item["kind"], TABLE[item["kind"]]
    imgs = dict(item.get("images") or {})
    existing = db.query(f"SELECT * FROM {table} WHERE project_id=? AND name=?", [project_id, item["name"]])
    values: dict[str, Any] = {**item["data"], **{k: imgs.get(k) or "" for k in LIB_IMAGES[kind]}}
    if kind == "character":
        values["sheet"] = imgs.get("sheet") or {}
        values["locked"] = 1
        values["library_id"] = lib_id
    if kind == "location":
        values["variants"] = imgs.get("variants") or {}
    if existing:
        row = db.update(table, existing[0]["id"], values)
    else:
        n = len(db.query(f"SELECT id FROM {table} WHERE project_id=?", [project_id]))
        row = db.insert(table, {**values, "project_id": project_id, "sort": n})
    if kind == "character":
        have = {o["name"] for o in db.query("SELECT * FROM outfits WHERE character_id=?", [row["id"]])}
        for o in imgs.get("outfits") or []:
            if o["name"] not in have:
                db.insert("outfits", {**o, "project_id": project_id, "character_id": row["id"]})
    return row


# ---------------- 接口 ----------------
def _get(table: str, row_id: str, label: str) -> dict:
    row = db.get(table, row_id)
    if not row:
        raise HTTPException(404, f"{label}不存在")
    return row


class SheetIn(BaseModel):
    views: list[str] = Field(default_factory=lambda: list(VIEWS))
    expressions: list[str] = Field(default_factory=lambda: list(EXPRESSIONS))


@router.post("/characters/{char_id}/sheet")
async def gen_sheet(char_id: str, body: SheetIn) -> dict:
    from .pipeline import exists
    c = _get("characters", char_id, "角色")
    if not exists(c.get("ref_image")):
        raise HTTPException(400, "请先生成定妆照")
    views = [v for v in body.views if v in VIEWS]
    exprs = [e for e in body.expressions if e in EXPRESSIONS]
    if not views and not exprs:
        raise HTTPException(400, "至少选一项")
    return queue.submit("char_sheet", project_id=c["project_id"], title=f"三视图/表情 · {c['name']}", owner_type="character",
                        owner_id=c["id"], params={"character_id": c["id"], "views": views, "expressions": exprs})


class OutfitIn(BaseModel):
    name: str
    appearance: str = ""


@router.post("/characters/{char_id}/outfits")
async def add_outfit(char_id: str, body: OutfitIn) -> dict:
    c = _get("characters", char_id, "角色")
    return db.insert("outfits", {"project_id": c["project_id"], "character_id": c["id"], "name": body.name.strip() or "新造型",
                                 "appearance": body.appearance.strip()})


@router.patch("/outfits/{outfit_id}")
async def update_outfit(outfit_id: str, body: dict[str, Any]) -> dict:
    _get("outfits", outfit_id, "造型")
    return db.update("outfits", outfit_id, {k: v for k, v in body.items() if k in {"name", "appearance", "ref_image"}})


@router.delete("/outfits/{outfit_id}")
async def delete_outfit(outfit_id: str) -> dict:
    o = _get("outfits", outfit_id, "造型")
    db.execute("DELETE FROM outfits WHERE id=?", [outfit_id])
    from .pipeline import shots_of
    for s in shots_of(o["project_id"]):
        if (s.get("outfits") or {}).get(o["character_id"]) == outfit_id:
            db.update("shots", s["id"], {"outfits": {k: v for k, v in s["outfits"].items() if v != outfit_id}})
    return {"ok": True}


@router.post("/outfits/{outfit_id}/image")
async def gen_outfit(outfit_id: str) -> dict:
    o = _get("outfits", outfit_id, "造型")
    return queue.submit("outfit_image", project_id=o["project_id"], title=f"造型定妆照 · {o['name']}", owner_type="outfit",
                        owner_id=o["id"], params={"outfit_id": o["id"]})


class SceneOutfitIn(BaseModel):
    character_id: str
    outfit_id: str = ""          # 空 = 恢复默认造型
    scene: str | None = None     # 按场次批量；None = 只改 shot_ids
    shot_ids: list[str] | None = None


@router.post("/projects/{project_id}/outfits/assign")
async def assign_outfit(project_id: str, body: SceneOutfitIn) -> dict:
    from .pipeline import shots_of
    n = 0
    for s in shots_of(project_id):
        if body.character_id not in (s.get("character_ids") or []):
            continue
        if body.scene is not None and (s.get("scene") or "") != body.scene:
            continue
        if body.shot_ids is not None and s["id"] not in body.shot_ids:
            continue
        o = {k: v for k, v in (s.get("outfits") or {}).items() if k != body.character_id}
        if body.outfit_id:
            o[body.character_id] = body.outfit_id
        db.update("shots", s["id"], {"outfits": o})
        n += 1
    return {"shots": n}


class VariantIn(BaseModel):
    variant: str = "夜晚"
    note: str = ""


@router.post("/locations/{loc_id}/variant")
async def gen_variant(loc_id: str, body: VariantIn) -> dict:
    from .pipeline import exists
    l = _get("locations", loc_id, "场景")
    if not exists(l.get("ref_image")):
        raise HTTPException(400, "请先生成场景图")
    return queue.submit("location_variant", project_id=l["project_id"], title=f"场景变体 · {l['name']} · {body.variant}",
                        owner_type="location", owner_id=l["id"], params={"location_id": l["id"], **body.model_dump()},
                        dedupe=False)


@router.post("/projects/{project_id}/style_ref")
async def upload_style_ref(project_id: str, file: UploadFile = File(...)) -> dict:
    from .api import _save_upload
    from .pipeline import add_asset
    _get("projects", project_id, "项目")
    target = await _save_upload(file, project_id, "uploads")
    a = add_asset(project_id, "style_ref", target, "project", project_id, {"uploaded": True})
    return db.update("projects", project_id, {"style_ref": a["path"]})


@router.get("/library")
async def list_library(kind: str = "") -> list[dict]:
    sql, args = "SELECT * FROM library", []
    if kind:
        sql += " WHERE kind=?"
        args.append(kind)
    return db.query(sql + " ORDER BY created_at DESC", args)


class LibSaveIn(BaseModel):
    kind: str
    id: str


@router.post("/library")
async def save_library(body: LibSaveIn) -> dict:
    if body.kind not in TABLE:
        raise HTTPException(400, "不支持的类型")
    return save_to_library(body.kind, _get(TABLE[body.kind], body.id, "条目"))


class LibImportIn(BaseModel):
    ids: list[str]


@router.post("/projects/{project_id}/library/import")
async def import_library(project_id: str, body: LibImportIn) -> dict:
    _get("projects", project_id, "项目")
    try:
        rows = [import_from_library(i, project_id) for i in body.ids]
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return {"imported": len(rows)}


@router.delete("/library/{lib_id}")
async def delete_library(lib_id: str) -> dict:
    from .pipeline import media_root
    db.execute("DELETE FROM library WHERE id=?", [lib_id])
    shutil.rmtree(media_root() / "library" / lib_id, ignore_errors=True)
    return {"ok": True}
