(() => {
  let state = null;
  let loading = false;
  let projectId = '';

  function pid() {
    try { return String(current || '').trim(); } catch (_) { return ''; }
  }
  function escapeHtml(value) {
    if (typeof esc === 'function') return esc(value);
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  async function request(url, options = {}) {
    const response = await fetch(url, options);
    const text = await response.text();
    let body;
    try { body = JSON.parse(text); } catch (_) { body = text; }
    if (!response.ok) throw new Error(body?.detail || body || `请求失败：${response.status}`);
    return body;
  }
  function notify(message, bad = false) {
    if (typeof toast === 'function') toast(message, bad);
    else alert(message);
  }

  function installStyles() {
    if (document.getElementById('v3AssetAuthoringStyles')) return;
    const style = document.createElement('style');
    style.id = 'v3AssetAuthoringStyles';
    style.textContent = `
      .v3-asset-panel{margin-top:14px}
      .v3-asset-head{display:flex;justify-content:space-between;align-items:flex-start;gap:12px;flex-wrap:wrap}
      .v3-asset-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(310px,1fr));gap:12px;margin-top:12px}
      .v3-asset-card{border:1px solid #2d3c52;border-radius:12px;padding:12px;background:rgba(9,17,30,.62)}
      .v3-asset-card h4{margin:0;font-size:16px}
      .v3-asset-version{display:inline-flex;margin-left:7px;padding:2px 7px;border:1px solid #395170;border-radius:999px;font-size:11px;color:#a9c6ea}
      .v3-asset-note{font-size:12px;color:#91a4bd;margin:5px 0 9px;line-height:1.5}
      .v3-asset-text{width:100%;min-height:155px;resize:vertical;font-size:12px;line-height:1.6}
      .v3-asset-actions{display:flex;gap:8px;align-items:center;margin-top:9px;flex-wrap:wrap}
      .v3-asset-impact{font-size:11px;color:#8fa4bd}
    `;
    document.head.appendChild(style);
  }

  function ensurePanel() {
    const view = document.getElementById('view-create');
    if (!view) return null;
    let panel = document.getElementById('v3AssetAuthoringPanel');
    if (panel) return panel;
    panel = document.createElement('div');
    panel.id = 'v3AssetAuthoringPanel';
    panel.className = 'panel v3-asset-panel';
    panel.innerHTML = `
      <div class="v3-asset-head">
        <div>
          <h3 style="margin:0">角色 / 场景 / 道具稳定设定</h3>
          <div class="sub" style="margin-top:5px">每个资产独立版本。修改某一个资产，只让真正引用它的下游镜头失效，不再整段推倒重做。</div>
        </div>
        <button class="btn" type="button" onclick="v3RefreshAuthoringAssets(true)">刷新资产</button>
      </div>
      <div id="v3AssetAuthoringSummary" class="notice" style="margin-top:10px">等待②角色 / ③视觉形成可复用资产。</div>
      <div id="v3AssetAuthoringGrid" class="v3-asset-grid"></div>
    `;
    const reference = document.getElementById('v3ReferencePanel');
    if (reference?.parentNode) reference.parentNode.insertBefore(panel, reference);
    else {
      const summary = view.querySelector('.summaryGrid');
      if (summary?.parentNode) summary.parentNode.insertBefore(panel, summary.nextSibling);
      else view.prepend(panel);
    }
    return panel;
  }

  function textId(entityId) {
    return `v3asset_${String(entityId || '').replace(/[^A-Za-z0-9_-]/g, '_')}`;
  }

  function render() {
    const panel = ensurePanel();
    if (!panel) return;
    const summary = document.getElementById('v3AssetAuthoringSummary');
    const grid = document.getElementById('v3AssetAuthoringGrid');
    const items = Array.isArray(state?.items) ? state.items : [];
    if (!items.length) {
      summary.textContent = '当前还没有可编辑的稳定资产。完成②角色和③视觉后，角色、场景、道具会分别出现在这里。';
      grid.innerHTML = '';
      return;
    }
    summary.textContent = `已建立 ${items.length} 个独立可复用资产。这里修改的是稳定身份/结构，不是某一个镜头里的动作、表情或机位。`;
    grid.innerHTML = items.map(item => {
      const id = textId(item.entity_id);
      return `<div class="v3-asset-card">
        <div style="display:flex;align-items:center;justify-content:space-between;gap:8px">
          <div><h4>${escapeHtml(item.label)} · ${escapeHtml(item.name || '未命名')}<span class="v3-asset-version">第 ${Number(item.profile_version || 1)} 版</span></h4></div>
        </div>
        <div class="v3-asset-note">${item.entity_type === 'character' ? '只写跨镜头不变的脸、发型、体型、服装、鞋履、配饰和固定配色。' : item.entity_type === 'prop' ? '只写稳定轮廓、结构、材质、颜色、纹样和长期辨识特征。' : '只写稳定空间结构、材质、固定陈设和可辨识空间锚点。'}</div>
        <textarea id="${id}" class="v3-asset-text">${escapeHtml(item.stable_design || '')}</textarea>
        <div class="v3-asset-actions">
          <button class="btn good" type="button" onclick="v3SaveAuthoringAsset('${escapeHtml(item.entity_id)}','${id}')">保存为新版本</button>
          <span class="v3-asset-impact">保存后仅重算引用此资产的下游结果</span>
        </div>
      </div>`;
    }).join('');
  }

  async function refresh(showError = false) {
    const currentId = pid();
    if (!currentId || loading) return;
    loading = true;
    try {
      state = await request(`/api/v3/studio/projects/${encodeURIComponent(currentId)}/authoring-assets`);
      projectId = currentId;
      render();
    } catch (error) {
      if (showError) notify(error.message || String(error), true);
    } finally {
      loading = false;
    }
  }

  window.v3RefreshAuthoringAssets = refresh;
  window.v3SaveAuthoringAsset = async (entityId, textareaId) => {
    const currentId = pid();
    if (!currentId) return;
    const text = String(document.getElementById(textareaId)?.value || '').trim();
    if (text.length < 8) return notify('稳定设定太短，请写清跨镜头必须保持一致的特征。', true);
    const reason = prompt('可选：这次修改了什么？例如“少年外套改为深蓝色，鞋履固定为黑色长靴”。', '') || '';
    try {
      const result = await request(`/api/v3/studio/projects/${encodeURIComponent(currentId)}/authoring-assets/${encodeURIComponent(entityId)}`, {
        method: 'PUT',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({stable_design: text, change_reason: reason}),
      });
      notify(`资产已保存为第 ${result.profile_version || '新'} 版；仅 ${result.stale_asset_count || 0} 个真正引用它的下游结果被标记为需要重算。`);
      state = null;
      await refresh(true);
      if (typeof v3RefreshReferences === 'function') await v3RefreshReferences(true);
      if (typeof refreshAll === 'function') await refreshAll();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };

  function hookRefreshAll() {
    if (typeof refreshAll !== 'function' || refreshAll.__v3AssetWrapped) return;
    const original = refreshAll;
    const wrapped = async function(...args) {
      const result = await original.apply(this, args);
      const currentId = pid();
      if (currentId && currentId !== projectId) state = null;
      setTimeout(() => refresh(false), 30);
      return result;
    };
    wrapped.__v3AssetWrapped = true;
    refreshAll = wrapped;
  }

  function boot() {
    installStyles();
    ensurePanel();
    hookRefreshAll();
    setTimeout(() => refresh(false), 120);
    setInterval(() => {
      const currentId = pid();
      if (currentId && currentId !== projectId) refresh(false);
    }, 1800);
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
