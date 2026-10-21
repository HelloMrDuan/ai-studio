/* 无限画布：剧本 / 角色 / 场景 / 道具 / 镜头 / 成片放在同一张画布上。
   画布只记「怎么摆」（位置、便签、分组、视口），内容就是项目数据，和流程视图同一份。 */
const CV = { vp: null, vpPid: null, drag: null, sel: new Set(), pend: null, saveT: null, dirty: false, pos: {},
  edges: localStorage.getItem('ais.cv.edges') || 'all' };
const CV_W = { script: 280, char: 170, loc: 220, prop: 160, shot: 240, final: 300, note: 220 };
const cvRatio = (a) => { const [w, h] = String(a || '16:9').split(':').map(Number); return w / h || 16 / 9; };

function cvState() {
  const p = S.snap.project;
  if (!p.canvas || typeof p.canvas !== 'object' || Array.isArray(p.canvas)) p.canvas = {};
  return p.canvas;
}
const cvNotes = () => (CV.pend && CV.pend.notes) || cvState().notes || [];
const cvSaved = () => ({ ...(cvState().nodes || {}), ...((CV.pend && CV.pend.nodes) || {}) });

/* ---------- 节点与连线 ---------- */
function cvItems() {
  const s = S.snap; const out = [{ id: 'script', type: 'script' }];
  s.characters.forEach((c) => out.push({ id: `char:${c.id}`, type: 'char', row: c }));
  s.locations.forEach((l) => out.push({ id: `loc:${l.id}`, type: 'loc', row: l }));
  (s.props || []).forEach((pr) => out.push({ id: `prop:${pr.id}`, type: 'prop', row: pr }));
  s.shots.forEach((x, i) => out.push({ id: `shot:${x.id}`, type: 'shot', row: x, i }));
  out.push({ id: 'final', type: 'final' });
  cvNotes().forEach((n) => out.push({ id: `note:${n.id}`, type: 'note', row: n }));
  return out;
}
function cvEdgeList() {
  const s = S.snap; const E = []; const has = new Set(cvItems().map((x) => x.id));
  const add = (a, b, k) => { if (has.has(a) && has.has(b)) E.push([a, b, k]); };
  if (s.project.script) {
    s.characters.forEach((c) => add('script', `char:${c.id}`, 'script'));
    s.locations.forEach((l) => add('script', `loc:${l.id}`, 'script'));
    (s.props || []).forEach((pr) => add('script', `prop:${pr.id}`, 'script'));
  }
  s.shots.forEach((x, i) => {
    (x.character_ids || []).forEach((c) => add(`char:${c}`, `shot:${x.id}`, 'char'));
    if (x.location_id) add(`loc:${x.location_id}`, `shot:${x.id}`, 'loc');
    (x.prop_ids || []).forEach((pr) => add(`prop:${pr}`, `shot:${x.id}`, 'prop'));
    if (x.continuous && i) add(`shot:${s.shots[i - 1].id}`, `shot:${x.id}`, 'cont');
    if (x.video) add(`shot:${x.id}`, 'final', 'final');
  });
  return E;
}
// 按场景分行（连续同场景的镜头排一行）
function cvScenes() {
  const rows = [];
  S.snap.shots.forEach((x) => {
    const key = x.scene || x.location_id || '';
    if (!rows.length || rows[rows.length - 1].key !== key) rows.push({ key, title: x.scene || '', shots: [] });
    rows[rows.length - 1].shots.push(x);
  });
  return rows;
}
const cvShotH = (x) => Math.round(CV_W.shot / cvRatio(shotAspect(x))) + 118;

/* 默认布局：剧本 | 角色 / 场景 / 道具 | 镜头（每场一行）| 成片 */
function cvDefaultLayout() {
  const s = S.snap; const pos = { script: { x: 0, y: 0 } };
  let y = 0; const cx = 340;
  const H = (id, est) => (CV.size && CV.size[id] ? CV.size[id][1] : est);   // 量过的真实高度优先
  const col = (rows, w, h, cols) => {
    for (let i = 0; i < rows.length; i += cols) {
      const line = rows.slice(i, i + cols);
      line.forEach((r, j) => { pos[r] = { x: cx + j * (w + 20), y }; });
      y += Math.max(...line.map((r) => H(r, h))) + 20;
    }
    if (rows.length) y += 30;
  };
  col(s.characters.map((c) => `char:${c.id}`), CV_W.char, 250, 2);
  col(s.locations.map((l) => `loc:${l.id}`), CV_W.loc, 200, 1);
  col((s.props || []).map((pr) => `prop:${pr.id}`), CV_W.prop, 190, 2);
  const sx = cx + Math.max(2 * (CV_W.char + 20), CV_W.loc + 20) + 120;
  let sy = 0; let maxCols = 1;
  cvScenes().forEach((row) => {
    row.shots.forEach((x, j) => { pos[`shot:${x.id}`] = { x: sx + j * (CV_W.shot + 24), y: sy + 28 }; });
    maxCols = Math.max(maxCols, row.shots.length);
    sy += Math.max(...row.shots.map((x) => H(`shot:${x.id}`, cvShotH(x)))) + 28 + 50;
  });
  pos.final = { x: sx + maxCols * (CV_W.shot + 24) + 100, y: 0 };
  return pos;
}
function cvPositions() {
  const def = cvDefaultLayout(); const saved = cvSaved(); const pos = {};
  cvItems().forEach((it) => {
    if (it.type === 'note') pos[it.id] = { x: it.row.x, y: it.row.y };
    else pos[it.id] = saved[it.id] ? { x: saved[it.id].x, y: saved[it.id].y } : (def[it.id] || { x: 0, y: 0 });
  });
  return pos;
}

/* ---------- 过期检测：上游换过图 / 视频后，下游标「需更新」 ---------- */
const cvT = (owner, path) => { if (!path) return 0; const a = (S.snap.assets[owner] || []).find((x) => x.path === path); return a ? a.created_at : 0; };
function cvStale() {
  const s = S.snap; const out = {};
  const refT = (type, id) => { const r = (type === 'char' ? s.characters : type === 'loc' ? s.locations : s.props || []).find((x) => x.id === id); return r ? cvT(r.id, r.ref_image) : 0; };
  let lastVideo = 0;
  s.shots.forEach((x) => {
    const tk = cvT(x.id, x.keyframe); const tv = cvT(x.id, x.video); const why = [];
    if (tk) {
      const ups = [...(x.character_ids || []).map((c) => ['char', c, '角色形象']), ...(x.location_id ? [['loc', x.location_id, '场景图']] : []),
        ...(x.prop_ids || []).map((pr) => ['prop', pr, '道具图'])];
      ups.forEach(([t, id, label]) => { if (refT(t, id) > tk + 1) why.push(`${label}已更换`); });
    }
    if (why.length) out[`shot:${x.id}`] = { what: 'keyframe', why: [...new Set(why)].join('、') + '，分镜图需要重出' };
    else if (tk && tv && tk > tv + 1) out[`shot:${x.id}`] = { what: 'video', why: '分镜图比视频新，视频需要重做' };
    lastVideo = Math.max(lastVideo, tv);
  });
  const tf = cvT(s.project.id, s.project.final_video);
  if (tf && lastVideo > tf + 1) out.final = { what: 'final', why: '有镜头视频比成片新，需要重新合成' };
  return out;
}
function cvThumbs(owner, kind, current, max = 6) {
  const list = assetsOf(owner, kind).slice(0, max); if (list.length < 2) return '';
  return `<div class="cv-thumbs">${list.map((a) => `<span class="cv-th ${a.path === current ? 'sel' : ''}" data-act="select-asset" data-id="${a.id}" title="设为当前使用"
    style="${isVideo(a.path) ? '' : `background-image:url('${media(a.path)}')`}">${isVideo(a.path) ? '▶' : ''}</span>`).join('')}</div>`;
}

/* ---------- 卡片 ---------- */
const cvImg = (path, ratio, extra = '') => `<div class="cv-img" style="aspect-ratio:${ratio};${path ? `background-image:url('${media(path)}')` : ''}" ${path ? `data-act="zoom" data-src="${media(path)}"` : ''}>${path ? '' : '<span class="muted small">暂无图</span>'}${extra}</div>`;
const CV_CARD = {
  script() {
    const p = S.snap.project; const scenes = (p.script || '').match(/^##\s/gm);
    return `<div class="cv-h">📜 剧本</div><div class="cv-b"><b>${esc(p.title)}</b>
      <div class="small muted clamp4">${esc(p.synopsis || (p.script ? p.script.slice(0, 120) : '还没有剧本'))}</div>
      <div class="small muted">${p.script ? `${p.script.length} 字 · ${scenes ? scenes.length : 0} 场` : ''} · ${p.aspect_ratio}</div></div>
      <div class="cv-f">${!p.script ? `<button class="btn small" data-act="gen-script" ${p.source_text ? '' : 'disabled'}>生成剧本</button>`
        : !S.snap.characters.length ? '<button class="btn small" data-act="cv-extract">提取角色与场景</button>'
          : !S.snap.shots.length ? '<button class="btn small" data-act="cv-storyboard">生成分镜表</button>' : ''}
        <button class="btn small ghost" data-act="cv-open" data-step="script">编辑剧本</button>${busyBox(`${p.id}:script`)}</div>`;
  },
  char(it) {
    const c = it.row;
    return `<div class="cv-h">👤 ${esc(c.name)}${c.locked ? ' 🔒' : ''}</div>${cvImg(c.ref_image, '3/4', busyBox(c.id))}
      ${cvThumbs(c.id, 'portrait', c.ref_image)}
      <div class="cv-f"><button class="btn small ${c.ref_image ? 'ghost' : ''}" data-act="gen-portrait" data-id="${c.id}" ${c.locked ? 'disabled title="已锁定"' : ''}>${c.ref_image ? '重做' : '生成形象'}</button>
        <button class="btn small ghost" data-act="cv-open" data-step="cast">详情</button></div>`;
  },
  loc(it) {
    const l = it.row;
    return `<div class="cv-h">🏞 ${esc(l.name)}</div>${cvImg(l.ref_image, '16/9', busyBox(l.id))}${cvThumbs(l.id, 'location', l.ref_image)}
      <div class="cv-f"><button class="btn small ${l.ref_image ? 'ghost' : ''}" data-act="gen-location" data-id="${l.id}">${l.ref_image ? '重做' : '生成场景图'}</button></div>`;
  },
  prop(it) {
    const pr = it.row;
    return `<div class="cv-h">🧩 ${esc(pr.name)}</div>${cvImg(pr.ref_image, '1/1', busyBox(pr.id))}${cvThumbs(pr.id, 'prop', pr.ref_image)}
      <div class="cv-f"><button class="btn small ${pr.ref_image ? 'ghost' : ''}" data-act="gen-prop" data-id="${pr.id}">${pr.ref_image ? '重做' : '生成'}</button></div>`;
  },
  shot(it) {
    const x = it.row; const a = shotAspect(x);
    const play = x.video ? `<span class="cv-play" data-act="zoom" data-src="${media(x.video)}" title="播放视频">▶</span>` : '';
    const st = [x.keyframe ? '<span class="ok">图✓</span>' : '<span class="muted">图</span>', x.video ? '<span class="ok">视频✓</span>' : '<span class="muted">视频</span>'];
    if ((x.dialogue || []).length) st.push(`<span class="muted">台词 ${x.dialogue.length}</span>`);
    return `<div class="cv-h"><b>#${it.i + 1}</b> <span class="muted small">${esc(x.shot_size || '')} · ${x.duration}s${x.aspect ? ` · ${a}` : ''}</span></div>
      ${cvImg(x.keyframe, a.replace(':', '/'), play + busyBox(x.id))}
      ${cvThumbs(x.id, 'keyframe', x.keyframe)}${cvThumbs(x.id, 'video', x.video)}
      ${it.stale ? `<div class="cv-stale small">⚠ ${esc(it.stale.why)}</div>` : ''}
      <div class="cv-b small clamp2" title="${esc(x.description || '')}">${esc(x.description || '')}</div>
      <div class="cv-f small">${st.join(' ')}</div>
      <div class="cv-f"><button class="btn small ${x.keyframe && !(it.stale && it.stale.what === 'keyframe') ? 'ghost' : ''}" data-act="gen-keyframe" data-id="${x.id}">${x.keyframe ? '重出图' : '出图'}</button>
        <button class="btn small ${x.keyframe && (!x.video || (it.stale && it.stale.what === 'video')) ? '' : 'ghost'}" data-act="gen-video" data-id="${x.id}" ${x.keyframe ? '' : 'disabled'}>${x.video ? '重做视频' : '出视频'}</button>
        <button class="btn small ghost" data-act="cv-open" data-step="${x.keyframe ? 'video' : 'board'}" data-anchor="shot-${x.id}" title="在流程视图里编辑这个镜头的全部设置">编辑</button></div>`;
  },
  final() {
    const p = S.snap.project; const n = S.snap.shots.length; const v = S.snap.shots.filter((x) => x.video).length;
    return `<div class="cv-h">🎬 成片</div>
      ${p.final_video ? `<div class="cv-img" style="aspect-ratio:${p.aspect_ratio.replace(':', '/')};${p.cover ? `background-image:url('${media(p.cover)}')` : ''}"><span class="cv-play" data-act="zoom" data-src="${media(p.final_video)}">▶</span>${busyBox(`${p.id}:assemble`)}</div>`
        : `<div class="cv-b small muted">镜头视频 ${v}/${n}${busyBox(`${p.id}:assemble`)}</div>`}
      ${CV_CARD._stale && CV_CARD._stale.final ? `<div class="cv-stale small">⚠ ${CV_CARD._stale.final.why}</div>` : ''}
      <div class="cv-f"><button class="btn small ${p.final_video && !(CV_CARD._stale && CV_CARD._stale.final) ? 'ghost' : ''}" data-act="cv-assemble" ${v ? '' : 'disabled'}>${p.final_video ? '重新合成' : '合成成片'}</button>
        <button class="btn small ghost" data-act="cv-open" data-step="final">成片设置</button></div>`;
  },
  note(it) {
    const n = it.row;
    return `<div class="cv-h row between"><span>📝 便签</span><span class="cv-x" data-act="cv-del-note" data-id="${n.id}" title="删除">✕</span></div>
      <textarea data-cv-note="${n.id}" placeholder="写点什么：灵感、修改意见、参考…">${esc(n.text)}</textarea>`;
  },
};

/* ---------- 渲染 ---------- */
function viewCanvas() {
  return `<div class="cv-root" id="cv-root">
    <div class="cv-world" id="cv-world"><svg class="cv-edges" id="cv-edges"></svg><div id="cv-groups"></div><div id="cv-nodes"></div></div>
    <div class="cv-bar">
      <button class="btn small ghost" data-act="cv-add-note">＋ 便签</button>
      <button class="btn small ghost" data-act="cv-auto" title="按 剧本 → 角色/场景 → 镜头 → 成片 重新排列">自动排列</button>
      <button class="btn small ghost" data-act="cv-fit">适应画布</button>
      <button class="btn small ghost" data-act="cv-zoom" data-d="0.8">－</button><span class="cv-zl" id="cv-zl">100%</span><button class="btn small ghost" data-act="cv-zoom" data-d="1.25">＋</button>
      <button class="btn small ghost" data-act="cv-batch" data-k="keyframes" title="给还没有分镜图的镜头出图">批量出图</button>
      <button class="btn small ghost" data-act="cv-batch" data-k="videos" title="给有分镜图、还没有视频的镜头出视频">批量出视频</button>
      <button class="btn small warn hidden" id="cv-stale-btn" data-act="cv-next-stale" title="依次定位到需要更新的卡片">⚠ 需更新</button>
      <button class="btn small ghost" data-act="cv-edges">${CV.edges === 'all' ? '连线：全部' : '连线：仅选中'}</button>
    </div>
    <div class="cv-hint muted small">拖空白处平移 · 滚轮 / 双指缩放 · 拖卡片移动（Shift 多选）· 把角色 / 场景 / 道具拖到镜头上即加入该镜头 · 点连线可断开 · 双击空白加便签</div></div>`;
}
function cvRenderNodes() {
  const pos = cvPositions(); CV.pos = pos;
  const stale = cvStale(); CV_CARD._stale = stale; CV.stale = stale;
  const sb = $('#cv-stale-btn'); if (sb) { const n = Object.keys(stale).length; sb.classList.toggle('hidden', !n); sb.textContent = `⚠ 需更新 ${n}`; }
  $('#cv-nodes').innerHTML = cvItems().map((it) => {
    const p = pos[it.id]; it.stale = stale[it.id];
    return `<div class="cv-node cv-${it.type} ${CV.sel.has(it.id) ? 'sel' : ''} ${it.stale ? 'stale' : ''} ${it.type === 'note' ? `nc-${it.row.color}` : ''}" data-node="${esc(it.id)}"
      style="left:${p.x}px;top:${p.y}px;width:${(it.type === 'note' && it.row.w) || CV_W[it.type]}px">${CV_CARD[it.type](it)}</div>`;
  }).join('');
  cvMeasure();
  // 默认布局按估计高度摆的，量到真实高度后再摆一次（只动没手动摆过的卡片）
  const again = cvPositions(); let moved = false;
  Object.entries(again).forEach(([k, p]) => {
    if (CV.pos[k] && (CV.pos[k].x !== p.x || CV.pos[k].y !== p.y)) {
      CV.pos[k] = p; moved = true;
      const el = document.querySelector(`.cv-node[data-node="${CSS.escape(k)}"]`); if (el) { el.style.left = `${p.x}px`; el.style.top = `${p.y}px`; }
    }
  });
  if (moved) cvMeasure();
  cvDrawEdges();
}
function cvMeasure() {
  CV.size = {};
  document.querySelectorAll('#cv-nodes .cv-node').forEach((el) => { CV.size[el.dataset.node] = [el.offsetWidth, el.offsetHeight]; });
}
function cvDrawEdges() {
  const svg = $('#cv-edges'); if (!svg) return;
  const hot = CV.sel;
  const paths = cvEdgeList().map(([a, b, k]) => {
    const pa = CV.pos[a]; const pb = CV.pos[b]; const sa = CV.size[a]; const sb = CV.size[b];
    if (!pa || !pb || !sa || !sb) return '';
    const on = hot.has(a) || hot.has(b);
    if (CV.edges !== 'all' && !on) return '';
    let x1 = pa.x + sa[0]; let y1 = pa.y + Math.min(sa[1] / 2, 60); let x2 = pb.x; let y2 = pb.y + Math.min(sb[1] / 2, 60);
    if (k === 'cont' && Math.abs(pa.y - pb.y) < 40) { y1 = pa.y + sa[1] / 2; y2 = pb.y + sb[1] / 2; }
    if (x2 < x1 - 20) { x1 = pa.x; x2 = pb.x + sb[0]; }
    const dx = Math.max(40, Math.abs(x2 - x1) / 2) * (x2 >= x1 ? 1 : -1);
    const d = `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
    const cut = ['char', 'loc', 'prop'].includes(k) ? `<path class="e-hit" d="${d}" data-a="${esc(a)}" data-b="${esc(b)}"><title>点击断开</title></path>` : '';
    return `<path class="e-${k} ${on ? 'hot' : ''}" d="${d}"/>${cut}`;
  });
  svg.innerHTML = paths.join('');
}
function cvApplyVp() {
  const w = $('#cv-world'); if (!w || !CV.vp) return;
  w.style.transform = `translate(${CV.vp.x}px,${CV.vp.y}px) scale(${CV.vp.z})`;
  const zl = $('#cv-zl'); if (zl) zl.textContent = `${Math.round(CV.vp.z * 100)}%`;
  const r = $('#cv-root'); if (r) r.style.backgroundPosition = `${CV.vp.x}px ${CV.vp.y}px`;
  if (r) r.style.backgroundSize = `${24 * CV.vp.z}px ${24 * CV.vp.z}px`;
}
function cvBounds(ids) {
  let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
  (ids || Object.keys(CV.pos)).forEach((id) => {
    const p = CV.pos[id]; const s = CV.size[id] || [200, 200]; if (!p) return;
    x0 = Math.min(x0, p.x); y0 = Math.min(y0, p.y); x1 = Math.max(x1, p.x + s[0]); y1 = Math.max(y1, p.y + s[1]);
  });
  return x0 === Infinity ? null : { x0, y0, x1, y1 };
}
function cvFit() {
  const r = $('#cv-root'); const b = cvBounds(); if (!r || !b) return;
  const W = r.clientWidth; const H = r.clientHeight; const pad = 60;
  const z = Math.max(0.1, Math.min(1.2, Math.min((W - pad * 2) / (b.x1 - b.x0 || 1), (H - pad * 2 - 40) / (b.y1 - b.y0 || 1))));
  CV.vp = { x: (W - (b.x1 - b.x0) * z) / 2 - b.x0 * z, y: (H - (b.y1 - b.y0) * z) / 2 - b.y0 * z + 20, z };
  cvApplyVp(); cvSaveVp();
}
function cvMount() {
  const root = $('#cv-root'); if (!root) return;
  cvRenderNodes();
  if (CV.vpPid !== S.pid) {
    CV.vpPid = S.pid; CV.sel.clear();
    const v = cvState().viewport;
    if (v && v.z) { CV.vp = { ...v }; cvApplyVp(); } else cvFit();
  } else cvApplyVp();
  root.addEventListener('pointerdown', cvDown);
  root.addEventListener('wheel', cvWheel, { passive: false });
  root.addEventListener('dblclick', (e) => { if (!e.target.closest('.cv-node')) cvAddNoteAt(e); });
  root.addEventListener('change', cvNoteChange);
  // 图片加载后卡片尺寸会变，重新量一次连线
  root.querySelectorAll('video').forEach((v) => v.addEventListener('loadedmetadata', () => { cvMeasure(); cvDrawEdges(); }, { once: true }));
}

/* ---------- 交互：平移 / 缩放 / 拖动 ---------- */
const cvWorldPt = (e) => { const r = $('#cv-root').getBoundingClientRect(); return { x: (e.clientX - r.left - CV.vp.x) / CV.vp.z, y: (e.clientY - r.top - CV.vp.y) / CV.vp.z }; };
function cvWheel(e) {
  if (e.target.closest('textarea') && !e.ctrlKey) return;
  e.preventDefault();
  const trackpadPan = !e.ctrlKey && (Math.abs(e.deltaX) > 0.5 || (e.deltaMode === 0 && Math.abs(e.deltaY) < 50 && !Number.isInteger(e.deltaY)));
  if (trackpadPan || e.shiftKey) {
    CV.vp.x -= e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX; CV.vp.y -= e.shiftKey ? 0 : e.deltaY;
  } else {
    const r = $('#cv-root').getBoundingClientRect(); const mx = e.clientX - r.left; const my = e.clientY - r.top;
    const z = Math.max(0.08, Math.min(3, CV.vp.z * Math.exp(-e.deltaY * (e.ctrlKey ? 0.01 : 0.0015))));
    CV.vp.x = mx - (mx - CV.vp.x) * z / CV.vp.z; CV.vp.y = my - (my - CV.vp.y) * z / CV.vp.z; CV.vp.z = z;
  }
  cvApplyVp(); cvSaveVp();
}
function cvDown(e) {
  if (e.button !== 0 && e.button !== 1) return;
  if (e.target.closest('.e-hit')) return;                 // 点连线：交给 click 处理断开
  const nodeEl = e.target.closest('.cv-node');
  if (nodeEl && e.button === 0) {
    if (e.target.closest('button,input,textarea,select,.cv-x')) return;
    const id = nodeEl.dataset.node;
    if (e.shiftKey) { if (CV.sel.has(id)) CV.sel.delete(id); else CV.sel.add(id); } else if (!CV.sel.has(id)) { CV.sel.clear(); CV.sel.add(id); }
    cvMarkSel();
    const ids = [...CV.sel].filter((k) => CV.pos[k]);
    CV.drag = { kind: 'node', sx: e.clientX, sy: e.clientY, ids, start: Object.fromEntries(ids.map((k) => [k, { ...CV.pos[k] }])), moved: false };
  } else if (!e.target.closest('.cv-bar')) {
    if (!e.shiftKey && e.button === 0 && CV.sel.size) { CV.sel.clear(); cvMarkSel(); }
    CV.drag = { kind: 'pan', sx: e.clientX, sy: e.clientY, vx: CV.vp.x, vy: CV.vp.y, moved: false };
  } else return;
  window.addEventListener('pointermove', cvMove);
  window.addEventListener('pointerup', cvUp, { once: true });
}
function cvMove(e) {
  const d = CV.drag; if (!d) return;
  const dx = e.clientX - d.sx; const dy = e.clientY - d.sy;
  if (!d.moved && Math.hypot(dx, dy) < 4) return;
  d.moved = true; document.body.classList.add('cv-dragging');
  if (d.kind === 'pan') { CV.vp.x = d.vx + dx; CV.vp.y = d.vy + dy; cvApplyVp(); return; }
  d.ids.forEach((k) => {
    const p = { x: Math.round(d.start[k].x + dx / CV.vp.z), y: Math.round(d.start[k].y + dy / CV.vp.z) };
    CV.pos[k] = p;
    const el = document.querySelector(`.cv-node[data-node="${CSS.escape(k)}"]`); if (el) { el.style.left = `${p.x}px`; el.style.top = `${p.y}px`; }
  });
  cvDrawEdges();
  // 角色 / 场景 / 道具拖到镜头上：高亮目标
  const t = d.ids.length === 1 && /^(char|loc|prop):/.test(d.ids[0]) ? cvDropTarget(e) : null;
  if (t !== d.target) {
    if (d.target) { const o = document.querySelector(`.cv-node[data-node="${CSS.escape(d.target)}"]`); if (o) o.classList.remove('drop'); }
    if (t) { const o = document.querySelector(`.cv-node[data-node="${CSS.escape(t)}"]`); if (o) o.classList.add('drop'); }
    d.target = t;
  }
}
function cvUp() {
  window.removeEventListener('pointermove', cvMove);
  document.body.classList.remove('cv-dragging');
  const d = CV.drag; CV.drag = null; if (!d) return;
  if (d.moved) window.addEventListener('click', (ev) => { ev.stopPropagation(); ev.preventDefault(); }, { capture: true, once: true });
  if (d.kind === 'pan') { if (d.moved) cvSaveVp(); } else if (d.moved && d.target) cvLink(d); else if (d.moved) cvCommitMove(d.ids);
  if (CV.dirty) { CV.dirty = false; renderProject(); }
}
function cvMarkSel() {
  document.querySelectorAll('#cv-nodes .cv-node').forEach((el) => el.classList.toggle('sel', CV.sel.has(el.dataset.node)));
  cvDrawEdges();
}
function cvCommitMove(ids) {
  const nodes = {}; let notes = null;
  ids.forEach((k) => {
    if (k.startsWith('note:')) {
      notes = notes || cvNotes().map((n) => ({ ...n }));
      const n = notes.find((x) => `note:${x.id}` === k); if (n) { n.x = CV.pos[k].x; n.y = CV.pos[k].y; }
    } else nodes[k] = CV.pos[k];
  });
  cvSave(notes ? { nodes, notes } : { nodes });
}

/* ---------- 保存（防抖；未保存的改动先盖在快照上） ---------- */
function cvSave(part) {
  const pend = CV.pend || {};
  if (part.nodes) pend.nodes = { ...(pend.nodes || {}), ...part.nodes };
  Object.keys(part).filter((k) => k !== 'nodes').forEach((k) => { pend[k] = part[k]; });
  CV.pend = pend; CV.pendPid = S.pid;
  clearTimeout(CV.saveT); CV.saveT = setTimeout(cvFlush, 500);
}
let cvVpT;
function cvSaveVp() { clearTimeout(cvVpT); cvVpT = setTimeout(() => cvSave({ viewport: CV.vp }), 1200); }
async function cvFlush() {
  const body = CV.pend; const pid = CV.pendPid; CV.pend = null; if (!body || !pid) return;
  try {
    const c = await api('PUT', `/api/projects/${pid}/canvas`, body);
    if (S.snap && S.snap.project.id === pid) S.snap.project.canvas = c;
  } catch { CV.pend = { ...body, ...(CV.pend || {}), nodes: { ...(body.nodes || {}), ...((CV.pend || {}).nodes || {}) } }; }
}
window.addEventListener('beforeunload', () => {
  if (CV.pend && CV.pendPid) fetch(`/api/projects/${CV.pendPid}/canvas`, { method: 'PUT', keepalive: true, headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(CV.pend) });
});

/* ---------- 便签 ---------- */
function cvAddNoteAt(e) {
  const p = e ? cvWorldPt(e) : (() => { const r = $('#cv-root'); return { x: (r.clientWidth / 2 - CV.vp.x) / CV.vp.z - 110, y: (r.clientHeight / 2 - CV.vp.y) / CV.vp.z - 60 }; })();
  const n = { id: Math.random().toString(36).slice(2, 12), x: Math.round(p.x), y: Math.round(p.y), w: 220, text: '', color: 'yellow' };
  const notes = [...cvNotes(), n]; cvState().notes = notes; cvSave({ notes });
  cvRenderNodes();
  const ta = document.querySelector(`[data-cv-note="${n.id}"]`); if (ta) ta.focus();
}
function cvNoteChange(e) {
  const ta = e.target.closest('[data-cv-note]'); if (!ta) return;
  const notes = cvNotes().map((n) => (n.id === ta.dataset.cvNote ? { ...n, text: ta.value } : n));
  cvState().notes = notes; cvSave({ notes });
}

Object.assign(ACTIONS, {
  'view-canvas'() { localStorage.setItem('ais.view', 'canvas'); location.hash = `#/p/${S.pid}/canvas`; },
  'view-flow'() { localStorage.setItem('ais.view', 'flow'); location.hash = `#/p/${S.pid}/${S.lastStep || 'script'}`; },
  'cv-open'(el) { S.anchor = el.dataset.anchor || ''; location.hash = `#/p/${S.pid}/${el.dataset.step}`; },
  'cv-add-note'() { cvAddNoteAt(null); },
  'cv-del-note'(el) {
    const notes = cvNotes().filter((n) => n.id !== el.dataset.id); cvState().notes = notes; CV.sel.delete(`note:${el.dataset.id}`);
    cvSave({ notes }); cvRenderNodes();
  },
  'cv-fit'() { cvFit(); },
  'cv-zoom'(el) {
    const r = $('#cv-root'); const mx = r.clientWidth / 2; const my = r.clientHeight / 2;
    const z = Math.max(0.08, Math.min(3, CV.vp.z * Number(el.dataset.d)));
    CV.vp.x = mx - (mx - CV.vp.x) * z / CV.vp.z; CV.vp.y = my - (my - CV.vp.y) * z / CV.vp.z; CV.vp.z = z; cvApplyVp(); cvSaveVp();
  },
  async 'cv-auto'() {
    if (!confirm('按默认布局重新排列所有卡片？（便签位置不变）')) return;
    clearTimeout(CV.saveT); if (CV.pend) { delete CV.pend.nodes; }
    S.snap.project.canvas = await api('POST', `/api/projects/${S.pid}/canvas/reset`);
    cvRenderNodes(); cvFit();
  },
  'cv-edges'(el) {
    CV.edges = CV.edges === 'all' ? 'sel' : 'all'; localStorage.setItem('ais.cv.edges', CV.edges);
    el.textContent = CV.edges === 'all' ? '连线：全部' : '连线：仅选中'; cvDrawEdges();
  },
});
document.addEventListener('keydown', (e) => {
  if (S.step !== 'canvas' || /INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName || '')) return;
  if ((e.key === 'Delete' || e.key === 'Backspace') && CV.sel.size) {
    const del = [...CV.sel].filter((k) => k.startsWith('note:')).map((k) => k.slice(5));
    if (!del.length) return;
    const notes = cvNotes().filter((n) => !del.includes(n.id)); cvState().notes = notes; cvSave({ notes });
    del.forEach((d) => CV.sel.delete(`note:${d}`)); cvRenderNodes();
  } else if (e.key === 'Escape') { CV.sel.clear(); cvMarkSel(); }
});

/* ---------- 拖拽建立关系 / 点连线断开 ---------- */
function cvDropTarget(e) {
  const pt = cvWorldPt(e);
  return Object.keys(CV.pos).find((k) => {
    if (!k.startsWith('shot:')) return false;
    const p = CV.pos[k]; const sz = CV.size[k] || [0, 0];
    return pt.x >= p.x && pt.x <= p.x + sz[0] && pt.y >= p.y && pt.y <= p.y + sz[1];
  }) || null;
}
function cvShotPatch(src, shot, remove = false) {
  const [t, id] = src.split(':');
  if (t === 'loc') return { location_id: remove ? '' : id };
  const key = t === 'char' ? 'character_ids' : 'prop_ids'; const cur = shot[key] || [];
  return { [key]: remove ? cur.filter((x) => x !== id) : cur.includes(id) ? cur : [...cur, id] };
}
async function cvLink(d) {
  const src = d.ids[0]; const shot = localRow('shots', d.target.slice(5));
  // 拖过去的卡片回到原位
  CV.pos[src] = d.start[src];
  const el = document.querySelector(`.cv-node[data-node="${CSS.escape(src)}"]`); if (el) { el.style.left = `${d.start[src].x}px`; el.style.top = `${d.start[src].y}px`; }
  const o = document.querySelector('.cv-node.drop'); if (o) o.classList.remove('drop');
  if (!shot) return;
  const idx = S.snap.shots.indexOf(shot) + 1; const name = cvName(src);
  const patch = cvShotPatch(src, shot);
  if (JSON.stringify(patch[Object.keys(patch)[0]]) === JSON.stringify(shot[Object.keys(patch)[0]])) { toast(`${name} 已经在镜头 #${idx} 里`); cvDrawEdges(); return; }
  await api('PATCH', `/api/shots/${shot.id}`, patch);
  toast(`已把 ${name} 加入镜头 #${idx}${shot.keyframe ? '（分镜图需要重出才会体现）' : ''}`);
  loadSnap(true);
}
function cvName(nodeId) {
  const [t, id] = nodeId.split(':'); const s = S.snap;
  const r = (t === 'char' ? s.characters : t === 'loc' ? s.locations : t === 'prop' ? s.props || [] : []).find((x) => x.id === id);
  return r ? `「${r.name}」` : '';
}
document.addEventListener('click', async (e) => {
  const hit = e.target.closest && e.target.closest('#cv-edges .e-hit'); if (!hit) return;
  const shot = localRow('shots', hit.dataset.b.slice(5)); if (!shot) return;
  const idx = S.snap.shots.indexOf(shot) + 1;
  if (!confirm(`把 ${cvName(hit.dataset.a)} 从镜头 #${idx} 中移除？`)) return;
  await api('PATCH', `/api/shots/${shot.id}`, cvShotPatch(hit.dataset.a, shot, true));
  toast('已断开'); loadSnap(true);
});

Object.assign(ACTIONS, {
  async 'cv-extract'() { await api('POST', `/api/projects/${S.pid}/extract`); toast('已开始提取角色与场景'); pollTasks(); },
  async 'cv-storyboard'() { submitted(await api('POST', `/api/projects/${S.pid}/storyboard/generate`)); },
  async 'cv-assemble'() { submitted(await api('POST', `/api/projects/${S.pid}/assemble`, {}), '已提交成片合成'); },
  async 'cv-batch'(el) {
    const r = await api('POST', `/api/projects/${S.pid}/batch/${el.dataset.k}`, { only_missing: true, count: 1 });
    submitted(r, r.submitted ? `已提交 ${r.submitted} 个任务` : '没有需要补的镜头');
  },
  'cv-next-stale'() {
    const ids = Object.keys(CV.stale || {}); if (!ids.length) return;
    CV.staleI = ((CV.staleI || 0) + 1) % ids.length; const id = ids[CV.staleI];
    CV.sel.clear(); CV.sel.add(id); cvMarkSel(); cvCenterOn(id);
    toast(CV.stale[id].why);
  },
});
function cvCenterOn(id) {
  const r = $('#cv-root'); const p = CV.pos[id]; const sz = CV.size[id] || [200, 200]; if (!r || !p) return;
  CV.vp.x = r.clientWidth / 2 - (p.x + sz[0] / 2) * CV.vp.z; CV.vp.y = r.clientHeight / 2 - (p.y + sz[1] / 2) * CV.vp.z;
  cvApplyVp(); cvSaveVp();
}
