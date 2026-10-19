/* AI Studio v3 前端：原生 JS 单页应用。所有生成操作都是后台任务，页面轮询任务状态并自动刷新。 */
const $ = (s, el = document) => el.querySelector(s);
const S = { meta: null, snap: null, pid: null, step: 'script', active: [], activeIds: new Set(), pending: false,
  ff: null, bgm: '', drawer: false, view: 'home' };
const STEPS = [
  ['script', '剧本'], ['cast', '角色与场景'], ['board', '分镜'], ['video', '视频'], ['final', '成片'],
];
const esc = (v) => String(v ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
const media = (p) => (p ? '/media/' + p.split('/').map(encodeURIComponent).join('/') : '');
const isVideo = (p) => /\.(mp4|webm|mov|mkv)$/i.test(p || '');

async function api(method, url, body) {
  const opt = { method, headers: {} };
  if (body instanceof FormData) opt.body = body;
  else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
  const r = await fetch(url, opt);
  const text = await r.text();
  let data = null;
  try { data = text ? JSON.parse(text) : null; } catch { data = text; }
  if (!r.ok) {
    const msg = (data && data.detail) ? (typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail)) : `请求失败 ${r.status}`;
    toast(msg, true);
    throw new Error(msg);
  }
  return data;
}
const upload = (url, file) => { const fd = new FormData(); fd.append('file', file); return api('POST', url, fd); };
function pickFile(accept) {
  return new Promise((res) => {
    const i = document.createElement('input'); i.type = 'file'; i.accept = accept;
    i.onchange = () => res(i.files[0] || null); i.click();
  });
}
let toastTimer;
function toast(msg, err = false) {
  const t = $('#toast'); t.textContent = msg; t.className = 'toast' + (err ? ' err' : '');
  clearTimeout(toastTimer); toastTimer = setTimeout(() => t.classList.add('hidden'), err ? 6000 : 2500);
}
function modal(html) { $('#modal-box').innerHTML = html; $('#modal').classList.remove('hidden'); }
function closeModal() { $('#modal').classList.add('hidden'); $('#modal-box').innerHTML = ''; }
function lightbox(src) {
  const lb = $('#lightbox');
  lb.innerHTML = isVideo(src) ? `<video src="${src}" controls autoplay></video>` : `<img src="${src}">`;
  lb.classList.remove('hidden');
}
$('#lightbox').onclick = (e) => { if (e.target.tagName !== 'VIDEO') { $('#lightbox').classList.add('hidden'); $('#lightbox').innerHTML = ''; } };
$('#modal').onclick = (e) => { if (e.target.id === 'modal') closeModal(); };

/* ---------------- 路由 ---------------- */
async function route() {
  const h = location.hash || '#/';
  document.querySelectorAll('nav a').forEach((a) => a.classList.remove('active'));
  if (!S.meta) S.meta = await api('GET', '/api/meta');
  let m;
  if ((m = h.match(/^#\/p\/([^/]+)(?:\/(\w+))?/))) {
    S.view = 'project'; S.pid = m[1]; S.step = m[2] || 'script';
    await loadSnap(true);
  } else if (h.startsWith('#/toolbox')) {
    S.view = 'toolbox'; S.pid = null; $('[data-nav=toolbox]').classList.add('active'); await renderToolbox();
  } else if (h.startsWith('#/system')) {
    S.view = 'system'; S.pid = null; $('[data-nav=system]').classList.add('active'); await renderSystem();
  } else {
    S.view = 'home'; S.pid = null; $('[data-nav=home]').classList.add('active'); await renderHome();
  }
}
window.addEventListener('hashchange', route);
document.addEventListener('click', (e) => {
  const g = e.target.closest('[data-go]'); const a = e.target.closest('[data-act]');
  if (g && !(a && g.contains(a) && a !== g)) location.hash = g.dataset.go;
});

/* ---------------- 首页 ---------------- */
async function renderHome() {
  const list = await api('GET', '/api/projects');
  const cards = list.map((p) => `
    <div class="card click" data-go="#/p/${p.id}/${p.videos ? 'video' : p.shots ? 'board' : 'script'}">
      <div class="cover" style="${p.cover ? `background-image:url('${media(p.cover)}')` : ''}">${p.cover ? '' : '🎞️'}</div>
      <div class="card-body">
        <div class="card-title">${esc(p.title)}</div>
        <div class="muted small">${esc(S.meta.styles[p.style] || p.style)} · ${esc(p.aspect_ratio)}${p.final_video ? ' · <span class="ok">已出成片</span>' : ''}</div>
        <div class="stats"><span>镜头 ${p.shots}</span><span>分镜图 ${p.keyframes}/${p.shots}</span><span>视频 ${p.videos}/${p.shots}</span></div>
        <div class="row between" style="margin-top:8px"><span class="muted small">${new Date(p.updated_at * 1000).toLocaleString()}</span>
        <button class="btn small danger" data-act="del-project" data-id="${p.id}">删除</button></div>
      </div>
    </div>`).join('');
  $('#view').innerHTML = `<div class="page"><h2>我的项目</h2><div class="cards">
    <div class="card click new-card" data-act="new-project"><b>＋</b>新建项目<span class="small">从创意 / 小说 / 剧本开始</span></div>${cards}</div></div>`;
}

function newProjectModal() {
  const styles = Object.entries(S.meta.styles).map(([k, v], i) => `<div class="style-opt ${i === 0 ? 'on' : ''}" data-style="${k}">${esc(v)}</div>`).join('');
  modal(`<h3>新建项目</h3>
    <label class="f">项目名称</label><input type="text" id="np-title" placeholder="可留空，生成剧本后自动命名">
    <label class="f">从哪里开始</label>
    <div class="chips" id="np-src"><span class="chip on" data-v="idea">一句话创意</span><span class="chip" data-v="novel">小说原文</span><span class="chip" data-v="script">已有剧本</span></div>
    <label class="f">素材内容</label><textarea id="np-text" rows="7" placeholder="例如：外卖小哥意外捡到一部能预知未来 10 秒的手机……"></textarea>
    <div class="grid3"><div><label class="f">画幅</label><select id="np-aspect">${S.meta.aspects.map((a) => `<option>${a}</option>`).join('')}</select></div>
    <div><label class="f">目标时长（秒）</label><input type="number" id="np-sec" value="60" min="10" max="600"></div>
    <div><label class="f">&nbsp;</label><label class="row small"><input type="checkbox" id="np-auto" checked> 创建后自动生成剧本</label></div></div>
    <label class="f">画面风格</label><div class="style-grid" id="np-style">${styles}</div>
    <label class="f">风格补充（可选）</label><input type="text" id="np-note" placeholder="例如：冷色调、雨夜、王家卫式色彩">
    <div class="row" style="justify-content:flex-end;margin-top:16px"><button class="btn ghost" data-act="close-modal">取消</button><button class="btn" data-act="create-project">创建</button></div>`);
  $('#np-src').onclick = (e) => { const c = e.target.closest('.chip'); if (!c) return; $('#np-src').querySelectorAll('.chip').forEach((x) => x.classList.toggle('on', x === c)); };
  $('#np-style').onclick = (e) => { const c = e.target.closest('.style-opt'); if (!c) return; $('#np-style').querySelectorAll('.style-opt').forEach((x) => x.classList.toggle('on', x === c)); };
}

/* ---------------- 项目工作台 ---------------- */
async function loadSnap(force = false) {
  if (!S.pid) return;
  try { S.snap = await api('GET', `/api/projects/${S.pid}`); } catch { location.hash = '#/'; return; }
  const ae = document.activeElement;
  if (!force && ae && $('#view').contains(ae) && /INPUT|TEXTAREA|SELECT/.test(ae.tagName) && ae.type !== 'checkbox') { S.pending = true; return; }
  S.pending = false;
  renderProject();
}
document.addEventListener('focusout', () => setTimeout(() => {
  const ae = document.activeElement;
  if (S.pending && !(ae && /INPUT|TEXTAREA|SELECT/.test(ae.tagName))) { S.pending = false; renderProject(); }
}, 50));

const charMap = () => Object.fromEntries(S.snap.characters.map((c) => [c.id, c]));
const assetsOf = (id, kind) => (S.snap.assets[id] || []).filter((a) => !kind || a.kind === kind);
const busyOf = (id) => S.active.find((t) => t.owner_id === id || t.owner_id === `${S.pid}:${id}`);
function busyBox(id) { return `<span class="busy-slot" data-owner="${esc(id)}">${busyInner(id)}</span>`; }
function busyInner(id) {
  const t = busyOf(id); if (!t) return '';
  return `<div class="busy"><div>${t.status === 'queued' ? '排队中…' : esc(t.message || '生成中…')}</div><div class="bar" style="width:70%"><i style="width:${t.progress}%"></i></div></div>`;
}
function stepDone(k) {
  const s = S.snap; const shots = s.shots;
  return { script: !!s.project.script, cast: s.characters.length > 0 && s.characters.every((c) => c.ref_image),
    board: shots.length > 0 && shots.every((x) => x.keyframe), video: shots.length > 0 && shots.every((x) => x.video),
    final: !!s.project.final_video }[k];
}
function stepMeta(k) {
  const s = S.snap; const n = s.shots.length;
  return { script: s.project.script ? `${s.project.script.length}字` : '', cast: `${s.characters.length}人/${s.locations.length}景`,
    board: n ? `${s.shots.filter((x) => x.keyframe).length}/${n}` : '', video: n ? `${s.shots.filter((x) => x.video).length}/${n}` : '', final: '' }[k];
}

function renderProject() {
  if (S.view !== 'project' || !S.snap) return;
  const y = window.scrollY; const c = $('.content'); const cy = c ? c.scrollTop : 0;
  const p = S.snap.project;
  const steps = STEPS.map(([k, label], i) => `<div class="step ${S.step === k ? 'active' : ''} ${stepDone(k) ? 'done' : ''}" data-go="#/p/${p.id}/${k}">
    <span class="num">${stepDone(k) ? '✓' : i + 1}</span>${label}<span class="meta">${stepMeta(k)}</span></div>`).join('');
  const body = { script: viewScript, cast: viewCast, board: viewBoard, video: viewVideo, final: viewFinal }[S.step] || viewScript;
  $('#view').innerHTML = `<div class="ws"><div class="steps"><div class="ptitle">${esc(p.title)}<div class="muted small">${esc(S.meta.styles[p.style] || '')} · ${p.aspect_ratio}</div></div>${steps}</div>
    <div class="content">${body()}</div></div>`;
  window.scrollTo(0, y); const c2 = $('.content'); if (c2) c2.scrollTop = cy;
}
const sel = (opts, cur, attrs) => `<select ${attrs}>${opts.map((o) => { const [v, l] = Array.isArray(o) ? o : [o, o]; return `<option value="${esc(v)}" ${String(v) === String(cur) ? 'selected' : ''}>${esc(l)}</option>`; }).join('')}</select>`;

function viewScript() {
  const p = S.snap.project; const busy = busyOf(`${p.id}:script`) || busyOf(p.id + ':script');
  return `<div class="toolbar"><span class="title">① 剧本</span>
      <button class="btn" data-act="gen-script" ${busy ? 'disabled' : ''}>${busy ? '剧本生成中…' : (p.script ? '重新生成剧本' : 'AI 生成剧本')}</button>
      <button class="btn ghost" data-act="extract" ${p.script ? '' : 'disabled'}>下一步：提取角色与场景 →</button></div>
    <div class="two">
      <div class="panel"><h3>素材与设定</h3>
        <label class="f">项目名称</label><input type="text" data-save="projects:${p.id}:title" value="${esc(p.title)}">
        <label class="f">素材类型</label>${sel([['idea', '一句话创意'], ['novel', '小说原文'], ['script', '已有剧本']], p.source_type, `data-save="projects:${p.id}:source_type"`)}
        <label class="f">素材内容</label><textarea rows="12" data-save="projects:${p.id}:source_text">${esc(p.source_text)}</textarea>
        <div class="grid3"><div><label class="f">画幅</label>${sel(S.meta.aspects, p.aspect_ratio, `data-save="projects:${p.id}:aspect_ratio" data-rerender="1"`)}</div>
        <div><label class="f">风格</label>${sel(Object.entries(S.meta.styles), p.style, `data-save="projects:${p.id}:style" data-rerender="1"`)}</div>
        <div><label class="f">目标时长（秒）</label><input type="number" data-save="projects:${p.id}:target_seconds" data-num="1" value="${p.target_seconds}"></div></div>
        <label class="f">风格补充</label><input type="text" data-save="projects:${p.id}:style_note" value="${esc(p.style_note)}" placeholder="会附加到所有画面提示词中">
      </div>
      <div class="panel"><h3>剧本 <span class="muted small">（可直接编辑，失焦自动保存）</span></h3>
        <label class="f">一句话梗概</label><input type="text" data-save="projects:${p.id}:synopsis" value="${esc(p.synopsis)}">
        <label class="f">剧本正文</label><textarea class="script-box" data-save="projects:${p.id}:script" placeholder="点击「AI 生成剧本」，或直接粘贴剧本。格式：## 场景1：地点 · 时间 / △画面动作 / 角色：（情绪）台词">${esc(p.script)}</textarea>
      </div></div>`;
}

function thumbs(owner, kind, current) {
  return `<div class="thumbs">${assetsOf(owner, kind).map((a) => `<div class="thumb ${a.path === current ? 'sel' : ''}" title="点击设为当前使用" data-act="select-asset" data-id="${a.id}"
    style="${isVideo(a.path) ? '' : `background-image:url('${media(a.path)}')`}">${isVideo(a.path) ? `<video src="${media(a.path)}#t=0.5" muted preload="metadata"></video>` : ''}<span class="x" data-act="del-asset" data-id="${a.id}">✕</span></div>`).join('')}</div>`;
}

function faceBox(c) {
  const cur = assetsOf(c.id, 'face').find((a) => a.path === c.face_image);
  const stale = cur && cur.meta && cur.meta.from_ref && c.ref_image && cur.meta.from_ref !== c.ref_image;
  return `<label class="f" style="margin-top:10px">脸部特写 <span class="muted small">锁五官</span></label>
    <div class="ref face" data-act="zoom" data-src="${media(c.face_image)}" style="${c.face_image ? `background-image:url('${media(c.face_image)}')` : ''}">${c.face_image ? '' : '暂无特写'}</div>
    ${stale ? '<div class="small warn" style="margin-top:4px">⚠ 定妆照已更换，建议重新生成</div>' : ''}
    ${thumbs(c.id, 'face', c.face_image)}
    <div class="row" style="margin-top:6px"><button class="btn small" data-act="gen-face" data-id="${c.id}" title="以当前定妆照为参考生成同一人的脸部特写">${c.face_image ? '重新生成' : '生成'}</button>
      <button class="btn small ghost" data-act="upload-face" data-id="${c.id}">上传</button></div>`;
}

function viewCast() {
  const s = S.snap; const busy = busyOf(`${s.project.id}:extract`);
  const chars = s.characters.map((c) => `<div class="card entity">
    <div><div class="ref" data-act="zoom" data-src="${media(c.ref_image)}" style="${c.ref_image ? `background-image:url('${media(c.ref_image)}')` : ''};position:relative">${c.ref_image ? '' : '暂无参考图'}${busyBox(c.id)}</div>
      ${thumbs(c.id, 'portrait', c.ref_image)}
      <div class="row" style="margin-top:6px"><select id="pc-${c.id}" style="width:56px">${[1, 2, 4].map((n) => `<option ${n === 2 ? 'selected' : ''}>${n}</option>`).join('')}</select>
      <button class="btn small" data-act="gen-portrait" data-id="${c.id}">生成</button></div>
      <button class="btn small ghost" style="margin-top:6px;width:100%" data-act="upload-portrait" data-id="${c.id}">上传参考图</button>
      ${faceBox(c)}</div>
    <div><div class="row"><input type="text" class="grow" data-save="characters:${c.id}:name" value="${esc(c.name)}" style="font-weight:600">
      <span class="chip ${c.locked ? 'on' : ''}" title="锁定后重新提取不会覆盖" data-act="toggle-lock" data-id="${c.id}">${c.locked ? '🔒 已锁定' : '🔓 锁定'}</span></div>
      <div class="grid3"><div><label class="f">定位</label><input type="text" data-save="characters:${c.id}:role" value="${esc(c.role)}"></div>
      <div><label class="f">性别</label><input type="text" data-save="characters:${c.id}:gender" value="${esc(c.gender)}"></div>
      <div><label class="f">年龄</label><input type="text" data-save="characters:${c.id}:age" value="${esc(c.age)}"></div></div>
      <label class="f">外貌与服装（出图核心）</label><textarea rows="4" data-save="characters:${c.id}:appearance">${esc(c.appearance)}</textarea>
      <label class="f">声音（视频配音）</label><input type="text" data-save="characters:${c.id}:voice" value="${esc(c.voice)}">
      <div class="row" style="justify-content:flex-end;margin-top:8px"><button class="btn small danger" data-act="del-char" data-id="${c.id}">删除角色</button></div></div></div>`).join('');
  const locs = s.locations.map((l) => `<div class="card entity">
    <div><div class="ref wide" data-act="zoom" data-src="${media(l.ref_image)}" style="${l.ref_image ? `background-image:url('${media(l.ref_image)}')` : ''};position:relative">${l.ref_image ? '' : '暂无场景图'}${busyBox(l.id)}</div>
      ${thumbs(l.id, 'location', l.ref_image)}
      <div class="row" style="margin-top:6px"><button class="btn small" data-act="gen-location" data-id="${l.id}">生成</button><button class="btn small ghost" data-act="upload-location" data-id="${l.id}">上传</button></div></div>
    <div><input type="text" data-save="locations:${l.id}:name" value="${esc(l.name)}" style="font-weight:600">
      <label class="f">场景描述</label><textarea rows="5" data-save="locations:${l.id}:description">${esc(l.description)}</textarea>
      <div class="row" style="justify-content:flex-end;margin-top:8px"><button class="btn small danger" data-act="del-loc" data-id="${l.id}">删除场景</button></div></div></div>`).join('');
  return `<div class="toolbar"><span class="title">② 角色与场景</span>
      <button class="btn" data-act="extract" ${busy ? 'disabled' : ''}>${busy ? '提取中…' : '从剧本提取'}</button>
      <button class="btn ghost" data-act="batch-portraits">批量生成参考图（仅缺失）</button>
      <button class="btn ghost" data-act="add-char">＋ 角色</button><button class="btn ghost" data-act="add-loc">＋ 场景</button>
      <button class="btn ghost" data-go="#/p/${s.project.id}/board">下一步：分镜 →</button></div>
    <p class="muted small">角色一致性靠两张图：<b>定妆照</b>（全身，锁服装体型）+ <b>脸部特写</b>（由定妆照派生，锁五官）。生成分镜图时，镜头里每个角色的这两张图（以及场景图）会一起交给 Qwen-Image 2.1 做多图参考；视频若有脸崩，可在④用「人脸修正」按特写一键换回。</p>
    <h3>角色（${s.characters.length}）</h3>${chars ? `<div class="entity-grid">${chars}</div>` : '<div class="empty">还没有角色，点击「从剧本提取」</div>'}
    <h3 style="margin-top:22px">场景（${s.locations.length}）</h3>${locs ? `<div class="entity-grid">${locs}</div>` : '<div class="empty">还没有场景</div>'}`;
}

function frameClass() { const a = S.snap.project.aspect_ratio; return a === '9:16' ? 'portrait' : (a === '1:1' || a === '3:4') ? 'square' : ''; }

function viewBoard() {
  const s = S.snap; const busy = busyOf(`${s.project.id}:storyboard`); const cm = charMap();
  const total = s.shots.reduce((a, x) => a + (x.duration || 0), 0);
  const speakers = [['旁白', '旁白'], ...s.characters.map((c) => [c.name, c.name])];
  const shots = s.shots.map((x, i) => `<div class="card shot" id="shot-${x.id}">
    <div class="idx">${i + 1}<div style="margin-top:8px"><button class="btn small ghost" data-act="move" data-id="${x.id}" data-d="-1">↑</button></div>
      <div style="margin-top:4px"><button class="btn small ghost" data-act="move" data-id="${x.id}" data-d="1">↓</button></div></div>
    <div><div class="frame ${frameClass()}" data-act="zoom" data-src="${media(x.keyframe)}" style="${x.keyframe ? `background-image:url('${media(x.keyframe)}')` : ''}">${x.keyframe ? '' : '暂无分镜图'}${busyBox(x.id)}</div>
      ${thumbs(x.id, 'keyframe', x.keyframe)}
      <div class="row" style="margin-top:8px"><select id="kc-${x.id}" style="width:60px">${[1, 2, 4].map((n) => `<option value="${n}">${n}张</option>`).join('')}</select>
        <button class="btn small" data-act="gen-keyframe" data-id="${x.id}">出图</button>
        <button class="btn small ghost" data-act="edit-keyframe" data-id="${x.id}" ${x.keyframe ? '' : 'disabled'}>改图</button>
        <button class="btn small ghost" data-act="upload-shot" data-id="${x.id}">上传</button>
        <button class="btn small ghost" data-act="plan" data-id="${x.id}">提示词</button>
        <button class="btn small ghost" data-act="face-fix" data-id="${x.id}" data-target="keyframe" ${x.keyframe && x.character_ids.length ? '' : 'disabled'} title="用角色脸部特写修正分镜图里的人脸">人脸修正</button></div></div>
    <div>
      <div class="grid4">
        <div><label class="f">景别</label>${sel(S.meta.shot_sizes, x.shot_size, `data-save="shots:${x.id}:shot_size"`)}</div>
        <div><label class="f">角度</label>${sel(S.meta.camera_angles, x.camera_angle, `data-save="shots:${x.id}:camera_angle"`)}</div>
        <div><label class="f">运镜</label>${sel(S.meta.camera_moves, x.camera_move, `data-save="shots:${x.id}:camera_move"`)}</div>
        <div><label class="f">时长（秒）</label><input type="number" step="0.5" min="1" max="20" data-num="1" data-save="shots:${x.id}:duration" value="${x.duration}"></div></div>
      <div class="row" style="margin-top:8px"><div style="width:200px"><label class="f">场景</label>${sel([['', '（未指定）'], ...s.locations.map((l) => [l.id, l.name])], x.location_id, `data-save="shots:${x.id}:location_id"`)}</div>
        <div class="grow"><label class="f">出镜角色（点击切换，顺序即参考图顺序）</label><div class="chips">${s.characters.map((c) => `<span class="chip ${x.character_ids.includes(c.id) ? 'on' : ''}" data-act="toggle-char" data-id="${x.id}" data-c="${c.id}">${esc(c.name)}${c.ref_image ? '' : ' ⚠'}</span>`).join('') || '<span class="muted small">先在②添加角色</span>'}</div></div></div>
      <label class="f">画面描述（首帧：站位、姿态、表情、环境、光线）</label><textarea rows="3" data-save="shots:${x.id}:description">${esc(x.description)}</textarea>
      <label class="f">镜头内动作（给视频模型）</label><textarea rows="2" data-save="shots:${x.id}:action">${esc(x.action)}</textarea>
      <label class="f">台词</label>${(x.dialogue || []).map((d, j) => `<div class="dlg-row">${sel(speakers.some((sp) => sp[0] === d.speaker) ? speakers : [[d.speaker, d.speaker], ...speakers], d.speaker, `data-dlg="${x.id}:${j}:speaker"`)}
        <input type="text" data-dlg="${x.id}:${j}:line" value="${esc(d.line)}" placeholder="台词"><input type="text" data-dlg="${x.id}:${j}:emotion" value="${esc(d.emotion)}" placeholder="情绪">
        <button class="btn small ghost" data-act="del-line" data-id="${x.id}" data-j="${j}">✕</button></div>`).join('')}
      <div class="row"><button class="btn small ghost" data-act="add-line" data-id="${x.id}">＋ 台词</button>
        <input type="text" class="grow" data-save="shots:${x.id}:sound" value="${esc(x.sound)}" placeholder="环境音 / 音效"></div>
      <details style="margin-top:8px"><summary class="muted small">高级：自定义出图画面描述（留空则用上面的画面描述）</summary>
        <textarea rows="3" data-save="shots:${x.id}:image_prompt">${esc(x.image_prompt)}</textarea></details>
      <div class="row" style="justify-content:flex-end;margin-top:8px"><button class="btn small ghost" data-act="add-shot" data-id="${x.id}">在后面插入镜头</button>
        <button class="btn small danger" data-act="del-shot" data-id="${x.id}">删除</button></div>
    </div></div>`).join('');
  return `<div class="toolbar"><span class="title">③ 分镜 <span class="muted small">${s.shots.length} 个镜头 · 约 ${total.toFixed(0)} 秒</span></span>
      <button class="btn" data-act="gen-board" ${busy ? 'disabled' : ''}>${busy ? '分镜生成中…' : (s.shots.length ? '重新生成分镜表' : 'AI 生成分镜表')}</button>
      <select id="batch-kc" style="width:80px"><option value="1">每镜1张</option><option value="2">每镜2张</option><option value="4">每镜4张</option></select>
      <button class="btn ghost" data-act="batch-keyframes" ${s.shots.length ? '' : 'disabled'}>批量出分镜图（仅缺失）</button>
      <button class="btn ghost" data-act="add-shot">＋ 镜头</button>
      <button class="btn ghost" data-go="#/p/${s.project.id}/video">下一步：视频 →</button></div>
    ${shots || '<div class="empty">还没有分镜。先完成①剧本和②角色，再点击「AI 生成分镜表」</div>'}`;
}

function viewVideo() {
  const s = S.snap; const r2v = S.meta.r2v;
  const modes = [['auto', '自动'], ['i2v', '图生视频（分镜图作首帧）'], ['r2v', r2v ? '参考生视频（分镜图+角色图）' : '参考生视频（未安装）']];
  const cards = s.shots.map((x, i) => `<div class="card vcard">
    <div class="row between"><b>#${i + 1} <span class="muted small">${esc(x.shot_size)} · ${esc(x.camera_move)} · ${x.duration}s</span></b>
      <span class="small ${x.video ? 'ok' : 'muted'}">${x.video ? '✓ 已生成' : '未生成'}</span></div>
    <div class="frame ${frameClass()}" style="margin-top:8px;${!x.video && x.keyframe ? `background-image:url('${media(x.keyframe)}')` : ''}">
      ${x.video ? `<video src="${media(x.video)}" controls preload="metadata"></video>` : (x.keyframe ? '' : '<span>无分镜图（将纯文生视频）</span>')}${busyBox(x.id)}</div>
    ${thumbs(x.id, 'video', x.video)}
    <div class="muted small" style="margin-top:6px;max-height:42px;overflow:hidden">${esc(x.description)}</div>
    <div class="grid3" style="margin-top:8px">${sel(modes, 'auto', `id="vm-${x.id}"`)}${sel([['draft', '草稿 480p'], ['final', '成品 768p']], 'draft', `id="vq-${x.id}"`)}
      <label class="row small"><input type="checkbox" id="vt-${x.id}" checked> 加速(Turbo)</label></div>
    <details style="margin-top:6px"><summary class="muted small">自定义视频提示词（留空自动生成）</summary><textarea rows="4" data-save="shots:${x.id}:video_prompt">${esc(x.video_prompt)}</textarea></details>
    <div class="row" style="margin-top:8px"><button class="btn small" data-act="gen-video" data-id="${x.id}">${x.video ? '重新生成' : '生成视频'}</button>
      <button class="btn small ghost" data-act="plan" data-id="${x.id}">提示词</button>
      <button class="btn small ghost" data-act="face-fix" data-id="${x.id}" data-target="video" ${x.video && x.character_ids.length ? '' : 'disabled'} title="用角色脸部特写对视频换脸，修正走样的人脸">人脸修正</button>
      <button class="btn small ghost" data-act="ff-enhance" data-id="${x.id}" ${x.video ? '' : 'disabled'} title="只做面部清晰度增强，不换脸">修脸</button>
      <button class="btn small ghost" data-act="upload-shot" data-id="${x.id}">上传</button></div></div>`).join('');
  return `<div class="toolbar"><span class="title">④ 视频 <span class="muted small">MiniMax H3 · 自带对白配音与音效</span></span>
      ${sel([['draft', '草稿 480p（快）'], ['final', '成品 768p']], 'draft', 'id="bv-q"')}
      ${sel(modes, 'auto', 'id="bv-m" style="width:200px"')}
      <label class="row small"><input type="checkbox" id="bv-t" checked> Turbo</label>
      <button class="btn" data-act="batch-videos" ${s.shots.length ? '' : 'disabled'}>批量生成（仅缺失）</button>
      <button class="btn ghost" data-go="#/p/${s.project.id}/final">下一步：成片 →</button></div>
    <p class="muted small">建议先用「草稿 + Turbo」快速过一遍节奏，满意后再对关键镜头用「成品」重生成。台词会按②里的角色声音描述由 H3 直接配音。</p>
    ${cards ? `<div class="vgrid">${cards}</div>` : '<div class="empty">还没有分镜</div>'}`;
}

function viewFinal() {
  const s = S.snap; const p = s.project; const busy = busyOf(`${p.id}:assemble`);
  const ok = s.shots.filter((x) => x.video).length;
  const tl = s.shots.map((x, i) => `<div class="tl-item ${x.video ? '' : 'miss'}"><div class="t" style="${x.keyframe ? `background-image:url('${media(x.keyframe)}')` : ''}">${x.keyframe ? '' : '#' + (i + 1)}</div>
    <div class="small ${x.video ? '' : 'bad'}">#${i + 1} · ${x.duration}s ${x.video ? '' : '缺视频'}</div></div>`).join('');
  const finals = assetsOf(p.id, 'final');
  return `<div class="toolbar"><span class="title">⑤ 成片</span><span class="muted small">可用镜头 ${ok}/${s.shots.length}（缺视频的镜头会被跳过）</span></div>
    <div class="panel"><h3>时间线</h3><div class="timeline">${tl || '<span class="muted">暂无镜头</span>'}</div>
      <div class="grid4" style="margin-top:10px">
        <label class="row"><input type="checkbox" id="as-sub" checked> 烧录中文字幕</label>
        <div>${sel([['final', '输出 768p'], ['draft', '输出 480p']], 'final', 'id="as-q"')}</div>
        <div class="row"><button class="btn small ghost" data-act="upload-bgm">${S.bgm ? '✓ 已选 BGM（更换）' : '上传背景音乐'}</button>${S.bgm ? '<button class="btn small ghost" data-act="clear-bgm">✕</button>' : ''}</div>
        <div class="row small">BGM 音量 <input type="range" id="as-vol" min="0" max="1" step="0.05" value="0.25" class="grow"></div></div>
      <div class="row" style="margin-top:12px"><button class="btn" data-act="assemble" ${ok && !busy ? '' : 'disabled'}>${busy ? '合成中… ' + busy.progress + '%' : '合成成片'}</button>
        <a class="btn ghost" href="${media(p.id + '/final/subtitles.srt')}" download>下载字幕 SRT</a></div></div>
    ${p.final_video ? `<div class="panel final" style="margin-top:16px"><div class="row between"><h3>当前成片</h3><a class="btn small" href="${media(p.final_video)}" download>下载 MP4</a></div>
      <video src="${media(p.final_video)}" controls></video></div>` : ''}
    ${finals.length ? `<div class="panel" style="margin-top:16px"><h3>历史版本</h3><table><tr><th>时间</th><th>镜头</th><th>时长</th><th></th></tr>
      ${finals.map((a) => `<tr><td>${new Date(a.created_at * 1000).toLocaleString()}</td><td>${a.meta.shots || ''}</td><td>${a.meta.seconds || ''}s</td>
      <td class="row"><button class="btn small ghost" data-act="select-asset" data-id="${a.id}">设为当前</button><a class="btn small ghost" href="${media(a.path)}" download>下载</a>
      <button class="btn small danger" data-act="del-asset" data-id="${a.id}">删除</button></td></tr>`).join('')}</table></div>` : ''}`;
}

/* ---------------- 字段保存 ---------------- */
const TABLE_URL = { projects: '/api/projects/', characters: '/api/characters/', locations: '/api/locations/', shots: '/api/shots/' };
function localRow(table, id) {
  if (!S.snap) return null;
  if (table === 'projects') return S.snap.project;
  return (S.snap[table] || []).find((r) => r.id === id);
}
document.addEventListener('change', async (e) => {
  const el = e.target;
  if (el.dataset.save) {
    const [table, id, field] = el.dataset.save.split(':');
    let value = el.value;
    if (el.dataset.num) value = Number(value);
    const row = localRow(table, id);
    if (row && row[field] === value) return;
    const saved = await api('PATCH', TABLE_URL[table] + id, { [field]: value });
    if (row) Object.assign(row, saved || {});
    if (el.dataset.rerender) await loadSnap(true);
    toast('已保存');
  } else if (el.dataset.dlg) {
    const [id, j, key] = el.dataset.dlg.split(':');
    const shot = localRow('shots', id);
    const dialogue = (shot.dialogue || []).map((d) => ({ ...d }));
    dialogue[Number(j)][key] = el.value;
    const saved = await api('PATCH', '/api/shots/' + id, { dialogue });
    Object.assign(shot, saved);
    toast('已保存');
  }
});

/* ---------------- 动作 ---------------- */
const val = (id) => { const el = document.getElementById(id); return el ? (el.type === 'checkbox' ? el.checked : el.value) : undefined; };
async function submitted(task, msg = '已加入任务队列') { toast(msg); await pollTasks(); await loadSnap(true); return task; }

const ACTIONS = {
  'close-modal': closeModal,
  'new-project': newProjectModal,
  async 'create-project'() {
    const body = { title: val('np-title') || '未命名项目', source_type: $('#np-src .chip.on').dataset.v, source_text: val('np-text'),
      aspect_ratio: val('np-aspect'), style: $('#np-style .style-opt.on').dataset.style, style_note: val('np-note'), target_seconds: Number(val('np-sec')) || 60 };
    const p = await api('POST', '/api/projects', body);
    const auto = val('np-auto') && body.source_text.trim();
    closeModal();
    if (auto) await api('POST', `/api/projects/${p.id}/script/generate`);
    location.hash = `#/p/${p.id}/script`;
  },
  async 'del-project'(el, e) {
    e.stopPropagation();
    if (!confirm('删除项目会同时删除所有图片和视频，确定吗？')) return;
    await api('DELETE', '/api/projects/' + el.dataset.id); renderHome();
  },
  zoom(el) { if (el.dataset.src) lightbox(el.dataset.src); },
  async 'gen-script'() {
    if (S.snap.project.script && !confirm('重新生成会覆盖当前剧本，确定吗？')) return;
    await submitted(await api('POST', `/api/projects/${S.pid}/script/generate`));
  },
  async extract() {
    await api('POST', `/api/projects/${S.pid}/extract`);
    toast('已开始提取角色与场景'); if (S.step !== 'cast') location.hash = `#/p/${S.pid}/cast`; else await loadSnap(true);
  },
  async 'batch-portraits'() { const r = await api('POST', `/api/projects/${S.pid}/batch/portraits`, { only_missing: true, count: 2 }); submitted(r, `已提交 ${r.submitted} 个出图任务`); },
  async 'add-char'() { await api('POST', `/api/projects/${S.pid}/characters`, { name: '新角色' }); loadSnap(true); },
  async 'add-loc'() { await api('POST', `/api/projects/${S.pid}/locations`, { name: '新场景' }); loadSnap(true); },
  async 'del-char'(el) { if (confirm('删除这个角色？')) { await api('DELETE', '/api/characters/' + el.dataset.id); loadSnap(true); } },
  async 'del-loc'(el) { if (confirm('删除这个场景？')) { await api('DELETE', '/api/locations/' + el.dataset.id); loadSnap(true); } },
  async 'toggle-lock'(el) { const c = localRow('characters', el.dataset.id); await api('PATCH', '/api/characters/' + c.id, { locked: !c.locked }); loadSnap(true); },
  async 'gen-portrait'(el) { submitted(await api('POST', `/api/characters/${el.dataset.id}/portrait`, { count: Number(val('pc-' + el.dataset.id)) || 2 })); },
  async 'gen-location'(el) { submitted(await api('POST', `/api/locations/${el.dataset.id}/image`, { count: 2 })); },
  async 'gen-face'(el) { submitted(await api('POST', `/api/characters/${el.dataset.id}/face`, { count: 1 })); },
  async 'upload-face'(el) { const f = await pickFile('image/*'); if (f) { await upload(`/api/characters/${el.dataset.id}/face/upload`, f); loadSnap(true); } },
  'face-fix'(el) {
    const shot = localRow('shots', el.dataset.id); const target = el.dataset.target; const cm = charMap();
    const chars = shot.character_ids.map((id) => cm[id]).filter(Boolean); const n = chars.length;
    const posOpts = [['skip', '不处理'], ...(n === 1 ? [['auto', '画面中最大的脸']] : []), ...Array.from({ length: Math.max(n, 2) + 1 }, (_, k) => [String(k), `从左数第 ${k + 1} 个人`])];
    const rows = chars.map((c, i) => `<div class="row" style="margin:8px 0">
      <div class="ref face small" style="${(c.face_image || c.ref_image) ? `background-image:url('${media(c.face_image || c.ref_image)}')` : ''}">${(c.face_image || c.ref_image) ? '' : '无'}</div>
      <div class="grow"><b>${esc(c.name)}</b><div class="muted small">${c.face_image ? '使用脸部特写' : (c.ref_image ? '⚠ 无脸部特写，将用定妆照' : '⚠ 没有参考图，无法修正')}</div></div>
      ${sel(posOpts, (c.face_image || c.ref_image) ? (n === 1 ? 'auto' : String(i)) : 'skip', `data-ffpos="${c.id}" style="width:150px"`)}</div>`).join('');
    modal(`<h3>人脸修正 · 镜头 #${shot.sort + 1}（${target === 'keyframe' ? '分镜图' : '视频'}）</h3>
      <p class="muted small">用 FaceFusion 把画面里的人脸换成角色的脸部特写，修正走样或串脸。多人镜头请指定每个角色在画面中（${target === 'video' ? '以第一帧为准' : ''}）从左到右的位置。结果作为新版本保存，可随时在候选里切回原版。</p>
      ${rows}
      <div class="row" style="margin-top:10px"><label class="row small"><input type="checkbox" id="ffx-enh" checked> 换脸后做面部增强（更清晰）</label>
        <div class="grow"></div>${sel([['hyperswap_1a_256', 'HyperSwap 1a（推荐）'], ['hyperswap_1b_256', 'HyperSwap 1b'], ['hyperswap_1c_256', 'HyperSwap 1c'], ['inswapper_128', 'InSwapper 128']], 'hyperswap_1a_256', 'id="ffx-model" style="width:190px"')}</div>
      <div class="row" style="justify-content:flex-end;margin-top:14px"><button class="btn ghost" data-act="close-modal">取消</button>
        <button class="btn" data-act="face-fix-go" data-id="${shot.id}" data-target="${target}">开始修正</button></div>`);
  },
  async 'face-fix-go'(el) {
    const items = [...document.querySelectorAll('[data-ffpos]')].map((s) => ({ character_id: s.dataset.ffpos, position: s.value }));
    const r = await api('POST', `/api/shots/${el.dataset.id}/facefix`, { target: el.dataset.target, items, enhance: val('ffx-enh'), swapper_model: val('ffx-model') });
    closeModal(); submitted(r, '已提交人脸修正');
  },
  async 'upload-portrait'(el) { const f = await pickFile('image/*'); if (f) { await upload(`/api/characters/${el.dataset.id}/upload`, f); loadSnap(true); } },
  async 'upload-location'(el) { const f = await pickFile('image/*'); if (f) { await upload(`/api/locations/${el.dataset.id}/upload`, f); loadSnap(true); } },
  async 'upload-shot'(el) { const f = await pickFile('image/*,video/*'); if (f) { await upload(`/api/shots/${el.dataset.id}/upload`, f); loadSnap(true); } },
  async 'select-asset'(el, e) { if (e.target.dataset.act === 'del-asset') return; await api('POST', '/api/assets/select', { asset_id: el.dataset.id }); loadSnap(true); },
  async 'del-asset'(el, e) { e.stopPropagation(); if (confirm('删除这个文件？')) { await api('DELETE', '/api/assets/' + el.dataset.id); loadSnap(true); } },
  async 'gen-board'() {
    if (S.snap.shots.length && !confirm('重新生成会替换全部镜头（包括已出的分镜图和视频的关联），确定吗？')) return;
    if (!S.snap.characters.length && !confirm('还没有角色设定，分镜里将无法关联角色参考图。仍然继续？')) return;
    submitted(await api('POST', `/api/projects/${S.pid}/storyboard/generate`));
  },
  async 'batch-keyframes'() { const r = await api('POST', `/api/projects/${S.pid}/batch/keyframes`, { only_missing: true, count: Number(val('batch-kc')) || 1 }); submitted(r, `已提交 ${r.submitted} 个出图任务`); },
  async 'batch-videos'() { const r = await api('POST', `/api/projects/${S.pid}/batch/videos`, { only_missing: true, quality: val('bv-q'), mode: val('bv-m'), turbo: val('bv-t') }); submitted(r, `已提交 ${r.submitted} 个视频任务`); },
  async 'add-shot'(el) { await api('POST', `/api/projects/${S.pid}/shots`, { after_id: el.dataset.id || null, description: '' }); loadSnap(true); },
  async 'del-shot'(el) { if (confirm('删除这个镜头？')) { await api('DELETE', '/api/shots/' + el.dataset.id); loadSnap(true); } },
  async move(el) {
    const ids = S.snap.shots.map((x) => x.id); const i = ids.indexOf(el.dataset.id); const j = i + Number(el.dataset.d);
    if (j < 0 || j >= ids.length) return;
    [ids[i], ids[j]] = [ids[j], ids[i]];
    await api('POST', `/api/projects/${S.pid}/shots/reorder`, { ids }); loadSnap(true);
  },
  async 'toggle-char'(el) {
    const shot = localRow('shots', el.dataset.id); const c = el.dataset.c;
    const ids = shot.character_ids.includes(c) ? shot.character_ids.filter((x) => x !== c) : [...shot.character_ids, c];
    await api('PATCH', '/api/shots/' + shot.id, { character_ids: ids }); loadSnap(true);
  },
  async 'add-line'(el) {
    const shot = localRow('shots', el.dataset.id);
    const first = shot.character_ids.length ? charMap()[shot.character_ids[0]]?.name : '旁白';
    await api('PATCH', '/api/shots/' + shot.id, { dialogue: [...(shot.dialogue || []), { speaker: first || '旁白', line: '', emotion: '' }] }); loadSnap(true);
  },
  async 'del-line'(el) {
    const shot = localRow('shots', el.dataset.id);
    await api('PATCH', '/api/shots/' + shot.id, { dialogue: shot.dialogue.filter((_, k) => k !== Number(el.dataset.j)) }); loadSnap(true);
  },
  async 'gen-keyframe'(el) { submitted(await api('POST', `/api/shots/${el.dataset.id}/keyframe`, { count: Number(val('kc-' + el.dataset.id)) || 1 })); },
  'edit-keyframe'(el) {
    modal(`<h3>指令改图</h3><p class="muted small">以当前分镜图为底图，只改你描述的部分（人物身份、构图会尽量保持）。</p>
      <textarea id="edit-ins" rows="4" placeholder="例如：让左边的女孩转头看向镜头，表情惊讶；把天空改成黄昏"></textarea>
      <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="btn ghost" data-act="close-modal">取消</button>
      <button class="btn" data-act="do-edit" data-id="${el.dataset.id}">开始改图</button></div>`);
  },
  async 'do-edit'(el) {
    const ins = val('edit-ins'); if (!ins.trim()) return toast('请填写修改说明', true);
    closeModal(); submitted(await api('POST', `/api/shots/${el.dataset.id}/keyframe/edit`, { instruction: ins }));
  },
  async plan(el) {
    const p = await api('GET', `/api/shots/${el.dataset.id}/plan`);
    const refs = p.keyframe.refs.map((r, i) => `<div style="text-align:center"><div class="thumb" style="width:72px;height:72px;background-image:url('${media(r)}')"></div><div class="small">&lt;image${i + 1}&gt;<br>${esc(p.keyframe.labels[i] || '')}</div></div>`).join('');
    modal(`<h3>实际发送给模型的内容</h3><p class="muted small">所见即所得：这里就是最终提示词，不会再被后台改写。可在分镜卡片里修改画面描述或自定义提示词。</p>
      <h4>分镜图（Qwen-Image 2.1，${p.keyframe.refs.length} 张参考图）</h4><div class="row">${refs || '<span class="muted small">无参考图（纯文生图）。建议先为角色生成参考图。</span>'}</div>
      <pre class="prompt">${esc(p.keyframe.prompt)}</pre>
      <h4>视频（MiniMax H3 · ${esc(p.video.mode || '-')}）</h4>${p.video.error ? `<p class="bad">${esc(p.video.error)}</p>` : ''}<pre class="prompt">${esc(p.video.prompt)}</pre>
      <div class="row" style="justify-content:flex-end"><button class="btn ghost" data-act="close-modal">关闭</button></div>`);
  },
  async 'gen-video'(el) {
    const id = el.dataset.id;
    submitted(await api('POST', `/api/shots/${id}/video`, { mode: val('vm-' + id), quality: val('vq-' + id), turbo: val('vt-' + id) }));
  },
  async 'ff-enhance'(el) {
    const shot = localRow('shots', el.dataset.id);
    submitted(await api('POST', '/api/facefusion/run', { processor: 'face_enhancer', target: shot.video, project_id: S.pid, owner_type: 'shot', owner_id: shot.id, apply: true }));
  },
  async 'upload-bgm'() { const f = await pickFile('audio/*'); if (f) { const r = await upload(`/api/projects/${S.pid}/upload`, f); S.bgm = r.path; renderProject(); } },
  'clear-bgm'() { S.bgm = ''; renderProject(); },
  async assemble() {
    submitted(await api('POST', `/api/projects/${S.pid}/assemble`, { subtitles: val('as-sub'), bgm: S.bgm, bgm_volume: Number(val('as-vol')), quality: val('as-q') }));
  },
  async 'task-cancel'(el) { await api('POST', `/api/tasks/${el.dataset.id}/cancel`); pollTasks(); },
  async 'task-retry'(el) { await api('POST', `/api/tasks/${el.dataset.id}/retry`); pollTasks(); },
  async 'release-gpu'() { toast('正在释放显存…'); await api('POST', '/api/system/release'); renderSystem(); },
  async 'ff-run'() {
    const proc = val('ff-proc'); const spec = S.ff.processors[proc]; const params = {};
    [...spec.params, ...S.ff.common].forEach((p) => {
      const el = document.getElementById('ffp-' + p.name); if (!el) return;
      if (p.type === 'multiselect') params[p.name] = [...el.selectedOptions].map((o) => o.value);
      else if (p.type === 'range') params[p.name] = Number(el.value);
      else params[p.name] = el.value;
    });
    if (!S.ffTarget) return toast('请先上传目标图片/视频', true);
    submitted(await api('POST', '/api/facefusion/run', { processor: proc, target: S.ffTarget, source: S.ffSource || '', params }));
    renderToolbox();
  },
  async 'ff-target'() { const f = await pickFile('image/*,video/*'); if (f) { S.ffTarget = (await upload('/api/projects/toolbox/upload', f)).path; renderToolbox(true); } },
  async 'ff-source'() { const f = await pickFile('image/*'); if (f) { S.ffSource = (await upload('/api/projects/toolbox/upload', f)).path; renderToolbox(true); } },
};
document.addEventListener('click', async (e) => {
  const el = e.target.closest('[data-act]'); if (!el) return;
  const fn = ACTIONS[el.dataset.act]; if (!fn) return;
  e.preventDefault();
  try { el.disabled = el.tagName === 'BUTTON' ? true : el.disabled; await fn(el, e); } catch (err) { console.error(err); } finally { if (el.tagName === 'BUTTON') el.disabled = false; }
});

/* ---------------- 工具箱 ---------------- */
async function renderToolbox(keep = false) {
  if (!S.ff) S.ff = await api('GET', '/api/facefusion/processors');
  const cur = keep ? val('ff-proc') : (val('ff-proc') || 'face_swapper');
  const spec = S.ff.processors[cur] || Object.values(S.ff.processors)[0];
  const field = (p) => {
    const id = 'ffp-' + p.name;
    if (p.type === 'select') return sel(p.options, p.default, `id="${id}"`);
    if (p.type === 'multiselect') return `<select id="${id}" multiple size="3">${p.options.map((o) => `<option ${(p.default || []).includes(o) ? 'selected' : ''}>${o}</option>`).join('')}</select>`;
    if (p.type === 'range') return `<div class="row"><input type="range" id="${id}" min="${p.min}" max="${p.max}" step="${p.step}" value="${p.default}" class="grow" oninput="this.nextElementSibling.textContent=this.value"><span class="small">${p.default}</span></div>`;
    return `<input type="text" id="${id}" value="${esc(p.default || '')}">`;
  };
  const tasks = (await api('GET', '/api/tasks?limit=200')).filter((t) => t.type === 'facefusion');
  const prev = (p) => (p ? (isVideo(p) ? `<video src="${media(p)}" controls style="max-width:100%;max-height:220px"></video>` : `<img src="${media(p)}" style="max-width:100%;max-height:220px;border-radius:8px">`) : '<span class="muted small">未选择</span>');
  $('#view').innerHTML = `<div class="page"><h2>工具箱 · FaceFusion</h2><div class="two">
    <div class="panel"><label class="f">功能</label>${sel(Object.entries(S.ff.processors).map(([k, v]) => [k, v.label]), spec === S.ff.processors[cur] ? cur : Object.keys(S.ff.processors)[0], 'id="ff-proc" onchange="renderToolbox(true)"')}
      <p class="muted small">${esc(spec.description || '')}</p>
      <div class="two"><div><label class="f">目标图片/视频</label><button class="btn small ghost" data-act="ff-target">上传</button><div style="margin-top:6px">${prev(S.ffTarget)}</div></div>
      ${spec.source_required || spec.source_kind ? `<div><label class="f">来源人物照片${spec.source_required ? '（必填）' : ''}</label><button class="btn small ghost" data-act="ff-source">上传</button><div style="margin-top:6px">${prev(S.ffSource)}</div></div>` : ''}</div>
      ${[...spec.params, ...S.ff.common].map((p) => `<label class="f">${esc(p.label)}</label>${field(p)}`).join('')}
      <div style="margin-top:14px"><button class="btn" data-act="ff-run">开始处理</button></div></div>
    <div class="panel"><h3>处理记录</h3>${tasks.map((t) => `<div class="task"><div class="row between"><b>${esc(t.title)}</b><span class="st ${t.status}">${t.status}</span></div>
      <div class="msg">${esc(t.message)}</div>${t.result && t.result.path ? `<div>${prev(t.result.path)}</div><a class="btn small ghost" href="${media(t.result.path)}" download>下载</a>` : ''}</div>`).join('') || '<div class="muted">暂无</div>'}</div></div></div>`;
}

/* ---------------- 系统状态 ---------------- */
async function renderSystem() {
  const s = await api('GET', '/api/system/status');
  const g = s.gpu; const mem = g.memory;
  const yes = (b, t = '正常', f = '缺失') => `<span class="${b ? 'ok' : 'bad'}">${b ? '✓ ' + t : '✗ ' + f}</span>`;
  $('#view').innerHTML = `<div class="page"><h2>系统状态</h2>
    <div class="grid3">
      <div class="panel"><h3>GPU</h3>${mem ? `<div>${esc(mem.name)}</div><div class="bar" style="margin:8px 0"><i style="width:${(mem.used / mem.total) * 100}%"></i></div><div class="muted small">${mem.used} / ${mem.total} MiB</div>` : '<div class="muted">未检测到 nvidia-smi</div>'}
        <div class="small" style="margin-top:8px">当前占用：${esc(g.owner)} ${g.busy ? '<span class="warn">（运行中：' + esc(g.detail) + '）</span>' : ''}</div>
        <button class="btn small ghost" style="margin-top:10px" data-act="release-gpu">释放显存</button></div>
      <div class="panel"><h3>服务</h3><table>
        <tr><td>ComfyUI</td><td>${yes(g.comfyui.running, '运行中', '未运行')}</td></tr>
        <tr><td>文字模型（${esc(s.llm.provider)}）</td><td>${s.llm.provider === 'api' ? '<span class="ok">外部 API</span>' : yes(g.llm.running, '已加载', '按需启动')}</td></tr>
        <tr><td>llama-server</td><td>${s.llm.provider === 'api' ? '-' : yes(s.llm.server_bin, '已编译', '未编译')}</td></tr>
        <tr><td>FaceFusion</td><td>${yes(s.facefusion, '已安装', '未安装')}</td></tr>
        <tr><td>ffmpeg</td><td>${yes(s.ffmpeg)}</td></tr></table></div>
      <div class="panel"><h3>磁盘与队列</h3><div>数据盘剩余 <b>${s.disk.free_gb} GB</b> / ${s.disk.total_gb} GB</div>
        <div class="muted small" style="margin-top:6px">排队任务：${s.queue.waiting}${s.queue.current ? '，正在执行 1 个' : ''}</div></div></div>
    <div class="panel" style="margin-top:16px"><h3>模型文件</h3><table><tr><th>文件</th><th>目录</th><th>大小</th><th>状态</th></tr>
      ${s.models.map((m) => `<tr><td>${esc(m.name)}</td><td>${esc(m.folder)}</td><td>${m.size_gb ? m.size_gb + ' GB' : '-'}</td><td>${m.ok ? '<span class="ok">✓</span>' : m.optional ? '<span class="muted">可选，未下载</span>' : '<span class="bad">✗ 缺失</span>'}</td></tr>`).join('')}
    </table><p class="muted small">缺失的模型可运行 <code>bash deploy/download_models.sh</code> 补齐（参考生视频模型加 <code>--r2v</code>）。</p></div></div>`;
}

/* ---------------- 任务轮询 ---------------- */
function renderTasks(list) {
  $('#task-list').innerHTML = list.map((t) => `<div class="task"><div class="row between"><b>${esc(t.title)}</b><span class="st ${t.status}">${{ queued: '排队', running: '运行中', cancelling: '取消中', done: '完成', failed: '失败', cancelled: '已取消' }[t.status] || t.status}</span></div>
    ${t.status === 'running' ? `<div class="bar" style="margin:6px 0"><i style="width:${t.progress}%"></i></div>` : ''}
    <div class="msg">${esc(t.message)}</div>
    <div class="row between"><span class="muted small">${new Date(t.created_at * 1000).toLocaleTimeString()}${t.project_id ? ` · <a href="#/p/${t.project_id}">打开项目</a>` : ''}</span>
    <span>${['queued', 'running'].includes(t.status) ? `<button class="btn small ghost" data-act="task-cancel" data-id="${t.id}">取消</button>` : ''}
    ${['failed', 'cancelled'].includes(t.status) ? `<button class="btn small ghost" data-act="task-retry" data-id="${t.id}">重试</button>` : ''}</span></div></div>`).join('') || '<div class="muted">暂无任务</div>';
}
async function pollTasks() {
  let active;
  try { active = await api('GET', '/api/tasks?active=1&limit=200'); } catch { return; }
  const ids = new Set(active.map((t) => t.id));
  const finished = [...S.activeIds].filter((id) => !ids.has(id));
  const progressChanged = JSON.stringify(active.map((t) => [t.id, t.progress, t.status])) !== JSON.stringify(S.active.map((t) => [t.id, t.progress, t.status]));
  S.active = active; S.activeIds = ids;
  const b = $('#task-badge'); b.textContent = active.length; b.classList.toggle('hidden', !active.length);
  const run = active.find((t) => t.status === 'running');
  const pill = $('#gpu-pill'); pill.textContent = run ? `${run.title} ${run.progress}%` : (active.length ? `排队 ${active.length}` : 'GPU 空闲'); pill.classList.toggle('busy', !!run);
  if (finished.length) {
    const recent = await api('GET', '/api/tasks?limit=100').catch(() => []);
    recent.filter((t) => finished.includes(t.id) && t.status === 'failed').forEach((t) => toast(`任务失败：${t.title} — ${t.message}`, true));
    if (S.view === 'project') await loadSnap();
    if (S.view === 'toolbox') renderToolbox(true);
  } else if (progressChanged && S.view === 'project') {
    document.querySelectorAll('.busy-slot').forEach((el) => { el.innerHTML = busyInner(el.dataset.owner); });
    const tb = document.querySelector('[data-act=assemble]');
    if (tb && busyOf(`${S.pid}:assemble`)) tb.textContent = `合成中… ${busyOf(`${S.pid}:assemble`).progress}%`;
  }
  if (S.drawer) renderTasks(await api('GET', '/api/tasks?limit=80'));
}
$('#task-btn').onclick = async () => { S.drawer = !S.drawer; $('#drawer').classList.toggle('hidden', !S.drawer); if (S.drawer) renderTasks(await api('GET', '/api/tasks?limit=80')); };
$('#drawer-close').onclick = () => { S.drawer = false; $('#drawer').classList.add('hidden'); };
$('#task-clear').onclick = async () => { await api('DELETE', '/api/tasks'); renderTasks(await api('GET', '/api/tasks?limit=80')); };

route();
setInterval(pollTasks, 2000);
pollTasks();
