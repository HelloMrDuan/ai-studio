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
