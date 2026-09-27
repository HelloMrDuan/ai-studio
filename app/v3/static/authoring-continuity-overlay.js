(() => {
  const STAGES = [
    ['01', '剧本'],
    ['02', '角色'],
    ['03', '视觉'],
    ['04', '分镜'],
  ];
  const ACTIVE = new Set(['queued', 'switching_gpu', 'running', 'generating']);
  const STATUS_ZH = {
    queued: '排队中', switching_gpu: '准备模型中', running: '生成中', generating: '生成中',
    completed: '候选待采用', confirmed: '已采用', rejected: '已丢弃', failed: '生成失败',
  };
  let referenceProject = '';
  let referenceState = null;
  let referenceLoading = false;

  function currentProjectId() {
    try { return String(current || ''); } catch (_) { return ''; }
  }
  function currentSnap() {
    try { return snap || null; } catch (_) { return null; }
  }
  function escapeHtml(value) {
    if (typeof esc === 'function') return esc(value);
    return String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  async function request(url, options = {}) {
    if (typeof api === 'function' && !options.raw) return api(url, options);
    const response = await fetch(url, options);
    const text = await response.text();
    let body;
    try { body = JSON.parse(text); } catch (_) { body = text; }
    if (!response.ok) throw new Error(body?.detail || body || `请求失败：${response.status}`);
    return body;
  }
  function post(url, body = {}) {
    return request(url, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify(body),
    });
  }
  function notify(message, bad = false) {
    if (typeof toast === 'function') toast(message, bad);
    else alert(message);
  }

  function installStyles() {
    if (document.getElementById('v3AuthoringStyles')) return;
    const style = document.createElement('style');
    style.id = 'v3AuthoringStyles';
    style.textContent = `
      .v3-ref-panel{margin-top:14px}
      .v3-ref-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}
      .v3-ref-head .sub{max-width:900px}
      .v3-ref-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:12px;margin-top:12px}
      .v3-ref-card{border:1px solid var(--border,#2a3548);border-radius:12px;background:rgba(10,18,31,.58);padding:12px;min-width:0}
      .v3-ref-card h4{margin:0 0 4px;font-size:16px}
      .v3-ref-meta{font-size:12px;color:#92a1b8;margin-bottom:8px}
      .v3-ref-preview{width:100%;aspect-ratio:4/3;object-fit:contain;border-radius:9px;background:#070d16;border:1px solid #263248;margin:8px 0}
      .v3-ref-prompt{width:100%;min-height:126px;resize:vertical;font-size:12px;line-height:1.55}
      .v3-ref-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:9px}
      .v3-ref-status{display:inline-flex;padding:3px 8px;border-radius:999px;font-size:12px;border:1px solid #31405a}
      .v3-ref-status.ready{color:#8fe0b5;border-color:#2e7251;background:#102b20}
      .v3-ref-status.wait{color:#ffd88a;border-color:#6f5426;background:#2b2110}
      .v3-ref-status.bad{color:#ffaaaa;border-color:#763c45;background:#2a1318}
      .v3-stage-edit{margin-left:auto;border:1px solid #496384;background:#16243a;color:#cfe2ff;border-radius:7px;padding:4px 8px;font-size:12px;cursor:pointer}
      .v3-stage-edit:hover{background:#203553}
      .v3-revision-note{margin-top:8px;font-size:12px;color:#9cafc7}
    `;
    document.head.appendChild(style);
  }

  function ensureReferencePanel() {
    const view = document.getElementById('view-create');
    if (!view) return null;
    let panel = document.getElementById('v3ReferencePanel');
    if (panel) return panel;
    panel = document.createElement('div');
    panel.id = 'v3ReferencePanel';
    panel.className = 'panel v3-ref-panel';
    panel.innerHTML = `
      <div class="v3-ref-head">
        <div>
          <h3 style="margin:0">一致性参考资产</h3>
          <div class="sub" style="margin-top:5px">角色、场景、道具先形成稳定参考图候选；你确认“采用”后，⑤分镜制作才会使用。无需手工上传参考图。</div>
        </div>
        <div class="row" style="gap:8px">
          <button class="btn" type="button" onclick="v3RefreshReferences(true)">刷新</button>
          <button class="btn primary" type="button" onclick="v3GenerateMissingReferences()">自动生成缺失参考图</button>
        </div>
      </div>
      <div id="v3ReferenceSummary" class="notice" style="margin-top:10px">正在读取一致性资产…</div>
      <div id="v3ReferenceGrid" class="v3-ref-grid"></div>
    `;
    const summary = view.querySelector('.summaryGrid');
    if (summary && summary.parentNode) summary.parentNode.insertBefore(panel, summary.nextSibling);
    else view.prepend(panel);
    return panel;
  }

  function referencePreview(item) {
    const candidate = item.candidate || {};
    const candidateUrl = (candidate.output_files || [])[0] || '';
    const url = candidateUrl || item.reference_url || '';
    return url ? `<img class="v3-ref-preview" src="${escapeHtml(url)}" loading="lazy">` : '<div class="empty" style="margin:8px 0">还没有参考图</div>';
  }

  function referenceStatus(item) {
    if (item.ready) return ['已采用', 'ready'];
    const candidate = item.candidate || {};
    const state = String(candidate.status || '').toLowerCase();
    if (state === 'failed') return ['生成失败', 'bad'];
    if (state) return [STATUS_ZH[state] || '处理中', 'wait'];
    return ['未生成', ''];
  }

  function renderReferences() {
    const panel = ensureReferencePanel();
    if (!panel) return;
    const state = referenceState;
    const summary = document.getElementById('v3ReferenceSummary');
    const grid = document.getElementById('v3ReferenceGrid');
    if (!state) {
      summary.textContent = '正在读取一致性资产…';
      grid.innerHTML = '';
      return;
    }
    const items = Array.isArray(state.items) ? state.items : [];
    if (!items.length) {
      summary.textContent = '当前还没有需要独立参考图的角色、场景或道具。完成②角色 / ③视觉后这里会自动出现。';
      grid.innerHTML = '';
      return;
    }
    summary.textContent = `共 ${state.required_count || items.length} 个可复用资产，已采用 ${state.ready_count || 0} 个。上传参考图不是必需项；系统可自动生成，但候选必须由你确认采用。`;
    grid.innerHTML = items.map(item => {
      const [label, cls] = referenceStatus(item);
      const candidate = item.candidate || {};
      const cstate = String(candidate.status || '').toLowerCase();
      const cid = String(candidate.candidate_id || '');
      const running = ACTIVE.has(cstate);
      const completed = cstate === 'completed';
      const promptId = `v3refprompt_${String(item.entity_id || '').replace(/[^A-Za-z0-9_-]/g, '_')}`;
      return `<div class="v3-ref-card">
        <div style="display:flex;justify-content:space-between;gap:8px;align-items:flex-start">
          <div><h4>${escapeHtml(item.label)} · ${escapeHtml(item.name || '未命名')}</h4><div class="v3-ref-meta">稳定身份参考，不使用镜头瞬时动作</div></div>
          <span class="v3-ref-status ${cls}">${escapeHtml(label)}</span>
        </div>
        ${referencePreview(item)}
        <label style="font-size:12px">生成要求（可修改后重新生成）</label>
        <textarea class="v3-ref-prompt" id="${promptId}">${escapeHtml(item.prompt_text || '')}</textarea>
        <div class="v3-ref-actions">
          ${completed ? `<button class="btn good" onclick="v3AdoptReference('${escapeHtml(cid)}')">采用候选</button><button class="btn danger" onclick="v3RejectReference('${escapeHtml(cid)}')">丢弃</button>` : ''}
          ${running ? '<button class="btn" disabled>正在生成</button>' : `<button class="btn ${item.ready ? '' : 'primary'}" onclick="v3GenerateReference('${escapeHtml(item.entity_id)}','${promptId}',${item.ready || completed ? 'true' : 'false'})">${item.ready || completed ? '重新生成候选' : '生成候选'}</button>`}
        </div>
      </div>`;
    }).join('');
  }

  async function refreshReferences(showError = false) {
    const projectId = currentProjectId();
    if (!projectId || referenceLoading) return;
    referenceLoading = true;
    try {
      referenceState = await request(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references`);
      referenceProject = projectId;
      renderReferences();
    } catch (error) {
      if (showError) notify(error.message || String(error), true);
    } finally {
      referenceLoading = false;
    }
  }

  window.v3RefreshReferences = refreshReferences;
  window.v3GenerateReference = async (entityId, promptId, force) => {
    const projectId = currentProjectId();
    if (!projectId) return;
    const prompt = String(document.getElementById(promptId)?.value || '').trim();
    try {
      await post(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references/${encodeURIComponent(entityId)}/generate`, {
        force: Boolean(force),
        prompt_text: prompt,
      });
      notify('一致性参考图候选已提交；生成完成后请预览并采用。');
      await refreshReferences(true);
      if (typeof refreshAll === 'function') await refreshAll();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };
  window.v3GenerateMissingReferences = async () => {
    const projectId = currentProjectId();
    if (!projectId) return;
    try {
      const result = await post(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/references/generate-missing`, {});
      const count = (result.submitted_entity_ids || []).length;
      const waiting = (result.waiting_adoption_entity_ids || []).length;
      notify(count ? `已提交 ${count} 个缺失参考图候选。` : waiting ? `有 ${waiting} 个候选已经生成，请先采用。` : '当前没有需要补生成的参考图。');
      referenceState = result.status || referenceState;
      renderReferences();
      if (typeof refreshAll === 'function') await refreshAll();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };
  window.v3AdoptReference = async candidateId => {
    const projectId = currentProjectId();
    if (!projectId || !candidateId) return;
    try {
      await post(`/api/director/workbench/projects/${encodeURIComponent(projectId)}/candidates/${encodeURIComponent(candidateId)}/confirm`, {output_index: 0});
      notify('一致性参考图已采用，后续镜头会自动引用这个正式版本。');
      await refreshReferences(true);
      if (typeof refreshAll === 'function') await refreshAll();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };
  window.v3RejectReference = async candidateId => {
    const projectId = currentProjectId();
    if (!projectId || !candidateId) return;
    try {
      await post(`/api/director/workbench/projects/${encodeURIComponent(projectId)}/candidates/${encodeURIComponent(candidateId)}/reject`, {});
      notify('参考图候选已丢弃，可以修改生成要求后重新生成。');
      await refreshReferences(true);
      if (typeof refreshAll === 'function') await refreshAll();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };

  function installStageEditButtons() {
    const state = currentSnap();
    const project = state?.project;
    const progress = document.getElementById('progress');
    if (!project || !progress) return;
    const completed = new Set((project.completed_stages || []).map(String));
    const steps = Array.from(progress.querySelectorAll('.step'));
    STAGES.forEach(([stage, name], index) => {
      const step = steps[index];
      if (!step) return;
      step.querySelectorAll('.v3-stage-edit').forEach(node => node.remove());
      if (!completed.has(stage)) return;
      const button = document.createElement('button');
      button.type = 'button';
      button.className = 'v3-stage-edit';
      button.textContent = `修改${name}`;
      button.title = `重新打开${name}阶段；旧版本保留，下游结果会标记为需要重算`;
      button.onclick = event => {
        event.stopPropagation();
        window.v3ReopenStage(stage, name);
      };
      step.appendChild(button);
    });
  }

  window.v3ReopenStage = async (stage, name) => {
    const projectId = currentProjectId();
    if (!projectId) return;
    const ok = confirm(`确定重新修改“${name}”阶段吗？\n\n旧版本不会删除，但从该阶段开始的后续分镜、图片、视频和成片都会标记为需要重新生成。`);
    if (!ok) return;
    const reason = prompt('可选：写一句这次要修改什么。后续重新生成时可继续在“额外创作要求”里补充。', '') || '';
    try {
      const result = await post(`/api/v3/studio/projects/${encodeURIComponent(projectId)}/stages/${encodeURIComponent(stage)}/reopen`, {reason});
      notify(`已回到${name}阶段；保留历史版本，${result.stale_asset_count || 0} 个下游资产已停止作为当前版本使用。`);
      referenceState = null;
      if (typeof refreshAll === 'function') await refreshAll();
      if (typeof showView === 'function') showView('create');
    } catch (error) {
      notify(error.message || String(error), true);
    }
  };

  function translateTechnicalError(message) {
    let text = String(message || '');
    if (/image_prompt\s*与\s*representative_state\s*不闭合/i.test(text)) {
      return '当前④分镜来自旧版本，或上游内容已经变化，画面提示词与镜头代表状态不一致。请点击“修改分镜”重新生成并确认④分镜后，再进入⑤制作。';
    }
    if (/video_start_prompt|video_prompt|representative_state|strict-shot-v2/i.test(text) && /不闭合|缺少|旧|invalid|mismatch/i.test(text)) {
      return '当前分镜制作合同已经过期或不完整。请回到④分镜重新生成并确认，不要继续使用旧候选。';
    }
    return text
      .replace(/completed/gi, '已完成')
      .replace(/queued/gi, '排队中')
      .replace(/running/gi, '生成中')
      .replace(/failed/gi, '失败');
  }

  const originalToast = typeof window.toast === 'function' ? window.toast : null;
  if (originalToast) {
    window.toast = function(message, bad = false) {
      return originalToast(translateTechnicalError(message), bad);
    };
  }

  const originalRenderAll = typeof window.renderAll === 'function' ? window.renderAll : null;
  if (originalRenderAll) {
    window.renderAll = function(...args) {
      const value = originalRenderAll.apply(this, args);
      installStageEditButtons();
      ensureReferencePanel();
      const projectId = currentProjectId();
      if (projectId && projectId !== referenceProject) {
        referenceState = null;
        refreshReferences(false);
      } else if (projectId) {
        refreshReferences(false);
      }
      return value;
    };
  }

  installStyles();
  ensureReferencePanel();
  installStageEditButtons();
  setInterval(() => {
    const projectId = currentProjectId();
    if (!projectId) return;
    installStageEditButtons();
    if (projectId !== referenceProject || !referenceState) refreshReferences(false);
  }, 2500);
})();
