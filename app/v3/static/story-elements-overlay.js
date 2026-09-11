(() => {
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

  function canonicalRows() {
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

  function renderCanonicalStoryElements() {
    const target = document.getElementById('entities');
    if (!target) return;
    const rows = canonicalRows();
    const counts = {character: 0, location: 0, prop: 0};
    for (const row of rows) counts[row.entity_type] += 1;

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
  }

  // Narrative scene/shot nodes belong to continuity and should not inflate the
  // reusable story-asset summary. Replace the original global renderer while
  // keeping the existing page and continuity view intact.
  window.renderEntities = renderCanonicalStoryElements;
  window.__v3StoryElementsCanonical = {
    enabled: true,
    entityTypes: ['character', 'location', 'prop'],
    excludesNarrativeScenes: true,
  };

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderCanonicalStoryElements);
  } else {
    renderCanonicalStoryElements();
  }
})();
