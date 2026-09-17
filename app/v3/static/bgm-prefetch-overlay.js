(() => {
  let state = null;
  let loadedProject = '';
  let busy = false;

  function pid() {
    try { return String(current || '').trim(); } catch (_) { return ''; }
  }
  function escHtml(value) {
    if (typeof esc === 'function') return esc(value);
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  function notify(message, bad = false) {
    if (typeof toast === 'function') toast(message, bad);
    else alert(message);
  }
  async function request(url, options = {}) {
    const response = await fetch(url, options);
    const text = await response.text();
    let body;
    try { body = JSON.parse(text); } catch (_) { body = text; }
    if (!response.ok) throw new Error(body?.detail || body || `请求失败：${response.status}`);
    return body;
  }

  function ensurePanel() {
    const view = document.getElementById('view-final');
    if (!view) return null;
    let panel = document.getElementById('v3BgmPrefetchPanel');
    if (panel) return panel;
    panel = document.createElement('div');
    panel.id = 'v3BgmPrefetchPanel';
    panel.className = 'panel';
    panel.innerHTML = `
      <div class="sectionHead">
        <div>
          <h3>背景音乐候选</h3>
          <div class="sectionHint">④确认后与画面制作并行准备。只提供候选，不会自动替换你选择的音乐。</div>
        </div>
        <button class="btn" type="button" onclick="v3PrepareBgmCandidates()">刷新候选</button>
      </div>
      <div id="v3BgmIntent" class="notice">等待正式分镜音乐意图。</div>
      <div id="v3BgmCandidates" class="cards" style="margin-top:10px"></div>`;
    view.prepend(panel);
    return panel;
  }

  function render() {
    if (!ensurePanel()) return;
    const intent = document.getElementById('v3BgmIntent');
    const list = document.getElementById('v3BgmCandidates');
    const candidates = Array.isArray(state?.candidates) ? state.candidates : [];
    const intentText = String(state?.music_intent || '').trim();
    const message = String(state?.message || '').trim();
    intent.innerHTML = `<b>音乐意图：</b>${escHtml(intentText || '分镜未明确音乐要求')}<br>${escHtml(message || '候选尚未准备')}`;
    if (!candidates.length) {
      list.innerHTML = '<div class="empty">暂无可用曲库候选。可以继续在成片区上传音乐；系统不会为了“自动化”随便乱配一首。</div>';
      return;
    }
    list.innerHTML = candidates.map(item => {
      const adopted = String(state?.adopted_candidate_id || '') === String(item.candidate_id || '');
      return `<div class="card">
        <h4>${escHtml(item.name || '背景音乐')}</h4>
        <div class="meta">${item.recommended ? '默认曲候选 · ' : ''}${adopted ? '已采用' : '等待试听采用'}</div>
        ${item.url ? `<audio controls preload="metadata" style="width:100%;margin-top:9px" src="${escHtml(item.url)}"></audio>` : ''}
        <div class="row">
          <button class="btn ${adopted ? '' : 'good'}" ${adopted ? 'disabled' : ''} type="button" onclick="v3AdoptBgmCandidate('${escHtml(item.candidate_id)}')">${adopted ? '已采用' : '采用这首'}</button>
        </div>
      </div>`;
    }).join('');
  }

  async function refresh(showError = false) {
    const project = pid();
    if (!project || busy) return;
    busy = true;
    try {
      state = await request(`/api/v3/studio/projects/${encodeURIComponent(project)}/bgm-prefetch`);
      loadedProject = project;
      render();
    } catch (error) {
      if (showError) notify(error.message || String(error), true);
    } finally {
      busy = false;
    }
  }

  window.v3PrepareBgmCandidates = async () => {
    const project = pid();
    if (!project) return;
    try {
      state = await request(`/api/v3/studio/projects/${encodeURIComponent(project)}/bgm-prefetch/prepare`, {method:'POST'});
      loadedProject = project;
      render();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };

  window.v3AdoptBgmCandidate = async candidateId => {
    const project = pid();
    if (!project) return;
    try {
      state = await request(`/api/v3/studio/projects/${encodeURIComponent(project)}/bgm-prefetch/${encodeURIComponent(candidateId)}/adopt`, {method:'POST'});
      loadedProject = project;
      render();
      notify('背景音乐候选已采用；你仍可在成片阶段上传另一首替换。');
      if (typeof v3LoadPostproduction === 'function') await v3LoadPostproduction(true);
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };

  function hookRefresh() {
    if (typeof refreshAll !== 'function' || refreshAll.__v3BgmWrapped) return;
    const original = refreshAll;
    const wrapped = async function(...args) {
      const result = await original.apply(this, args);
      const project = pid();
      if (project && project !== loadedProject) state = null;
      setTimeout(() => refresh(false), 40);
      return result;
    };
    wrapped.__v3BgmWrapped = true;
    refreshAll = wrapped;
  }

  function boot() {
    ensurePanel();
    hookRefresh();
    setTimeout(() => refresh(false), 180);
    setInterval(() => {
      const project = pid();
      if (project && project !== loadedProject) refresh(false);
    }, 1800);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
