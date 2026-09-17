(() => {
  const STYLE_ID = 'xiaoduan-asset-explorer-style';
  const ROOT_ID = 'xiaoduan-asset-explorer-root';
  const DIRECT_ID = 'xiaoduan-direct-media-preview';

  const esc = (value) => String(value ?? '')
    .replaceAll('&', '&amp;')
    .replaceAll('<', '&lt;')
    .replaceAll('>', '&gt;')
    .replaceAll('"', '&quot;')
    .replaceAll("'", '&#39;');

  function currentProjectId() {
    try {
      if (typeof current !== 'undefined' && current) return String(current);
    } catch (_) {}
    if (window.current) return String(window.current);
    const active = document.querySelector('.projectItem.active');
    const click = active?.getAttribute('onclick') || '';
    const hit = click.match(/(?:selectProject|openProject)\(['"]([^'"]+)['"]\)/);
    return hit ? hit[1] : '';
  }

  function addStyle() {
    if (document.getElementById(STYLE_ID)) return;
    const style = document.createElement('style');
    style.id = STYLE_ID;
    style.textContent = `
      #xd-asset-button{position:fixed;right:22px;bottom:22px;z-index:94;border:1px solid #49688f;background:#17345c;color:#fff;border-radius:999px;padding:11px 16px;font-weight:800;box-shadow:0 10px 30px #0008;cursor:pointer}
      #xd-asset-button:hover{filter:brightness(1.12)}
      .xd-asset-modal{position:fixed;inset:0;z-index:120;background:#02050bd9;display:none;align-items:center;justify-content:center;padding:20px}.xd-asset-modal.show{display:flex}
      .xd-asset-shell{width:min(1500px,96vw);height:min(920px,94vh);background:#0b1320;border:1px solid #2c405f;border-radius:16px;display:grid;grid-template-rows:auto auto 1fr;overflow:hidden;box-shadow:0 24px 80px #000b}
      .xd-asset-head{display:flex;justify-content:space-between;gap:16px;align-items:center;padding:16px 18px;border-bottom:1px solid #24344d}.xd-asset-head h2{margin:0;font-size:20px}.xd-close{border:1px solid #425571;background:#152238;color:#eef4ff;border-radius:8px;padding:7px 11px;cursor:pointer}
      .xd-asset-tools{display:flex;gap:8px;align-items:center;flex-wrap:wrap;padding:10px 18px;border-bottom:1px solid #1f2e45}.xd-asset-tools input,.xd-asset-tools select{background:#07101c;color:#eef4ff;border:1px solid #2a3e5c;border-radius:8px;padding:8px 10px}.xd-asset-tools input{min-width:260px;flex:1}.xd-count{color:#90a4c2;font-size:12px}
      .xd-asset-body{min-height:0;overflow:auto;padding:16px 18px}.xd-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(255px,1fr));gap:12px}.xd-card{background:#08111e;border:1px solid #263a58;border-radius:12px;padding:10px;cursor:pointer;min-width:0}.xd-card:hover{border-color:#5b7eac;background:#0c1829}.xd-card-preview{height:170px;border-radius:9px;background:#03070d;display:flex;align-items:center;justify-content:center;overflow:hidden;margin-bottom:9px}.xd-card-preview img,.xd-card-preview video{max-width:100%;max-height:100%;object-fit:contain}.xd-card-preview audio{width:92%}.xd-text-preview{padding:10px;white-space:pre-wrap;word-break:break-word;color:#cbd7e8;font-size:12px;line-height:1.55;max-height:100%;overflow:hidden}.xd-card-title{font-weight:800;font-size:13px;white-space:nowrap;text-overflow:ellipsis;overflow:hidden}.xd-card-meta{font-size:11px;color:#8fa2bd;margin-top:5px;line-height:1.5}.xd-pill{display:inline-block;border:1px solid #334b6d;border-radius:999px;padding:2px 6px;margin-right:4px;color:#a9bdd8}.xd-empty{color:#8fa0bb;padding:30px;text-align:center}
      .xd-preview-shell{width:min(1500px,96vw);height:min(940px,96vh);background:#080f19;border:1px solid #314969;border-radius:16px;display:grid;grid-template-columns:minmax(0,1fr) 360px;overflow:hidden}.xd-preview-main{min-width:0;display:flex;align-items:center;justify-content:center;background:#020408;padding:18px;overflow:auto}.xd-preview-main img{max-width:100%;max-height:88vh;object-fit:contain}.xd-preview-main video{width:100%;max-height:88vh}.xd-preview-main audio{width:min(760px,90%)}.xd-preview-main pre{width:100%;height:100%;margin:0;white-space:pre-wrap;word-break:break-word;color:#e7eef9;background:#07101c;border-radius:10px;padding:16px;overflow:auto;line-height:1.65}.xd-preview-side{border-left:1px solid #25364f;padding:16px;overflow:auto;background:#0c1523}.xd-preview-side h3{margin:0 0 12px}.xd-detail-row{border-bottom:1px solid #1d2b40;padding:8px 0}.xd-detail-row b{display:block;font-size:11px;color:#879bb8;margin-bottom:3px}.xd-detail-row div{font-size:12px;color:#dce6f5;word-break:break-word;white-space:pre-wrap}.xd-preview-actions{display:flex;gap:8px;margin-bottom:12px;position:sticky;top:0;background:#0c1523;padding-bottom:8px}.xd-preview-actions button{border:1px solid #425571;background:#152238;color:#eef4ff;border-radius:8px;padding:7px 10px;cursor:pointer}
      img[data-xd-media-preview="1"],video[data-xd-media-preview="1"]{cursor:zoom-in!important}
      @media(max-width:900px){.xd-preview-shell{grid-template-columns:1fr;grid-template-rows:minmax(0,1fr) 300px}.xd-preview-side{border-left:0;border-top:1px solid #25364f}.xd-asset-shell{height:96vh}}
    `;
    document.head.appendChild(style);
  }

  function ensureRoot() {
    addStyle();
    let root = document.getElementById(ROOT_ID);
    if (root) return root;
    root = document.createElement('div');
    root.id = ROOT_ID;
    root.innerHTML = `
      <button id="xd-asset-button" type="button">作品资产</button>
      <div id="xd-asset-modal" class="xd-asset-modal" aria-hidden="true">
        <div class="xd-asset-shell">
          <div class="xd-asset-head"><div><h2>作品资产</h2><div class="xd-count" id="xd-asset-subtitle">跨阶段查看正式资产、生成候选和新版资源</div></div><button class="xd-close" id="xd-asset-close">关闭</button></div>
          <div class="xd-asset-tools">
            <select id="xd-kind"><option value="all">全部类型</option><option value="image">图片</option><option value="video">视频</option><option value="audio">音频</option><option value="text">文本/结构</option></select>
            <select id="xd-stage"><option value="all">全部阶段</option><option value="01">① 剧本</option><option value="02">② 角色</option><option value="03">③ 视觉</option><option value="04">④ 分镜</option><option value="05">⑤ 制作</option><option value="06">⑥ 成片</option></select>
            <input id="xd-search" placeholder="搜索名称、角色、用途、逻辑键…">
            <span class="xd-count" id="xd-count"></span>
          </div>
          <div class="xd-asset-body" id="xd-asset-body"><div class="xd-empty">选择作品后即可查看全部资产。</div></div>
        </div>
      </div>
      <div id="${DIRECT_ID}" class="xd-asset-modal" aria-hidden="true"></div>
    `;
    document.body.appendChild(root);
    bindRoot(root);
    return root;
  }

  let snapshot = null;

  async function loadAssets() {
    const projectId = currentProjectId();
    if (!projectId) throw new Error('请先选择一个作品');
    const res = await fetch(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/asset-explorer`, {cache: 'no-store'});
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || '读取作品资产失败');
    snapshot = data;
    renderList();
    return data;
  }

  function statusCN(value) {
    const map = {ready:'已就绪',generated:'已生成',candidate_ready:'待采用',adopted:'已采用',superseded:'已替换',completed:'已完成',running:'生成中',failed:'失败',queued:'排队中',stale:'已过期',current:'当前'};
    return map[String(value || '').toLowerCase()] || String(value || '');
  }

  function previewHTML(entry) {
    if (entry.kind === 'image' && entry.url) return `<img loading="lazy" src="${esc(entry.url)}" alt="${esc(entry.name)}">`;
    if (entry.kind === 'video' && entry.url) return `<video preload="metadata" src="${esc(entry.url)}"></video>`;
    if (entry.kind === 'audio' && entry.url) return `<audio controls preload="none" src="${esc(entry.url)}"></audio>`;
    const text = entry.text || (entry.url ? '点击查看文件' : '暂无可预览内容');
    return `<div class="xd-text-preview">${esc(text.slice(0, 900))}</div>`;
  }

  function renderList() {
    const body = document.getElementById('xd-asset-body');
    if (!body || !snapshot) return;
    const kind = document.getElementById('xd-kind')?.value || 'all';
    const stage = document.getElementById('xd-stage')?.value || 'all';
    const q = (document.getElementById('xd-search')?.value || '').trim().toLowerCase();
    const entries = (snapshot.entries || []).filter(entry => {
      if (kind !== 'all' && entry.kind !== kind) return false;
      if (stage !== 'all' && String(entry.stage || '') !== stage) return false;
      if (!q) return true;
      const hay = [entry.name, entry.asset_role, entry.logical_key, entry.source_type, ...(entry.entity_ids || [])].join(' ').toLowerCase();
      return hay.includes(q);
    });
    const count = document.getElementById('xd-count');
    if (count) count.textContent = `${entries.length} / ${(snapshot.entries || []).length} 项`;
    if (!entries.length) {
      body.innerHTML = '<div class="xd-empty">当前筛选条件下没有资产。</div>';
      return;
    }
    body.innerHTML = `<div class="xd-grid">${entries.map((entry, index) => `
      <article class="xd-card" data-xd-entry="${index}">
        <div class="xd-card-preview">${previewHTML(entry)}</div>
        <div class="xd-card-title">${esc(entry.name || '未命名资产')}</div>
        <div class="xd-card-meta"><span class="xd-pill">${esc(entry.source_type || '')}</span><span class="xd-pill">${esc(entry.kind || '')}</span>${entry.stage ? `<span class="xd-pill">阶段 ${esc(entry.stage)}</span>` : ''}<br>${esc(entry.asset_role || entry.logical_key || '')}<br>${esc(statusCN(entry.status))}${entry.version ? ` · v${entry.version}` : ''}</div>
      </article>`).join('')}</div>`;
    const shown = entries;
    body.querySelectorAll('[data-xd-entry]').forEach(card => {
      card.addEventListener('click', () => openEntry(shown[Number(card.dataset.xdEntry)]));
    });
  }

  function detailRows(entry) {
    const rows = [
      ['来源', entry.source_type],
      ['阶段', entry.stage],
      ['类型', entry.kind],
      ['用途', entry.asset_role],
      ['版本', entry.version ? `v${entry.version}` : ''],
      ['状态', statusCN(entry.status)],
      ['依赖状态', statusCN(entry.dependency_state)],
      ['逻辑键', entry.logical_key],
      ['实体', (entry.entity_ids || []).join(', ')],
      ['上游资产', (entry.parent_asset_ids || []).join(', ')],
      ['参考资产', (entry.reference_ids || []).join(', ')],
      ['下游引用', (entry.used_by || []).map(x => `${x.name || x.id}`).join('\n')],
      ['生成来源', JSON.stringify(entry.source || {}, null, 2)],
      ['生成信息', JSON.stringify(entry.metadata || {}, null, 2)],
    ].filter(([, value]) => value !== undefined && value !== null && String(value).trim());
    return rows.map(([label, value]) => `<div class="xd-detail-row"><b>${esc(label)}</b><div>${esc(value)}</div></div>`).join('');
  }

  function contentHTML(entry) {
    if (entry.kind === 'image' && entry.url) return `<img src="${esc(entry.url)}" alt="${esc(entry.name)}">`;
    if (entry.kind === 'video' && entry.url) return `<video controls autoplay src="${esc(entry.url)}"></video>`;
    if (entry.kind === 'audio' && entry.url) return `<audio controls autoplay src="${esc(entry.url)}"></audio>`;
    return `<pre>${esc(entry.text || '当前资产没有可读取的文本内容。')}</pre>`;
  }

  function openEntry(entry) {
    if (!entry) return;
    const modal = document.getElementById(DIRECT_ID);
    modal.innerHTML = `<div class="xd-preview-shell">
      <div class="xd-preview-main">${contentHTML(entry)}</div>
      <aside class="xd-preview-side"><div class="xd-preview-actions"><button type="button" data-xd-close-preview>关闭</button>${entry.url ? `<button type="button" data-xd-new-window>单独打开</button>` : ''}</div><h3>${esc(entry.name || '资产预览')}</h3>${detailRows(entry)}</aside>
    </div>`;
    modal.classList.add('show');
    modal.setAttribute('aria-hidden', 'false');
    modal.querySelector('[data-xd-close-preview]')?.addEventListener('click', () => closePreview());
    modal.querySelector('[data-xd-new-window]')?.addEventListener('click', () => window.open(entry.url, '_blank', 'noopener'));
  }

  function closePreview() {
    const modal = document.getElementById(DIRECT_ID);
    if (!modal) return;
    modal.classList.remove('show');
    modal.setAttribute('aria-hidden', 'true');
    modal.innerHTML = '';
  }

  function openDirectMedia(el) {
    const src = el.currentSrc || el.src || '';
    if (!src) return;
    const tag = el.tagName.toLowerCase();
    openEntry({
      name: el.alt || el.title || (tag === 'video' ? '视频预览' : '图片预览'),
      kind: tag === 'video' ? 'video' : 'image',
      url: src,
      source_type: '当前页面生成结果',
      status: '',
      used_by: [],
    });
  }

  function attachMediaOpeners(root = document) {
    root.querySelectorAll?.('img,video').forEach(el => {
      if (el.dataset.xdMediaPreview === '1') return;
      if (el.closest(`#${ROOT_ID}`)) return;
      const src = el.currentSrc || el.src || '';
      if (!src || src.startsWith('data:image/svg')) return;
      el.dataset.xdMediaPreview = '1';
      el.title = el.title || '点击放大查看';
      el.addEventListener('click', event => {
        if (el.closest('a,button')) return;
        event.preventDefault();
        event.stopPropagation();
        openDirectMedia(el);
      });
    });
  }

  function bindRoot(root) {
    root.querySelector('#xd-asset-button')?.addEventListener('click', async () => {
      const modal = root.querySelector('#xd-asset-modal');
      modal.classList.add('show');
      modal.setAttribute('aria-hidden', 'false');
      const body = root.querySelector('#xd-asset-body');
      body.innerHTML = '<div class="xd-empty">正在读取作品资产…</div>';
      try { await loadAssets(); }
      catch (err) { body.innerHTML = `<div class="xd-empty">${esc(err.message || err)}</div>`; }
    });
    root.querySelector('#xd-asset-close')?.addEventListener('click', () => {
      root.querySelector('#xd-asset-modal')?.classList.remove('show');
    });
    ['xd-kind','xd-stage','xd-search'].forEach(id => root.querySelector(`#${id}`)?.addEventListener(id === 'xd-search' ? 'input' : 'change', renderList));
    root.querySelector('#xd-asset-modal')?.addEventListener('click', event => {
      if (event.target.id === 'xd-asset-modal') event.currentTarget.classList.remove('show');
    });
    root.querySelector(`#${DIRECT_ID}`)?.addEventListener('click', event => {
      if (event.target.id === DIRECT_ID) closePreview();
    });
    document.addEventListener('keydown', event => {
      if (event.key === 'Escape') {
        closePreview();
        root.querySelector('#xd-asset-modal')?.classList.remove('show');
      }
    });
  }

  function boot() {
    ensureRoot();
    attachMediaOpeners();
    const observer = new MutationObserver(records => {
      for (const record of records) for (const node of record.addedNodes) if (node.nodeType === 1) attachMediaOpeners(node);
    });
    observer.observe(document.body, {childList:true, subtree:true});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot, {once:true});
  else boot();
})();
