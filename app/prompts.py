"""文字模型提示词 + 出图/出视频提示词构建（确定性，不再经过任何审计改写）。"""
from __future__ import annotations

import json
import re
from typing import Any

STYLES: dict[str, dict[str, str]] = {
    "cinematic": {"label": "写实电影", "prompt": "电影级写实摄影，35mm 电影镜头质感，自然真实的光影，细腻的皮肤与材质细节，浅景深"},
    "manju": {"label": "国漫厚涂", "prompt": "国风漫剧厚涂插画风格，精致的人物五官，强烈的光影对比，色彩饱满，高完成度"},
    "anime": {"label": "日系动漫", "prompt": "日系二次元动画风格，赛璐璐上色，干净利落的线条，明亮通透的色彩"},
    "guofeng": {"label": "古风水墨", "prompt": "中国古风插画风格，水墨晕染结合工笔细节，典雅的东方配色"},
    "3d": {"label": "3D 动画", "prompt": "3D 动画电影风格，皮克斯式质感，柔和的全局光照，可爱圆润的造型"},
    "cyberpunk": {"label": "赛博朋克", "prompt": "赛博朋克电影风格，霓虹灯光，潮湿反光的街道，高对比冷暖色调"},
}

SHOT_SIZES = ["远景", "全景", "中景", "近景", "特写"]
CAMERA_ANGLES = ["平视", "俯视", "仰视", "过肩", "主观视角", "侧面"]
CAMERA_MOVES = ["固定", "推镜", "拉镜", "摇镜", "移镜", "跟拍", "环绕", "升降"]


def style_text(project: dict) -> str:
    base = STYLES.get(project.get("style") or "", STYLES["cinematic"])["prompt"]
    note = (project.get("style_note") or "").strip()
    return f"{base}，{note}" if note else base


# ---------------- ① 剧本 ----------------
SCRIPT_SYSTEM = """你是资深短视频/短剧编剧。把用户给的素材改编成适合 AI 生成的短视频剧本。
要求：
1. 节奏紧凑，开头 3 秒有钩子，结尾有反转或情绪落点。
2. 按场景组织，每个场景以「## 场景N：地点 · 时间」开头。
3. 场景内用「△」写可拍摄的画面和动作，用「角色名：（情绪）台词」写对白。只写镜头能拍到的内容，不写心理活动。
4. 人物数量克制（主要角色不超过 5 个），每个角色名字固定不变。
只输出 JSON：{"title":"片名","synopsis":"一句话梗概","script":"完整剧本（Markdown）"}"""


def script_user(project: dict) -> str:
    kind = {"idea": "创意/梗概", "novel": "小说原文", "script": "已有剧本（请整理成标准格式，尽量保留原意）"}.get(
        project.get("source_type") or "idea", "创意")
    source = (project.get("source_text") or "").strip()[:24000]
    return (f"素材类型：{kind}\n目标成片时长：约 {project.get('target_seconds') or 60} 秒\n"
            f"画面风格：{style_text(project)}\n\n素材：\n{source}")


def validate_script(data: Any) -> dict:
    if not isinstance(data, dict) or not str(data.get("script") or "").strip():
        raise ValueError("缺少 script 字段")
    return {"title": str(data.get("title") or "").strip(), "synopsis": str(data.get("synopsis") or "").strip(),
            "script": str(data["script"]).strip()}


# ---------------- ② 角色与场景 ----------------
EXTRACT_SYSTEM = """你是影视美术指导。从剧本中提取所有出镜角色和场景，写成可以直接用于 AI 绘图的设定。
角色 appearance 必须具体可画：性别年龄感、脸型五官、发型发色、体型、服装（款式+颜色+材质）、标志性配饰。不同角色之间外貌差异要明显。
voice 写声音特征（如：清亮的少女声 / 低沉沙哑的中年男声），用于视频配音。
人设卡：personality 性格（3~5 个关键词+一句说明），motivation 核心动机/想要什么，relations 与其他角色的关系，
speech 说话方式（语速、用词习惯、语气），catchphrase 口头禅（没有就留空）。
场景 description 写清：空间类型、时代、陈设、主色调、光线。
props 是关键道具：在两个及以上镜头出现、或推动剧情的具体物品（信物、照片、信件、武器、手机等），最多 6 个，没有就给空数组。
道具 description 写清外观细节：材质、颜色、尺寸、新旧程度、上面的图案或内容（例如：泛黄的旧照片，白色窄边，上面是两个少年在海边的合影）。
只输出 JSON：
{"characters":[{"name":"","role":"主角|配角|龙套","gender":"","age":"","appearance":"","personality":"","voice":"",
"motivation":"","relations":"","speech":"","catchphrase":""}],
 "locations":[{"name":"","description":""}],
 "props":[{"name":"","description":""}]}"""


def extract_user(project: dict) -> str:
    return f"画面风格：{style_text(project)}\n\n剧本：\n{(project.get('script') or '')[:24000]}"


def validate_extract(data: Any) -> dict:
    if not isinstance(data, dict):
        raise ValueError("顶层必须是对象")
    chars, seen = [], set()
    for c in data.get("characters") or []:
        name = str((c or {}).get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            chars.append({k: str(c.get(k) or "").strip() for k in
                          ("name", "role", "gender", "age", "appearance", "personality", "voice",
                           "motivation", "relations", "speech", "catchphrase")})
    locs, seen = [], set()
    for loc in data.get("locations") or []:
        name = str((loc or {}).get("name") or "").strip()
        if name and name not in seen:
            seen.add(name)
            locs.append({"name": name, "description": str(loc.get("description") or "").strip()})
    props, seen = [], set()
    for pr in data.get("props") or []:
        name = str((pr or {}).get("name") or "").strip() if isinstance(pr, dict) else ""
        if name and name not in seen:
            seen.add(name)
            props.append({"name": name, "description": str(pr.get("description") or "").strip()})
    if not chars and not locs:
        raise ValueError("没有提取到任何角色或场景")
    return {"characters": chars, "locations": locs, "props": props[:6]}


def portrait_prompt(project: dict, char: dict) -> str:
    return (f"角色设定参考图，单人，正面全身站立，表情自然，双手自然下垂，纯浅灰色干净背景，柔和均匀的影棚光。"
            f"角色：{char.get('name')}，{char.get('gender')}，{char.get('age')}。外貌与服装：{char.get('appearance')}。"
            f"画风：{style_text(project)}。画面中只有这一个人，不要文字和水印。")


def face_prompt(project: dict, char: dict, has_ref: bool) -> str:
    """脸部特写：以定妆照为 <image1> 派生，保证与全身图是同一个人；近景/特写镜头靠它锁五官。"""
    who = (f"以 <image1> 中的角色为准，生成同一个人的脸部特写。脸型五官、肤色、发型发色、妆容和配饰必须与 <image1> 完全一致。"
           if has_ref else f"角色：{char.get('name')}，{char.get('gender')}，{char.get('age')}。外貌：{char.get('appearance')}。")
    return (f"角色脸部特写参考图，头肩构图，正脸平视镜头，双眼看向镜头，表情自然放松，五官清晰锐利，不遮挡面部。{who}"
            f"纯浅灰色干净背景，柔和均匀的影棚光。画风：{style_text(project)}。画面中只有这一个人，不要文字和水印。")


def char_ref_lines(chars: list[dict], ref_map: dict[str, dict], tag: str = "image") -> list[str]:
    """ref_map: {角色id: {"full": 序号, "face": 序号}}，生成「<imageN> 是谁的什么参考」说明。"""
    lines = []
    for c in chars:
        m = ref_map.get(c["id"]) or {}
        full, face = m.get("full"), m.get("face")
        body = f"{c['_view']}" if c.get("_view") else "全身"
        outfit = f"（造型：{c['_outfit']}）" if c.get("_outfit") else ""
        expr = f"，表情参考其「{c['_expr']}」特写" if c.get("_expr") else ""
        if full and face:
            lines.append(f"<{tag}{full}> 是角色「{c['name']}」的{body}设定图{outfit}（体型、发型、服装），<{tag}{face}> 是其脸部特写（五官{expr}）。"
                         f"画面中的「{c['name']}」必须与这两张图是同一个人：脸型五官以 <{tag}{face}> 为准，服装以 <{tag}{full}> 为准。")
        elif full or face:
            n = full or face
            lines.append(f"<{tag}{n}> 是角色「{c['name']}」的形象参考（{c.get('appearance') or ''}）。"
                         f"画面中的「{c['name']}」必须与 <{tag}{n}> 是同一个人：脸型五官、发型发色、服装完全一致。")
        else:
            lines.append(f"角色「{c['name']}」的外貌：{c.get('appearance') or ''}。")
    return lines


def prop_prompt(project: dict, prop: dict) -> str:
    return (f"道具设定参考图，单个物品居中完整展示，略俯视的产品摄影角度，纯浅灰色干净背景，柔和均匀的影棚光，细节清晰。"
            f"道具：{prop.get('name')}。{prop.get('description') or ''}。画风：{style_text(project)}。"
            f"除了道具本身的图案外，画面中没有人物，不要额外的文字和水印。")


def prop_ref_lines(props: list[tuple[dict, int | None]], tag: str = "image") -> list[str]:
    lines = []
    for prop, idx in props:
        desc = f"（{prop['description']}）" if prop.get("description") else ""
        if idx:
            lines.append(f"<{tag}{idx}> 是道具「{prop['name']}」的外观参考{desc}，画面中的{prop['name']}必须与它外观一致。")
        else:
            lines.append(f"道具「{prop['name']}」{desc or '按剧情描述绘制'}。")
    return lines


def voice_sample_prompt(project: dict, char: dict) -> tuple[str, str]:
    """用 H3 给角色「设计」一段干净的音色样本（之后 IndexTTS2 用它克隆，所有镜头同一个声音）。"""
    line = f"你好，我是{char.get('name')}。今天发生的事情，我一定要亲口告诉你。"
    voice = char.get("voice") or "自然的声音"
    prompt = (f"{style_text(project)}。脸部特写，{char.get('name')}（{char.get('gender') or ''}，{char.get('age') or ''}）正对镜头，"
              f"表情自然，嘴部清晰地开合说话。\n{char.get('name')}（{voice}）用中文清晰、平稳地说：\u201c{line}\u201d\n"
              f"声音：安静的录音室，只有这一个人的说话声，没有背景音乐、没有环境音、没有其他人声。不要出现字幕和文字。")
    return prompt, line


def location_prompt(project: dict, loc: dict) -> str:
    return (f"电影场景概念图，空镜，画面中没有任何人物。场景：{loc.get('name')}。{loc.get('description')}。"
            f"画风：{style_text(project)}。不要文字和水印。")


# ---------------- ③ 分镜 ----------------
STORYBOARD_SYSTEM = f"""你是分镜导演。把剧本拆成可以逐镜头生成画面和视频的分镜表。
规则：
1. 每个镜头只表达一个清晰的瞬间/动作，时长 3~10 秒；有台词的镜头按每秒约 4 个字估算并留 1 秒余量。
2. shot_size 只能是 {SHOT_SIZES}；camera_angle 只能是 {CAMERA_ANGLES}；camera_move 只能是 {CAMERA_MOVES}。
3. characters 只能填【角色表】里的名字，写出这个镜头画面里真正能看到的所有角色——包括前景虚化、背影、过肩、只露半身的角色也要写。
4. description 写这一镜头首帧的静态画面：每个角色的位置（画面左/中/右、前景/背景）、姿态、表情、朝向，环境与光线。多人时必须写清每个人的站位，不能含糊。
5. action 写镜头内发生的动作和变化（给视频模型用）。
6. dialogue 为本镜头的台词，speaker 必须是角色表中的名字；没有台词就给空数组。
7. location 填【场景表】里的场景名；props 填【道具表】里本镜头画面中能看到的道具名（没有就空数组）。
8. 景别要有变化，重要情绪用近景/特写，交代环境用全景/远景。
9. continuous：本镜头是否与上一个镜头「动作连续」——同一场景、同一时刻、人物动作直接接着上一镜继续（例如上一镜伸手、这一镜接过东西），中间没有时间跳跃和场景切换时为 true；换场景、跳时间、切到别的人物或景别跳变很大时为 false。第一个镜头为 false。
10. 严格遵守【时长预算】：所有镜头 duration 之和不超过预算的 110%；超了就合并相邻镜头或删掉次要的过场/空镜，保留关键情节和台词。
只输出 JSON：{{"shots":[{{"scene":"场景名","location":"","shot_size":"","camera_angle":"","camera_move":"","characters":[""],
"props":[""],"description":"","action":"","dialogue":[{{"speaker":"","line":"","emotion":""}}],"sound":"环境音/音效","duration":5,"continuous":false}}]}}"""


def storyboard_user(project: dict, chars: list[dict], locs: list[dict], chunk: str, prev: str,
                    props: list[dict] | None = None) -> str:
    roster = "\n".join(f"- {c['name']}（{c.get('role') or ''}）：{c.get('appearance') or ''}"
                       + (f"；说话方式：{c['speech']}" if c.get("speech") else "")
                       + (f"；性格：{c['personality']}" if c.get("personality") else "") for c in chars) or "（无）"
    places = "\n".join(f"- {l['name']}：{l.get('description') or ''}" for l in locs) or "（无）"
    if props:
        places += "\n\n【道具表】\n" + "\n".join(f"- {p['name']}：{p.get('description') or ''}" for p in props)
    tail = f"\n【上一段最后一个镜头】{prev}\n" if prev else ""
    total = int(project.get("target_seconds") or 60)
    script = project.get("script") or chunk
    budget = max(5, round(total * min(1.0, len(chunk) / max(1, len(script)))))
    shots = max(1, round(budget / 5))
    plan = (f"\n【时长预算】本段约 {budget} 秒（全片目标 {total} 秒），建议 {max(1, shots - 1)}~{shots + 1} 个镜头。\n")
    return f"【角色表】\n{roster}\n\n【场景表】\n{places}\n{tail}{plan}\n【本段剧本】\n{chunk}"


_VISIBLE_CUES = ("背影", "肩", "前景", "背景", "侧脸", "身影", "侧身", "坐", "站", "走", "画面左", "画面右", "画面中",
                 "进入", "身穿", "面部", "脸", "手")


def visible_characters(shot: dict, names: list[str]) -> list[str]:
    """LLM 常漏填过肩/前景背影里的角色 → 描述里以「可见」方式出现的角色名补进 characters（否则生图会把他画成别人）。"""
    out = [n for n in shot.get("characters") or [] if n in names]
    for clause in re.split(r"[，。；,;！!？?\n]", shot.get("description") or ""):
        for n in names:
            if n in out or n not in clause:
                continue
            rest = clause.split(n, 1)[1]
            if rest.startswith(("的笔迹", "的字", "的照片", "的名字", "的声音")):
                continue
            if any(c in clause for c in _VISIBLE_CUES):
                out.append(n)
    return out


def split_script(script: str, limit: int = 5000) -> list[str]:
    parts = re.split(r"(?m)^(?=#{1,3}\s|场景\s*\d|第.{1,4}场)", script)
    chunks, cur = [], ""
    for p in parts:
        if cur and len(cur) + len(p) > limit:
            chunks.append(cur)
            cur = ""
        cur += p
    if cur.strip():
        chunks.append(cur)
    return [c for c in chunks if c.strip()] or [script]


def _pick(value: Any, options: list[str], default: str) -> str:
    value = str(value or "").strip()
    for opt in options:
        if opt in value:
            return opt
    return default


def match_name(name: str, names: list[str]) -> str | None:
    name = str(name or "").strip()
    if not name:
        return None
    if name in names:
        return name
    for n in names:
        if n and (n in name or name in n):
            return n
    return None


def visible_props(shot: dict, names: list[str]) -> list[str]:
    """LLM 填的 props + 画面描述里提到的道具名。"""
    out = [n for n in (match_name(x, names) for x in shot.get("props") or []) if n]
    text = (shot.get("description") or "") + (shot.get("action") or "")
    out += [n for n in names if n and n in text and n not in out]
    return list(dict.fromkeys(out))


def make_storyboard_validator(char_names: list[str]):
    def validate(data: Any) -> list[dict]:
        rows = data.get("shots") if isinstance(data, dict) else data
        if not isinstance(rows, list) or not rows:
            raise ValueError("shots 必须是非空数组")
        out = []
        for i, r in enumerate(rows, 1):
            if not isinstance(r, dict) or not str(r.get("description") or "").strip():
                raise ValueError(f"第 {i} 个镜头缺少 description")
            chars = []
            for n in r.get("characters") or []:
                m = match_name(n, char_names)
                if m and m not in chars:
                    chars.append(m)
            lines = []
            for d in r.get("dialogue") or []:
                if isinstance(d, dict) and str(d.get("line") or "").strip():
                    sp = match_name(d.get("speaker"), char_names) or str(d.get("speaker") or "旁白")
                    lines.append({"speaker": sp, "line": str(d["line"]).strip(),
                                  "emotion": str(d.get("emotion") or "").strip()})
                    if sp in char_names and sp not in chars:
                        chars.append(sp)
            try:
                duration = float(r.get("duration") or 5)
            except (TypeError, ValueError):
                duration = 5.0
            spoken = sum(len(l["line"]) for l in lines)
            duration = max(duration, spoken / 4 + 1 if spoken else 0)
            out.append({
                "scene": str(r.get("scene") or "").strip(),
                "location": str(r.get("location") or "").strip(),
                "shot_size": _pick(r.get("shot_size"), SHOT_SIZES, "中景"),
                "camera_angle": _pick(r.get("camera_angle"), CAMERA_ANGLES, "平视"),
                "camera_move": _pick(r.get("camera_move"), CAMERA_MOVES, "固定"),
                "characters": chars,
                "props": [str(x).strip() for x in (r.get("props") or []) if str(x).strip()] if isinstance(r.get("props"), list) else [],
                "description": str(r["description"]).strip(),
                "action": str(r.get("action") or "").strip(),
                "dialogue": lines,
                "sound": str(r.get("sound") or "").strip(),
                "duration": round(min(max(duration, 2.0), 15.0), 1),
                "continuous": r.get("continuous") in (True, 1, "true", "True", "是") and bool(out),
            })
        return out
    return validate


# ---------------- ④ 分镜图 ----------------
def keyframe_prompt(project: dict, shot: dict, chars: list[dict], loc: dict | None,
                    ref_map: dict[str, dict], loc_idx: int | None,
                    props: list[tuple[dict, int | None]] = ()) -> str:
    """ref_map 见 char_ref_lines；loc_idx 为场景参考图的序号（None 表示没有）。"""
    lines = [f"生成一张电影分镜画面，{shot.get('shot_size') or '中景'}，{shot.get('camera_angle') or '平视'}。"]
    lines.append("画面内容：" + (shot.get("image_prompt") or shot.get("description") or "").strip())
    if (shot.get("performance") or "").strip():
        lines.append("人物表演（表情与视线）：" + shot["performance"].strip())
    lines += char_ref_lines(chars, ref_map)
    if loc:
        if loc_idx:
            lines.append(f"<image{loc_idx}> 是场景「{loc['name']}」的环境参考，保持空间布局、陈设和色调一致，但按本镜头的机位重新构图。")
        elif loc.get("description"):
            lines.append(f"场景：{loc['name']}，{loc['description']}。")
    lines += prop_ref_lines(list(props))
    if chars:
        names = "、".join(f"「{c['name']}」" for c in chars)
        lines.append(f"画面中共有 {len(chars)} 个主要人物：{names}，每人只出现一次，彼此外貌明显不同，不要出现多余人物。")
    else:
        lines.append("这是空镜头，画面中没有人物。")
    lines.append(f"画风：{style_text(project)}。")
    lines.append("不要出现任何文字、字幕、水印或分镜编号。")
    return "\n".join(lines)


# ---------------- ⑤ 视频 ----------------
def video_prompt(project: dict, shot: dict, chars_by_name: dict[str, dict], loc: dict | None,
                 ref_descs: list[str] | None = None) -> str:
    """ref_descs：参考生视频时 <Picture 2..> 的说明（<Picture 1> 固定是分镜图）。"""
    if (shot.get("video_prompt") or "").strip():
        body = shot["video_prompt"].strip()
    else:
        parts = [f"{style_text(project)}。"]
        if loc:
            parts.append(f"场景：{loc['name']}。")
        from .videox import camera_text
        parts.append(f"镜头：{shot.get('shot_size')}，{shot.get('camera_angle')}。运镜：{camera_text(shot)}。")
        parts.append("画面：" + (shot.get("description") or ""))
        if shot.get("action"):
            parts.append("动作：" + shot["action"])
        if (shot.get("performance") or "").strip():
            parts.append("表演：" + shot["performance"].strip())
        dub = bool(project.get("dub_mode"))
        for d in shot.get("dialogue") or []:
            c = chars_by_name.get(d.get("speaker") or "") or {}
            if dub:   # 配音模式：画面里只做说话的动作，声音之后用固定音色配，避免 H3 每镜换一个声音
                if c:
                    parts.append(f"{d['speaker']}在说话{('，' + d['emotion']) if d.get('emotion') else ''}，嘴部自然开合。")
                continue
            voice = "，".join(x for x in (c.get("voice"), d.get("emotion")) if x)
            parts.append(f"{d['speaker']}（{voice or '自然的声音'}）用中文说：\u201c{d['line']}\u201d")
        if shot.get("sound"):
            parts.append("声音：" + shot["sound"])
        if dub:
            parts.append("音频只有环境音和音效，没有任何人声、对白、旁白和背景音乐。")
        if shot.get("_chain"):
            parts.append("本镜头紧接上一个镜头：开头画面就是上一镜头的最后一帧，人物位置、姿态和动作从那里自然延续，不要跳变。")
        if shot.get("_end_frame"):
            parts.append("镜头结束时画面到达指定的尾帧状态。")
        if not dub and any((d.get("line") or "").strip() for d in shot.get("dialogue") or []):
            parts.insert(0, "画面中不出现任何字幕或台词文字，台词只出现在声音里。")
        parts.append("单一连续镜头，一镜到底，中间不切换画面、不转场。人物外貌、服装和场景与首帧保持一致，动作自然连贯。"
                     "画面干净，没有字幕、文字和水印。")
        body = "\n".join(parts)
    if ref_descs:
        head = ["<Picture 1> 是本镜头的构图与画面参考。"]
        for i, desc in enumerate(ref_descs, start=2):
            head.append(f"<Picture {i}> 是{desc}。")
        body = "\n".join(head) + "\n" + body
    return body


def dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)
