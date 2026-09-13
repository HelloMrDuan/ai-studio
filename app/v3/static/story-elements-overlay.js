(() => {
  let canonicalCache = { projectId: '', rows: [], loadedAt: 0 };
  let canonicalLoading = null;

  function escapeHtml(value) {
    return String(value ?? '').replace(/[&<>"']/g, c => ({
      '&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'
    }[c]));
  }

  function normalizedName(value) {
    return String(value ?? '')
      .normalize('NFKC')
      .replace(/[\u200b-\u200d\ufeff\s]+/g, '')
      .toLowerCase();
  }

  function currentProjectId() {
    try { return String(current || '').trim(); }
    catch (_) { return ''; }
  }

  function legacyFallbackRows() {
    let rows = [];
    try { rows = Array.isArray(snap?.entities) ? snap.entities : []; }
    catch (_) { rows = []; }

    const allowed = new Set(['character', 'location', 'prop']);
    const seen = new Set();
    const result = [];
    for (const row of rows) {
      if (!row || !allowed.has(String(row.entity_type || ''))) continue;
      const name = String(row.name || '').trim();
      const key = `${row.entity_type}:${normalizedName(name)}`;
      if (!name || seen.has(key)) continue;
      seen.add(key);
      result.push(row);
    }
    return result;
  }

  function canonicalRows() {
    const projectId = currentProjectId();
    if (projectId && canonicalCache.projectId === projectId && canonicalCache.loadedAt) {
      return canonicalCache.rows;
    }
    return legacyFallbackRows();
  }

  async function refreshCanonicalProjection(force = false) {
    const projectId = currentProjectId();
    if (!projectId) return;
    const now = Date.now();
    if (!force && canonicalCache.projectId === projectId && now - canonicalCache.loadedAt < 2500) return;
    if (canonicalLoading) return canonicalLoading;

    canonicalLoading = (async () => {
      try {
        const response = await fetch(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/story-elements`, {
          headers: { 'Accept': 'application/json' },
          cache: 'no-store',
        });
        if (!response.ok) throw new Error(`story-elements ${response.status}`);
        const payload = await response.json();
        const rows = Array.isArray(payload?.entities) ? payload.entities : [];
        canonicalCache = {
          projectId,
          rows,
          loadedAt: Date.now(),
        };
        renderCanonicalStoryElements(false);
      } catch (error) {
        console.error('规范故事元素读取失败，暂时保留旧快照显示：', error);
      } finally {
        canonicalLoading = null;
      }
    })();
    return canonicalLoading;
  }

  function renderCanonicalStoryElements(scheduleRefresh = true) {
    const target = document.getElementById('entities');
    if (!target) return;
    const rows = canonicalRows();
    const counts = {character: 0, location: 0, prop: 0};
    for (const row of rows) {
      const kind = String(row?.entity_type || '');
      if (Object.prototype.hasOwnProperty.call(counts, kind)) counts[kind] += 1;
    }

    const overview = [
      ['角色', counts.character],
      ['场景', counts.location],
      ['道具', counts.prop],
    ].map(([label, count]) =>
      `<div class="entityCount">${label}<b>${count}</b></div>`
    ).join('');

    const label = {character: '角色', location: '场景', prop: '道具'};
    const sample = rows.slice(0, 8).map(row =>
      `<div class="entityPill"><span>${label[row.entity_type] || '元素'}</span>${escapeHtml(row.name)}</div>`
    ).join('');
    const more = rows.length > 8
      ? `<div class="meta" style="margin-top:7px">其余 ${rows.length - 8} 个在“连续性”中查看。</div>`
      : '';

    target.innerHTML = rows.length
      ? `<div class="entityOverview">${overview}</div><div class="entityPills">${sample}</div>${more}`
      : '<div class="empty">暂无故事元素</div>';

    if (scheduleRefresh) refreshCanonicalProjection(false);
  }

  window.renderEntities = renderCanonicalStoryElements;
  window.__v3StoryElementsCanonical = {
    enabled: true,
    source: 'typed-story-elements-projection',
    entityTypes: ['character', 'location', 'prop'],
    excludesNarrativeScenes: true,
    refresh: () => refreshCanonicalProjection(true),
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', () => {
      renderCanonicalStoryElements(true);
      refreshCanonicalProjection(true);
    });
  } else {
    renderCanonicalStoryElements(true);
    refreshCanonicalProjection(true);
  }
})();
