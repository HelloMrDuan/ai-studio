/* AI Studio 专业创作功能（编剧室 / 导演审校 / 修改建议 …）。与 app.js 共享全局函数与状态。 */

/* ---------------- 通用：行级 diff ---------------- */
function lineDiff(a, b) {
  const x = String(a || '').split('\n'); const y = String(b || '').split('\n');
  const n = x.length; const m = y.length;
  if (n * m > 400000) return y.map((l) => ({ t: '=', l }));
  const dp = Array.from({ length: n + 1 }, () => new Int32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) for (let j = m - 1; j >= 0; j--) dp[i][j] = x[i] === y[j] ? dp[i + 1][j + 1] + 1 : Math.max(dp[i + 1][j], dp[i][j + 1]);
  const out = []; let i = 0; let j = 0;
  while (i < n && j < m) {
    if (x[i] === y[j]) { out.push({ t: '=', l: x[i] }); i++; j++; } else if (dp[i + 1][j] >= dp[i][j + 1]) { out.push({ t: '-', l: x[i++] }); } else out.push({ t: '+', l: y[j++] });
  }
  while (i < n) out.push({ t: '-', l: x[i++] });
  while (j < m) out.push({ t: '+', l: y[j++] });
  return out;
}
const diffHtml = (a, b) => `<div class="diff">${lineDiff(a, b).map((d) => `<div class="d${d.t === '+' ? 'a' : d.t === '-' ? 'r' : ''}">${d.t === '=' ? '&nbsp;' : d.t} ${esc(d.l)}</div>`).join('')}</div>`;

/* ---------------- ① 编剧室 ---------------- */
function writerPanel() {
  const s = S.snap; const p = s.project; const w = s.writer || {}; const pid = p.id;
  const bBusy = busyOf(`${pid}:beats`); const rBusy = busyOf(`${pid}:script_review`);
  const beats = (w.beats || []).map((b, i) => `<div class="beat-row" data-i="${i}">
      <input type="text" class="bt-time" value="${esc(b.time)}" placeholder="0-3秒"><input type="text" class="bt-name" value="${esc(b.name)}" placeholder="钩子">
      <textarea rows="2" class="bt-content" placeholder="这一拍发生什么">${esc(b.content)}</textarea><input type="text" class="bt-emo" value="${esc(b.emotion)}" placeholder="情绪">
      <button class="btn small ghost" data-act="beat-del" data-i="${i}">✕</button></div>`).join('');
  const last = w.last; const dims = w.dimensions || [];
  const card = last ? `<div class="score-card"><div class="score-total ${last.total >= w.threshold ? 'ok' : 'bad'}">${last.total}<span class="small">/100</span></div>
      <div class="grow">${dims.map((d) => { const v = (last.scores || {})[d.key] || 0; return `<div class="score-row"><span>${d.label}</span><div class="bar"><i style="width:${v}%;${v < 80 ? 'background:var(--warn)' : ''}"></i></div><b>${v}</b></div>`; }).join('')}</div></div>
    <div class="small muted" style="margin:6px 0">${esc(last.summary || '')}${w.reviewed ? '' : ' <span class="warn">（剧本在审稿后有修改）</span>'}</div>
    ${(last.issues || []).length ? `<ul class="issues">${last.issues.map((i) => `<li><b>${esc(dims.find((d) => d.key === i.dim)?.label || i.dim)}</b> ${esc(i.where)}：${esc(i.problem)} <span class="muted">→ ${esc(i.suggestion)}</span></li>`).join('')}</ul>` : ''}
    ${(last.changes || []).length ? `<div class="small">自动改稿：${last.changes.map(esc).join('；')}</div>` : ''}` : '<div class="muted small">还没有审稿。评分卡：钩子强度、冲突、节奏、逻辑、人设一致、台词，各 0~100 分。</div>';
  const versions = (w.versions || []).slice().reverse().map((v) => `<tr><td>${new Date(v.at * 1000).toLocaleString()}</td><td>${esc(v.label)}</td><td>${v.total ?? '-'}</td><td>${v.chars} 字</td>
      <td class="row"><button class="btn small ghost" data-act="ver-diff" data-i="${v.i}">与当前对比</button><button class="btn small ghost" data-act="ver-restore" data-i="${v.i}">恢复</button></td></tr>`).join('');
  return `<div class="panel" style="margin-top:16px"><div class="row between"><h3>编剧室</h3>
      <div class="row small"><label>题材</label>${sel(Object.entries(S.meta.genres || {}), p.genre || 'general', `data-save="projects:${pid}:genre" data-rerender="1" style="width:130px"`)}</div></div>
    <div class="two">
      <div><div class="row between"><b>节拍表</b><span class="row">
          <button class="btn small" data-act="gen-beats" ${bBusy ? 'disabled' : ''}>${bBusy ? '设计中…' : (beats ? '重新设计' : 'AI 设计节拍表')}</button>
          <button class="btn small ghost" data-act="beat-add">＋ 节拍</button>${beats ? '<button class="btn small ghost" data-act="beats-save">保存</button>' : ''}</span></div>
        <p class="muted small">先定节拍（3 秒钩子 → 冲突升级 → 反转 → 结尾卡点），确认后再写剧本；生成剧本时会严格按节拍展开。${w.logline ? `<br>卖点：<b>${esc(w.logline)}</b>` : ''}</p>
        <div id="beats">${beats || '<div class="muted small">暂无节拍表（不做也可以直接写剧本）</div>'}</div></div>
      <div><div class="row between"><b>评分卡</b><span class="row">
          <button class="btn small" data-act="review-script" data-fix="1" ${p.script && !rBusy ? '' : 'disabled'} title="低于阈值时自动按意见改稿，最多 ${w.max_rounds} 轮">${rBusy ? '审稿中… ' + rBusy.progress + '%' : '审稿 + 自动改稿'}</button>
          <button class="btn small ghost" data-act="review-script" data-fix="0" ${p.script && !rBusy ? '' : 'disabled'}>只打分</button></span></div>
        <div class="row small" style="margin:6px 0">合格线 <input type="number" min="50" max="100" value="${w.threshold}" id="wr-th" style="width:60px"> 分，自动改稿最多
          <input type="number" min="0" max="3" value="${w.max_rounds}" id="wr-rounds" style="width:50px"> 轮 <button class="btn small ghost" data-act="writer-cfg">保存</button></div>
        ${card}</div></div>
    ${versions ? `<details style="margin-top:10px"><summary class="small">剧本版本（${(w.versions || []).length}）</summary><table>${versions}</table></details>` : ''}</div>`;
}
function readBeats() {
  return [...document.querySelectorAll('#beats .beat-row')].map((r) => ({ time: $('.bt-time', r).value, name: $('.bt-name', r).value, content: $('.bt-content', r).value, emotion: $('.bt-emo', r).value }));
}

function bioBox(c) {
  const f = (key, label, ph) => `<div><label class="f">${label}</label><input type="text" data-save="characters:${c.id}:${key}" value="${esc(c[key] || '')}" placeholder="${ph}"></div>`;
  const filled = ['personality', 'motivation', 'relations', 'speech', 'catchphrase'].filter((k) => (c[k] || '').trim()).length;
  return `<details class="bio" ${filled ? '' : ''}><summary class="small">人设卡 <span class="muted">${filled}/5</span>（剧本改写、分镜台词、台词打磨都会参考）</summary>
    <div class="grid2">${f('personality', '性格', '外冷内热，嘴硬心软')}${f('motivation', '动机', '想找回失踪的妹妹')}
    ${f('relations', '关系', '林晚的前男友')}${f('speech', '说话方式', '话少、句子短、不爱解释')}${f('catchphrase', '口头禅', '无所谓')}</div></details>`;
}

/* ---------------- 修改建议（台词打磨 / 导演批注） ---------------- */
const SRC_LABEL = { polish: '台词打磨', director: '导演批注', rules: '镜头规则' };
function sugValue(v) {
  if (Array.isArray(v)) return v.map((d) => (d && typeof d === 'object' ? `${d.speaker || ''}：${d.line || ''}${d.emotion ? `（${d.emotion}）` : ''}` : String(d))).join('\n') || '（无）';
  if (v && typeof v === 'object') return JSON.stringify(v, null, 1);
  return String(v ?? '') || '（空）';
}
function suggestPanel(sources) {
  const list = (S.snap.suggestions || []).filter((x) => !sources || sources.includes(x.source));
  if (!list.length) return '';
  const FIELD = { dialogue: '台词', shot_size: '景别', camera_angle: '角度', camera_move: '运镜', duration: '时长', description: '画面描述', action: '镜头内动作', performance: '表演指导', transition: '转场' };
  const items = list.map((x) => `<div class="sug"><div class="row between"><b>${esc(x.label || '')} · ${esc(FIELD[x.field] || x.field)}</b><span class="muted small">${esc(SRC_LABEL[x.source] || x.source)}</span></div>
      <div class="small muted">${esc(x.reason)}</div>
      <div class="sug-cmp"><div class="old">${esc(sugValue(x.old_value))}</div><div class="arrow">→</div><div class="new">${esc(sugValue(x.new_value))}</div></div>
      <div class="row" style="justify-content:flex-end"><button class="btn small" data-act="sug-accept" data-id="${x.id}">接受</button><button class="btn small ghost" data-act="sug-reject" data-id="${x.id}">拒绝</button></div></div>`).join('');
  return `<div class="panel sug-panel" style="margin-bottom:16px"><div class="row between"><h3>修改建议 <span class="muted small">${list.length} 条待处理</span></h3>
      <span class="row"><button class="btn small" data-act="sug-all" data-a="accept">全部接受</button><button class="btn small ghost" data-act="sug-all" data-a="reject">全部拒绝</button></span></div>
    <div class="sug-list">${items}</div></div>`;
}
function overlongTag(x) {
  const lines = (x.dialogue || []).map((d) => String(d.line || '')).join('').replace(/[\s，。！？、；：,.!?;:…—“”"'（）()]/g, '');
  const lim = Math.max(0, (x.duration || 0) * 4 - 2);
  return lines.length > lim && lines.length ? `<span class="bad small" title="按每秒约 4 字估算">⚠ 台词 ${lines.length} 字，超出 ${x.duration}s 镜头（建议 ≤ ${Math.floor(lim)} 字）</span>` : '';
}

Object.assign(ACTIONS, {
  async 'gen-beats'() { submitted(await api('POST', `/api/projects/${S.pid}/beats`)); },
  'beat-add'() { const w = S.snap.writer; w.beats = [...readBeats(), { time: '', name: '', content: '', emotion: '' }]; renderProject(); },
  async 'beat-del'(el) { const b = readBeats(); b.splice(Number(el.dataset.i), 1); await api('PUT', `/api/projects/${S.pid}/beats`, { beats: b }); loadSnap(true); },
  async 'beats-save'() { await api('PUT', `/api/projects/${S.pid}/beats`, { beats: readBeats() }); toast('节拍表已保存'); loadSnap(true); },
  async 'review-script'(el) { submitted(await api('POST', `/api/projects/${S.pid}/script/review`, { auto_fix: el.dataset.fix === '1' })); },
  async 'writer-cfg'() { await api('PATCH', `/api/projects/${S.pid}/writer`, { threshold: Number(val('wr-th')), max_rounds: Number(val('wr-rounds')) }); toast('已保存'); loadSnap(true); },
  async 'ver-diff'(el) {
    const v = await api('GET', `/api/projects/${S.pid}/script/versions/${el.dataset.i}`);
    modal(`<h3>版本对比：${esc(v.label)} → 当前剧本</h3><p class="muted small">红色为该版本有、当前没有；绿色为当前新增。</p>${diffHtml(v.script, S.snap.project.script)}
      <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="btn ghost" data-act="close-modal">关闭</button><button class="btn" data-act="ver-restore" data-i="${el.dataset.i}">恢复这个版本</button></div>`);
  },
  async 'ver-restore'(el) { if (!confirm('用这个版本替换当前剧本？（当前剧本会存为一个版本）')) return; closeModal(); await api('POST', `/api/projects/${S.pid}/script/versions/${el.dataset.i}/restore`); toast('已恢复'); loadSnap(true); },
  async polish() { submitted(await api('POST', `/api/projects/${S.pid}/polish`, {}), '已提交台词打磨，结果会作为修改建议出现在上方'); },
  async 'sug-accept'(el) { const r = await api('POST', `/api/suggestions/${el.dataset.id}/accept`); toast(r.conflict ? '已应用（该内容之前被手动改过，已覆盖）' : '已应用'); loadSnap(true); },
  async 'sug-reject'(el) { await api('POST', `/api/suggestions/${el.dataset.id}/reject`); loadSnap(true); },
  async 'sug-all'(el) { const r = await api('POST', `/api/projects/${S.pid}/suggestions/bulk`, { action: el.dataset.a }); toast(`已处理 ${r.done} 条`); loadSnap(true); },
});

/* ---------------- ③ 导演审校 ---------------- */
function directorPanel() {
  const d = S.snap.director || {}; const issues = d.issues || []; const last = d.last; const pid = S.snap.project.id;
  if (!S.snap.shots.length) return '';
  const busy = busyOf(`${pid}:director`);
  const warn = issues.filter((x) => x.level === 'warn').length;
  const list = issues.map((x) => `<li class="${x.level === 'warn' ? 'warn' : 'muted'}"><b>【${esc(x.label)}】</b>#${x.shots.join('、#')} ${esc(x.message)}${x.fix ? ' <span class="ok small">可自动修</span>' : ''}</li>`).join('');
  const curve = last && (last.curve || []).length ? `<div class="curve" title="情绪曲线（导演判断的每镜情绪强度）">${last.curve.map((c) => `<i style="height:${c.intensity * 10}%" title="#${c.index}：${c.intensity}"></i>`).join('')}</div>` : '';
  return `<div class="panel" style="margin-bottom:16px"><div class="row between"><h3>导演审校 <span class="small ${warn ? 'warn' : 'ok'}">${issues.length ? `规则检查 ${warn} 个问题${issues.length - warn ? `、${issues.length - warn} 个提示` : ''}` : '✓ 规则检查通过'}</span></h3>
      <span class="row"><button class="btn small ghost" data-act="rules-fix" ${issues.some((x) => x.fix) ? '' : 'disabled'}>把可修复的问题转成修改建议</button>
      <button class="btn small" data-act="director" ${busy ? 'disabled' : ''} title="逐镜表演指导、转场、节奏，并按镜头语言规则给出修改建议">${busy ? '导演审校中…' : 'AI 导演审校'}</button></span></div>
    <p class="muted small">规则：轴线（180°）、景别交替、对话正反打、场景开头建立镜头、动作接剪、台词时长。AI 导演会给每个镜头写表演指导（表情、动作节拍、视线）、按情绪曲线调节奏、选转场，所有修改都先作为建议，逐条确认。</p>
    ${list ? `<details ${warn ? 'open' : ''}><summary class="small">问题列表</summary><ul class="issues">${list}</ul></details>` : ''}
    ${last ? `<div class="small" style="margin-top:6px">上次审校：${esc(last.summary || '')}</div>${curve}` : ''}</div>`;
}
function shotDirectorFields(x, i) {
  return `<div class="grid2" style="margin-top:4px"><div><label class="f">表演指导（表情 / 动作节拍 / 视线）</label><input type="text" data-save="shots:${x.id}:performance" value="${esc(x.performance || '')}" placeholder="先低头沉默，再抬眼直视对方"></div>
    <div><label class="f">与上一镜的转场</label>${sel(S.meta.transitions || ['硬切'], x.transition || '硬切', `data-save="shots:${x.id}:transition" ${i ? '' : 'disabled'}`)}</div></div>`;
}
Object.assign(ACTIONS, {
  async director() { submitted(await api('POST', `/api/projects/${S.pid}/director`), '已提交导演审校，结果会作为修改建议出现'); },
  async 'rules-fix'() { const r = await api('POST', `/api/projects/${S.pid}/director/fix`); toast(`生成 ${r.suggestions} 条修改建议`); loadSnap(true); },
});

/* ---------------- ② 美术与选角：三视图/表情、服装造型、场景变体、画风、素材库 ---------------- */
TABLE_URL.outfits = '/api/outfits/';
const outfitsOf = (cid) => (S.snap.outfits || []).filter((o) => o.character_id === cid);
const scenesOf = () => [...new Set(S.snap.shots.map((x) => x.scene || '').filter(Boolean))];
function sheetBox(c) {
  const sh = c.sheet || {}; const ex = sh.expr || {}; const views = S.meta.views || { side: '侧面', back: '背面' };
  const slot = (path, label) => `<div class="slot" ${path ? `data-act="zoom" data-src="${media(path)}" style="background-image:url('${media(path)}')"` : ''}><span>${esc(label)}</span></div>`;
  const items = [...Object.entries(views).map(([k, l]) => slot(sh[k], l)), ...(S.meta.expressions || []).map((e) => slot(ex[e], e))];
  const cands = [...assetsOf(c.id, 'view'), ...assetsOf(c.id, 'expression')];
  const used = new Set([...Object.values(views).map((_, i) => sh[Object.keys(views)[i]]), ...Object.values(ex)]);
  const done = Object.keys(views).filter((k) => sh[k]).length + Object.keys(ex).length;
  return `<details class="art-box"><summary class="small">三视图与表情 <span class="muted">${done}/${items.length}</span></summary>
    <p class="muted small" style="margin:4px 0">镜头里角色背对/侧对镜头时自动用对应视图做参考；台词情绪匹配到表情时用对应表情特写锁五官。</p>
    <div class="sheet">${items.join('')}</div>
    ${cands.length ? `<div class="thumbs">${cands.map((a) => { const m = a.meta || {}; return `<div class="thumb ${used.has(a.path) ? 'sel' : ''}" title="${esc((views[m.view] || m.expression || '') + '：点击设为当前')}" data-act="select-asset" data-id="${a.id}" style="background-image:url('${media(a.path)}')"><span class="x" data-act="del-asset" data-id="${a.id}">✕</span></div>`; }).join('')}</div>` : ''}
    <div class="row" style="margin-top:6px"><button class="btn small" data-act="gen-sheet" data-id="${c.id}" ${c.ref_image ? '' : 'disabled title="先生成定妆照"'}>${done ? '补齐缺失' : '一键生成'}</button>
      <button class="btn small ghost" data-act="gen-sheet" data-id="${c.id}" data-all="1" ${c.ref_image ? '' : 'disabled'}>全部重做</button></div></details>`;
}
function outfitBox(c) {
  const os = outfitsOf(c.id); const scenes = scenesOf();
  const rows = os.map((o) => `<div class="outfit"><div class="ref mini" ${o.ref_image ? `data-act="zoom" data-src="${media(o.ref_image)}" style="background-image:url('${media(o.ref_image)}')"` : ''}>${o.ref_image ? '' : '无图'}${busyBox(o.id)}</div>
    <div class="grow"><div class="row"><input type="text" class="grow" data-save="outfits:${o.id}:name" value="${esc(o.name)}"><button class="btn small ghost" data-act="del-outfit" data-id="${o.id}">删除</button></div>
      <textarea rows="2" data-save="outfits:${o.id}:appearance" placeholder="这套造型的服装、发型、妆容">${esc(o.appearance)}</textarea>
      ${thumbs(o.id, 'outfit', o.ref_image)}
      <div class="row"><button class="btn small" data-act="gen-outfit" data-id="${o.id}">生成定妆照</button>
        ${scenes.length ? sel([['', '应用到场次…'], ['*', '全部镜头'], ...scenes.map((s) => [s, s])], '', `data-outfit-scene="${c.id}:${o.id}" style="width:150px"`) : ''}</div></div></div>`).join('');
  return `<details class="art-box" ${os.length ? 'open' : ''}><summary class="small">服装造型 <span class="muted">${os.length} 套</span></summary>
    <p class="muted small" style="margin:4px 0">换装、回忆、变身等用不同造型；按场次指定后，该场镜头出图用这套定妆照。</p>${rows}
    <button class="btn small ghost" data-act="add-outfit" data-id="${c.id}">＋ 造型</button></details>`;
}
function variantBox(l) {
  const v = l.variants || {}; const names = [...new Set([...(S.meta.variants || []), ...Object.keys(v)])];
  return `<label class="f">场景变体 <span class="muted small">同一地点的昼夜/天气，镜头描述里出现「夜、雨…」自动选用</span></label>
    <div class="chips">${names.map((n) => v[n] ? `<span class="chip on" data-act="zoom" data-src="${media(v[n])}" title="点击查看">✓ ${esc(n)}</span>` : `<span class="chip" data-act="gen-variant" data-id="${l.id}" data-v="${esc(n)}" title="以场景图为基础生成${esc(n)}版本" ${l.ref_image ? '' : 'style="opacity:.5"'}>＋ ${esc(n)}</span>`).join('')}</div>
    ${assetsOf(l.id, 'loc_variant').length ? thumbs(l.id, 'loc_variant', '') : ''}`;
}
function styleBox() {
  const p = S.snap.project;
  return `<div class="panel" style="margin-top:16px"><h3>画风锁定 <span class="muted small">所有出图共用，保证整片画风统一</span></h3>
    <div class="row" style="align-items:flex-start;gap:16px">
      <div><div class="ref mini" ${p.style_ref ? `data-act="zoom" data-src="${media(p.style_ref)}" style="background-image:url('${media(p.style_ref)}')"` : ''}>${p.style_ref ? '' : '无'}</div>
        <div class="row" style="margin-top:6px"><button class="btn small ghost" data-act="upload-style">上传画风参考图</button>${p.style_ref ? '<button class="btn small ghost" data-act="clear-style">不用</button>' : ''}</div></div>
      <div class="grow"><p class="muted small" style="margin-top:0">画风参考图会作为额外参考图交给 Qwen-Image（只学画风色调，不学内容）。也可以指定风格 LoRA（放到 ComfyUI/models/loras/ 下）。</p>
        <div class="grid2"><div><label class="f">风格 LoRA 文件名</label><input type="text" data-save="projects:${p.id}:style_lora" value="${esc(p.style_lora || '')}" placeholder="如 my_style.safetensors"></div>
        <div><label class="f">LoRA 强度</label><input type="number" step="0.05" min="0" max="2" data-num="1" data-save="projects:${p.id}:style_lora_strength" value="${p.style_lora_strength ?? 0.8}"></div></div></div></div></div>`;
}
function shotArtFields(x) {
  const s = S.snap; const loc = s.locations.find((l) => l.id === x.location_id);
  const outs = x.character_ids.map((cid) => [s.characters.find((c) => c.id === cid), outfitsOf(cid)]).filter(([c, os]) => c && os.length);
  const vars = Object.keys((loc && loc.variants) || {});
  if (!outs.length && !vars.length) return '';
  return `<div class="row" style="margin-top:6px;flex-wrap:wrap">${outs.map(([c, os]) => `<span class="small">${esc(c.name)} 造型 ${sel([['', '默认'], ...os.map((o) => [o.id, o.name])], (x.outfits || {})[c.id] || '', `data-shot-outfit="${x.id}:${c.id}" style="width:110px"`)}</span>`).join('')}
    ${vars.length ? `<span class="small">场景版本 ${sel([['', '自动'], ['原图', '原图'], ...vars.map((v) => [v, v])], x.location_variant || '', `data-save="shots:${x.id}:location_variant" style="width:100px"`)}</span>` : ''}</div>`;
}
document.addEventListener('change', async (e) => {
  const el = e.target;
  if (el.dataset.shotOutfit) {
    const [sid, cid] = el.dataset.shotOutfit.split(':');
    await api('POST', `/api/projects/${S.pid}/outfits/assign`, { character_id: cid, outfit_id: el.value, shot_ids: [sid] }); toast('已保存'); loadSnap(true);
  } else if (el.dataset.outfitScene && el.value) {
    const [cid, oid] = el.dataset.outfitScene.split(':');
    const r = await api('POST', `/api/projects/${S.pid}/outfits/assign`, { character_id: cid, outfit_id: oid, scene: el.value === '*' ? null : el.value });
    toast(`已应用到 ${r.shots} 个镜头`); loadSnap(true);
  }
});
async function libraryModal() {
  const items = await api('GET', '/api/library'); const kinds = { character: '角色', location: '场景', prop: '道具' };
  modal(`<h3>素材库 <span class="muted small">跨项目复用定妆照、三视图、造型和场景图（系列剧同一角色）</span></h3>
    ${items.length ? `<div class="lib-grid">${items.map((it) => { const img = (it.images || {}).ref_image; return `<label class="lib-item"><input type="checkbox" value="${it.id}">
      <div class="ref mini" style="${img ? `background-image:url('${media(img)}')` : ''}"></div><div><b>${esc(it.name)}</b><div class="muted small">${kinds[it.kind] || it.kind}</div></div>
      <span class="x" data-act="lib-del" data-id="${it.id}" title="从素材库删除">✕</span></label>`; }).join('')}</div>` : '<div class="empty">素材库是空的。在角色/场景卡片上点「存入素材库」</div>'}
    <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="btn ghost" data-act="close-modal">关闭</button><button class="btn" data-act="lib-import" ${items.length ? '' : 'disabled'}>导入选中</button></div>`);
}
Object.assign(ACTIONS, {
  async 'gen-sheet'(el) {
    const c = S.snap.characters.find((x) => x.id === el.dataset.id); const sh = c.sheet || {}; const ex = sh.expr || {};
    const body = el.dataset.all ? {} : { views: Object.keys(S.meta.views || {}).filter((k) => !sh[k]), expressions: (S.meta.expressions || []).filter((k) => !ex[k]) };
    if (body.views && !body.views.length && !body.expressions.length) return toast('已经齐了，要重做请点「全部重做」');
    submitted(await api('POST', `/api/characters/${c.id}/sheet`, body));
  },
  async 'add-outfit'(el) { const name = prompt('造型名称（如：西装、校服、古装、受伤后）'); if (!name) return; await api('POST', `/api/characters/${el.dataset.id}/outfits`, { name }); loadSnap(true); },
  async 'del-outfit'(el) { if (!confirm('删除这套造型？用到它的镜头会恢复默认造型')) return; await api('DELETE', `/api/outfits/${el.dataset.id}`); loadSnap(true); },
  async 'gen-outfit'(el) { submitted(await api('POST', `/api/outfits/${el.dataset.id}/image`)); },
  async 'gen-variant'(el) { submitted(await api('POST', `/api/locations/${el.dataset.id}/variant`, { variant: el.dataset.v })); },
  async 'upload-style'() { const f = await pickFile('image/*'); if (f) { await upload(`/api/projects/${S.pid}/style_ref`, f); loadSnap(true); } },
  async 'clear-style'() { await api('PATCH', `/api/projects/${S.pid}`, { style_ref: '' }); loadSnap(true); },
  async 'lib-save'(el) { await api('POST', '/api/library', { kind: el.dataset.k, id: el.dataset.id }); toast('已存入素材库（同名会覆盖）'); },
  library: libraryModal,
  async 'lib-del'(el) { if (!confirm('从素材库删除？')) return; await api('DELETE', `/api/library/${el.dataset.id}`); libraryModal(); },
  async 'lib-import'() {
    const ids = [...document.querySelectorAll('.lib-item input:checked')].map((i) => i.value); if (!ids.length) return toast('先勾选要导入的');
    const r = await api('POST', `/api/projects/${S.pid}/library/import`, { ids }); closeModal(); toast(`已导入 ${r.imported} 项（已锁定）`); loadSnap(true);
  },
});

/* ---------------- ③ 分镜增强：站位、姿势参考、局部重绘、多版本对比 ---------------- */
function shotLayoutFields(x) {
  const s = S.snap; const chars = x.character_ids.map((cid) => s.characters.find((c) => c.id === cid)).filter(Boolean);
  const pos = x.positions || {};
  return `<div class="row" style="margin-top:6px;flex-wrap:wrap;gap:10px">${chars.map((c) => `<span class="small">${esc(c.name)} 站位 ${sel([['', '自动'], ...(S.meta.positions || []).map((p) => [p, p])], pos[c.id] || '', `data-pos="${x.id}:${c.id}" style="width:80px"`)}</span>`).join('')}
    <span class="small">姿势参考 ${x.pose_ref ? `<span class="pose-thumb" data-act="zoom" data-src="${media(x.pose_ref)}" style="background-image:url('${media(x.pose_ref)}')"></span><button class="btn small ghost" data-act="pose-clear" data-id="${x.id}">不用</button>` : ''}
      <button class="btn small ghost" data-act="pose-upload" data-id="${x.id}" title="上传一张姿势/构图参考（真人照片、火柴人、其他画面都行），只学姿势不学长相">${x.pose_ref ? '换' : '上传'}</button></span></div>`;
}
document.addEventListener('change', async (e) => {
  const el = e.target; if (!el.dataset.pos) return;
  const [sid, cid] = el.dataset.pos.split(':'); const shot = S.snap.shots.find((x) => x.id === sid);
  const positions = { ...(shot.positions || {}) }; if (el.value) positions[cid] = el.value; else delete positions[cid];
  Object.assign(shot, await api('PATCH', `/api/shots/${sid}`, { positions })); toast('已保存');
});
async function compareModal(sid, kind) {
  const r = await api('GET', `/api/shots/${sid}/compare?kind=${kind}`); const vid = kind === 'video';
  const card = (it) => `<div class="cmp ${it.current ? 'sel' : ''}">
    ${vid ? `<video src="${media(it.path)}" muted loop preload="metadata" controls></video>` : `<img src="${media(it.path)}" data-act="zoom" data-src="${media(it.path)}">`}
    <div class="small"><b>#${it.n}</b>${it.current ? ' <span class="ok">当前使用</span>' : ''}${it.qa_ok === true ? ' <span class="ok">✓质检</span>' : it.qa_ok === false ? ` <span class="warn" title="${esc(it.qa.join('；'))}">⚠ ${esc(it.qa[0] || '质检未过')}</span>` : ''}
      ${it.edit ? `<div class="muted">${it.region ? '局部重绘' : '改图'}：${esc(it.edit)}</div>` : ''}${it.mode ? `<div class="muted">${esc(it.mode)}</div>` : ''}</div>
    <div class="row">${it.current ? '' : `<button class="btn small" data-act="cmp-pick" data-id="${it.id}" data-s="${sid}" data-k="${kind}">用这个</button>`}
      ${vid ? '' : `<button class="btn small ghost" data-act="cmp-pose" data-id="${it.id}" data-s="${sid}" title="用这张图的姿势做参考重新出图">设为姿势参考</button>`}</div></div>`;
  modal(`<div class="row between"><h3>多版本对比 · ${vid ? '视频' : '分镜图'}（${r.items.length}）</h3>${vid ? '<button class="btn small ghost" data-act="cmp-play">同步播放</button>' : ''}</div>
    <div class="cmp-grid">${r.items.map(card).join('') || '<div class="empty">还没有候选</div>'}</div>
    <div class="row" style="justify-content:flex-end;margin-top:12px"><button class="btn ghost" data-act="close-modal">关闭</button></div>`);
  $('#modal-box').classList.add('wide');
}
function regionModal(sid) {
  const x = S.snap.shots.find((s) => s.id === sid); const chars = x.character_ids.map((cid) => S.snap.characters.find((c) => c.id === cid)).filter(Boolean);
  modal(`<h3>局部重绘 <span class="muted small">在图上拖出要改的区域，只重画这一块，其余像素不变</span></h3>
    <div class="region-wrap"><img id="rg-img" src="${media(x.keyframe)}" draggable="false"><div id="rg-box" class="hidden"></div></div>
    <label class="f">这一块改成什么</label><input type="text" id="rg-ins" placeholder="例如：手里的杯子换成红色保温杯 / 表情改成微笑 / 去掉背景里的路人">
    ${chars.length ? `<label class="f">区域里有这些角色（带上其参考图，重画脸/手时选上）</label><div class="chips" id="rg-chars">${chars.map((c) => `<span class="chip" data-c="${c.id}">${esc(c.name)}</span>`).join('')}</div>` : ''}
    <div class="row" style="justify-content:flex-end;margin-top:12px"><select id="rg-n" style="width:70px"><option value="1">1张</option><option value="2">2张</option></select>
      <button class="btn ghost" data-act="close-modal">取消</button><button class="btn" data-act="rg-go" data-id="${sid}">重绘</button></div>`);
  $('#modal-box').classList.add('wide');
  const img = $('#rg-img'); const box = $('#rg-box'); let st = null; S.rgBox = null;
  const pt = (e) => { const r = img.getBoundingClientRect(); return [Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), Math.min(1, Math.max(0, (e.clientY - r.top) / r.height))]; };
  img.onmousedown = (e) => { st = pt(e); e.preventDefault(); };
  window.onmousemove = (e) => { if (!st) return; const p = pt(e); const b = [Math.min(st[0], p[0]), Math.min(st[1], p[1]), Math.abs(p[0] - st[0]), Math.abs(p[1] - st[1])];
    S.rgBox = b; box.classList.remove('hidden'); Object.assign(box.style, { left: b[0] * 100 + '%', top: b[1] * 100 + '%', width: b[2] * 100 + '%', height: b[3] * 100 + '%' }); };
  window.onmouseup = () => { st = null; };
  const ch = $('#rg-chars'); if (ch) ch.onclick = (e) => { const c = e.target.closest('.chip'); if (c) c.classList.toggle('on'); };
}
Object.assign(ACTIONS, {
  async 'pose-upload'(el) { const f = await pickFile('image/*'); if (f) { await upload(`/api/shots/${el.dataset.id}/pose`, f); loadSnap(true); } },
  async 'pose-clear'(el) { await api('PATCH', `/api/shots/${el.dataset.id}`, { pose_ref: '' }); loadSnap(true); },
  compare(el) { compareModal(el.dataset.id, el.dataset.k || 'keyframe'); },
  async 'cmp-pick'(el) { await api('POST', '/api/assets/select', { asset_id: el.dataset.id }); await loadSnap(true); compareModal(el.dataset.s, el.dataset.k); },
  async 'cmp-pose'(el) { await api('POST', `/api/shots/${el.dataset.s}/pose/from_keyframe`, { asset_id: el.dataset.id }); toast('已设为姿势参考，重新出图时生效'); loadSnap(true); },
  'cmp-play'() { document.querySelectorAll('.cmp video').forEach((v) => { v.currentTime = 0; v.play(); }); },
  region(el) { regionModal(el.dataset.id); },
  async 'rg-go'(el) {
    const ins = val('rg-ins').trim(); if (!S.rgBox || S.rgBox[2] < 0.02 || S.rgBox[3] < 0.02) return toast('先在图上拖出要改的区域'); if (!ins) return toast('写一下要改成什么');
    const ids = [...document.querySelectorAll('#rg-chars .chip.on')].map((c) => c.dataset.c);
    closeModal(); submitted(await api('POST', `/api/shots/${el.dataset.id}/keyframe/region`, { instruction: ins, box: S.rgBox, character_ids: ids, count: Number(val('rg-n')) }));
  },
});

/* ---------------- ④ 视频增强：运镜方向/速度、视频延长、动作参考 ---------------- */
function cameraFields(x) {
  const dirs = (S.meta.camera_dirs || {})[x.camera_move];
  if (!dirs && (x.camera_move || '固定') === '固定') return '';
  return `<div class="row small" style="margin-top:6px;gap:10px">${dirs ? `<span>运镜方向 ${sel([['', '默认'], ...dirs.map((d) => [d, d])], x.camera_dir || '', `data-save="shots:${x.id}:camera_dir" style="width:80px"`)}</span>` : ''}
    <span>运镜速度 ${sel([['', '缓慢'], ['快', '快速']], x.camera_speed || '', `data-save="shots:${x.id}:camera_speed" style="width:80px"`)}</span></div>`;
}
function videoExtras(x) {
  const m = S.meta.motion;
  const enh = ((assetsOf(x.id, 'video').find((a) => a.path === x.video) || {}).meta || {}).enhanced || [];
  const E = S.meta.enhance || {};
  return `<details style="margin-top:6px"><summary class="muted small">延长 / 动作参考 / 增强${x.motion_ref ? ' · <span class="ok">已有动作参考</span>' : ''}${enh.length ? ` · <span class="ok">${esc(enh.join('、'))}</span>` : ''}</summary>
    <div class="row" style="margin-top:6px">${sel([['1080p', '超分到 1080p'], ['1440p', '超分到 1440p'], ['4k', '超分到 4K']], '1080p', `id="up-t-${x.id}" style="width:130px"`)}
      <button class="btn small ghost" data-act="shot-upscale" data-id="${x.id}" ${x.video && (E.esrgan || E.seedvr2) ? '' : 'disabled'} title="${E.esrgan || E.seedvr2 ? '生成超分版本（可在版本里切回原版）' : '未安装超分组件：bash deploy/10_enhance.sh'}">超分</button>
      ${sel([[48, '补帧到 48fps'], [60, '补帧到 60fps'], [30, '补帧到 30fps']], 48, `id="fi-f-${x.id}" style="width:130px"`)}
      <button class="btn small ghost" data-act="shot-interp" data-id="${x.id}" ${x.video ? '' : 'disabled'} title="${E.rife ? 'RIFE 补帧' : '未安装 RIFE，将用 ffmpeg 运动补偿插帧（CPU，较慢）'}">补帧</button></div>
    <div class="row" style="margin-top:6px"><input type="text" class="grow" id="ve-ins-${x.id}" placeholder="接下来的动作（可空），如：他转身走向门口">
      ${sel([[3, '+3秒'], [4, '+4秒'], [6, '+6秒'], [8, '+8秒']], 4, `id="ve-s-${x.id}" style="width:80px"`)}
      <button class="btn small" data-act="video-extend" data-id="${x.id}" ${x.video ? '' : 'disabled'} title="取当前视频最后一帧续写，再拼成一个长镜头">延长</button></div>
    <div class="row" style="margin-top:6px">${x.motion_ref ? `<video src="${media(x.motion_ref)}" muted preload="metadata" style="width:90px;border-radius:4px"></video>` : ''}
      <button class="btn small ghost" data-act="motion-upload" data-id="${x.id}" title="上传一段动作参考视频（舞蹈、打斗、手势…）">${x.motion_ref ? '换参考视频' : '上传动作参考视频'}</button>
      <button class="btn small ghost" data-act="motion-gen" data-id="${x.id}" ${m && x.motion_ref && x.keyframe ? '' : 'disabled'} title="${m ? '用参考视频驱动分镜图里的人物动作' : '未配置动作参考工作流（data/workflows/motion.json）'}">按参考动作生成</button></div></details>`;
}
Object.assign(ACTIONS, {
  async 'video-extend'(el) { const id = el.dataset.id; submitted(await api('POST', `/api/shots/${id}/video/extend`, { instruction: val(`ve-ins-${id}`), seconds: Number(val(`ve-s-${id}`)), quality: val(`vq-${id}`) || 'draft', turbo: !!val(`vt-${id}`) })); },
  async 'motion-upload'(el) { const f = await pickFile('video/*'); if (f) { await upload(`/api/shots/${el.dataset.id}/motion`, f); loadSnap(true); } },
  async 'motion-gen'(el) { submitted(await api('POST', `/api/shots/${el.dataset.id}/video/motion`, { quality: val(`vq-${el.dataset.id}`) || 'draft' })); },
});

/* ---------------- 视觉审片：整片报告、逐镜结果、一键返工 ---------------- */
const scoreCls = (v) => (v >= 8 ? 'ok' : v >= 6 ? 'warn' : 'bad');
function reviewPanel() {
  const p = S.snap.project; const rep = p.review_report || {}; const on = S.meta.vlm; const busy = (S.active || []).find((t) => t.type === 'film_review' && t.project_id === p.id);
  const labels = { count: '人数', identity: '角色', action: '动作表情', anatomy: '肢体畸形', artifact: '画面崩坏', props: '道具', text: '乱码文字', flicker: '闪烁跳变', camera: '运镜', audio: '音画' };
  const issues = Object.entries(rep.issues || {}).map(([k, v]) => `${labels[k] || k} ${v}`).join(' · ');
  return `<div class="panel" style="margin-bottom:16px"><div class="row between"><h3>视觉审片 ${rep.at ? `<span class="small ${scoreCls(rep.avg || 0)}">平均 ${rep.avg ?? '-'} 分 · 审了 ${rep.reviewed}/${rep.shots} 镜 · ${(rep.bad || []).length ? `不合格 #${rep.bad.join('、#')}` : '全部合格'}</span>` : ''}</h3>
    <span class="row"><button class="btn small ghost" data-act="rework" ${(rep.bad || []).length ? '' : 'disabled'} title="把审片指出的问题和修正描述写进提示词，重抽不合格的镜头">一键返工</button>
      <button class="btn small" data-act="film-review" ${on && !busy ? '' : 'disabled'}>${busy ? '审片中…' : '整片审片'}</button></span></div>
    <p class="muted small">${on ? `视觉模型（${S.meta.vlm_provider === 'api' ? '接口' : '本地 Qwen3.6 + 视觉投影'}）逐镜检查：人数、角色、动作表情、手指肢体、画面崩坏、道具、乱码文字${S.meta.asr ? '、台词音画核对' : ''}；视频抽 6 帧看闪烁和人脸突变。${S.meta.vlm_provider === 'api' ? '出图/出视频后也会即时审，不合格自动重抽。' : ''}` : '未配置视觉模型：bash deploy/08_vlm.sh（本地）或在 .env 设置 VLM_BASE_URL（接口），见 docs/DEPLOY.md'}</p>
    ${issues ? `<div class="small">问题分布：${issues}</div>` : ''}</div>`;
}
function reviewLine(x, target) {
  const r = x.review || {};
  if (!r.at || r.target !== target || r.path !== x[target]) return '';
  return `<details class="small" style="margin-top:4px"><summary><span class="${scoreCls(r.score)}">审片 ${r.score} 分${r.ok ? ' ✓' : ' ⚠ 需返工'}</span>${(r.issues || []).length ? ` · ${r.issues.length} 个问题` : ''}</summary>
    <ul class="issues">${(r.issues || []).map((i) => `<li class="${i.severity === 'high' ? 'warn' : 'muted'}">【${esc(i.label)}】${esc(i.detail)}</li>`).join('')}</ul>
    ${r.fix ? `<div class="muted">建议：${esc(r.fix)}</div>` : ''}${r.heard ? `<div class="muted">识别到的台词：${esc(r.heard)}</div>` : ''}
    <div class="row" style="margin-top:4px"><button class="btn small ghost" data-act="rework-one" data-id="${x.id}">按审片意见重做</button></div></details>`;
}
Object.assign(ACTIONS, {
  async 'film-review'() { submitted(await api('POST', `/api/projects/${S.pid}/review`, {}), '已提交整片审片'); },
  async rework() { const r = await api('POST', `/api/projects/${S.pid}/review/rework`, {}); toast(`已提交 ${r.tasks.length} 个返工任务`); loadSnap(true); },
  async 'rework-one'(el) { const r = await api('POST', `/api/projects/${S.pid}/review/rework`, { shot_ids: [el.dataset.id] }); toast(`已提交 ${r.tasks.length} 个返工任务`); loadSnap(true); },
});

/* ---------------- ⑤ 后期：调色 / 字幕包装 / 声音 / 画质（每个分区一个函数，按顺序拼起来） ---------------- */
const POST_SECTIONS = [];
function postPanel() { return `<div class="panel" style="margin-top:16px"><h3>后期设置 <span class="muted small">合成成片时生效，改完点「合成成片」</span></h3>${POST_SECTIONS.map((f) => f()).join('')}</div>`; }
const jget = (field, key, dflt) => { const o = S.snap.project[field] || {}; return o[key] === undefined ? dflt : o[key]; };
const jchk = (field, key, dflt, label, tip = '') => `<label class="row small" title="${esc(tip)}"><input type="checkbox" data-jsave="${field}.${key}" ${jget(field, key, dflt) ? 'checked' : ''}> ${label}</label>`;
document.addEventListener('change', async (e) => {
  const el = e.target; if (!el.dataset.jsave) return;
  const [field, key] = el.dataset.jsave.split('.'); const p = S.snap.project;
  let v = el.type === 'checkbox' ? el.checked : el.value; if (el.dataset.num) v = Number(v);
  const obj = { ...(p[field] || {}), [key]: v };
  Object.assign(p, await api('PATCH', `/api/projects/${p.id}`, { [field]: obj })); toast('已保存');
  if (el.dataset.rerender || key === 'enabled') renderProject();
});
POST_SECTIONS.push(() => {
  const s = S.snap; const on = jget('grade', 'enabled', false);
  const luts = Object.entries(S.meta.luts || { '': '不用' });
  return `<div class="post-sec"><div class="row between"><b>调色统一 / 去闪烁</b>${jchk('grade', 'enabled', false, '启用', '每个场次以基准镜头做色彩匹配，全片色调统一')}</div>
    <div class="grid4 ${on ? '' : 'dim'}" style="margin-top:6px">
      <div>${jchk('grade', 'match', true, '镜头间色彩匹配')}${jchk('grade', 'per_scene', true, '按场次分组匹配', '日景夜景不会被拉成一样')}</div>
      <div><label class="f">基准镜头</label>${sel([['', '自动（组内最接近平均色调）'], ...s.shots.map((x, i) => [x.id, `#${i + 1} ${x.scene || ''}`])], jget('grade', 'base_shot', ''), 'data-jsave="grade.base_shot"')}</div>
      <div><label class="f">风格 LUT</label>${sel(luts.map(([k, v]) => [k, k ? `${k}（${v}）` : v]), jget('grade', 'lut', ''), 'data-jsave="grade.lut"')}</div>
      <div><label class="f">匹配强度 ${jget('grade', 'strength', 0.7)}</label><input type="range" min="0.2" max="1" step="0.1" value="${jget('grade', 'strength', 0.7)}" data-jsave="grade.strength" data-num="1" data-rerender="1">
        ${jchk('grade', 'deflicker', false, '时域去闪烁')}</div></div></div>`;
});
const jtext = (field, key, dflt, ph = '', attrs = '') => `<input type="text" data-jsave="${field}.${key}" value="${esc(jget(field, key, dflt) ?? '')}" placeholder="${esc(ph)}" ${attrs}>`;
/* 字幕与包装：post.subtitle / post.title_card / post.end_card 是嵌套对象，用 data-jsave2="post.subtitle.preset" */
document.addEventListener('change', async (e) => {
  const el = e.target; if (!el.dataset.jsave2) return;
  const [field, sub, key] = el.dataset.jsave2.split('.'); const p = S.snap.project;
  let v = el.type === 'checkbox' ? el.checked : el.value; if (el.dataset.num) v = Number(v);
  const obj = { ...(p[field] || {}) }; obj[sub] = { ...(obj[sub] || {}), [key]: v };
  Object.assign(p, await api('PATCH', `/api/projects/${p.id}`, { [field]: obj })); toast('已保存');
  if (key === 'enabled') renderProject();
});
const pget = (sub, key, dflt) => { const o = ((S.snap.project.post || {})[sub]) || {}; return o[key] === undefined ? dflt : o[key]; };
const pchk = (sub, key, dflt, label) => `<label class="row small"><input type="checkbox" data-jsave2="post.${sub}.${key}" ${pget(sub, key, dflt) ? 'checked' : ''}> ${label}</label>`;
const ptext = (sub, key, dflt, ph = '') => `<input type="text" data-jsave2="post.${sub}.${key}" value="${esc(pget(sub, key, dflt) ?? '')}" placeholder="${esc(ph)}">`;
POST_SECTIONS.push(() => {
  const presets = Object.entries(S.meta.sub_presets || { 经典: '白字黑边' });
  return `<div class="post-sec"><b>字幕样式</b>
    <div class="grid4" style="margin-top:6px">
      <div><label class="f">样式</label>${sel(presets.map(([k, v]) => [k, `${k}（${v}）`]), pget('subtitle', 'preset', '经典'), 'data-jsave2="post.subtitle.preset"')}</div>
      <div><label class="f">位置</label>${sel([['bottom', '底部'], ['middle', '中间'], ['top', '顶部']], pget('subtitle', 'position', 'bottom'), 'data-jsave2="post.subtitle.position"')}</div>
      <div><label class="f">字号倍数</label><input type="number" step="0.1" min="0.5" max="2" data-num="1" data-jsave2="post.subtitle.scale" value="${pget('subtitle', 'scale', 1)}"></div>
      <div><label class="f">文字颜色（可空）</label>${ptext('subtitle', 'color', '', '#FFE600')}</div></div>
    <div class="row" style="margin-top:6px;gap:14px;flex-wrap:wrap">${pchk('subtitle', 'karaoke', false, '逐字高亮（卡拉 OK）')}${pchk('subtitle', 'highlight', true, '关键词花字（【】里的字和下面的关键词）')}${pchk('subtitle', 'speaker', false, '显示说话人')}</div>
    <label class="f">关键词（逗号分隔，变色放大）</label>${ptext('subtitle', 'keywords', '', '如：离婚, 真相, 三千万')}</div>
  <div class="post-sec"><div class="row between"><b>转场与片头片尾</b><label class="row small"><input type="checkbox" data-jsave="post.transitions" ${jget('post', 'transitions', true) ? 'checked' : ''}> 使用分镜里设置的转场（叠化/闪白/黑场）</label></div>
    <div class="grid2" style="margin-top:6px">
      <div>${pchk('title_card', 'enabled', false, '片头标题卡')}<div class="grid2">${ptext('title_card', 'text', '', '标题（默认项目名）')}${ptext('title_card', 'subtitle', '', '集数，如：第 1 集')}</div></div>
      <div>${pchk('end_card', 'enabled', false, '片尾卡')}<div class="grid2">${ptext('end_card', 'text', '', '未完待续')}${ptext('end_card', 'subtitle', '', '如：关注看下一集')}</div></div></div></div>
  <div class="post-sec"><div class="row between"><b>封面</b>${jchk('post', 'cover', true, '合成时自动生成封面')}</div>
    <div class="row" style="margin-top:6px;align-items:flex-start;gap:14px">
      ${S.snap.project.cover ? `<img src="${media(S.snap.project.cover)}" class="cover-thumb" data-act="zoom" data-src="${media(S.snap.project.cover)}">` : '<div class="cover-thumb empty small">暂无</div>'}
      <div class="grow"><div class="grid2">${ptext('cover_opts', 'title', '', '封面标题（默认项目名，可用 \\n 换行）')}${ptext('cover_opts', 'subtitle', '', '副标题（默认梗概前 24 字）')}</div>
        <div class="row" style="margin-top:6px">${sel([['', '自动选高光镜头'], ...S.snap.shots.filter((x) => x.keyframe).map((x) => [x.id, `#${x.sort + 1} ${x.shot_size}`])], '', 'id="cv-shot" style="width:160px"')}
          ${sel([['3:4', '3:4 竖版'], ['9:16', '9:16 竖屏'], ['16:9', '16:9 横版'], ['1:1', '1:1']], pget('cover_opts', 'aspect', '3:4'), 'data-jsave2="post.cover_opts.aspect" id="cv-asp" style="width:110px"')}
          <button class="btn small" data-act="make-cover">生成封面</button>${S.snap.project.cover ? `<a class="btn small ghost" href="${media(S.snap.project.cover)}" download>下载</a>` : ''}</div></div></div></div>`;
});
Object.assign(ACTIONS, {
  async 'make-cover'() { const r = await api('POST', `/api/projects/${S.pid}/cover`, { shot_id: val('cv-shot'), aspect: val('cv-asp'), title: pget('cover_opts', 'title', '').replace(/\\n/g, '\n') }); toast('封面已生成'); loadSnap(true); },
});

/* ---------------- 声音后期：旁白/解说、音效库、AI 配乐、闪避混音、响度 ---------------- */
function shotSoundFields(x) {
  const narr = (S.snap.assets[x.id] || []).some((a) => a.kind === 'narration');
  const sfx = (x.sfx || []).map((f) => f.name);
  return `<div class="row small" style="margin-top:6px;gap:8px;align-items:flex-start"><span style="white-space:nowrap;padding-top:6px">解说词</span>
    <textarea rows="1" class="grow" data-save="shots:${x.id}:narration" placeholder="旁白读出的解说（可空；台词说话人选「旁白」也会配音）">${esc(x.narration || '')}</textarea>
    ${x.narration_audio ? `<audio src="${media(x.narration_audio)}" controls preload="none" style="height:30px;width:170px"></audio>` : narr ? '' : ''}</div>
    ${sfx.length ? `<div class="muted small">手动音效：${sfx.map(esc).join('、')} <a href="#" data-act="sfx-clear" data-id="${x.id}">改回自动匹配</a></div>` : ''}`;
}
const soundLine = (m) => (m && m.layers ? `<div class="muted small">声音：${[m.narration ? `旁白×${m.narration}` : '', m.sfx ? `音效×${m.sfx}` : '', m.bgm ? `BGM(${m.bgm})` : ''].filter(Boolean).join(' · ') || '原声'}${m.target ? ` · ${m.target} LUFS` : ''}</div>` : '');
POST_SECTIONS.push(() => {
  const s = S.snap; const p = s.project; const voices = s.characters.filter((c) => c.voice_ref);
  const vcur = pget('sound', 'voice', '');
  const vopts = [['', '（未选，用 data/voices/narrator.wav）'], ...voices.map((c) => [`char:${c.id}`, `角色音色：${c.name}`])];
  if (vcur && !vcur.startsWith('char:')) vopts.push([vcur, '上传的旁白样本']);
  const nCount = s.shots.filter((x) => (x.narration || '').trim() || (x.dialogue || []).some((d) => ['旁白', '解说', '画外音'].includes(d.speaker))).length;
  const styles = Object.keys(S.meta.narr_styles || { 解说: '' });
  const bgmA = assetsOf(p.id, 'bgm')[0];
  const segs = bgmA ? (bgmA.meta.segments || []).map((g) => `${g.mood} ${Math.round(g.seconds)}s`).join(' → ') : '';
  if (!S.sfxLib) { S.sfxLib = []; api('GET', '/api/sfx').then((r) => { S.sfxLib = r; if (S.step === 'final') renderProject(); }).catch(() => {}); }
  return `<div class="post-sec"><div class="row between"><b>旁白 / 解说</b>${pchk('sound', 'narration', true, '成片里混入旁白配音')}</div>
    <div class="grid3" style="margin-top:6px">
      <div><label class="f">旁白音色</label>${sel(vopts, vcur, 'data-jsave2="post.sound.voice"')}<button class="btn small ghost" style="margin-top:4px" data-act="narr-voice">上传旁白样本</button></div>
      <div><label class="f">语气</label>${ptext('sound', 'emo', '平静、娓娓道来', '如：低沉、悬疑')}</div>
      <div><label class="f">AI 写解说词</label><div class="row">${sel(styles.map((k) => [k, k]), '解说', 'id="nw-style" style="width:100px"')}<label class="row small"><input type="checkbox" id="nw-over"> 覆盖已有</label>
        <button class="btn small ghost" data-act="narr-write" title="${esc((S.meta.narr_styles || {})[styles[0]] || '')}">写解说词</button></div></div></div>
    <div class="row" style="margin-top:6px"><button class="btn small" data-act="narr-make" ${nCount && S.meta.voice && S.meta.voice.tts !== false ? '' : 'disabled'}>为 ${nCount} 个镜头配旁白</button>
      <span class="muted small">解说词写在④分镜每个镜头里；有对白配音的镜头，旁白排在对白之后</span></div></div>
  <div class="post-sec"><div class="row between"><b>音效</b>${pchk('sound', 'sfx_auto', true, '按镜头「环境音/音效」自动匹配音效库')}</div>
    <div class="row small" style="margin-top:6px;gap:10px">音量 <input type="range" min="0" max="1.5" step="0.05" value="${pget('sound', 'sfx_volume', 0.6)}" data-num="1" data-jsave2="post.sound.sfx_volume" style="width:120px">
      <button class="btn small ghost" data-act="sfx-upload">上传音效</button><span class="muted">文件名即标签，如「雨声_环境.wav」「关门声.mp3」；带 雨/风/人群/环境 等字样的会循环铺满镜头</span></div>
    <div class="chips" style="margin-top:6px">${(S.sfxLib || []).map((f) => `<span class="chip" title="${esc(f.tags.join(' '))}">${esc(f.name)}${f.loop ? ' ↻' : ''} <a href="#" data-act="sfx-del" data-name="${esc(f.name)}">✕</a></span>`).join('') || '<span class="muted small">音效库为空（服务器 data/sfx 目录）</span>'}</div></div>
  <div class="post-sec"><div class="row between"><b>AI 配乐（ACE-Step）</b>${pchk('sound', 'bgm_auto', true, '没上传 BGM 时使用生成的配乐')}</div>
    <div class="row" style="margin-top:6px;align-items:center;gap:10px">${p.bgm ? `<audio src="${media(p.bgm)}" controls preload="none" style="height:32px"></audio>` : '<span class="muted small">暂无配乐</span>'}
      ${segs ? `<span class="muted small">情绪段落：${esc(segs)}</span>` : ''}</div>
    <div class="row" style="margin-top:6px">${ptext('sound', 'style', '', '风格标签（英文更准，可空），如：chinese traditional, guzheng')}
      <label class="row small"><input type="checkbox" id="bgm-single"> 全片一首</label>
      <button class="btn small" data-act="bgm-gen" ${S.meta.music ? '' : 'disabled'} title="${S.meta.music ? '按场次情绪曲线分段生成，段间交叉淡化' : '未安装 ACE-Step：在服务器运行 bash deploy/09_music.sh'}">生成配乐</button>
      <button class="btn small ghost" data-act="bgm-plan">预览情绪分段</button></div></div>
  <div class="post-sec"><b>混音</b><div class="row" style="margin-top:6px;gap:14px;flex-wrap:wrap">${pchk('sound', 'duck', true, '人声闪避（说话时 BGM 自动压低）')}${pchk('sound', 'loudnorm', true, '响度标准化')}
    <span class="small">目标 ${sel([[-14, '-14 LUFS（抖音/快手/B站）'], [-16, '-16 LUFS（播客/YouTube 偏安静）'], [-23, '-23 LUFS（广电）']], pget('sound', 'lufs', -14), 'data-num="1" data-jsave2="post.sound.lufs" style="width:220px"')}</span></div></div>`;
});
Object.assign(ACTIONS, {
  async 'narr-voice'() { const f = await pickFile('audio/*'); if (f) { Object.assign(S.snap.project, await upload(`/api/projects/${S.pid}/narration/voice`, f)); toast('旁白音色已设置'); renderProject(); } },
  async 'narr-write'() { submitted(await api('POST', `/api/projects/${S.pid}/narration/write`, { style: val('nw-style'), overwrite: !!val('nw-over') })); },
  async 'narr-make'() { submitted(await api('POST', `/api/projects/${S.pid}/narration`, {})); },
  async 'sfx-upload'() { const f = await pickFile('audio/*'); if (f) { await upload('/api/sfx', f); S.sfxLib = null; renderProject(); } },
  async 'sfx-del'(el) { if (!confirm(`删除音效 ${el.dataset.name}？`)) return; await api('DELETE', `/api/sfx/${encodeURIComponent(el.dataset.name)}`); S.sfxLib = null; renderProject(); },
  async 'sfx-clear'(el) { await api('PATCH', `/api/shots/${el.dataset.id}`, { sfx: [] }); loadSnap(true); },
  async 'bgm-gen'() { submitted(await api('POST', `/api/projects/${S.pid}/bgm`, { single: !!val('bgm-single') })); },
  async 'bgm-plan'() { const r = await api('GET', `/api/projects/${S.pid}/bgm/plan`); alert(r.segments.map((g, i) => `${i + 1}. ${g.mood}  ${Math.round(g.seconds)} 秒（${g.shots.length} 个镜头）\n   ${g.tags}`).join('\n') || '还没有分镜'); },
});

/* ---------------- 画质增强：逐镜头超分 / 补帧、成片帧率 ---------------- */
Object.assign(ACTIONS, {
  async 'shot-upscale'(el) { submitted(await api('POST', `/api/shots/${el.dataset.id}/upscale`, { target: val(`up-t-${el.dataset.id}`) })); },
  async 'shot-interp'(el) { submitted(await api('POST', `/api/shots/${el.dataset.id}/interpolate`, { fps: Number(val(`fi-f-${el.dataset.id}`)) })); },
  async 'enhance-all'(el) {
    const kind = el.dataset.kind; const body = kind === 'upscale' ? { kind, target: val('en-t') } : { kind, fps: Number(val('en-f')) };
    const r = await api('POST', `/api/projects/${S.pid}/enhance`, body); toast(r.submitted ? `已提交 ${r.submitted} 个镜头` : '没有需要处理的镜头'); pollTasks();
  },
});
POST_SECTIONS.push(() => {
  const E = S.meta.enhance || {}; const vids = S.snap.shots.filter((x) => x.video).length;
  return `<div class="post-sec"><div class="row between"><b>画质增强</b><span class="muted small">${E.esrgan ? '超分：Real-ESRGAN' : E.seedvr2 ? '超分：SeedVR2' : '超分未安装（deploy/10_enhance.sh）'} · ${E.rife ? '补帧：RIFE' : '补帧：ffmpeg（未装 RIFE）'}</span></div>
    <div class="row" style="margin-top:6px;gap:10px;flex-wrap:wrap">
      ${sel([['1080p', '1080p'], ['1440p', '1440p'], ['4k', '4K']], '1080p', 'id="en-t" style="width:90px"')}<button class="btn small ghost" data-act="enhance-all" data-kind="upscale" ${vids && (E.esrgan || E.seedvr2) ? '' : 'disabled'}>全部镜头超分</button>
      ${sel([[48, '48fps'], [60, '60fps'], [30, '30fps']], 48, 'id="en-f" style="width:90px"')}<button class="btn small ghost" data-act="enhance-all" data-kind="interpolate" ${vids ? '' : 'disabled'}>全部镜头补帧</button>
      <span class="small">成片帧率 ${sel([[24, '24'], [30, '30'], [48, '48'], [60, '60']], jget('post', 'fps', 24), 'data-jsave="post.fps" data-num="1" style="width:70px"')}</span>
      <span class="muted small">已处理过的镜头自动跳过；建议先配音再增强；超分后成片选「输出 1080p/1440p」</span></div></div>`;
});

/* ---------------- 导出工程：剪映草稿 / FCPXML / EDL ---------------- */
function exportPanel() {
  const p = S.snap.project; const ex = assetsOf(p.id, 'export'); const busy = busyOf(`${p.id}:export`);
  const ok = S.snap.shots.some((x) => x.video);
  const tr = (m) => Object.entries({ video: '画面', dialogue: '对白', narration: '旁白', ambient: '环境音', sfx: '音效', bgm: '配乐', subs: '字幕' })
    .filter(([k]) => (m.tracks || {})[k]).map(([k, v]) => `${v}${m.tracks[k]}`).join(' · ');
  return `<div class="panel" style="margin-top:16px"><div class="row between"><h3>导出工程 <span class="muted small">剪映草稿（分轨）· FCPXML（达芬奇/FCP）· EDL · SRT，打包成 zip</span></h3>
    <button class="btn small" data-act="export-project" ${ok && !busy ? '' : 'disabled'}>${busy ? '打包中… ' + busy.progress + '%' : '导出剪映草稿 / 工程'}</button></div>
    <div class="muted small">解压后双击「修复素材路径.bat」，再把「剪映草稿」里的文件夹复制到剪映草稿目录。有配音分轨的镜头画面已静音，对白/环境音在单独轨道上。</div>
    ${ex.length ? `<table style="margin-top:8px">${ex.slice(0, 5).map((a) => `<tr><td>${new Date(a.created_at * 1000).toLocaleString()}</td><td class="small">${tr(a.meta)}</td><td>${a.meta.seconds || ''}s · ${a.meta.size_mb || ''}MB</td>
      <td class="row"><a class="btn small ghost" href="${media(a.path)}" download>下载</a><button class="btn small danger" data-act="del-asset" data-id="${a.id}">删除</button></td></tr>`).join('')}</table>` : ''}</div>`;
}
Object.assign(ACTIONS, { async 'export-project'() { submitted(await api('POST', `/api/projects/${S.pid}/export`, {})); } });
