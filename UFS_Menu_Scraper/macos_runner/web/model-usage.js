(() => {
  'use strict';

  const PAGE_SIZE = 30;
  const SOURCES = [['all', '全部用途'], ['capture', '门店采集'], ['agent', 'GUI Agent 导航'], ['annotation', '人工标注'], ['connection_test', '连接测试'], ['material_analysis', '素材分析'], ['other', '其他']];
  const GROUPS = [['day', '按日期'], ['run', '按队列'], ['task', '按门店'], ['model', '按模型']];
  const $ = (selector, root = document) => root.querySelector(selector);
  const esc = value => String(value ?? '').replace(/[&<>"']/g, ch => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[ch]));
  const n = value => value == null ? '未返回' : Number(value).toLocaleString('zh-CN');
  const usageValue = (sum, knownCalls) => Number(knownCalls) > 0 ? n(sum) : '未返回';
  const compactId = value => value ? String(value).length > 18 ? `${String(value).slice(0, 8)}…${String(value).slice(-6)}` : String(value) : '—';
  const localDate = date => `${date.getFullYear()}-${String(date.getMonth()+1).padStart(2,'0')}-${String(date.getDate()).padStart(2,'0')}`;
  const timestamp = value => value ? new Date(value).toLocaleString('zh-CN', {hour12:false}) : '—';
  const sourceLabel = source => ({capture:'门店采集', agent:'GUI Agent 导航', annotation:'人工标注', connection_test:'连接测试', material_analysis:'素材分析', other:'其他'})[source] || source || '—';
  const statusLabel = status => ({success:'成功', error:'失败', running:'进行中', interrupted:'中断'})[status] || status || '—';

  function init() {
    const opener = document.getElementById('model-usage-open');
    if (!opener || document.getElementById('model-usage-dialog')) return;
    const dialog = document.createElement('dialog');
    dialog.id = 'model-usage-dialog';
    dialog.className = 'model-usage-dialog';
    dialog.setAttribute('aria-labelledby', 'mu-title');
    dialog.innerHTML = `
      <section class="mu-shell">
        <header class="mu-header">
          <div><h2 id="mu-title">模型用量</h2><p class="card-desc">按本机日期查看调用计量与接口结果</p></div>
          <button type="button" class="mu-close" aria-label="关闭">×</button>
        </header>
        <div class="mu-controls">
          <div class="mu-filters">
            <label>开始日期<input id="mu-from" type="date"></label><label>结束日期<input id="mu-to" type="date"></label>
            <label>用途<select id="mu-source">${SOURCES.map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}</select></label>
            <label>队列<select id="mu-run"><option value="">全部队列</option></select></label>
            <label class="mu-group-field">统计维度<select id="mu-group">${GROUPS.map(([v,l])=>`<option value="${v}">${l}</option>`).join('')}</select></label>
            <button type="button" class="btn btn-primary" id="mu-refresh">刷新</button>
          </div>
          <div class="mu-mode-row"><div class="mu-tabs" role="tablist" aria-label="用量视图"><button type="button" class="mu-tab active" data-mode="stats" role="tab" aria-selected="true">统计</button><button type="button" class="mu-tab" data-mode="calls" role="tab" aria-selected="false">每次调用</button></div><div class="mu-view-tools"><button type="button" class="btn btn-quiet" id="mu-clear">全部 / 清除筛选</button><span id="mu-read-time" class="mu-read-time">尚未读取</span></div></div>
          <div class="mu-summary" id="mu-summary"></div>
          <div class="mu-usage-note" id="mu-usage-note"></div>
          <div class="mu-drill-chip" id="mu-drill-chip" hidden></div>
          <div id="mu-notice" class="mu-notice" aria-live="polite"></div>
        </div>
        <div class="mu-scroll" id="mu-scroll" role="region" aria-label="用量结果" tabindex="0"><div id="mu-content"></div></div>
        <footer class="mu-footer"><span id="mu-page-info"></span><div><button type="button" class="btn" id="mu-prev">上一页</button><button type="button" class="btn" id="mu-next">下一页</button></div></footer>
      </section>`;
    document.body.append(dialog);

    const state = {mode:'stats', page:1, pages:1, request:0, taskId:'', taskName:'', lastFocus:null};
    const from = $('#mu-from', dialog), to = $('#mu-to', dialog), source = $('#mu-source', dialog), run = $('#mu-run', dialog), group = $('#mu-group', dialog);
    const content = $('#mu-content', dialog), summary = $('#mu-summary', dialog), notice = $('#mu-notice', dialog);
    const params = () => {
      const p = new URLSearchParams({date_from:from.value,date_to:to.value,group_by:group.value,source:source.value,page:String(state.page),page_size:String(PAGE_SIZE)});
      if(run.value) p.set('run_id',run.value);
      if(state.taskId) p.set('task_id',state.taskId);
      return p;
    };
    const setMode = value => { state.mode=value; dialog.querySelectorAll('.mu-tab').forEach(tab=>{const active=tab.dataset.mode===value;tab.classList.toggle('active',active);tab.setAttribute('aria-selected',String(active));}); };
    const renderSummary = data => {
      const s=data||{};
      const tiles=[['调用次数',n(s.calls)],['输入 Token',usageValue(s.input_tokens,s.input_tokens_known_calls)],['输出 Token',usageValue(s.output_tokens,s.output_tokens_known_calls)],['总 Token',usageValue(s.total_tokens,s.total_tokens_known_calls)]];
      summary.innerHTML=tiles.map(([label,value],i)=>`<div class="mu-stat ${i===3?'mu-stat-total':''}"><span>${label}</span><strong>${value}</strong>${i?`<small>覆盖 ${n(s[['input_tokens_known_calls','output_tokens_known_calls','total_tokens_known_calls'][i-1]])} / ${n(s.calls)} 次调用</small>`:''}</div>`).join('');
      $('#mu-usage-note',dialog).textContent=`用量仅含已知记录 · 接口结果：成功 ${n(s.success_calls)} / 失败 ${n(s.error_calls)} / 进行中 ${n(s.running_calls)} / 中断 ${n(s.interrupted_calls)}（成功仅表示接口调用成功，不代表门店点击成功）· 用量状态：完整 ${n(s.known_usage_calls)} / 部分 ${n(s.partial_usage_calls)} / 未返回 ${n(s.unknown_usage_calls)}`;
    };
    const clickAttrs = (kind,value) => `class="mu-link" data-kind="${kind}" data-value="${esc(value)}"`;
    const renderGroups = data => {
      const rows=data.groups||[];
      if(!rows.length) return emptyMarkup();
      const first=group.value==='day'?'日期':group.value==='run'?'队列':group.value==='task'?'门店':'模型';
      return `<div class="mu-group-count">共 ${n(data.groups_total ?? rows.length)} 个分组${Number(data.groups_total)>200?'，最多展示 200 组，请缩小日期或用途范围。':''}</div><div class="mu-table-wrap"><table class="mu-table"><thead><tr><th>${first}</th><th>调用</th><th>成功</th><th>失败</th><th>已知用量</th><th>部分用量</th><th>未返回用量</th><th>输入 Token</th><th>输出 Token</th><th>总 Token</th><th>耗时</th></tr></thead><tbody>${rows.map(row=>{
        let kind='',value='';
        if(group.value==='run'&&row.run_id){kind='run';value=row.run_id;}
        else if(group.value==='task'&&row.task_id){kind='task';value=row.task_id;}
        else if(group.value==='day'){kind='day';value=row.key;}
        const label=row.source_name||row.label||row.key||'—';
        return `<tr><td>${kind?`<button type="button" ${clickAttrs(kind,value)} data-name="${esc(row.source_name||label)}">${esc(label)}</button>`:esc(label)}</td><td>${n(row.calls)}</td><td>${n(row.success_calls)}</td><td>${n(row.error_calls)}</td><td>${n(row.known_usage_calls)}</td><td>${n(row.partial_usage_calls)}</td><td>${n(row.unknown_usage_calls)}</td><td>${usageValue(row.input_tokens,row.input_tokens_known_calls)}</td><td>${usageValue(row.output_tokens,row.output_tokens_known_calls)}</td><td><strong>${usageValue(row.total_tokens,row.total_tokens_known_calls)}</strong></td><td>${n(row.elapsed_ms)} ms</td></tr>`;
      }).join('')}</tbody></table></div>`;
    };
    const emptyMarkup = () => `<div class="mu-empty"><span class="mu-empty-icon">∅</span><strong>暂无模型用量记录</strong><p>计量启用后记录，历史调用无法补算。</p></div>`;
    const renderCalls = data => {
      const rows=data.calls||[];
      if(!rows.length) return emptyMarkup();
      return `<div class="mu-table-wrap"><table class="mu-table mu-call-table"><thead><tr><th>时间</th><th>门店 / 用途</th><th>模型</th><th>输入 Token</th><th>输出 Token</th><th>总 Token</th><th>耗时</th><th>接口结果</th></tr></thead><tbody>${rows.map((c,i)=>`<tr class="mu-call-row"><td><button type="button" class="mu-expand" data-expand="${i}" aria-expanded="false">＋</button>${esc(timestamp(c.started_at))}</td><td>${c.task_id?`<button type="button" ${clickAttrs('task',c.task_id)} data-name="${esc(c.source_name||compactId(c.task_id))}">${esc(c.source_name||compactId(c.task_id))}</button>`:esc(c.source_name||'—')}<small>${esc(sourceLabel(c.source))}</small></td><td>${esc(c.returned_model||c.model||'—')}<small>${esc(c.provider||'')}</small></td><td>${n(c.input_tokens)}</td><td>${n(c.output_tokens)}</td><td><strong>${n(c.total_tokens)}</strong></td><td>${n(c.elapsed_ms)} ms</td><td><span class="mu-status mu-${esc(c.status)}">${esc(statusLabel(c.status))}</span></td></tr><tr class="mu-detail-row" data-detail="${i}" hidden><td colspan="8"><div class="mu-detail-grid">${[['run_id',compactId(c.run_id)],['task_id',compactId(c.task_id)],['阶段',c.stage],['步骤',c.step],['尝试 ID',compactId(c.attempt_id)],['缓存命中（输入内）',c.cached_tokens],['缓存写入（输入内）',c.cache_write_tokens],['推理 Token（输出内）',c.reasoning_tokens],['图像 Token',c.image_tokens],['请求 ID',c.request_id],['错误类型',c.error_type],['HTTP 状态',c.http_status],['用量状态',c.usage_state],['结束时间',timestamp(c.finished_at)]].map(([k,v])=>`<div><span>${esc(k)}</span><strong>${v==null||v===''?'—':esc(k.includes('Token')||k.includes('tokens')?n(v):v)}</strong></div>`).join('')}${c.error_type?`<div class="mu-error-detail"><span>错误</span><strong>${esc(c.error_type)}</strong></div>`:''}</div></td></tr>`).join('')}</tbody></table></div>`;
    };
    async function load() {
      const request=++state.request;
      summary.replaceChildren();
      $('#mu-usage-note',dialog).textContent='';
      $('#mu-drill-chip',dialog).hidden=!state.taskId;
      $('#mu-drill-chip',dialog).innerHTML=state.taskId?`门店筛选：<strong>${esc(state.taskName||compactId(state.taskId))}</strong><button type="button" id="mu-clear-task">清除门店筛选</button>`:'';
      notice.textContent='正在读取…'; notice.className='mu-notice is-loading'; content.innerHTML='';
      $('#mu-refresh',dialog).disabled=true;
      if(state.mode==='stats'){
        $('#mu-page-info',dialog).textContent='';
        $('.mu-footer',dialog).hidden=true;
      } else $('.mu-footer',dialog).hidden=false;
      if(from.value&&to.value&&from.value>to.value){
        notice.textContent='开始日期不能晚于结束日期，请调整日期范围。';notice.className='mu-notice is-error';content.innerHTML='';$('#mu-refresh',dialog).disabled=false;return;
      }
      try {
        const response=await fetch(`/api/model-usage?${params()}`,{method:'GET',cache:'no-store',headers:{Accept:'application/json'}});
        let data={}; try{data=await response.json();}catch{}
        if(!response.ok) throw new Error(data.detail||data.error||`请求失败（${response.status}）`);
        if(request!==state.request) return;
        renderSummary(data.summary);
        const runs=data.filters?.runs||[];
        const selected=run.value;
        run.innerHTML='<option value="">全部队列</option>'+runs.map(item=>`<option value="${esc(item.run_id)}">${esc(item.label||compactId(item.run_id))}</option>`).join('');
        if(runs.some(item=>String(item.run_id)===selected)) run.value=selected;
        state.page=Math.max(1,Number(data.pagination?.page)||state.page);
        state.pages=Math.max(1,Number(data.pagination?.pages)||1);
        if(state.mode==='calls'){
          $('#mu-page-info',dialog).textContent=`第 ${data.pagination?.page||state.page} / ${state.pages} 页 · 共 ${n(data.pagination?.total)} 条`;
          $('#mu-prev',dialog).disabled=state.page<=1; $('#mu-next',dialog).disabled=state.page>=state.pages;
        }
        content.innerHTML=state.mode==='stats'?renderGroups(data):renderCalls(data);
        notice.textContent='';notice.className='mu-notice';
        $('#mu-read-time',dialog).textContent=`最近读取 ${new Date().toLocaleTimeString('zh-CN',{hour12:false})}`;
      } catch(error) {
        if(request!==state.request)return;
        content.innerHTML=`<div class="mu-error">${esc(error.message||'读取失败')}</div>`;
        notice.textContent='读取失败，可手动重试';notice.className='mu-notice is-error';
      } finally {if(request===state.request)$('#mu-refresh',dialog).disabled=false;}
    }
    function resetDates(){const today=new Date(),start=new Date(today);start.setDate(start.getDate()-6);from.value=localDate(start);to.value=localDate(today);}
    resetDates();
    function clearFilters(){resetDates();source.value='all';run.value='';state.taskId='';state.taskName='';state.page=1;group.value='day';setMode('stats');load();}
    opener.addEventListener('click',()=>{state.lastFocus=document.activeElement;if(!dialog.open){dialog.showModal();load();}});
    $('.mu-close',dialog).addEventListener('click',()=>dialog.close());
    dialog.addEventListener('close',()=>state.lastFocus?.focus());
    dialog.addEventListener('cancel',()=>{});
    for(const control of [from,to,source,run,group]) control.addEventListener('change',()=>{state.page=1;state.taskId='';state.taskName='';load();});
    $('#mu-refresh',dialog).addEventListener('click',load);
    $('#mu-clear',dialog).addEventListener('click',clearFilters);
    dialog.querySelectorAll('.mu-tab').forEach(tab=>tab.addEventListener('click',()=>{setMode(tab.dataset.mode);state.page=1;load();}));
    $('#mu-prev',dialog).addEventListener('click',()=>{if(state.page>1){state.page--;load();}});
    $('#mu-next',dialog).addEventListener('click',()=>{if(state.page<state.pages){state.page++;load();}});
    content.addEventListener('click',event=>{
      const action=event.target.closest('[data-kind]');
      if(action){const {kind,value}=action.dataset;state.page=1;if(kind==='run'&&value){run.value=value;group.value='task';setMode('stats');state.taskId='';state.taskName='';}else if(kind==='task'&&value){state.taskId=value;state.taskName=action.dataset.name||action.textContent;setMode('calls');}else if(kind==='day'){from.value=value;to.value=value;group.value='run';setMode('stats');}load();return;}
      const expander=event.target.closest('[data-expand]');if(expander){const index=expander.dataset.expand,row=$(`[data-detail="${index}"]`,content),open=row.hidden;row.hidden=!open;expander.textContent=open?'−':'＋';expander.setAttribute('aria-expanded',String(open));}
    });
    $('#mu-drill-chip',dialog).addEventListener('click',event=>{if(event.target.closest('#mu-clear-task')){state.taskId='';state.taskName='';state.page=1;load();}});
  }
  if(document.readyState==='loading') document.addEventListener('DOMContentLoaded',init,{once:true}); else init();
})();
