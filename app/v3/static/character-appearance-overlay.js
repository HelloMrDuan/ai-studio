(() => {
  let state = null;
  let loading = false;
  let lastProject = '';

  function pid(){ try{return String(current||'').trim()}catch(_){return ''} }
  function escHtml(value){
    if(typeof esc==='function') return esc(value);
    return String(value??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  }
  async function req(url, options={}){
    const response=await fetch(url,options); const text=await response.text(); let body;
    try{body=JSON.parse(text)}catch(_){body=text}
    if(!response.ok) throw new Error(body?.detail||body||`请求失败：${response.status}`);
    return body;
  }
  function notify(message,bad=false){ if(typeof toast==='function') toast(message,bad); else alert(message) }
  function safe(value){ return String(value||'').replace(/[^A-Za-z0-9_-]/g,'_') }

  function styles(){
    if(document.getElementById('v3AppearanceStyles')) return;
    const style=document.createElement('style'); style.id='v3AppearanceStyles';
    style.textContent=`
      .v3-look-panel{margin-top:14px}.v3-look-head{display:flex;justify-content:space-between;gap:12px;align-items:flex-start;flex-wrap:wrap}
      .v3-look-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(330px,1fr));gap:12px;margin-top:12px}
      .v3-look-card{background:rgba(9,17,30,.62);border:1px solid #2d3c52;border-radius:12px;padding:12px}
      .v3-look-card h4{margin:0 0 4px}.v3-look-sub{font-size:11px;color:#8fa4bd;line-height:1.5;margin-bottom:8px}
      .v3-look-field{display:flex;flex-direction:column;gap:5px;margin-top:8px}.v3-look-field label{font-size:11px;color:#9fb0c7}
      .v3-look-field input,.v3-look-field textarea{width:100%;border:1px solid #263754;background:#08111e;color:#eef4ff;border-radius:8px;padding:9px}
      .v3-look-field textarea{min-height:115px;resize:vertical}.v3-look-actions{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:10px}
      .v3-look-badge{display:inline-flex;padding:2px 7px;border-radius:999px;border:1px solid #395170;color:#a9c6ea;font-size:11px;margin-left:6px}
    `;
    document.head.appendChild(style);
  }

  function ensurePanel(){
    const view=document.getElementById('view-create'); if(!view) return null;
    let panel=document.getElementById('v3CharacterAppearancePanel'); if(panel) return panel;
    panel=document.createElement('div'); panel.id='v3CharacterAppearancePanel'; panel.className='panel v3-look-panel';
    panel.innerHTML=`
      <div class="v3-look-head">
        <div>
          <h3 style="margin:0">角色形象版本</h3>
          <div class="sub" style="margin-top:5px">同一个角色可以有默认造型、冬装、战斗破损、结局造型等版本。分镜绑定具体版本，不再让角色整部片从头到尾只有一套衣服。</div>
        </div>
        <button class="btn" type="button" onclick="v3RefreshCharacterAppearances(true)">刷新形象版本</button>
      </div>
      <div id="v3AppearanceSummary" class="notice" style="margin-top:10px">完成②角色后会自动建立默认造型。</div>
      <div id="v3AppearanceGrid" class="v3-look-grid"></div>
    `;
    const assetPanel=document.getElementById('v3AssetAuthoringPanel');
    if(assetPanel?.parentNode) assetPanel.parentNode.insertBefore(panel,assetPanel.nextSibling);
    else view.appendChild(panel);
    return panel;
  }

  function byCharacter(){
    const rows=Array.isArray(state?.appearances)?state.appearances:[]; const map={};
    rows.forEach(row=>{const key=String(row.character_entity_id||'');(map[key]||(map[key]=[])).push(row)});
    return map;
  }

  function render(){
    if(!ensurePanel()) return;
    const summary=document.getElementById('v3AppearanceSummary'); const grid=document.getElementById('v3AppearanceGrid');
    const groups=byCharacter(); const keys=Object.keys(groups);
    if(!keys.length){summary.textContent='当前还没有角色形象版本。完成②角色并确认后，系统会从角色稳定设定生成默认造型。';grid.innerHTML='';return}
    const count=(state?.appearances||[]).length; summary.textContent=`当前 ${keys.length} 个角色，共 ${count} 个正式形象版本。修改形象版本会形成新版本，不静默覆盖历史。`;
    const names={};
    try{(window.__v3AuthoringAssets?.items||[]).forEach(x=>{if(x.entity_type==='character')names[x.entity_id]=x.name})}catch(_){}
    grid.innerHTML=keys.map(entityId=>{
      const rows=groups[entityId]; const display=names[entityId]||rows[0]?.character_name||'角色';
      return `<div class="v3-look-card">
        <h4>${escHtml(display)}<span class="v3-look-badge">${rows.length} 个形象</span></h4>
        <div class="v3-look-sub">默认造型继承角色稳定身份；剧情服装、伤势或阶段性变化请新增形象版本，并写清变化原因和生效剧情节点。</div>
        ${rows.map(row=>appearanceEditor(entityId,row)).join('')}
        <div class="v3-look-actions"><button class="btn" type="button" onclick="v3NewCharacterAppearance('${escHtml(entityId)}')">新增形象版本</button></div>
      </div>`;
    }).join('');
  }

  function appearanceEditor(entityId,row){
    const key=safe(`${entityId}_${row.appearance_id}`); const nodes=(row.effective_story_node_ids||[]).join(',');
    return `<div style="border-top:1px solid #22324a;margin-top:10px;padding-top:10px">
      <div style="display:flex;justify-content:space-between;gap:8px;align-items:center"><b>${escHtml(row.name||row.appearance_id||'形象')}</b><span class="v3-look-badge">第 ${Number(row.asset_version||1)} 版</span></div>
      <div class="v3-look-field"><label>形象名称</label><input id="look_name_${key}" value="${escHtml(row.name||'')}" /></div>
      <div class="v3-look-field"><label>稳定形象设定</label><textarea id="look_design_${key}">${escHtml(row.stable_design||'')}</textarea></div>
      <div class="v3-look-field"><label>变化原因</label><input id="look_reason_${key}" value="${escHtml(row.change_reason||'')}" placeholder="例如：进入雪山后更换深蓝冬装" /></div>
      <div class="v3-look-field"><label>生效剧情节点（逗号分隔，可空）</label><input id="look_nodes_${key}" value="${escHtml(nodes)}" placeholder="例如：P03,P04,P05" /></div>
      <div class="v3-look-actions"><button class="btn good" type="button" onclick="v3SaveCharacterAppearance('${escHtml(entityId)}','${escHtml(row.appearance_id)}','${key}')">保存为新版本</button></div>
    </div>`;
  }

  async function refresh(showError=false){
    const id=pid(); if(!id||loading) return; loading=true;
    try{state=await req(`/api/v3/studio/projects/${encodeURIComponent(id)}/character-appearances`);lastProject=id;render()}
    catch(error){if(showError)notify(error.message||String(error),true)} finally{loading=false}
  }

  window.v3RefreshCharacterAppearances=refresh;
  window.v3SaveCharacterAppearance=async(entityId,appearanceId,key)=>{
    const id=pid(); if(!id)return;
    const name=String(document.getElementById(`look_name_${key}`)?.value||'').trim();
    const stable=String(document.getElementById(`look_design_${key}`)?.value||'').trim();
    const reason=String(document.getElementById(`look_reason_${key}`)?.value||'').trim();
    const nodes=String(document.getElementById(`look_nodes_${key}`)?.value||'').split(/[,，]/).map(x=>x.trim()).filter(Boolean);
    if(stable.length<8)return notify('形象设定太短，请写清服装、鞋履、配色以及与默认造型不同的稳定特征。',true);
    try{
      const result=await req(`/api/v3/studio/projects/${encodeURIComponent(id)}/character-appearances/${encodeURIComponent(entityId)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({appearance_id:appearanceId,name,stable_design:stable,change_reason:reason,effective_story_node_ids:nodes})});
      notify(`形象版本已保存为第 ${result.asset_version||'新'} 版。`); await refresh(true); if(typeof refreshAll==='function')await refreshAll();
    }catch(error){notify(error.message||String(error),true)}
  };

  window.v3NewCharacterAppearance=async(entityId)=>{
    const id=pid(); if(!id)return;
    const name=(prompt('新形象名称，例如“雪山造型”','')||'').trim(); if(!name)return;
    const reason=(prompt('为什么出现这个形象版本？例如“进入雪山后更换御寒服装”','')||'').trim(); if(!reason)return notify('新增形象版本必须填写变化原因。',true);
    const design=(prompt('写出这个版本的稳定形象设定。可以在默认身份上只描述服装/伤势/固定配色等变化。','')||'').trim(); if(design.length<8)return notify('形象设定太短。',true);
    const nodes=(prompt('生效剧情节点，可用逗号分隔；不知道可以留空','')||'').split(/[,，]/).map(x=>x.trim()).filter(Boolean);
    const appearanceId=`look_${Date.now().toString(36)}`;
    try{
      await req(`/api/v3/studio/projects/${encodeURIComponent(id)}/character-appearances/${encodeURIComponent(entityId)}`,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({appearance_id:appearanceId,name,stable_design:design,change_reason:reason,effective_story_node_ids:nodes})});
      notify('新形象版本已建立。'); await refresh(true); if(typeof refreshAll==='function')await refreshAll();
    }catch(error){notify(error.message||String(error),true)}
  };

  function hook(){
    if(typeof refreshAll!=='function'||refreshAll.__v3AppearanceWrapped)return;
    const original=refreshAll; const wrapped=async function(...args){const result=await original.apply(this,args);const id=pid();if(id!==lastProject)state=null;setTimeout(()=>refresh(false),50);return result};
    wrapped.__v3AppearanceWrapped=true; refreshAll=wrapped;
  }
  function boot(){styles();ensurePanel();hook();setTimeout(()=>refresh(false),180)}
  if(document.readyState==='loading')document.addEventListener('DOMContentLoaded',boot);else boot();
})();
