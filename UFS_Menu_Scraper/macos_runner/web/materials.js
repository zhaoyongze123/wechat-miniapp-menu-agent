(() => {
  'use strict';
  const $ = (id) => document.getElementById(id);
  if (typeof window.__materialsInit !== 'function') return;
  window.__materialsInit(({ api, token }) => {
    const state = { batches: [], batchId: '', batch: null, storeId: '', detail: null, selectionSeq: 0, imageLoadSeq: 0, filter: 'pending', search: '', selectedCandidate: null, selectedImageIndex: null, manualBox: null, manualBoxImageIndex: null, manualPreviewUrl: '', manualBoxBeforeDraw: null, manualImageBeforeDraw: null, manualPreviewBeforeDraw: '', selectedBeforeDraw: null, manualMode: false, repairMode: false, uploadMode: 'append', dragging: null, busy: false, files: [], pollTimer: null, selectedReleaseId: '', analysisDismissed: false, analysisStarted: '', analysisCloseTimer: null, reanalyzeMode: false };
    const purposeLabels = { ordering: '点餐', enterprise_wechat: '企业微信', other: '其他', unknown: '未知' };
    const statusLabels = { ready_to_analyze: '待分析', uploading: '上传中', queued: '排队分析', analyzing: '分析中', completed: '分析完成', failed: '分析失败', ready: '待确认', interrupted: '已中断', error: '分析失败' };
    const esc = (v) => String(v ?? '');
    const node = (tag, cls, text) => { const n = document.createElement(tag); if (cls) n.className = cls; if (text !== undefined) n.textContent = esc(text); return n; };
    const url = (suffix) => `/api/materials${suffix}`;
    const idPath = (storeId) => `/batches/${encodeURIComponent(state.batchId)}/stores/${encodeURIComponent(storeId)}`;
    function setError(id, message) { const box = $(id); box.classList.remove('inline-success'); box.textContent = message || ''; box.hidden = !message; }
    function setSuccess(id, message) { const box = $(id); box.classList.add('inline-success'); box.textContent = message || ''; box.hidden = !message; }
    function setBusy(value) { state.busy = value; document.querySelectorAll('#page-materials button, #page-materials select, #page-materials input, #page-materials textarea').forEach((el) => { if (!['materials-search', 'materials-files', 'materials-folder'].includes(el.id)) el.disabled = value || el.dataset.baseDisabled === 'true'; }); updateControls(); if ($('materials-manage-dialog')?.open) renderManagement(); }
    function readyToReview() { return !!state.batch && ['completed', 'ready'].includes(state.batch.status); }
    function canEditDetail() { const decision = normalizeDecision(state.detail?.decision || selectedStore()?.decision); return readyToReview() && !!state.detail && (decision === 'pending' || (decision === 'skipped' && state.repairMode)); }
    const OPEN_JOB_STATUSES = ['queued', 'running', 'pause_requested', 'paused', 'interrupted'];
    const ACTIVE_JOB_STATUSES = ['queued', 'running', 'pause_requested'];
    function currentJob() { return state.batch?.job || null; }
    function jobOpen() { return OPEN_JOB_STATUSES.includes(currentJob()?.status); }
    function jobActive() { return ACTIVE_JOB_STATUSES.includes(currentJob()?.status); }
    function updateControls() {
      const ready = readyToReview(), hasDetail = !!state.detail, selected = state.selectedCandidate !== null;
      const batchStatus = state.batch?.status, running = jobActive();
      // 运行态归 job：有未结束任务时按钮只用于查看进度，不再允许重复发起。
      const canAnalyze = batchStatus === 'ready_to_analyze' && !jobOpen(), canReanalyze = batchStatus === 'completed' && !jobOpen();
      $('materials-analyze').textContent = running || jobOpen() ? '查看进度' : canReanalyze ? '重新分析' : '开始分析';
      $('materials-analyze').disabled = state.busy || !state.batchId || !(canAnalyze || canReanalyze || jobOpen());
      $('materials-accept-recommended').disabled = state.busy || !ready || !Number(state.batch?.summary?.pending_stores || 0);
      $('materials-publish').disabled = state.busy || !ready || !Number(state.batch?.summary?.confirmed_stores || 0);
      $('materials-batch').disabled = state.busy || !state.batches.length;
      $('materials-release-version').disabled = state.busy || !(state.batch?.releases || []).length;
      const selectedRelease = (state.batch?.releases || []).find((r) => String(r.release_id) === String(state.selectedReleaseId));
      $('materials-release-info').textContent = selectedRelease
        ? [selectedRelease.active?'当前使用':'',selectedRelease.store_count+' 家',selectedRelease.description,state.batch?.has_changes?'有未发布修改':''].filter(Boolean).join(' · ')
        : '尚未发布';
      $('materials-activate-release').textContent=selectedRelease?.active?'正在使用':'用于采集';
      $('materials-activate-release').disabled = state.busy || !selectedRelease || !!selectedRelease.active || !['completed', 'ready'].includes(state.batch?.status);
      const decision = normalizeDecision(state.detail?.decision || selectedStore()?.decision), editable = decision === 'pending' || (decision === 'skipped' && state.repairMode);
      document.querySelectorAll('input[name=materials-candidate]').forEach(input=>input.disabled=state.busy||!editable);
      $('materials-confirm').hidden = !editable;
      $('materials-confirm').textContent = decision === 'skipped' ? '确认修复并下一家' : '确认并下一家';
      $('materials-skip').hidden = decision !== 'pending';
      $('materials-undo').hidden = decision !== 'confirmed';
      $('materials-next-pending').hidden = decision !== 'skipped';
      $('materials-next-pending').textContent = state.repairMode ? '退出修复' : '重新处理';
      $('materials-confirm').disabled = state.busy || !ready || !hasDetail || !selected;
      $('materials-skip').disabled = state.busy || !ready || !hasDetail;
      $('materials-undo').disabled = state.busy || !ready || !hasDetail;
      $('materials-next-pending').disabled = state.busy || !ready || !hasDetail;
      for (const id of ['materials-store-actions']) $(id).hidden = !editable;
      for (const id of ['materials-store-add-files', 'materials-store-replace-files', 'materials-store-reanalyze']) $(id).disabled = state.busy || !editable;
      $('materials-store-reanalyze').disabled = state.busy || !editable || !(state.detail?.images || []).length;
      $('materials-manual-toggle').hidden = !editable || !hasDetail || !(state.detail.images || []).length;
      $('materials-manual-toggle').disabled = state.busy || !editable;
      $('materials-manual-toggle').textContent = state.manualMode ? '取消框选' : state.manualBox ? '重新框选' : '框选二维码';
      $('materials-manual-remove').hidden = !state.manualBox;
      $('materials-manual-remove').disabled = state.busy || !editable || !state.manualBox;
      const visibleStores = filteredStores();
      const visibleIndex = visibleStores.findIndex((s) => String(s.store_id) === String(state.storeId));
      $('materials-prev').disabled = state.busy || visibleIndex <= 0;
      const noNext=state.busy||visibleIndex<0||visibleIndex>=visibleStores.length-1;
      $('materials-next').disabled=noNext;
      $('materials-review-next').hidden=!['confirmed','skipped'].includes(decision);
      $('materials-review-next').disabled=noNext;
    }
    async function rawRequest(path, options = {}) {
      const send = () => fetch(path, {...options, headers:{...(options.headers || {}),'X-Capture-Token':token},cache:'no-store'});
      let response = await send();
      if (response.status === 403) {
        const session = await api('/api/session'); token = session.token; response = await send();
      }
      let data; try { data = await response.json(); } catch { data = {}; }
      if (!response.ok) throw new Error(data?.detail || `请求失败（${response.status}）`);
      return data;
    }
    async function loadBatches(selectCurrent = true) {
      const previous = state.batchId;
      const response = await api(url('/batches'));
      state.batches = Array.isArray(response.batches) ? response.batches : [];
      if (!state.batchId || !state.batches.some((b) => String(b.batch_id) === String(state.batchId))) state.batchId = (selectCurrent && previous && state.batches.some((b) => String(b.batch_id) === previous) ? previous : '') || String(state.batches.find((b)=>b.current_active)?.batch_id || state.batches[0]?.batch_id || '');
      renderBatchOptions();
      if (state.batchId) await refreshBatch(false); else { state.batch = null; renderBatch(); scheduleBatchPoll(); }
    }
    function renderBatchOptions() {
      const select = $('materials-batch'), selected = state.batchId;
      select.replaceChildren();
      if (!state.batches.length) select.append(node('option', '', '暂无素材批次'));
      for (const b of state.batches) { const label = b.name || `清洗批次 ${String(b.created_at || '').slice(0, 10) || '未命名'}`; const o = node('option', '', label); o.value = String(b.batch_id); select.append(o); }
      select.value = selected;
      $('materials-current-description').textContent = state.batch ? [state.batch.name || '未命名批次', state.batch.description || ''].filter(Boolean).join(' · ') : '尚未选择批次';
    }
    function scheduleBatchPoll() {
      clearTimeout(state.pollTimer); state.pollTimer = null;
      if (!state.batchId || document.hidden) return;
      // 有未结束 job（含暂停/中断）就持续轮询，便于任务中心与弹窗同步计数。
      if (!jobOpen() && state.batch?.status !== 'uploading') return;
      state.pollTimer = window.setTimeout(refreshCurrentBatch, jobActive() ? 1000 : 3000);
    }
    async function refreshBatch(fromPoll = true) {
      if (!state.batchId) return;
      const requestedBatch = state.batchId;
      try {
        const data = await api(url(`/batches/${encodeURIComponent(requestedBatch)}`));
        if (requestedBatch !== state.batchId) return;
        state.batch = data;
        const releases = Array.isArray(data.releases) ? data.releases : [];
        if (!releases.some((r) => String(r.release_id) === String(state.selectedReleaseId))) state.selectedReleaseId = String(releases[0]?.release_id || '');
        const batchRow = state.batches.find((item) => String(item.batch_id) === String(requestedBatch));
        if (batchRow) Object.assign(batchRow, data);
        renderBatchOptions();
        renderBatch();
        if (!fromPoll && state.storeId && !data.stores?.some((s) => String(s.store_id) === String(state.storeId))) { state.storeId = ''; state.detail = null; renderDetail(); }
        if (!fromPoll && data.status === 'completed' && !state.storeId) { const first = (data.stores || []).find((s) => s.decision === 'pending') || data.stores?.[0]; if (first) await selectStore(first.store_id); }
      } catch (err) { if (requestedBatch === state.batchId) setError('materials-batch-error', err.message); }
      finally { scheduleBatchPoll(); }
    }
    function analysisDialogParts() {
      const dialog = $('materials-analysis-dialog');
      if (!dialog) return null;
      return { dialog, title: $('materials-analysis-title'), stage: $('materials-analysis-stage'), track: $('materials-analysis-track'), fill: $('materials-analysis-fill'), count: $('materials-analysis-count'), percent: $('materials-analysis-percent'), action: $('materials-analysis-hide') };
    }
    function analysisProgress() {
      const value = state.batch?.progress || {};
      const total = Math.max(0, Number(value.total) || 0);
      const done = Math.min(total, Math.max(0, Number(value.done) || 0));
      return { total, done, percent: total ? Math.min(100, Math.round(done / total * 100)) : 0 };
    }
    function fillAnalysisDialog(percent, { title, stage, count, action }) {
      const ui = analysisDialogParts();
      if (!ui) return;
      ui.fill.style.width = `${percent}%`;
      ui.track.setAttribute('aria-valuenow', String(percent));
      ui.title.textContent = title;
      ui.stage.textContent = stage;
      ui.count.textContent = count;
      ui.percent.textContent = count ? `${percent}%` : '';
      ui.action.textContent = action;
    }
    function formatDuration(seconds) {
      const value = Math.max(0, Number(seconds) || 0);
      if (!value) return '';
      if (value < 60) return `约 ${Math.max(1, Math.round(value))} 秒`;
      if (value < 3600) return `约 ${Math.round(value / 60)} 分钟`;
      return `约 ${(value / 3600).toFixed(1)} 小时`;
    }
    function syncAnalysisButtons(job) {
      const status = job?.status || '';
      $('materials-analysis-pause').hidden = status !== 'running';
      $('materials-analysis-resume').hidden = !['paused', 'interrupted'].includes(status);
      $('materials-analysis-cancel').hidden = !OPEN_JOB_STATUSES.includes(status);
      $('materials-analysis-retry').hidden = !(Number(job?.failed_stores || job?.counts?.failed || 0) > 0 && ['completed', 'failed'].includes(status));
    }
    function renderAnalysisDialog() {
      const ui = analysisDialogParts();
      if (!ui) return;
      const job = currentJob(), status = job?.status || '', { total, done, percent } = analysisProgress();
      syncAnalysisButtons(job);
      if (ACTIVE_JOB_STATUSES.includes(status)) {
        const stage = status === 'queued' ? '排队等待前一个任务结束…'
          : status === 'pause_requested' ? '正在完成当前门店并暂停…'
          : job?.stage || '本地二维码检测与用途识别';
        const current = job?.current_store_name ? ` · 当前：${job.current_store_name}` : '';
        const eta = formatDuration(job?.eta_seconds);
        fillAnalysisDialog(percent, {
          title: state.reanalyzeMode ? '正在重新分析门店' : '正在分析素材',
          stage: `${stage}${current}${eta ? ` · 预计剩余${eta}` : ''}`,
          count: total ? `${done} / ${total} 家` : '',
          action: '后台运行',
        });
        const otherOpen = [...document.querySelectorAll('#page-materials dialog[open]')].some((dialog) => dialog !== ui.dialog);
        if (!state.analysisDismissed && !ui.dialog.open && !otherOpen) ui.dialog.show();
        return;
      }
      if (['paused', 'interrupted'].includes(status)) {
        fillAnalysisDialog(percent, {
          title: status === 'paused' ? '分析已暂停' : '分析已中断',
          stage: '已处理完的门店结果已保留，点击“继续”从下一家接着跑。',
          count: total ? `${done} / ${total} 家` : '',
          action: '关闭',
        });
        if (!state.analysisDismissed && !ui.dialog.open
          && ![...document.querySelectorAll('#page-materials dialog[open]')].some((dialog) => dialog !== ui.dialog)) ui.dialog.show();
        return;
      }
      if (status === 'failed' && job) {
        fillAnalysisDialog(percent, {
          title: '分析未完成',
          stage: job.error || job.message || '部分门店处理失败，可只重试失败门店。',
          count: total ? `${done} / ${total} 家` : '',
          action: '关闭',
        });
        if (!state.analysisDismissed && !ui.dialog.open
          && ![...document.querySelectorAll('#page-materials dialog[open]')].some((dialog) => dialog !== ui.dialog)) ui.dialog.show();
        return;
      }
      clearTimeout(state.analysisCloseTimer); state.analysisCloseTimer = null;
      if (['completed', 'ready'].includes(state.batch?.status)) {
        state.analysisStarted = '';
        fillAnalysisDialog(100, { title: state.reanalyzeMode ? '重新分析完成' : '分析完成', stage: '正在载入门店与二维码候选…', count: total ? `${total} / ${total} 家` : '', action: '关闭' });
        state.reanalyzeMode = false;
        if (ui.dialog.open) state.analysisCloseTimer = window.setTimeout(() => ui.dialog.close(), 1600);
        return;
      }
      if (['failed', 'error'].includes(state.batch?.status)) {
        fillAnalysisDialog(percent, { title: '分析未完成', stage: state.batch?.error || '分析失败，请检查批次后重试。', count: total ? `${done} / ${total} 家` : '', action: '关闭' });
        if (state.analysisStarted && state.analysisStarted === state.batch?.batch_id && !state.analysisDismissed && !ui.dialog.open
          && ![...document.querySelectorAll('#page-materials dialog[open]')].some((dialog) => dialog !== ui.dialog)) ui.dialog.show();
        state.analysisStarted = '';
        state.reanalyzeMode = false;
      }
    }
    function openAnalysisDialog() {
      const ui = analysisDialogParts();
      if (!ui) return;
      state.analysisDismissed = false;
      renderAnalysisDialog();
      if (!ui.dialog.open) ui.dialog.show();
    }
    function renderBatch() {
      const release=state.batch?.release;
      $('materials-release-info').textContent=release
        ? `${release.version}${release.active?' · 当前使用':''}${state.batch?.has_changes?' · 有未发布修改':''}`
        : '尚未发布素材版本';
      renderReleaseOptions();

      const stores = Array.isArray(state.batch?.stores) ? state.batch.stores : [];
      const counts = { all: stores.length, pending: 0, confirmed: 0, skipped: 0 };
      for (const item of stores) { const decision = normalizeDecision(item.decision); counts[decision]++; }
      for (const [key, count] of Object.entries(counts)) { const target = document.querySelector(`[data-filter-count="${key}"]`); if (target) target.textContent = String(count); }
      if(state.batch?.error)setError('materials-batch-error',`${state.batch.error}。请重新导入素材创建新批次。`);
      const active = jobActive() || state.batch?.status === 'uploading';
      const step = !state.batch ? 0 : active ? 1 : state.batch.snapshot_id ? 3 : ['completed', 'ready'].includes(state.batch.status) ? 2 : state.batch.status === 'ready_to_analyze' ? 0 : 1;
      document.querySelectorAll('.materials-step').forEach((el, i) => { el.classList.toggle('active', i === step); el.classList.toggle('done', i < step); });
      const p = state.batch?.progress;
      if (p && Number(p.total) > 0) { const pct = Math.min(100, Math.round(Number(p.done) / Number(p.total) * 100)); $('materials-progress-bar').style.width = `${pct}%`; $('materials-upload-progress').textContent = `${p.done} / ${p.total}`; }
      const s = state.batch?.summary || {};
      const stats = s.detection_stats || {};
      const roundsText = stats.circle_enabled
        ? ` · 圆形码命中 ${Number(stats.circle?.hit_stores)||0} 家 · 方形码命中 ${Number(stats.square?.hit_stores)||0} 家 · 检测用时 ${((Number(stats.total_ms)||0)/1000).toFixed(1)} 秒`
        : s.circle_detector_error ? ' · 圆形码检测不可用，已按原链路完成' : '';
      $('materials-store-subtitle').textContent = state.batch ? `共 ${Number(s.total_stores)||0} 家门店 · ${Number(s.total_images)||0} 张原图 · ${Number(s.pending_stores)||0} 待审核 · ${Number(s.confirmed_stores)||0} 已确认 · ${Number(s.skipped_stores)||0} 不合格${roundsText}` : '二维码候选和原始图片会显示在这里。';
      renderStores(stores);
      if (active) { const ratio = p && Number(p.total) ? Number(p.done) / Number(p.total) : 0; const stage = state.batch.status === 'analyzing' && p && Number(p.total) ? (stats.circle_enabled ? (ratio < 1 / 3 ? '圆形小程序码检测' : ratio < 2 / 3 ? '方形二维码检测' : '本地 OCR 用途筛选') : (ratio < 0.5 ? '本地二维码检测' : '本地 OCR 用途筛选')) : (statusLabels[state.batch.status] || '处理中'); $('materials-upload-progress').textContent = `${stage}${p && Number(p.total) ? ` · ${p.done}/${p.total} 步` : ''}`; }
      renderAnalysisDialog();
      updateControls();
    }
    function renderReleaseOptions() {
      const select = $('materials-release-version');
      const releases = Array.isArray(state.batch?.releases) ? state.batch.releases : [];
      select.replaceChildren();
      if (!releases.length) {const option=node('option','','暂无已发布版本');option.value='';select.append(option);}
      for (const release of releases) {
        const date = release.published_at ? String(release.published_at).slice(5, 10).replace('-', '/') : '';
        const suffix = release.active ? '当前' : date;
        const option = node('option', '', `${release.version}${suffix ? ` · ${suffix}` : ''}`);
        option.value = String(release.release_id);
        if (release.description) option.title = release.description;
        select.append(option);
      }
      select.value = state.selectedReleaseId;
    }
    function normalizeDecision(value) { return value === 'confirmed' || value === 'confirm' ? 'confirmed' : value === 'skipped' || value === 'skip' ? 'skipped' : 'pending'; }
    function selectedStore() { return (state.batch?.stores || []).find((s) => String(s.store_id) === String(state.storeId)); }
    function filteredStores() { const rows = state.batch?.stores || [], query = state.search.trim().toLocaleLowerCase(); return rows.filter((s) => (state.filter === 'all' || state.filter === normalizeDecision(s.decision)) && (!query || String(s.source_name || '').toLocaleLowerCase().includes(query))); }
    function decisionLabel(store) { const decision = normalizeDecision(store.decision); return decision === 'confirmed' ? '已确认' : decision === 'skipped' ? '不合格' : '待审核'; }
    function renderStores(stores) {
      const list = $('materials-store-list'), scrollTop=list.scrollTop; list.replaceChildren();
      const visible = filteredStores();
      if (!visible.length) { list.append(node('div', 'materials-empty-filter', stores.length ? '没有符合条件的门店。' : '此批次尚无门店。')); return; }
      for (const store of visible) {
        const row = node('button', `materials-store-row${String(store.store_id) === String(state.storeId) ? ' active' : ''}`); row.type = 'button';
        row.disabled = state.busy;
        const main = node('span', 'materials-store-main'), name = node('span', 'materials-store-name', store.source_name || '未命名门店'), meta = node('span', 'materials-store-meta', normalizeDecision(store.decision)==='skipped'?[store.rejection?.label||'未记录原因',store.rejection?.detail].filter(Boolean).join(' · '):store.reason || store.analysis_status || '');
        main.append(name, meta); row.append(main, node('span', `materials-state ${store.decision || ''}`, decisionLabel(store)));
        row.addEventListener('click', () => selectStore(store.store_id)); list.append(row);
      }
      list.scrollTop=scrollTop;
    }
    function storeIndex() { return (state.batch?.stores || []).findIndex((s) => String(s.store_id) === String(state.storeId)); }
    function setReviewView(review){
      $('page-materials').classList.toggle('show-review',review);
      $('materials-show-list').setAttribute('aria-pressed',String(!review));
      $('materials-show-review').setAttribute('aria-pressed',String(review));
    }
    $('materials-show-list').addEventListener('click',()=>setReviewView(false));
    $('materials-show-review').addEventListener('click',()=>setReviewView(true));
    async function selectStore(storeId, repairMode = false) {
      setReviewView(true);
      $('materials-review-body').scrollTop=0;
      $('materials-review-feedback').hidden = true;
      state.storeId = String(storeId); state.repairMode = repairMode; const requestedStore = state.storeId, requestedBatch = state.batchId, seq = ++state.selectionSeq; state.selectedCandidate = null; state.selectedImageIndex = null; clearManualCandidate(); state.detail = null; renderStores(state.batch?.stores || []); renderDetail();
      setError('materials-review-error', '');
      try {
        const detail = await api(url(`/batches/${encodeURIComponent(requestedBatch)}/stores/${encodeURIComponent(requestedStore)}`));
        if (seq !== state.selectionSeq || requestedBatch !== state.batchId || requestedStore !== state.storeId) return;
        state.detail = detail;
        const selected = state.detail.selected;
        if (selected && selected.image_index != null && selected.candidate_index != null) { state.selectedCandidate = `${selected.image_index}:${selected.candidate_index}`; state.selectedImageIndex = selected.image_index; }
        else if (selected?.box) { state.manualBox = selected.box; state.manualBoxImageIndex = Number(selected.image_index); state.selectedImageIndex = selected.image_index; state.selectedCandidate = 'manual'; }
        renderDetail();
      } catch (err) { if (seq === state.selectionSeq && requestedBatch === state.batchId && requestedStore === state.storeId) setError('materials-review-error', err.message); }
    }
    function imageUrl(imageIndex) { return url(`${idPath(state.storeId)}/images/${encodeURIComponent(imageIndex)}`); }
    function candidateUrl(imageIndex, candidateIndex) { return url(`${idPath(state.storeId)}/images/${encodeURIComponent(imageIndex)}/candidates/${encodeURIComponent(candidateIndex)}`); }
    function renderDetail() {
      const d = state.detail;
      $('materials-store-title').textContent = d?.source_name || '未选择门店';
      renderRejectionSummary(d);
      $('materials-store-subtitle').textContent = d ? (d.reason || d.analysis_status || '查看原图与二维码候选，逐项核对用途分类和依据。') : '二维码候选和原始图片会显示在这里。';
      $('materials-original').hidden = true; $('materials-canvas').hidden = true; $('materials-original-empty').hidden = false;
      $('materials-image-list').replaceChildren();
      $('materials-original-empty').textContent = d ? '正在加载原图…' : '暂无原图';
      $('materials-candidates').replaceChildren(node('div', 'materials-placeholder', d ? '正在加载候选…' : '暂无候选'));
      renderManualNote();
      if (!d) { $('materials-candidate-count').textContent = '等待分析'; updateControls(); return; }
      const images = Array.isArray(d.images) ? d.images : [];
      const detectedCount=images.reduce((n, i) => n + (i.candidates?.length || 0), 0), totalCount=detectedCount+(state.manualBox?1:0);
      $('materials-candidate-count').textContent = `${totalCount} 个候选${state.manualBox?' · 含人工框选':''}`;
      const imageList = $('materials-image-list'); imageList.replaceChildren();
      if (state.selectedImageIndex == null && images.length) state.selectedImageIndex = images[0].image_index;
      for (const image of images) { const manualCount=state.manualBox&&String(state.manualBoxImageIndex)===String(image.image_index)?1:0; const choose = node('button', `materials-image-choice${String(image.image_index)===String(state.selectedImageIndex)?' active':''}`, `${image.display_name || `原图 ${Number(image.image_index)+1}`} · ${(image.candidates?.length || 0)+manualCount} 个候选`); choose.type='button'; choose.title=image.display_name || ''; choose.disabled=state.busy; choose.addEventListener('click',()=>{state.selectedImageIndex=image.image_index;state.selectedCandidate=null;state.manualMode=false;renderDetail();}); imageList.append(choose); }
      const host = $('materials-candidates'); host.replaceChildren();
      const candidates = [];
      for (const image of images) for (const c of image.candidates || []) candidates.push({ image, candidate: c });
      const groups = new Map();
      for (const { candidate } of candidates) if (candidate.payload_sha256 && !groups.has(candidate.payload_sha256)) groups.set(candidate.payload_sha256, `G${groups.size + 1}`);
      for (const { image, candidate } of candidates) {
        const card = node('article', `materials-candidate${String(state.selectedCandidate) === `${image.image_index}:${candidate.candidate_index}` ? ' selected' : ''}`);
        const thumb = document.createElement('img'); thumb.className = 'materials-candidate-thumb'; thumb.alt = `候选二维码裁剪 ${candidate.candidate_index + 1}`; thumb.src = candidateUrl(image.image_index, candidate.candidate_index); thumb.addEventListener('click', () => openPreview(thumb.src));
        const title = node('div', 'materials-candidate-title', `原图 ${Number(image.image_index)+1} · 候选 ${Number(candidate.candidate_index) + 1}`); if (candidate.recommendation) title.append(node('span', 'materials-rec-tag', '推荐')); if (candidate.code_type === 'mini_program_code') title.append(node('span', 'materials-rec-tag', '第一轮命中')); if (candidate.source === 'vision_fallback') title.append(node('span', 'materials-rec-tag', '旧批次模型补检'));
        const quality = candidate.quality_score == null ? '未评分' : `质量分 ${Number(candidate.quality_score).toFixed(2)}`;
        const box = candidate.box || {};
        const dimensions = Number.isFinite(Number(box.width)) && Number.isFinite(Number(box.height)) ? `${Math.round(box.width)}×${Math.round(box.height)} px` : '尺寸未知';
        const isMini = candidate.code_type === 'mini_program_code';
        const metaText = isMini
          ? `微信圆形小程序码 · 结构验证通过 · 待微信实际验证 · ${dimensions} · 短边 ${candidate.short_edge_px ?? '—'} px · ${quality}`
          : `同码分组 ${groups.get(candidate.payload_sha256) || '未知'} · ${dimensions} · 短边 ${candidate.short_edge_px ?? '—'} px · ${quality}`;
        const meta = node('div', 'materials-candidate-meta', metaText);
        const purpose = purposeLabels[candidate.purpose] ? candidate.purpose : 'unknown';
        const purposeRow = node('div', 'materials-purpose-row', `用途：${purposeLabels[purpose]}`);
        const reason = node('div', 'materials-purpose-reason', candidate.purpose_reason || (candidate.purpose ? '分类未提供依据，请结合原图人工判断。' : '旧数据没有用途分类，请结合原图人工选择。'));
        const pick = document.createElement('input'); pick.type = 'radio'; pick.name = 'materials-candidate'; pick.disabled=state.busy || !canEditDetail(); pick.checked = String(state.selectedCandidate) === `${image.image_index}:${candidate.candidate_index}`; pick.setAttribute('aria-label', `确认候选 ${candidate.candidate_index + 1}`);
        const select = () => { state.selectedCandidate = `${image.image_index}:${candidate.candidate_index}`; state.selectedImageIndex=image.image_index; state.manualMode = false; renderDetail(); };
        pick.addEventListener('change', select); card.addEventListener('click', (e) => { if (!state.busy && canEditDetail() && e.target !== pick && e.target !== thumb) select(); }); card.append(thumb, title, meta, purposeRow, reason, pick); host.append(card);
      }
      if(state.manualBox) host.append(renderManualCandidate());
      if (!candidates.length && !state.manualBox) host.append(node('div', 'materials-placeholder materials-empty-candidates', '未检测到二维码候选'));
      if (images.length) loadOriginal(state.selectedImageIndex ?? images[0].image_index, state.batchId, state.storeId, state.selectionSeq);
      updateControls();
    }
    function renderManualNote() {
      const note=$('materials-manual-note');
      note.hidden=!state.manualMode&&!state.manualBox;
      note.classList.toggle('is-active',state.manualMode);
      note.textContent=state.manualMode
        ? '拖动框住完整二维码，松开后加入候选。方向键可微调位置。'
        : state.selectedCandidate==='manual'
          ? '已选择人工框选 · 确认后写入当前批次版本；采集时仍需扫码验证。'
          : '人工框选候选 · 选择后确认即可使用。';
    }
    function renderManualCandidate() {
      const imageIndex=Number(state.manualBoxImageIndex ?? state.selectedImageIndex ?? 0), box=state.manualBox;
      const selected=state.selectedCandidate==='manual';
      const card=node('article',`materials-candidate materials-candidate-manual${selected?' selected':''}`);
      const thumb=document.createElement('img'); thumb.id='materials-manual-thumb'; thumb.className='materials-candidate-thumb'; thumb.alt='人工框选二维码区域预览';
      if(state.manualPreviewUrl)thumb.src=state.manualPreviewUrl;
      thumb.addEventListener('click',()=>{if(thumb.src)openPreview(thumb.src);});
      const header=node('div','materials-candidate-title',`原图 ${imageIndex+1} · 人工框选`);
      const tag=node('span','materials-manual-tag','人工'); header.append(tag);
      const meta=node('div','materials-candidate-meta',`框选区域 ${Math.round(box.width)}×${Math.round(box.height)} px`);
      const status=node('div','materials-manual-status',selected?'已选择 · 尚未解码':'未解码');
      const pick=document.createElement('input');pick.type='radio';pick.name='materials-candidate';pick.checked=selected;pick.disabled=state.busy||!canEditDetail();pick.setAttribute('aria-label','选择人工框选的二维码候选');
      const select=()=>{state.selectedCandidate='manual';state.selectedImageIndex=imageIndex;state.manualMode=false;renderDetail();};
      pick.addEventListener('change',select);
      const actions=node('div','materials-manual-candidate-actions');
      const adjust=node('button','btn materials-candidate-secondary','调整框选');adjust.type='button';adjust.disabled=state.busy||!canEditDetail();adjust.addEventListener('click',(event)=>{event.stopPropagation();state.selectedImageIndex=imageIndex;beginManualDrawing();});
      const remove=node('button','btn btn-quiet materials-candidate-secondary','移除');remove.type='button';remove.disabled=state.busy||!canEditDetail();remove.addEventListener('click',(event)=>{event.stopPropagation();removeManualCandidate();});
      actions.append(adjust,remove);
      card.addEventListener('click',(event)=>{if(!state.busy&&canEditDetail()&&!['BUTTON','INPUT','IMG'].includes(event.target.tagName))select();});
      card.append(thumb,header,meta,status,pick,actions);
      return card;
    }
    function renderRejectionSummary(detail) {
      const host = $('materials-rejection-summary'); host.replaceChildren();
      const rejection = normalizeDecision(detail?.decision) === 'skipped' ? (detail?.rejection || { label: '未记录原因', detail: '' }) : null;
      host.hidden = !rejection;
      if (!rejection) return;
      const copy = node('button', 'btn materials-copy-reason', '复制原因'); copy.type = 'button';
      const text = [rejection.label || '未记录原因', rejection.detail].filter(Boolean).join(' · ');
      const reasonText=node('span','materials-rejection-text',text);reasonText.title=text;host.append(reasonText);
      copy.addEventListener('click', async () => { try { await navigator.clipboard.writeText(text); copy.textContent = '已复制'; window.setTimeout(() => { copy.textContent = '复制原因'; }, 1400); } catch { setError('materials-review-error', '复制失败，请检查浏览器剪贴板权限。'); } });
      host.append(copy);
    }
    function loadOriginal(index, batchId, storeId, seq) {
      const img = $('materials-original'), imageSeq = ++state.imageLoadSeq; img.hidden = true;
      const showImage = () => {
        if (imageSeq !== state.imageLoadSeq || batchId !== state.batchId || storeId !== state.storeId || seq !== state.selectionSeq || String(img.dataset.imageIndex) !== String(index)) return;
        img.hidden = false; $('materials-original-empty').hidden = true;
        const hasManualBox=!!state.manualBox&&String(state.manualBoxImageIndex)===String(index);
        if (state.manualMode || hasManualBox) drawManualCanvas(img);
        else $('materials-canvas').hidden=true;
        if(hasManualBox){
          const preview=createManualPreview(img,state.manualBox);
          if(preview){state.manualPreviewUrl=preview;const thumb=$('materials-manual-thumb');if(thumb)thumb.src=preview;}
        }
      };
      img.onload = showImage; img.onerror = () => { if (imageSeq === state.imageLoadSeq && batchId === state.batchId && storeId === state.storeId && seq === state.selectionSeq) $('materials-original-empty').textContent = '原图加载失败'; };
      img.dataset.imageIndex = String(index); const nextSrc = new URL(imageUrl(index), location.href).href;
      if (img.src === nextSrc && img.complete && img.naturalWidth) showImage(); else img.src = nextSrc;
      img.onclick = () => openPreview(img.src);
    }
    $('materials-manual-toggle').addEventListener('click', () => {
      if (!canEditDetail() || state.busy) return;
      if(state.manualMode)cancelManualDrawing();else beginManualDrawing();
    });
    $('materials-manual-remove').addEventListener('click',removeManualCandidate);
    function beginManualDrawing(){
      if(!canEditDetail()||state.busy)return;
      state.manualBoxBeforeDraw=state.manualBox?{...state.manualBox}:null;
      state.manualImageBeforeDraw=state.manualBoxImageIndex;
      state.manualPreviewBeforeDraw=state.manualPreviewUrl;
      state.selectedBeforeDraw=state.selectedCandidate;
      state.manualMode=true;state.dragging=null;state.selectedCandidate=null;
      renderDetail();
      const img=$('materials-original');if(img.naturalWidth)drawManualCanvas(img);
      renderManualNote();updateControls();
      $('materials-canvas').focus();
    }
    function restoreManualBeforeDraw(){
      state.manualBox=state.manualBoxBeforeDraw?{...state.manualBoxBeforeDraw}:null;
      state.manualBoxImageIndex=state.manualImageBeforeDraw;
      state.manualPreviewUrl=state.manualPreviewBeforeDraw;
      state.selectedCandidate=state.selectedBeforeDraw;
      state.manualBoxBeforeDraw=null;state.manualImageBeforeDraw=null;state.manualPreviewBeforeDraw='';state.selectedBeforeDraw=null;
    }
    function cancelManualDrawing(){
      state.manualMode=false;state.dragging=null;restoreManualBeforeDraw();renderDetail();
    }
    function clearManualCandidate(){
      state.manualMode=false;state.dragging=null;state.manualBox=null;state.manualBoxImageIndex=null;state.manualPreviewUrl='';
      state.manualBoxBeforeDraw=null;state.manualImageBeforeDraw=null;state.manualPreviewBeforeDraw='';state.selectedBeforeDraw=null;
    }
    function removeManualCandidate(){
      if(state.busy||!canEditDetail())return;
      if(state.selectedCandidate==='manual')state.selectedCandidate=null;
      clearManualCandidate();
      renderDetail();
    }
    function drawManualCanvas(img) {
      const canvas = $('materials-canvas'), wrap = canvas.parentElement; if (!img.naturalWidth || !wrap.clientWidth) return;
      if (canvas.width !== img.naturalWidth || canvas.height !== img.naturalHeight) { canvas.width = img.naturalWidth; canvas.height = img.naturalHeight; }
      const maxHeight = window.innerHeight * (window.matchMedia('(max-width: 899px)').matches ? .34 : .36);
      const scale = Math.min((wrap.clientWidth - 2) / canvas.width, maxHeight / canvas.height, 1); canvas.style.width = `${canvas.width * scale}px`; canvas.style.height = `${canvas.height * scale}px`;
      canvas.dataset.imageIndex=String(img.dataset.imageIndex||state.selectedImageIndex||'');
      canvas.classList.toggle('is-drawing',state.manualMode);
      const ctx = canvas.getContext('2d'); ctx.clearRect(0,0,canvas.width,canvas.height);ctx.drawImage(img, 0, 0);
      const b = state.manualBox;
      if (b) {
        ctx.fillStyle='rgba(11,11,11,.09)';ctx.fillRect(b.x,b.y,b.width,b.height);
        ctx.lineWidth=Math.max(4,canvas.width/240);ctx.strokeStyle='rgba(255,255,255,.96)';ctx.strokeRect(b.x,b.y,b.width,b.height);
        ctx.lineWidth=Math.max(2,canvas.width/480);ctx.strokeStyle='#0b0b0b';ctx.strokeRect(b.x,b.y,b.width,b.height);
        const handle=Math.max(8,canvas.width/90);ctx.fillStyle='#fff';ctx.strokeStyle='#0b0b0b';ctx.lineWidth=Math.max(2,canvas.width/600);
        for(const [x,y] of [[b.x,b.y],[b.x+b.width,b.y],[b.x,b.y+b.height],[b.x+b.width,b.y+b.height]]){ctx.beginPath();ctx.arc(x,y,handle/2,0,Math.PI*2);ctx.fill();ctx.stroke();}
      }
      canvas.hidden = false; img.hidden = true;
    }
    // 显示画布会缩放适配卡片，但框选坐标始终换算回原图像素，并与原图序号一起提交。
    function canvasPoint(event) { const canvas = $('materials-canvas'), r = canvas.getBoundingClientRect(); return { x: Math.max(0, Math.min(canvas.width, (event.clientX-r.left)*canvas.width/r.width)), y: Math.max(0, Math.min(canvas.height, (event.clientY-r.top)*canvas.height/r.height)) }; }
    function createManualPreview(img,box){
      if(!img?.naturalWidth||!box)return'';
      const pad=Math.max(14,Math.round(Math.max(box.width,box.height)*.22));
      const left=Math.max(0,Math.floor(box.x-pad)),top=Math.max(0,Math.floor(box.y-pad));
      const right=Math.min(img.naturalWidth,Math.ceil(box.x+box.width+pad)),bottom=Math.min(img.naturalHeight,Math.ceil(box.y+box.height+pad));
      const width=Math.max(1,right-left),height=Math.max(1,bottom-top),preview=document.createElement('canvas');
      preview.width=width;preview.height=height;preview.getContext('2d').drawImage(img,left,top,width,height,0,0,width,height);
      return preview.toDataURL('image/jpeg',.88);
    }
    $('materials-canvas').addEventListener('pointerdown', (e) => { if(!state.manualMode||state.busy||!canEditDetail())return; const p=canvasPoint(e);state.dragging=p;state.manualBox=null;state.manualPreviewUrl='';e.currentTarget.focus();e.currentTarget.setPointerCapture(e.pointerId); });
    $('materials-canvas').addEventListener('pointermove', (e) => { if (!state.dragging || state.busy) return; const p=canvasPoint(e);state.manualBox={x:Math.round(Math.min(p.x,state.dragging.x)),y:Math.round(Math.min(p.y,state.dragging.y)),width:Math.round(Math.abs(p.x-state.dragging.x)),height:Math.round(Math.abs(p.y-state.dragging.y))};drawManualCanvas($('materials-original'));renderManualNote(); });
    function finishDrag(){
      if(!state.dragging)return;
      state.dragging=null;
      if(!state.manualBox||state.manualBox.width<10||state.manualBox.height<10){state.manualMode=false;restoreManualBeforeDraw();renderDetail();return;}
      state.manualBoxImageIndex=Number($('materials-canvas').dataset.imageIndex);
      state.manualPreviewUrl=createManualPreview($('materials-original'),state.manualBox);
      state.selectedCandidate='manual';state.manualMode=false;
      state.manualBoxBeforeDraw=null;state.manualImageBeforeDraw=null;state.manualPreviewBeforeDraw='';state.selectedBeforeDraw=null;
      renderDetail();window.requestAnimationFrame(()=>$('materials-canvas').focus());
    }
    $('materials-canvas').addEventListener('pointerup',finishDrag);
    $('materials-canvas').addEventListener('pointercancel',()=>{if(state.dragging)cancelManualDrawing();});
    $('materials-canvas').addEventListener('keydown',(event)=>{
      if(!state.manualBox||!state.manualMode&&state.selectedCandidate!=='manual'||!['ArrowUp','ArrowDown','ArrowLeft','ArrowRight'].includes(event.key))return;
      event.preventDefault();const step=event.shiftKey?10:1,b={...state.manualBox};
      if(event.key==='ArrowLeft')b.x=Math.max(0,b.x-step);if(event.key==='ArrowRight')b.x=Math.min($('materials-canvas').width-b.width,b.x+step);
      if(event.key==='ArrowUp')b.y=Math.max(0,b.y-step);if(event.key==='ArrowDown')b.y=Math.min($('materials-canvas').height-b.height,b.y+step);
      state.manualBox=b;state.manualPreviewUrl=createManualPreview($('materials-original'),b);drawManualCanvas($('materials-original'));const thumb=$('materials-manual-thumb');if(thumb)thumb.src=state.manualPreviewUrl;renderManualNote();
    });
    $('materials-canvas').addEventListener('lostpointercapture',()=>{if(state.dragging)cancelManualDrawing();});
    function openPreview(src) { $('materials-preview-image').src=src; $('materials-preview').hidden=false; }
    function closePreview() { $('materials-preview').hidden=true; $('materials-preview-image').removeAttribute('src'); }
    $('materials-preview-close').addEventListener('click', closePreview); $('materials-preview').addEventListener('click',(e)=>{if(e.target===$('materials-preview'))closePreview();});
    async function uploadStoreFiles(mode, selected) {
      const files=[...selected].filter(file=>/\.(png|jpe?g|webp)$/i.test(file.name)&&(!file.type||/^image\/(png|jpeg|webp)$/i.test(file.type)));
      const total=files.reduce((sum,file)=>sum+file.size,0);
      if(!files.length){setError('materials-review-error','请选择 PNG、JPG 或 WEBP 图片。');return;}
      if(files.length>100||files.some(file=>file.size>50*1024*1024)||total>500*1024*1024){setError('materials-review-error','单店最多补充 100 张图片，每张不超过 50 MB，总量不超过 500 MB。');return;}
      if(mode==='replace'&&(state.detail?.images||[]).length&&!window.confirm('替换后，当前门店将只保留这次选择的图片。原始输入目录不会修改。继续吗？'))return;
      const batchId=state.batchId,storeId=state.storeId;let uploadId='',committed=false;
      setBusy(true);setError('materials-review-error','');
      try{
        const session=await rawRequest(url(`${idPath(storeId)}/uploads`),{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});
        uploadId=String(session.upload_id);const uploaded=[];
        for(const file of files){
          const query=new URLSearchParams({filename:file.name});
          uploaded.push(await rawRequest(url(`${idPath(storeId)}/uploads/${encodeURIComponent(uploadId)}/file?${query.toString()}`),{method:'PUT',headers:{'Content-Type':'application/octet-stream'},body:file}));
        }
        const result=await rawRequest(url(`${idPath(storeId)}/uploads/${encodeURIComponent(uploadId)}/commit`),{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode,files:uploaded})});
        committed=true;
        await refreshBatch();
        if(batchId!==state.batchId||storeId!==state.storeId)return;
        state.detail=await api(url(`${idPath(storeId)}`));
        state.selectedImageIndex=mode==='replace'?0:Math.max(0,(state.detail.images||[]).length-files.length);
        state.selectedCandidate=null;clearManualCandidate();
        $('materials-review-feedback').textContent=`${result.uploaded} 张图片已${mode==='replace'?'替换':'补充'}，请点击“重新分析”。`;
        $('materials-review-feedback').hidden=false;renderDetail();
      }catch(error){
        setError('materials-review-error',error.message);
        if(uploadId&&!committed){try{await rawRequest(url(`${idPath(storeId)}/uploads/${encodeURIComponent(uploadId)}`),{method:'DELETE'});}catch{}}
      }finally{setBusy(false);}
    }
    $('materials-store-add-files').addEventListener('click',()=>{state.uploadMode='append';document.querySelector('.materials-source-menu').open=false;$('materials-store-files').value='';$('materials-store-files').click();});
    $('materials-store-replace-files').addEventListener('click',()=>{state.uploadMode='replace';document.querySelector('.materials-source-menu').open=false;$('materials-store-files').value='';$('materials-store-files').click();});
    document.addEventListener('click',event=>{const menu=document.querySelector('.materials-source-menu');if(menu?.open&&!menu.contains(event.target))menu.open=false;});
    $('materials-store-files').addEventListener('change',event=>{if(event.target.files?.length)void uploadStoreFiles(state.uploadMode,event.target.files);});
    const storeDropTarget = document.querySelector('#page-materials .materials-review-panel');
    let storeDragDepth = 0;
    const clearStoreDropState = () => { storeDragDepth = 0; storeDropTarget.classList.remove('is-file-drop'); };
    storeDropTarget.addEventListener('dragenter', event => {
      if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return;
      event.preventDefault();
      if (!canEditDetail() || state.busy) return;
      storeDragDepth++;
      storeDropTarget.classList.add('is-file-drop');
    });
    storeDropTarget.addEventListener('dragover', event => {
      if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return;
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = canEditDetail() && !state.busy ? 'copy' : 'none';
    });
    storeDropTarget.addEventListener('dragleave', event => {
      if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return;
      storeDragDepth = Math.max(0, storeDragDepth - 1);
      if (!storeDragDepth) storeDropTarget.classList.remove('is-file-drop');
    });
    storeDropTarget.addEventListener('drop', event => {
      if (!Array.from(event.dataTransfer?.types || []).includes('Files')) return;
      event.preventDefault();
      clearStoreDropState();
      if (!canEditDetail() || state.busy) return;
      const files = event.dataTransfer?.files;
      if (!files?.length) { setError('materials-review-error', '没有读取到图片文件；请拖入图片，或使用“选择文件”。'); return; }
      // 拖放默认补充素材，避免一次拖放误替换已有图片。
      state.uploadMode = 'append';
      void uploadStoreFiles('append', files);
    });
    $('materials-store-reanalyze').addEventListener('click',async()=>{
      if(state.busy||!canEditDetail())return;
      const batchId=state.batchId,storeId=state.storeId;setBusy(true);setError('materials-review-error','');
      try{await rawRequest(url(`${idPath(storeId)}/reanalyze`),{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'});await refreshBatch();if(batchId!==state.batchId||storeId!==state.storeId)return;state.detail=await api(url(`${idPath(storeId)}`));state.selectedCandidate=null;state.selectedImageIndex=null;clearManualCandidate();$('materials-review-feedback').textContent='当前门店重新分析完成。';$('materials-review-feedback').hidden=false;renderDetail();}
      catch(error){setError('materials-review-error',error.message);}
      finally{setBusy(false);}
    });
    async function uploadSelectedFiles(metadata = {}) {
      const files = [...state.files];
      let batchId = null;
      if (!files.length || state.busy || files.length > 10000 || files.some((file) => file.size > 50*1024*1024) || files.reduce((sum,file) => sum + file.size,0) > 2*1024*1024*1024) return;
      setBusy(true); setError('materials-import-error',''); $('materials-progress-bar').style.width='0%'; $('materials-upload-progress').textContent='准备上传…';
      try {
        const created = await rawRequest(url('/uploads'), { method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify(metadata) });
        batchId = String(created.batch_id); let done=0;
        for (const file of files) {
          const rel = file.webkitRelativePath || file.name;
          await rawRequest(url(`/uploads/${encodeURIComponent(batchId)}/file?path=${encodeURIComponent(rel)}`), { method:'PUT', headers:{'Content-Type':'application/octet-stream'}, body:file });
          done++; $('materials-progress-bar').style.width=`${Math.round(done/files.length*100)}%`; $('materials-upload-progress').textContent=`${done} / ${files.length}`;
        }
        await rawRequest(url(`/uploads/${encodeURIComponent(batchId)}/finish`), { method:'POST', headers:{'Content-Type':'application/json'}, body:'{}' });
        state.batchId=batchId; state.storeId=''; state.detail=null; state.selectionSeq++; state.selectedCandidate=null; state.selectedImageIndex=null; clearManualCandidate(); state.files=[]; $('materials-files').value=''; $('materials-folder').value=''; $('materials-file-count').textContent=`上传完成：${files.length} 张图片`; await loadBatches();
      } catch(err) {
        let message = err.message;
        if (batchId) {
          try { await api(url(`/uploads/${encodeURIComponent(batchId)}/abort`), {method:'POST',body:'{}'}); }
          catch (abortError) { message += `；上传状态未更新：${abortError.message}`; }
          state.batchId = batchId; state.failedUploadBatchId = batchId;
          await loadBatches(); $('materials-new-submit').textContent = '查看已创建批次';
          message += '。已创建的批次保留在管理列表，可检查或删除。';
        }
        setError('materials-import-error',message);
      }
      finally { setBusy(false); }
    }
    function selectedFiles(files) {
      const raw = [...files];
      if (!raw.length) return; // 浏览器取消文件面板时不会触发 change，也不会发起上传请求。
      const all = raw.filter((file) => /\.(png|jpe?g|webp)$/i.test(file.name) && (!file.type || /^image\/(png|jpeg|webp)$/i.test(file.type)));
      const tooLarge = all.find((file) => file.size > 50*1024*1024), total = all.reduce((sum,file) => sum + file.size,0), tooMany = all.length > 10000;
      state.files = all;
      $('materials-file-count').textContent = `${all.length} 张有效图片已选择${raw.length > all.length ? `（忽略 ${raw.length-all.length} 个非支持文件）` : ''}`;
      const error = tooLarge ? `${tooLarge.webkitRelativePath || tooLarge.name} 超过单文件 50 MB 限制。` : total > 5*1024*1024*1024 ? '所选图片总量超过 5 GB 限制。' : tooMany ? '图片数量超过 10,000 张限制。' : !all.length ? '没有找到支持的图片文件。' : '';
      setError('materials-import-error', error);
      $('materials-new-upload-count').textContent = all.length ? `${all.length} 张图片` : '';
    }
    function openFilePicker(input) {
      if (state.busy) return;
      state.files = [];
      $('materials-file-count').textContent = '尚未选择文件';
      $('materials-upload-progress').textContent = '';
      setError('materials-import-error','');
      input.value = '';
      input.click();
    }
    $('materials-new-pick-files').addEventListener('click', () => { document.querySelector('input[name="materials-source"][value="files"]').checked=true; updateSource(); openFilePicker($('materials-files')); });
    $('materials-new-pick-folder').addEventListener('click', () => { document.querySelector('input[name="materials-source"][value="folder"]').checked=true; updateSource(); openFilePicker($('materials-folder')); });
    $('materials-files').addEventListener('change', (event) => selectedFiles(event.target.files));
    $('materials-folder').addEventListener('change', (event) => selectedFiles(event.target.files));
    const dropZone = $('materials-dropzone');
    dropZone.addEventListener('click', () => {
      if (state.busy) return;
      document.querySelector('input[name="materials-source"][value="files"]').checked = true;
      updateSource();
      openFilePicker($('materials-files'));
    });
    ['dragenter', 'dragover'].forEach((type) => dropZone.addEventListener(type, (event) => {
      event.preventDefault();
      if (event.dataTransfer) event.dataTransfer.dropEffect = 'copy';
      dropZone.classList.add('is-drag-over');
    }));
    ['dragleave', 'dragend'].forEach((type) => dropZone.addEventListener(type, () => dropZone.classList.remove('is-drag-over')));
    dropZone.addEventListener('drop', (event) => {
      event.preventDefault();
      dropZone.classList.remove('is-drag-over');
      if (state.busy) return;
      const files = event.dataTransfer?.files;
      if (!files?.length) {
        state.files = [];
        setError('materials-import-error', '没有读取到图片文件；请拖入图片，或使用“选择文件夹”。');
        return;
      }
      document.querySelector('input[name="materials-source"][value="files"]').checked = true;
      updateSource();
      selectedFiles(files);
    });
    const reanalyzeDialog = $('materials-reanalyze-dialog');
    function reanalyzeCandidates() { return (state.batch?.stores || []).filter((store) => normalizeDecision(store.decision) !== 'confirmed'); }
    function syncReanalyzeSelection() {
      const boxes = [...reanalyzeDialog.querySelectorAll('input[name="materials-reanalyze-store"]')];
      const checked = boxes.filter((box) => box.checked).length, all = $('materials-reanalyze-all');
      all.checked = boxes.length > 0 && checked === boxes.length;
      all.indeterminate = checked > 0 && checked < boxes.length;
      all.disabled = state.busy || !boxes.length;
      $('materials-reanalyze-count').textContent = boxes.length ? `已选 ${checked} / ${boxes.length} 家` : '';
      $('materials-reanalyze-submit').disabled = state.busy || !checked;
    }
    function openReanalyzeDialog() {
      const rows = reanalyzeCandidates(), list = $('materials-reanalyze-list');
      list.replaceChildren();
      if (!rows.length) list.append(node('div', 'reanalyze-empty', '没有可重新分析的门店；已确认的门店需先撤销确认。'));
      for (const store of rows) {
        const row = document.createElement('label'); row.className = 'reanalyze-row';
        const box = document.createElement('input'); box.type = 'checkbox'; box.name = 'materials-reanalyze-store'; box.value = String(store.store_id);
        box.checked = false; box.addEventListener('change', syncReanalyzeSelection);
        row.append(box, node('span', 'reanalyze-name', store.source_name || '未命名门店'), node('span', 'reanalyze-meta', decisionLabel(store)));
        list.append(row);
      }
      setError('materials-reanalyze-error', '');
      syncReanalyzeSelection();
      list.scrollTop = 0;
      reanalyzeDialog.showModal();
    }
    $('materials-reanalyze-cancel').addEventListener('click', () => reanalyzeDialog.close());
    reanalyzeDialog.addEventListener('cancel', (event) => { if (state.busy) event.preventDefault(); });
    $('materials-reanalyze-all').addEventListener('change', (event) => {
      reanalyzeDialog.querySelectorAll('input[name="materials-reanalyze-store"]').forEach((box) => { box.checked = event.target.checked; });
      syncReanalyzeSelection();
    });
    $('materials-reanalyze-submit').addEventListener('click', async () => {
      if (state.busy || !state.batchId) return;
      const storeIds = [...reanalyzeDialog.querySelectorAll('input[name="materials-reanalyze-store"]:checked')].map((box) => box.value);
      if (!storeIds.length) { setError('materials-reanalyze-error', '请选择要重新分析的门店。'); return; }
      setBusy(true); setError('materials-batch-error', '');
      const ui = analysisDialogParts();
      try {
        await api(url(`/batches/${encodeURIComponent(state.batchId)}/reanalyze`), { method: 'POST', body: JSON.stringify({ store_ids: storeIds }) });
        reanalyzeDialog.close();
        state.analysisDismissed = false; state.analysisStarted = state.batchId; state.reanalyzeMode = true;
        if (ui) { fillAnalysisDialog(0, { title: '正在重新分析门店', stage: '正在重跑所选门店的二维码检测与用途识别…', count: `0 / ${storeIds.length} 家`, action: '后台运行' }); if (!ui.dialog.open) ui.dialog.show(); }
        await refreshBatch(false);
      } catch (err) { setError('materials-reanalyze-error', err.message); }
      finally { setBusy(false); if (reanalyzeDialog.open) syncReanalyzeSelection(); }
    });
    $('materials-analyze').addEventListener('click', async () => {
      if (state.busy || !state.batchId) return;
      if (jobOpen()) { openAnalysisDialog(); return; }
      if (state.batch?.status === 'completed') { openReanalyzeDialog(); return; }
      setBusy(true); setError('materials-batch-error',''); state.analysisDismissed = false; state.analysisStarted = state.batchId; state.reanalyzeMode = false;
      const ui = analysisDialogParts();
      if (ui) { fillAnalysisDialog(0, { title: '正在分析素材', stage: '正在创建分析任务…', count: '', action: '后台运行' }); if (!ui.dialog.open) ui.dialog.show(); }
      try { await api(url(`/batches/${encodeURIComponent(state.batchId)}/analyze`),{method:'POST',body:'{}'}); await refreshBatch(false); }
      catch(err) { state.analysisStarted = ''; ui?.dialog.close(); setError('materials-batch-error',err.message); }
      finally { setBusy(false); }
    });
    async function jobAction(action, confirmText='') {
      const job = currentJob();
      if (!job || (confirmText && !window.confirm(confirmText))) return;
      try {
        await api(`/materials/jobs/${encodeURIComponent(job.id)}/${action}`, { method: 'POST', body: '{}' });
        state.analysisDismissed = false;
        await refreshBatch(false);
      } catch (err) { setError('materials-batch-error', err.message); }
    }
    $('materials-analysis-pause').addEventListener('click', () => jobAction('pause'));
    $('materials-analysis-resume').addEventListener('click', () => jobAction('resume'));
    $('materials-analysis-cancel').addEventListener('click', () => jobAction('cancel', '取消当前分析任务？已完成门店的结果会保留。'));
    $('materials-analysis-retry').addEventListener('click', async () => {
      try {
        state.analysisDismissed = false;
        await api(url(`/batches/${encodeURIComponent(state.batchId)}/retry-failed`), { method: 'POST', body: '{}' });
        await refreshBatch(false);
      } catch (err) { setError('materials-batch-error', err.message); }
    });
    $('materials-analysis-hide').addEventListener('click', () => $('materials-analysis-dialog').close());
    $('materials-analysis-dialog').addEventListener('close', () => { state.analysisDismissed = true; clearTimeout(state.analysisCloseTimer); state.analysisCloseTimer = null; });
    $('materials-batch').addEventListener('change', async (e) => { state.analysisDismissed = false; state.analysisStarted = ''; state.reanalyzeMode = false; $('materials-analysis-dialog').close(); state.batchId=e.target.value; state.selectedReleaseId=''; state.selectionSeq++; state.storeId=''; state.detail=null; state.selectedCandidate=null; state.selectedImageIndex=null; clearManualCandidate(); setError('materials-batch-error',''); await refreshBatch(false); renderDetail(); });
    function reconcileVisibleSelection() { if (state.storeId && !filteredStores().some((s) => String(s.store_id) === String(state.storeId))) { const next = filteredStores()[0]; state.selectionSeq++; state.storeId = ''; state.detail = null; if (next) void selectStore(next.store_id); else { renderDetail(); renderStores(state.batch?.stores || []); } } else renderStores(state.batch?.stores || []); updateControls(); }
    $('materials-search').addEventListener('input',(e)=>{state.search=e.target.value;reconcileVisibleSelection();});
    document.querySelectorAll('[data-material-filter]').forEach((b)=>b.addEventListener('click',()=>{state.filter=b.dataset.materialFilter;document.querySelectorAll('[data-material-filter]').forEach((x)=>x.classList.toggle('active',x===b));reconcileVisibleSelection();}));
    async function decide(decision, rejection = null) {
      if (state.busy || !state.detail) return;
      const batchId = state.batchId, storeId = state.storeId, wasRepairing = state.repairMode && normalizeDecision(state.detail.decision)==='skipped', body={decision};
      if (rejection) { body.reject_reason = rejection.code; body.reject_detail = rejection.detail; }
      if (decision==='confirm') {
        if (state.selectedCandidate==='manual' && state.manualBox) { body.image_index=Number(state.manualBoxImageIndex); body.box=state.manualBox; }
        else if (state.selectedCandidate && state.selectedCandidate!=='manual') { const [image_index,candidate_index]=state.selectedCandidate.split(':').map(Number); body.image_index=image_index; body.candidate_index=candidate_index; }
        else return;
      }
      setBusy(true); setError('materials-review-error','');
      try {
        await api(url(`/batches/${encodeURIComponent(batchId)}/stores/${encodeURIComponent(storeId)}/decision`),{method:'POST',body:JSON.stringify(body)});
        await refreshBatch(); if (batchId !== state.batchId || storeId !== state.storeId) return;
        state.detail=await api(url(`/batches/${encodeURIComponent(batchId)}/stores/${encodeURIComponent(storeId)}`));
        if(decision==='confirm'&&wasRepairing){
          state.repairMode=false;state.selectedCandidate=null;clearManualCandidate();renderDetail();
          const rows=state.batch?.stores||[],idx=rows.findIndex(row=>String(row.store_id)===String(storeId));
          const next=rows.slice(idx+1).find(row=>normalizeDecision(row.decision)==='skipped')||rows.slice(0,Math.max(0,idx)).find(row=>normalizeDecision(row.decision)==='skipped');
          if(next)await selectStore(next.store_id,true);
          else{$('materials-review-feedback').textContent='修复已确认，不合格记录已移除。';$('materials-review-feedback').hidden=false;state.filter='all';document.querySelectorAll('[data-material-filter]').forEach(button=>{button.classList.toggle('active',button.dataset.materialFilter==='all');button.setAttribute('aria-pressed',String(button.dataset.materialFilter==='all'));});renderStores(state.batch?.stores||[]);updateControls();}
        }else{renderDetail();await nextPending();}
        return true;
      }
      catch(err){setError(rejectionDialog.open?'materials-rejection-error':'materials-review-error',err.message);return false;} finally{setBusy(false);}
    }
    async function nextPending() { const rows=state.batch?.stores||[], idx=storeIndex(); const next=rows.slice(idx+1).find((s)=>normalizeDecision(s.decision)==='pending') || rows.slice(0,Math.max(0,idx)).find((s)=>normalizeDecision(s.decision)==='pending'); if(next) { $('materials-review-feedback').hidden=true; await selectStore(next.store_id); } else { const feedback=$('materials-review-feedback'); feedback.textContent='当前批次已无待审核门店。'; feedback.hidden=false; renderDetail(); } }
    $('materials-confirm').addEventListener('click',()=>decide('confirm'));
    const rejectionDialog = $('materials-rejection-dialog');
    $('materials-skip').addEventListener('click',()=>{ if (!state.busy && state.detail) { $('materials-rejection-detail').value='';$('materials-rejection-code').value='blur';$('materials-rejection-detail').required=false;$('materials-rejection-detail').placeholder=''; setError('materials-rejection-error',''); rejectionDialog.showModal(); } });
    $('materials-rejection-cancel').addEventListener('click',()=>rejectionDialog.close());
    rejectionDialog.addEventListener('cancel',event=>{if(state.busy)event.preventDefault();});
    $('materials-rejection-code').addEventListener('change',()=>{
      const other=$('materials-rejection-code').value==='other';
      $('materials-rejection-detail').required=other;
      $('materials-rejection-detail').placeholder=other?'请填写具体原因':'';
    });
    $('materials-rejection-form').addEventListener('submit', async (event) => { event.preventDefault(); const code=$('materials-rejection-code').value, detail=$('materials-rejection-detail').value.trim(); if (code==='other' && !detail) { setError('materials-rejection-error','请填写其他原因。'); return; } if(await decide('skip',{code,detail}))rejectionDialog.close(); });
    $('materials-prev').addEventListener('click',()=>moveStore(-1)); $('materials-next').addEventListener('click',()=>moveStore(1));
    async function moveStore(delta) { const rows=filteredStores(), i=rows.findIndex((s)=>String(s.store_id)===String(state.storeId))+delta; if(rows[i]) await selectStore(rows[i].store_id,state.repairMode&&normalizeDecision(rows[i].decision)==='skipped'); }
    async function returnToReview(){
      if(state.busy||!state.detail)return;
      const batchId=state.batchId,storeId=state.storeId;
      setBusy(true);setError('materials-review-error','');
      try{
        await api(url(`/batches/${encodeURIComponent(batchId)}/stores/${encodeURIComponent(storeId)}/decision`),
          {method:'POST',body:JSON.stringify({decision:'pending'})});
        await refreshBatch();
        if(batchId!==state.batchId||storeId!==state.storeId)return;
        state.filter='pending';
        document.querySelectorAll('[data-material-filter]').forEach(button=>{
          button.classList.toggle('active',button.dataset.materialFilter==='pending');
          button.setAttribute('aria-pressed',String(button.dataset.materialFilter==='pending'));
        });
        await selectStore(storeId);
      }catch(err){setError('materials-review-error',err.message);}
      finally{setBusy(false);}
    }
    $('materials-undo').addEventListener('click',returnToReview);
    $('materials-next-pending').addEventListener('click',()=>{
      if(state.busy||!state.detail)return;
      state.repairMode=!state.repairMode;clearManualCandidate();state.selectedCandidate=null;
      $('materials-review-feedback').textContent=state.repairMode?'正在修复此店。修改会保留在原批次中；确认后才从不合格清单移除。':'已退出修复，原不合格记录仍保留。';
      $('materials-review-feedback').hidden=false;renderDetail();
    });
    $('materials-review-next').addEventListener('click',()=>moveStore(1));
    window.addEventListener('materials-open-review', async (event) => {
      const batchId = String(event.detail?.batch_id || ''), storeId = String(event.detail?.store_id || '');
      if (!batchId || !storeId) return;
      state.batchId = batchId; state.storeId = ''; state.detail = null; state.search = ''; state.filter = 'skipped'; state.repairMode = true; state.manualMode = false; state.selectionSeq++;
      $('materials-search').value = '';
      document.querySelectorAll('[data-material-filter]').forEach((button) => {
        const active = button.dataset.materialFilter === 'skipped';
        button.classList.toggle('active', active); button.setAttribute('aria-pressed', String(active));
      });
      setReviewView(true);
      try {
        await loadBatches(false);
        if (String(state.batchId) !== batchId) return;
        if (!(state.batch?.stores || []).some((store) => String(store.store_id) === storeId)) throw new Error('该门店已不在此素材批次中');
        await selectStore(storeId,true);
      } catch (error) { setError('materials-batch-error', error.message); }
    });
    $('materials-accept-recommended').addEventListener('click', async () => {
      if (state.busy) return;
      const batchId = state.batchId, storeId = state.storeId, pendingBefore = Number(state.batch?.summary?.pending_stores || 0);
      setBusy(true); setError('materials-batch-error', '');
      try {
        const result = await api(url(`/batches/${encodeURIComponent(batchId)}/accept-recommendations`), { method: 'POST', body: '{}' });
        await refreshBatch();
        if (batchId === state.batchId) {
          const confirmed = Number(result?.confirmed || 0), left = Math.max(0, pendingBefore - confirmed);
          setSuccess('materials-batch-error', left > 0 ? `已确认 ${confirmed} 家；${left} 家未批量确认（仅识别到企微码或缺少可用候选），请逐店确认。` : `已确认 ${confirmed} 家自动推荐门店。`);
        }
        if (batchId === state.batchId && storeId && storeId === state.storeId) { state.detail = await api(url(`/batches/${encodeURIComponent(batchId)}/stores/${encodeURIComponent(storeId)}`)); renderDetail(); }
      } catch (err) { setError('materials-batch-error', err.message); }
      finally { setBusy(false); }
    });
    const releaseDialog=$('materials-release-dialog');
    $('materials-publish').addEventListener('click',()=>{
      if(state.busy)return;
      setError('materials-release-error','');
      $('materials-release-name').value=state.batch?.name || '';
      $('materials-release-version-input').value=state.batch?.suggested_version || 'v1.0.0';
      $('materials-release-description').value='';
      const changes=state.batch?.draft_changes||{};
      $('materials-release-count').textContent=`${state.batch?.summary?.confirmed_stores||0} 家门店 · 新增 ${changes.added||0} · 移除 ${changes.removed||0} · 更换 ${changes.changed||0}`;
      releaseDialog.showModal();
    });
    $('materials-release-cancel').addEventListener('click',()=>releaseDialog.close());
    releaseDialog.addEventListener('cancel',event=>{if(state.busy)event.preventDefault();});
    $('materials-release-form').addEventListener('submit',async event=>{
      event.preventDefault();
      if(state.busy)return;
      const name=$('materials-release-name').value.trim();
      const version=$('materials-release-version-input').value.trim();
      if(!name){setError('materials-release-error','请填写批次名称。');return;}
      if(!/^v\d+\.\d+\.\d+$/.test(version)){setError('materials-release-error','版本号格式应为 v1.0.0。');return;}
      const description=$('materials-release-description').value.trim();
      setBusy(true);
      setError('materials-release-error','');
      try{
        const result=await api(url(`/batches/${encodeURIComponent(state.batchId)}/publish`),{method:'POST',body:JSON.stringify({name,version,description})});
        state.selectedReleaseId=result.release.release_id;
        releaseDialog.close();
        window.dispatchEvent(new Event("materials-published"));
        await loadBatches();
        setSuccess('materials-batch-error',`${result.release.batch_name} · ${result.release.version} 已发布${result.release.active?'，已用于采集':'，当前任务继续使用原版本'}`);
      }catch(err){
        setError(releaseDialog.open?'materials-release-error':'materials-batch-error',err.message);
      }finally{setBusy(false);}
    });
    $('materials-release-version').addEventListener('change', () => { state.selectedReleaseId = $('materials-release-version').value; updateControls(); });
    $('materials-activate-release').addEventListener('click', async () => {
      if (state.busy || !state.batchId) return;
      const release = (state.batch?.releases || []).find((r) => String(r.release_id) === String(state.selectedReleaseId));
      if (!release || release.active) return;
      setBusy(true); setError('materials-batch-error', '');
      try {
        await api(url(`/batches/${encodeURIComponent(state.batchId)}/activate`), {method:'POST', body:JSON.stringify({version:release.version})});
        window.dispatchEvent(new Event('materials-published'));
        await loadBatches();
        setSuccess('materials-batch-error', `${release.version} 已设为当前采集素材。`);
      } catch (err) { setError('materials-batch-error', err.message); }
      finally { setBusy(false); }
    });
    const newDialog=$('materials-new-dialog');
    function updateSource(){const source=document.querySelector('input[name="materials-source"]:checked')?.value||'path';$('materials-source-path').hidden=source!=='path';$('materials-source-files').hidden=source==='path';$('materials-new-pick-files').hidden=source!=='files';$('materials-new-pick-folder').hidden=source!=='folder';}
    document.querySelectorAll('input[name="materials-source"]').forEach(input=>input.addEventListener('change',updateSource));
    $('materials-new-batch').addEventListener('click',()=>{if(state.busy)return;$('materials-new-form').reset();state.failedUploadBatchId=null;$('materials-new-submit').textContent='创建批次';state.files=[];$('materials-file-count').textContent='尚未选择图片';$('materials-new-upload-count').textContent='';$('materials-upload-progress').textContent='';$('materials-progress-bar').style.width='0';$('materials-new-path').value='';setError('materials-import-error','');updateSource();newDialog.showModal();$('materials-new-name').focus();});
    $('materials-new-cancel').addEventListener('click',()=>{if(!state.busy)newDialog.close();});
    newDialog.addEventListener('cancel',e=>{if(state.busy)e.preventDefault();});
    $('materials-new-form').addEventListener('submit',async e=>{e.preventDefault();if(state.busy)return;if(state.failedUploadBatchId){newDialog.close();return;}const name=$('materials-new-name').value.trim(),description=$('materials-new-description').value.trim(),source=document.querySelector('input[name="materials-source"]:checked')?.value||'path';if(!name){setError('materials-import-error','请填写批次名称。');return;}if(name.length>80||description.length>500){setError('materials-import-error','名称最多 80 字，描述最多 500 字。');return;}if(source==='path'){const inputDir=$('materials-new-path').value.trim();if(!/^\/(?:[^\0]+)?$/.test(inputDir)){setError('materials-import-error','请输入有效的本机绝对目录路径。');return;}setBusy(true);setError('materials-import-error','');try{const result=await api(url('/batches'),{method:'POST',body:JSON.stringify({input_dir:inputDir,name,description})});state.batchId=String(result.batch_id);state.storeId='';state.detail=null;state.selectionSeq++;state.selectedCandidate=null;state.selectedImageIndex=null;clearManualCandidate();newDialog.close();await loadBatches();}catch(err){setError('materials-import-error',err.message);}finally{setBusy(false);}return;}if(!state.files.length){setError('materials-import-error','请至少选择一张 PNG、JPG 或 WEBP 图片。');return;}await uploadSelectedFiles({name,description});if(!$('materials-import-error').hidden)return;newDialog.close();});

    let manageDeleted = false, manageRows = [], editRow = null, managementSequence = 0;
    const manageDialog = $('materials-manage-dialog'), editDialog = $('materials-edit-dialog');
    const actionDialog = $('materials-action-dialog');
    function confirmBatchAction(title, copy, label) {
      $('materials-action-title').textContent = title;
      $('materials-action-copy').textContent = copy;
      $('materials-action-confirm').textContent = label;
      actionDialog.returnValue = '';
      actionDialog.showModal();
      return new Promise(resolve => actionDialog.addEventListener('close', () => resolve(actionDialog.returnValue === 'confirm'), {once:true}));
    }
    async function loadManagement() {
      const seq = ++managementSequence;
      $('materials-manage-list').replaceChildren(node('div','materials-placeholder','正在加载…'));
      setError('materials-manage-error','');
      try {
        const data = await api(url(`/batch-management?deleted=${manageDeleted}`));
        if (seq !== managementSequence) return;
        manageRows = Array.isArray(data.batches) ? data.batches : [];
        renderManagement();
      } catch (err) {
        if (seq !== managementSequence) return;
        $('materials-manage-list').replaceChildren(node('div','materials-empty-filter',`加载失败：${err.message}`));
      }
    }
    function managementButton(label, handler, disabled = false, danger = false) {
      const button = node('button', danger ? 'btn btn-danger' : 'btn', label);
      button.type = 'button'; button.dataset.baseDisabled = String(disabled);
      button.disabled = state.busy || disabled; button.addEventListener('click', handler);
      return button;
    }
    function renderManagement() {
      const list = $('materials-manage-list'), scroll = list.scrollTop;
      list.replaceChildren();
      const query = $('materials-manage-search').value.trim().toLocaleLowerCase();
      const rows = manageRows.filter(b => `${b.name||''} ${b.description||''}`.toLocaleLowerCase().includes(query));
      if (!rows.length) {
        list.append(node('div','materials-empty-filter',query ? '没有匹配的批次' : manageDeleted ? '回收站为空' : '还没有清洗批次'));
        return;
      }
      for (const batch of rows) {
        const row = node('article','materials-manage-row');
        const title = node('div','materials-manage-title');
        title.append(node('strong','',batch.name || '未命名批次'));
        if (batch.current_active) title.append(node('span','materials-current-tag','当前采集使用'));
        if (batch.description) title.append(node('span','materials-manage-description',batch.description));
        const summary = batch.summary || {};
        const info = node('div','materials-manage-info');
        info.append(node('span','',`${statusLabels[batch.status] || '未知状态'} · ${summary.total_stores||0} 家 · ${batch.versions_count||0} 个版本`));
        info.append(node('span','',`${summary.confirmed_stores||0} 已确认 · ${summary.pending_stores||0} 待审核`));
        if (summary.detection_stats?.circle_enabled) info.append(node('span','',`圆形码 ${summary.detection_stats.circle?.hit_stores||0} 家 · 方形码 ${summary.detection_stats.square?.hit_stores||0} 家 · ${((Number(summary.detection_stats.total_ms)||0)/1000).toFixed(1)} 秒`));
        info.append(node('span','',`更新 ${String(batch.updated_at||'').slice(0,16).replace('T',' ')}`));
        const actions = node('div','materials-manage-actions');
        if (manageDeleted) {
          actions.append(managementButton('恢复', () => mutateRow(batch,'restore')));
          actions.append(managementButton('彻底删除', () => mutateRow(batch,'purge'), !batch.can_purge, true));
          if (!batch.can_purge) actions.append(node('small','materials-block-reason',batch.purge_block_reason || '已发布版本或历史记录需要保留'));
        } else {
          actions.append(managementButton('打开', async () => {
            if (state.busy) return;
            state.batchId = String(batch.batch_id); state.storeId = ''; state.detail = null;
            manageDialog.close(); renderBatchOptions(); await refreshBatch(false);
          }));
          actions.append(managementButton('编辑', () => openEdit(batch)));
          if (batch.current_active) {
            actions.append(managementButton('清除选择', () => clearActiveSelection(batch), !batch.can_clear_selection));
            actions.append(node('small','materials-block-reason',batch.clear_selection_block_reason || '切换或清除采集选择后可移入回收站'));
          } else {
            actions.append(managementButton('删除', () => mutateRow(batch,'delete'), !batch.can_delete, true));
            if (!batch.can_delete) actions.append(node('small','materials-block-reason',batch.delete_block_reason));
          }
        }
        row.append(title,info,actions); list.append(row);
      }
      list.scrollTop = scroll;
    }
    async function mutateRow(batch, action) {
      if (state.busy) return;
      const name = batch.name || '未命名批次';
      if (action === 'delete' && !await confirmBatchAction('移入回收站', `“${name}”将从可用批次中移除，可随时恢复。已发布版本、原始目录和采集截图保留。`, '移入回收站')) return;
      if (action === 'purge' && !await confirmBatchAction('彻底删除批次', `“${name}”及其程序托管的上传、分析文件将永久删除，无法恢复。用户原始目录和最终菜单截图保留。`, '彻底删除')) return;
      setBusy(true);
      try {
        const path = url(`/batches/${encodeURIComponent(batch.batch_id)}`);
        if (action === 'restore') await api(`${path}/restore`, {method:'POST',body:'{}'});
        else await api(action === 'purge' ? `${path}/permanent` : path, {method:'DELETE'});
        await loadBatches(); await loadManagement();
        window.dispatchEvent(new Event('materials-published'));
        setSuccess('materials-manage-error', action === 'restore' ? '批次已恢复' : action === 'purge' ? '批次已彻底删除' : '已移入回收站');
      } catch (err) { setError('materials-manage-error',err.message); }
      finally { setBusy(false); }
    }
    function openEdit(batch) {
      editRow = batch; $('materials-edit-name').value = batch.name || '';
      $('materials-edit-description').value = batch.description || '';
      setError('materials-edit-error',''); editDialog.showModal(); $('materials-edit-name').focus();
    }
    editDialog.addEventListener('cancel', event => { if (state.busy) event.preventDefault(); });
    $('materials-edit-cancel').addEventListener('click', () => editDialog.close());
    $('materials-edit-form').addEventListener('submit', async event => {
      event.preventDefault(); if (!editRow || state.busy) return;
      const name = $('materials-edit-name').value.trim(), description = $('materials-edit-description').value.trim();
      if (!name) { setError('materials-edit-error','请输入批次名称'); return; }
      setBusy(true);
      try {
        await api(url(`/batches/${encodeURIComponent(editRow.batch_id)}`),{method:'PATCH',body:JSON.stringify({name,description})});
        editDialog.close(); await loadBatches(); await loadManagement();
        window.dispatchEvent(new Event('materials-published'));
        setSuccess('materials-manage-error','批次信息已保存');
      } catch (err) { setError('materials-edit-error',err.message); }
      finally { setBusy(false); }
    });
    async function clearActiveSelection(batch) {
      if (state.busy || !await confirmBatchAction('清除采集选择', `清除“${batch.name}”的当前采集选择。工作台将暂时没有素材；版本和历史结果保留。`, '清除选择')) return;
      setBusy(true);
      try {
        await api(url('/selection/clear'), {method:'POST',body:JSON.stringify({batch_id:batch.batch_id})});
        await loadBatches(); await loadManagement();
        window.dispatchEvent(new Event('materials-published'));
        setSuccess('materials-manage-error','已清除采集选择，可重新选择发布版本');
      } catch (err) { setError('materials-manage-error',err.message); }
      finally { setBusy(false); }
    }
    manageDialog.addEventListener('cancel', event => { if (state.busy) event.preventDefault(); });
    $('materials-manage-batches').addEventListener('click', () => {
      manageDeleted = false; $('materials-manage-search').value = '';
      $('materials-tab-active').setAttribute('aria-selected','true'); $('materials-tab-trash').setAttribute('aria-selected','false');
      manageDialog.showModal(); void loadManagement();
    });
    $('materials-manage-close').addEventListener('click', () => manageDialog.close());
    for (const [id, deleted] of [['materials-tab-active',false],['materials-tab-trash',true]]) {
      $(id).addEventListener('click', () => {
        manageDeleted = deleted; $('materials-manage-search').value = '';
        $('materials-tab-active').setAttribute('aria-selected',String(!deleted));
        $('materials-tab-trash').setAttribute('aria-selected',String(deleted)); void loadManagement();
      });
    }
    $('materials-manage-search').addEventListener('input',renderManagement);
    async function refreshCurrentBatch() { if (!state.batchId || document.hidden) return; await refreshBatch(true); }
    loadBatches(false).catch((err)=>setError('materials-batch-error',err.message));
    document.addEventListener('visibilitychange', () => {
      if (document.hidden) { clearTimeout(state.pollTimer); state.pollTimer = null; }
      else scheduleBatchPoll();
    });
    updateControls();
  });
})();
