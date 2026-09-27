(() => {
  const FIELD_GROUPS = [
    ['基础', [
      ['title','镜头标题','text'], ['summary','镜头摘要','textarea'], ['duration_seconds','时长（秒）','number'],
    ]],
    ['画面调度', [
      ['composition','构图','text'], ['shot_size','景别','text'], ['camera','机位','text'], ['camera_move','镜头运动','text'],
      ['action','人物动作','textarea'], ['performance','表演 / 表情','textarea'], ['environment','环境 / 光线','textarea'],
    ]],
    ['声音', [
      ['dialogue','对白','textarea'], ['narration','旁白','textarea'], ['sound','音效','text'], ['music','音乐','text'],
    ]],
    ['严格生成合同', [
      ['representative_state','静态代表状态','textarea'],
      ['video_start_state','视频起始状态','textarea'],
      ['video_end_state','视频结束状态','textarea'],
      ['image_prompt','分镜画面生成要求','textarea'],
      ['video_prompt','视频生成要求','textarea'],
    ]],
  ];
  let activeShotId = '';
  let activeData = null;

  function pid() {
    try { return String(current || '').trim(); } catch (_) { return ''; }
  }
  function escHtml(value) {
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
    if (document.getElementById('v3ShotAuthoringStyles')) return;
    const style = document.createElement('style');
    style.id = 'v3ShotAuthoringStyles';
    style.textContent = `
      .v3-shot-edit-btn{border-color:#496384!important;background:#16243a!important;color:#cfe2ff!important}
      #v3ShotDialog{width:min(980px,94vw);max-height:90vh;padding:0;border:1px solid #33435d;border-radius:14px;background:#101826;color:#e8eef8;box-shadow:0 20px 80px rgba(0,0,0,.55)}
      #v3ShotDialog::backdrop{background:rgba(0,0,0,.68)}
      .v3-shot-modal-head{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;padding:16px 18px;border-bottom:1px solid #29364a;position:sticky;top:0;background:#101826;z-index:2}
      .v3-shot-modal-body{padding:16px 18px;overflow:auto;max-height:calc(90vh - 150px)}
      .v3-shot-group{margin-bottom:16px;padding:12px;border:1px solid #29364a;border-radius:10px;background:#0c1420}
      .v3-shot-group h4{margin:0 0 10px;font-size:14px;color:#b8cbe5}
      .v3-shot-fields{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:10px}
      .v3-shot-field.wide{grid-column:1/-1}
      .v3-shot-field label{display:block;font-size:12px;color:#91a4bd;margin-bottom:5px}
      .v3-shot-field input,.v3-shot-field textarea{width:100%;background:#09111d;color:#edf3fb;border:1px solid #34455f;border-radius:7px;padding:8px}
      .v3-shot-field textarea{min-height:88px;resize:vertical;line-height:1.55}
      .v3-shot-contract-note{padding:9px 11px;border:1px solid #66552c;background:#29220f;color:#f0d48a;border-radius:8px;margin-bottom:10px;font-size:12px;line-height:1.55}
      .v3-shot-modal-foot{display:flex;gap:9px;justify-content:flex-end;padding:12px 18px;border-top:1px solid #29364a;position:sticky;bottom:0;background:#101826}
      @media(max-width:700px){.v3-shot-fields{grid-template-columns:1fr}}
    `;
    document.head.appendChild(style);
  }

  function ensureDialog() {
    let dialog = document.getElementById('v3ShotDialog');
    if (dialog) return dialog;
    dialog = document.createElement('dialog');
    dialog.id = 'v3ShotDialog';
    dialog.innerHTML = `
      <div class="v3-shot-modal-head">
        <div><h3 id="v3ShotDialogTitle" style="margin:0">修改镜头</h3><div id="v3ShotVersion" class="meta" style="margin-top:4px"></div></div>
        <button class="btn" type="button" id="v3ShotClose">关闭</button>
      </div>
      <div class="v3-shot-modal-body">
        <div class="v3-shot-contract-note">这里只修改当前镜头。保存后会生成新的镜头合同版本，只让这个镜头自己的画面、视频及其下游成片失效；其他镜头不受影响。生成画面时仍会执行严格一致性校验。</div>
        <div id="v3ShotFields"></div>
        <div class="field"><label>本次修改说明（可选）</label><input id="v3ShotReason" placeholder="例如：动作改为弯腰拾起玉佩，保持人物服装不变"></div>
      </div>
      <div class="v3-shot-modal-foot"><button class="btn" type="button" id="v3ShotCancel">取消</button><button class="btn good" type="button" id="v3ShotSave">保存为此镜头新版本</button></div>
    `;
    document.body.appendChild(dialog);
    dialog.querySelector('#v3ShotClose').onclick = () => dialog.close();
    dialog.querySelector('#v3ShotCancel').onclick = () => dialog.close();
    dialog.querySelector('#v3ShotSave').onclick = saveShot;
    return dialog;
  }

  function fieldControl(field, label, type, value) {
    const id = `v3shotfield_${field}`;
    const wide = type === 'textarea' ? ' wide' : '';
    if (type === 'textarea') return `<div class="v3-shot-field${wide}"><label>${escHtml(label)}</label><textarea id="${id}" data-shot-field="${field}">${escHtml(value ?? '')}</textarea></div>`;
    if (type === 'number') return `<div class="v3-shot-field"><label>${escHtml(label)}</label><input id="${id}" data-shot-field="${field}" type="number" min="0.1" max="120" step="0.1" value="${escHtml(value ?? '')}"></div>`;
    return `<div class="v3-shot-field"><label>${escHtml(label)}</label><input id="${id}" data-shot-field="${field}" value="${escHtml(value ?? '')}"></div>`;
  }

  function renderFields(fields) {
    const root = document.getElementById('v3ShotFields');
    root.innerHTML = FIELD_GROUPS.map(([group, defs]) => `<section class="v3-shot-group"><h4>${escHtml(group)}</h4><div class="v3-shot-fields">${defs.map(([field,label,type]) => fieldControl(field,label,type,fields?.[field])).join('')}</div></section>`).join('');
  }

  async function openShot(shotId) {
    const project = pid();
    if (!project || !shotId) return;
    try {
      activeData = await request(`/api/v3/studio/projects/${encodeURIComponent(project)}/shots/${encodeURIComponent(shotId)}/authoring`);
      activeShotId = shotId;
      const dialog = ensureDialog();
      document.getElementById('v3ShotDialogTitle').textContent = `修改镜头 · ${shotId}`;
      document.getElementById('v3ShotVersion').textContent = activeData.contract_version ? `当前人工修改合同：第 ${activeData.contract_version} 版` : '当前仍是④自动生成版本';
      document.getElementById('v3ShotReason').value = '';
      renderFields(activeData.fields || {});
      dialog.showModal();
    } catch (error) {
      notify(error.message || String(error), true);
    }
  }

  async function saveShot() {
    const project = pid();
    if (!project || !activeShotId) return;
    const fields = {};
    document.querySelectorAll('#v3ShotDialog [data-shot-field]').forEach(node => {
      fields[node.dataset.shotField] = node.type === 'number' ? Number(node.value) : node.value;
    });
    const reason = String(document.getElementById('v3ShotReason')?.value || '').trim();
    try {
      const result = await request(`/api/v3/studio/projects/${encodeURIComponent(project)}/shots/${encodeURIComponent(activeShotId)}/authoring`, {
        method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify({fields, reason})
      });
      ensureDialog().close();
      notify(`镜头已保存为第 ${result.contract_version || '新'} 版；仅 ${result.stale_asset_count || 0} 个相关下游结果需要重算。`);
      if (typeof refreshAll === 'function') await refreshAll();
      if (typeof showView === 'function') showView('make');
    } catch (error) {
      notify(error.message || String(error), true);
    }
  }

  function installShotButtons(root = document) {
    root.querySelectorAll?.('.shotCard[id^="shotcard_"]').forEach(card => {
      if (card.querySelector('.v3-shot-edit-btn')) return;
      const shotId = String(card.id || '').replace(/^shotcard_/, '');
      if (!shotId) return;
      const actions = card.querySelector('.shotActions');
      if (!actions) return;
      const btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'btn v3-shot-edit-btn';
      btn.textContent = '修改此镜头';
      btn.onclick = () => openShot(shotId);
      actions.appendChild(btn);
    });
  }

  function boot() {
    installStyles();
    ensureDialog();
    installShotButtons();
    const observer = new MutationObserver(mutations => {
      for (const mutation of mutations) {
        for (const node of mutation.addedNodes) {
          if (node instanceof Element) {
            if (node.matches?.('.shotCard[id^="shotcard_"]')) installShotButtons(node.parentElement || document);
            else installShotButtons(node);
          }
        }
      }
    });
    observer.observe(document.body, {childList:true, subtree:true});
  }

  if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', boot);
  else boot();
})();
