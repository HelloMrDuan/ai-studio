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

/* ---------- 分组：按场景自动分组 + 手动分组；可折叠 ---------- */
const cvSavedGroups = () => (CV.pend && CV.pend.groups) || cvState().groups || [];
const cvPrefs = () => ({ ...(cvState().prefs || {}), ...((CV.pend && CV.pend.prefs) || {}) });
function cvGroups() {
  const saved = cvSavedGroups(); const out = []; const exists = new Set(cvItems().map((x) => x.id));
  if (cvPrefs().scene_groups !== false) {
    cvScenes().forEach((row, i) => {
      const id = `scene:${i}:${row.key}`.slice(0, 24); const st = saved.find((g) => g.id === id) || {};
      out.push({ id, title: row.title || `场景 ${i + 1}`, members: row.shots.map((x) => `shot:${x.id}`), auto: 'scene', collapsed: !!st.collapsed });
    });
  }
  saved.filter((g) => !g.auto).forEach((g) => {
    const members = (g.members || []).filter((m) => exists.has(m));
    if (members.length) out.push({ ...g, members });
  });
  return out;
}
// 被折叠分组藏起来的节点 → 分组 id
function cvHidden(groups) {
  const h = {};
  groups.filter((g) => g.collapsed).forEach((g) => g.members.forEach((m) => { if (!h[m]) h[m] = g.id; }));
  return h;
}
function cvSaveGroups(mutate) {
  const groups = cvSavedGroups().map((g) => ({ ...g }));
  mutate(groups);
  cvState().groups = groups; cvSave({ groups });
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
      <button class="btn small ghost" data-act="cv-scene-groups">${cvPrefs().scene_groups === false ? '场景分组：关' : '场景分组：开'}</button>
    </div>
    <div class="cv-bar cv-selbar hidden" id="cv-selbar"></div>
    <canvas class="cv-mini" id="cv-mini" width="220" height="150" title="小地图：点击或拖动跳转"></canvas>
    <div class="cv-hint muted small">拖空白处平移 · 滚轮 / 双指缩放 · 拖卡片移动（Shift 点选 / 框选多张）· 把角色 / 场景 / 道具拖到镜头上即加入该镜头 · 点连线可断开 · 双击空白加便签</div></div>`;
}
function cvRenderNodes() {
  const pos = cvPositions(); CV.pos = pos;
  const stale = cvStale(); CV_CARD._stale = stale; CV.stale = stale;
  const sb = $('#cv-stale-btn'); if (sb) { const n = Object.keys(stale).length; sb.classList.toggle('hidden', !n); sb.textContent = `⚠ 需更新 ${n}`; }
  CV.groups = cvGroups(); CV.hidden = cvHidden(CV.groups);
  $('#cv-nodes').innerHTML = cvItems().map((it) => {
    const p = pos[it.id]; it.stale = stale[it.id];
    return `<div class="cv-node cv-${it.type} ${CV.sel.has(it.id) ? 'sel' : ''} ${it.stale ? 'stale' : ''} ${CV.hidden[it.id] ? 'hidden' : ''} ${it.type === 'note' ? `nc-${it.row.color}` : ''}" data-node="${esc(it.id)}"
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
  cvDrawGroups(); cvDrawEdges(); cvSelBar(); cvMini();
}
function cvMeasure() {
  const old = CV.size || {}; CV.size = {};
  document.querySelectorAll('#cv-nodes .cv-node').forEach((el) => {
    const k = el.dataset.node;
    CV.size[k] = el.offsetWidth ? [el.offsetWidth, el.offsetHeight] : (old[k] || [CV_W[k.split(':')[0]] || 220, 240]);   // 折叠隐藏的沿用旧尺寸
  });
}
const GP = 16;                                           // 分组框内边距
function cvDrawGroups() {
  const box = $('#cv-groups'); if (!box) return;
  const html = []; const H = CV.hidden || {};
  // 先画折叠的（展开的分组如果包含被折叠的卡片，框住折叠块）
  const order = [...(CV.groups || [])].sort((a, b) => Number(b.collapsed) - Number(a.collapsed));
  order.forEach((g) => {
    const key = `grp:${g.id}`;
    const ids = g.collapsed ? g.members : [...new Set(g.members.map((m) => (H[m] && H[m] !== g.id ? `grp:${H[m]}` : m)))];
    if (!g.collapsed && ids.some((k) => k.startsWith('grp:'))) ids.forEach((k) => { if (k.startsWith('grp:') && !CV.size[k]) CV.size[k] = [260, 96]; });
    const b = cvBounds(ids); if (!b) return;
    if (g.collapsed) {
      const thumbs = g.members.map((m) => localRow('shots', m.slice(5))).filter((x) => x && x.keyframe).slice(0, 4);
      CV.pos[key] = { x: b.x0, y: b.y0 };
      html.push(`<div class="cv-gbox ${g.auto ? 'auto' : ''} ${CV.sel.has(key) ? 'sel' : ''}" data-group="${esc(g.id)}" style="left:${b.x0}px;top:${b.y0}px">
        <div class="cv-gt"><span class="cv-gtt">${esc(g.title)}</span><span class="muted small">${g.members.length} 张卡片</span>
          <span class="cv-gb" data-act="cv-group-toggle" data-id="${esc(g.id)}" title="展开">▸</span>${g.auto ? '' : `<span class="cv-gb" data-act="cv-group-del" data-id="${esc(g.id)}" title="解散分组">✕</span>`}</div>
        <div class="cv-gthumbs">${thumbs.map((x) => `<span style="background-image:url('${media(x.keyframe)}')"></span>`).join('') || '<span class="muted small">（无图）</span>'}</div></div>`);
    } else {
      delete CV.pos[key];
      const gp = g.auto ? GP : GP + 22;                   // 手动分组框套在场景分组外面，标题不重叠
      html.push(`<div class="cv-group ${g.auto ? 'auto' : ''} ${g.color ? `gc-${g.color}` : ''}" data-group="${esc(g.id)}" style="left:${b.x0 - gp}px;top:${b.y0 - gp - 26 - (g.auto ? 0 : 26)}px;width:${b.x1 - b.x0 + gp * 2}px;height:${b.y1 - b.y0 + gp * 2 + 26 + (g.auto ? 0 : 26)}px">
        <div class="cv-gt"><span class="cv-gtt" title="拖动标题可整组移动">${esc(g.title)}</span>${g.auto ? '' : `<span class="cv-gb" data-act="cv-group-rename" data-id="${esc(g.id)}" title="改名">✎</span>`}
          <span class="cv-gb" data-act="cv-group-toggle" data-id="${esc(g.id)}" title="折叠">▾</span>${g.auto ? '' : `<span class="cv-gb" data-act="cv-group-del" data-id="${esc(g.id)}" title="解散分组">✕</span>`}</div></div>`);
    }
  });
  box.innerHTML = html.join('');
  box.querySelectorAll('.cv-gbox').forEach((el) => { CV.size[`grp:${el.dataset.group}`] = [el.offsetWidth, el.offsetHeight]; });
}
function cvDrawEdges() {
  const svg = $('#cv-edges'); if (!svg) return;
  const hot = CV.sel;
  const H = CV.hidden || {}; const vis = (id) => (H[id] ? `grp:${H[id]}` : id);
  const seen = new Set();
  const paths = cvEdgeList().map(([a0, b0, k]) => {
    const a = vis(a0); const b = vis(b0);
    if (a === b) return '';
    if (a !== a0 || b !== b0) { const key = `${a}>${b}>${k}`; if (seen.has(key)) return ''; seen.add(key); }
    const pa = CV.pos[a]; const pb = CV.pos[b]; const sa = CV.size[a]; const sb = CV.size[b];
    if (!pa || !pb || !sa || !sb) return '';
    const on = hot.has(a) || hot.has(b);
    if (CV.edges !== 'all' && !on) return '';
    let x1 = pa.x + sa[0]; let y1 = pa.y + Math.min(sa[1] / 2, 60); let x2 = pb.x; let y2 = pb.y + Math.min(sb[1] / 2, 60);
    if (k === 'cont' && Math.abs(pa.y - pb.y) < 40) { y1 = pa.y + sa[1] / 2; y2 = pb.y + sb[1] / 2; }
    if (x2 < x1 - 20) { x1 = pa.x; x2 = pb.x + sb[0]; }
    const dx = Math.max(40, Math.abs(x2 - x1) / 2) * (x2 >= x1 ? 1 : -1);
    const d = `M${x1},${y1} C${x1 + dx},${y1} ${x2 - dx},${y2} ${x2},${y2}`;
    const cut = ['char', 'loc', 'prop'].includes(k) && a === a0 && b === b0 ? `<path class="e-hit" d="${d}" data-a="${esc(a)}" data-b="${esc(b)}"><title>点击断开</title></path>` : '';
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
  cvMini();
}
/* ---------- 小地图 ---------- */
let cvMiniRaf = 0;
function cvMini() {
  if (cvMiniRaf) return;
  cvMiniRaf = requestAnimationFrame(() => {
    cvMiniRaf = 0;
    const c = $('#cv-mini'); const r = $('#cv-root'); if (!c || !r || !CV.vp) return;
    const b = cvBounds(); if (!b) return;
    const view = { x0: -CV.vp.x / CV.vp.z, y0: -CV.vp.y / CV.vp.z, x1: (r.clientWidth - CV.vp.x) / CV.vp.z, y1: (r.clientHeight - CV.vp.y) / CV.vp.z };
    const all = { x0: Math.min(b.x0, view.x0), y0: Math.min(b.y0, view.y0), x1: Math.max(b.x1, view.x1), y1: Math.max(b.y1, view.y1) };
    const pad = 6; const k = Math.min((c.width - pad * 2) / (all.x1 - all.x0 || 1), (c.height - pad * 2) / (all.y1 - all.y0 || 1));
    const ox = pad - all.x0 * k + ((c.width - pad * 2) - (all.x1 - all.x0) * k) / 2; const oy = pad - all.y0 * k + ((c.height - pad * 2) - (all.y1 - all.y0) * k) / 2;
    CV.miniT = { k, ox, oy };
    const g = c.getContext('2d'); g.clearRect(0, 0, c.width, c.height);
    const COL = { script: '#8b93a7', char: '#6c8cff', loc: '#3ecf8e', prop: '#f5a524', shot: '#c5cad6', final: '#9b6cff', note: '#f7e08a', grp: '#6c8cff' };
    Object.entries(CV.pos).forEach(([id, p]) => {
      if ((CV.hidden || {})[id]) return;
      const sz = CV.size[id] || [200, 200]; const t = id.split(':')[0];
      g.fillStyle = (CV.stale || {})[id] ? '#f5a524' : COL[t] || '#8b93a7'; g.globalAlpha = CV.sel.has(id) ? 1 : 0.55;
      g.fillRect(ox + p.x * k, oy + p.y * k, Math.max(2, sz[0] * k), Math.max(2, sz[1] * k));
    });
    g.globalAlpha = 1; g.strokeStyle = '#fff'; g.lineWidth = 1;
    g.strokeRect(ox + view.x0 * k, oy + view.y0 * k, (view.x1 - view.x0) * k, (view.y1 - view.y0) * k);
  });
}
function cvMiniJump(e) {
  const c = $('#cv-mini'); const r = $('#cv-root'); const t = CV.miniT; if (!c || !t) return;
  const rc = c.getBoundingClientRect();
  const wx = (e.clientX - rc.left - t.ox) / t.k; const wy = (e.clientY - rc.top - t.oy) / t.k;
  CV.vp.x = r.clientWidth / 2 - wx * CV.vp.z; CV.vp.y = r.clientHeight / 2 - wy * CV.vp.z;
  cvApplyVp(); cvSaveVp();
}
function cvBounds(ids) {
  let x0 = Infinity; let y0 = Infinity; let x1 = -Infinity; let y1 = -Infinity;
  (ids || Object.keys(CV.pos).filter((k) => !k.startsWith('grp:') && !(CV.hidden || {})[k])).forEach((id) => {
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
  const mini = $('#cv-mini');
  mini.addEventListener('pointerdown', (e) => {
    e.stopPropagation(); cvMiniJump(e);
    const mv = (ev) => cvMiniJump(ev);
    window.addEventListener('pointermove', mv);
    window.addEventListener('pointerup', () => window.removeEventListener('pointermove', mv), { once: true });
  });
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
  if (e.target.closest('.cv-mini')) return;
  const gEl = e.target.closest('.cv-gt, .cv-gbox');
  if (gEl && e.button === 0) {                             // 拖分组标题 = 整组移动
    if (e.target.closest('[data-act]')) return;
    const g = (CV.groups || []).find((x) => x.id === gEl.closest('[data-group]').dataset.group); if (!g) return;
    const ids = g.members.filter((k) => CV.pos[k]);
    CV.drag = { kind: 'node', group: g.id, sx: e.clientX, sy: e.clientY, ids, start: Object.fromEntries(ids.map((k) => [k, { ...CV.pos[k] }])), moved: false };
    window.addEventListener('pointermove', cvMove); window.addEventListener('pointerup', cvUp, { once: true });
    return;
  }
  const nodeEl = e.target.closest('.cv-node');
  if (nodeEl && e.button === 0) {
    if (e.target.closest('button,input,textarea,select,.cv-x')) return;
    const id = nodeEl.dataset.node;
    if (e.shiftKey) { if (CV.sel.has(id)) CV.sel.delete(id); else CV.sel.add(id); } else if (!CV.sel.has(id)) { CV.sel.clear(); CV.sel.add(id); }
    cvMarkSel();
    const ids = [...CV.sel].filter((k) => CV.pos[k]);
    CV.drag = { kind: 'node', sx: e.clientX, sy: e.clientY, ids, start: Object.fromEntries(ids.map((k) => [k, { ...CV.pos[k] }])), moved: false };
  } else if (!e.target.closest('.cv-bar')) {
    if (e.shiftKey && e.button === 0) {                    // Shift + 拖空白 = 框选
      const r = $('#cv-root').getBoundingClientRect();
      CV.drag = { kind: 'box', sx: e.clientX, sy: e.clientY, ox: r.left, oy: r.top, base: new Set(CV.sel), moved: false };
    } else {
      if (e.button === 0 && CV.sel.size) { CV.sel.clear(); cvMarkSel(); }
      CV.drag = { kind: 'pan', sx: e.clientX, sy: e.clientY, vx: CV.vp.x, vy: CV.vp.y, moved: false };
    }
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
  if (d.kind === 'box') { cvBoxSelect(d, e); return; }
  d.ids.forEach((k) => {
    const p = { x: Math.round(d.start[k].x + dx / CV.vp.z), y: Math.round(d.start[k].y + dy / CV.vp.z) };
    CV.pos[k] = p;
    const el = document.querySelector(`.cv-node[data-node="${CSS.escape(k)}"]`); if (el) { el.style.left = `${p.x}px`; el.style.top = `${p.y}px`; }
  });
  cvDrawGroups(); cvDrawEdges(); cvMini();
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
  if (d.moved) {
    const eat = (ev) => { ev.stopPropagation(); ev.preventDefault(); };
    window.addEventListener('click', eat, { capture: true, once: true });
    setTimeout(() => window.removeEventListener('click', eat, { capture: true }), 60);
  }
  if (d.kind === 'box') { const b = $('#cv-boxsel'); if (b) b.remove(); cvSelBar(); }
  if (d.kind === 'pan') { if (d.moved) cvSaveVp(); } else if (d.kind === 'node' && d.moved && d.target) cvLink(d); else if (d.kind === 'node' && d.moved) cvCommitMove(d.ids);
  if (CV.dirty) { CV.dirty = false; renderProject(); }
}
function cvMarkSel() {
  document.querySelectorAll('#cv-nodes .cv-node').forEach((el) => el.classList.toggle('sel', CV.sel.has(el.dataset.node)));
  cvDrawEdges(); cvSelBar();
}
function cvBoxSelect(d, e) {
  let el = $('#cv-boxsel');
  if (!el) { el = document.createElement('div'); el.id = 'cv-boxsel'; el.className = 'cv-boxsel'; $('#cv-root').appendChild(el); }
  const x0 = Math.min(d.sx, e.clientX) - d.ox; const y0 = Math.min(d.sy, e.clientY) - d.oy;
  const x1 = Math.max(d.sx, e.clientX) - d.ox; const y1 = Math.max(d.sy, e.clientY) - d.oy;
  Object.assign(el.style, { left: `${x0}px`, top: `${y0}px`, width: `${x1 - x0}px`, height: `${y1 - y0}px` });
  const w0 = { x: (x0 - CV.vp.x) / CV.vp.z, y: (y0 - CV.vp.y) / CV.vp.z }; const w1 = { x: (x1 - CV.vp.x) / CV.vp.z, y: (y1 - CV.vp.y) / CV.vp.z };
  CV.sel = new Set(d.base);
  Object.entries(CV.pos).forEach(([k, p]) => {
    if (k.startsWith('grp:') || (CV.hidden || {})[k]) return;
    const sz = CV.size[k] || [0, 0];
    if (p.x < w1.x && p.x + sz[0] > w0.x && p.y < w1.y && p.y + sz[1] > w0.y) CV.sel.add(k);
  });
  document.querySelectorAll('#cv-nodes .cv-node').forEach((n) => n.classList.toggle('sel', CV.sel.has(n.dataset.node)));
}
// 选中多张卡片时出现的操作条
function cvSelBar() {
  const bar = $('#cv-selbar'); if (!bar) return;
  const ids = [...CV.sel].filter((k) => !k.startsWith('grp:'));
  const shots = ids.filter((k) => k.startsWith('shot:')); const notes = ids.filter((k) => k.startsWith('note:'));
  bar.classList.toggle('hidden', ids.length < 2 && !notes.length);
  bar.innerHTML = `<span class="muted small">已选 ${ids.length}</span>
    ${ids.length >= 2 ? '<button class="btn small ghost" data-act="cv-group-make">成组</button>' : ''}
    ${shots.length ? `<button class="btn small ghost" data-act="cv-sel-run" data-k="keyframe">出图 ${shots.length}</button><button class="btn small ghost" data-act="cv-sel-run" data-k="video">出视频 ${shots.length}</button>` : ''}
    ${notes.length ? `<button class="btn small ghost" data-act="cv-sel-del-notes">删除便签 ${notes.length}</button>` : ''}
    <button class="btn small ghost" data-act="cv-sel-clear">取消选择</button>`;
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

Object.assign(ACTIONS, {
  'cv-group-toggle'(el) {
    const g = (CV.groups || []).find((x) => x.id === el.dataset.id); if (!g) return;
    if (!g.collapsed) g.members.forEach((m) => CV.sel.delete(m));
    cvSaveGroups((gs) => {
      const cur = gs.find((x) => x.id === g.id);
      if (cur) cur.collapsed = !g.collapsed; else gs.push({ id: g.id, auto: g.auto || '', title: g.title, members: [], collapsed: !g.collapsed });
    });
    cvRenderNodes();
  },
  'cv-group-make'() {
    const members = [...CV.sel].filter((k) => !k.startsWith('grp:')); if (members.length < 2) return;
    const title = prompt('分组名称', '分组') || '分组';
    cvSaveGroups((gs) => gs.push({ id: Math.random().toString(36).slice(2, 12), title, members, color: '' }));
    cvRenderNodes();
  },
  'cv-group-rename'(el) {
    const g = cvSavedGroups().find((x) => x.id === el.dataset.id); if (!g) return;
    const t = prompt('分组名称', g.title); if (!t) return;
    cvSaveGroups((gs) => { gs.find((x) => x.id === g.id).title = t; }); cvRenderNodes();
  },
  'cv-group-del'(el) {
    if (!confirm('解散这个分组？（卡片保留）')) return;
    cvSaveGroups((gs) => { const i = gs.findIndex((x) => x.id === el.dataset.id); if (i >= 0) gs.splice(i, 1); }); cvRenderNodes();
  },
  'cv-scene-groups'(el) {
    const on = cvPrefs().scene_groups === false;
    const prefs = { ...cvPrefs(), scene_groups: on }; cvState().prefs = prefs; cvSave({ prefs });
    el.textContent = on ? '场景分组：开' : '场景分组：关'; cvRenderNodes();
  },
  async 'cv-sel-run'(el) {
    const ids = [...CV.sel].filter((k) => k.startsWith('shot:')).map((k) => k.slice(5));
    const k = el.dataset.k; let n = 0;
    for (const id of ids) {
      const x = localRow('shots', id); if (!x || (k === 'video' && !x.keyframe)) continue;
      await api('POST', `/api/shots/${id}/${k}`, k === 'keyframe' ? { count: 1 } : {}); n += 1;
    }
    submitted(null, n ? `已提交 ${n} 个任务` : '选中的镜头还没有分镜图');
  },
  'cv-sel-del-notes'() {
    const del = [...CV.sel].filter((k) => k.startsWith('note:')).map((k) => k.slice(5));
    if (!confirm(`删除 ${del.length} 张便签？`)) return;
    const notes = cvNotes().filter((n) => !del.includes(n.id)); cvState().notes = notes; cvSave({ notes });
    del.forEach((d) => CV.sel.delete(`note:${d}`)); cvRenderNodes();
  },
  'cv-sel-clear'() { CV.sel.clear(); cvMarkSel(); },
});
document.addEventListener('keydown', (e) => {
  if (S.step !== 'canvas' || /INPUT|TEXTAREA|SELECT/.test((document.activeElement || {}).tagName || '')) return;
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'a') {
    e.preventDefault(); CV.sel = new Set(Object.keys(CV.pos).filter((k) => !k.startsWith('grp:') && !(CV.hidden || {})[k])); cvMarkSel();
  }
});
