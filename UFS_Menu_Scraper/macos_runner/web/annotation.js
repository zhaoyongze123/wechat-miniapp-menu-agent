(() => {
  'use strict';

  const $ = (id) => document.getElementById(id);
  const CATEGORY_LABELS = {
    miniapp_home: '小程序首页 / 门店入口',
    store_select: '定位 / 选择门店',
    people_grid: '人数选择',
    consent_checkbox: '协议 / 隐私勾选',
    close_icon: '广告 / 弹窗关闭',
    system_notification: '系统权限 / 通知',
    resting_menu: '菜单页面',
    other: '其他 / 未归类'
  };
  // 第二层“界面样式”暂不开放：变体必须从真实截图归纳，避免用推测词条引导标注。
  const STATUS_LABELS = {
    idle: '尚未开始',
    starting: '正在启动',
    opening: '正在打开小程序',
    ready: '等待框选',
    clicked: '已点击，等待确认',
    paused: '已暂停',
    closing: '正在关闭小程序',
    close_failed: '关闭小程序失败',
    open_failed: '打开小程序失败',
    error: '标注启动失败',
    finished: '已完成',
    stopped: '已结束'
  };
  const TASK_STATUS_LABELS = {
    pending: '待处理',
    running: '运行中',
    captured: '已成功',
    needs_review: '未检查',
    error: '异常',
    blocked: '阻塞'
  };
  const REJECTION_LABELS = {
    blur: '图片模糊',
    invalid_qr: '二维码无效',
    store_mismatch: '门店不匹配',
    store_closed: '门店已关店',
    miniapp_unavailable: '小程序到期或未绑定',
    scan_order_unsupported: '门店不支持扫码点餐',
    qr_external_web: '二维码指向外部网页（非小程序）',
    menu_unavailable: '门店未配置菜单 / 无可用菜品',
    duplicate: '重复素材',
    other: '其他原因'
  };

  let TOKEN = '';
  let currentPage = 'overview';
  let busy = false;
  let pollTimer = null;
  let state = null;
  let tasksLoaded = false;
  let selected = new Set();
  let taskFilter = 'all';
  let image = null;
  let imageKey = '';
  let box = null;
  let drag = null;
  let stepCursor = -1;
  let stepTaskId = '';
  // 已标注门店的只读回看：不依赖当前会话是否开始/结束。
  let review = null;
  let reviewCursor = 0;
  let reviewImageKey = '';
  // 每张新画面自动跑一次“本地规则 → 模型”的初始判断，供人工审核。
  let autoDecideFrame = '';
  let deciding = false;

  async function refreshToken(force) {
    if (TOKEN && !force) return TOKEN;
    try {
      const response = await fetch('/api/session', {cache: 'no-store'});
      const data = await response.json();
      TOKEN = typeof data?.token === 'string' ? data.token : '';
    } catch {
      TOKEN = '';
    }
    return TOKEN;
  }

  async function api(path, options = {}) {
    const method = (options.method || 'GET').toUpperCase();
    const mutation = !['GET', 'HEAD'].includes(method);
    const send = async () => {
      const headers = {...(options.headers || {})};
      if (mutation) {
        headers['Content-Type'] = 'application/json';
        headers['X-Capture-Token'] = await refreshToken();
      }
      const response = await fetch(path, {...options, headers, cache: 'no-store'});
      let data;
      try { data = await response.json(); }
      catch { data = {detail: `服务器返回了无法读取的响应（${response.status}）`}; }
      return {response, data};
    };
    let {response, data} = await send();
    if (mutation && response.status === 403 && String(data?.detail || '').includes('本机操作令牌无效')) {
      await refreshToken(true);
      ({response, data} = await send());
    }
    if (!response.ok) throw new Error(data?.detail || `请求失败（${response.status}）`);
    return data;
  }

  function toast(message) {
    const node = $('toast');
    if (!node) return;
    node.textContent = message;
    node.classList.add('show');
    clearTimeout(toast._timer);
    toast._timer = setTimeout(() => node.classList.remove('show'), 3200);
  }

  function initOptions() {
    const category = $('anno-category');
    for (const [value, label] of Object.entries(CATEGORY_LABELS)) {
      const option = document.createElement('option');
      option.value = value;
      option.textContent = label;
      category.append(option);
    }
  }

  function taskBucket(task) {
    if (String(task.reject_reason || '').trim()) return 'rejected';
    if (task.stage === 'annotation_complete') return 'annotated';
    switch (task.status) {
      case 'pending': return 'pending';
      case 'needs_review':
      case 'error':
      case 'blocked': return 'review';
      case 'captured': return 'captured';
      default: return 'other';
    }
  }

  function filteredTasks() {
    const tasks = state?.__tasks || [];
    return taskFilter === 'all' ? tasks : tasks.filter((task) => taskBucket(task) === taskFilter);
  }

  function renderFilterCounts() {
    const tasks = state?.__tasks || [];
    const counts = {all: tasks.length, pending: 0, review: 0, captured: 0, annotated: 0, rejected: 0};
    for (const task of tasks) {
      const bucket = taskBucket(task);
      if (bucket in counts) counts[bucket] += 1;
    }
    for (const [key, value] of Object.entries(counts)) {
      const target = document.querySelector(`[data-anno-count="${key}"]`);
      if (target) target.textContent = String(value);
    }
    for (const button of document.querySelectorAll('[data-anno-filter]')) {
      const active = button.dataset.annoFilter === taskFilter;
      button.classList.toggle('active', active);
      button.setAttribute('aria-pressed', String(active));
    }
  }

  function storeSessionState(task, completedMap, rejectedMap, skippedMap) {
    if (state?.active && state?.task?.id === task.id) {
      return {kind: 'current', label: '正在标注'};
    }
    const done = completedMap.get(task.id);
    if (done) return {kind: 'completed', label: `已完成 ${done.steps || 0} 步`};
    if (rejectedMap.has(task.id)) {
      const item = rejectedMap.get(task.id);
      const reason = REJECTION_LABELS[item.reason] || item.reason || '';
      return {kind: 'rejected', label: reason ? `不合格 · ${reason}` : '不合格'};
    }
    if (skippedMap.has(task.id)) return {kind: 'skipped', label: '已跳过'};
    // 已标记不合格的门店不属于“待处理”，否则下次会话会误以为可以继续标注。
    const rejection = String(task.reject_reason || '').trim();
    if (rejection) {
      return {kind: 'rejected', label: `不合格 · ${REJECTION_LABELS[rejection] || rejection}`};
    }
    if (state?.active) return {kind: 'todo', label: '待处理'};
    switch (taskBucket(task)) {
      case 'annotated': return {kind: 'completed', label: '已标注'};
      case 'captured': return {kind: 'completed', label: '已成功'};
      case 'pending': return {kind: 'todo', label: '待处理'};
      default: return {kind: 'review', label: TASK_STATUS_LABELS[task.status] || task.status || '未检查'};
    }
  }

  function renderTasks() {
    renderFilterCounts();
    const list = $('anno-store-list');
    const tasks = state?.__tasks || [];
    const visible = filteredTasks();
    const session = Boolean(state?.active);
    const completedMap = new Map((state?.completed || []).map((item) => [item.task_id, item]));
    const rejectedMap = new Map((state?.rejected || []).map((item) => [item.task_id, item]));
    const skippedMap = new Map((state?.skipped || []).map((item) => [item.task_id, item]));
    const signature = [
      taskFilter,
      session ? state.status : '',
      state?.task?.id || '',
      (state?.completed || []).map((item) => `${item.task_id}:${item.steps}`).join(','),
      (state?.rejected || []).map((item) => item.task_id).join(','),
      (state?.skipped || []).map((item) => item.task_id).join(','),
      tasks.map((task) => `${task.id}:${task.status || ''}:${task.reject_reason ? 1 : 0}`).join('|'),
    ].join('#');
    if (signature === renderTasks._signature) return;
    renderTasks._signature = signature;
    list.textContent = '';
    if (!tasks.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = '当前素材版本没有门店；请先在素材清洗中发布版本。';
      list.append(empty);
      syncSelection();
      return;
    }
    if (!visible.length) {
      const empty = document.createElement('div');
      empty.className = 'empty';
      empty.textContent = '没有符合当前筛选条件的门店。';
      list.append(empty);
      syncSelection();
      return;
    }
    for (const task of visible.slice(0, 300)) {
      const rowState = storeSessionState(task, completedMap, rejectedMap, skippedMap);
      const label = document.createElement('label');
      label.className = `anno-store ${rowState.kind}` + (session ? ' locked' : '');
      if (!session) {
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.value = task.id;
        input.checked = selected.has(task.id);
        input.addEventListener('change', () => {
          if (input.checked) selected.add(task.id);
          else selected.delete(task.id);
          syncSelection();
        });
        label.append(input);
      }
      const copy = document.createElement('span');
      copy.className = 'anno-store-copy';
      const top = document.createElement('span');
      top.className = 'anno-store-top';
      const name = document.createElement('strong');
      name.textContent = task.source_name || task.id;
      const badge = document.createElement('span');
      badge.className = `anno-store-badge ${rowState.kind}`;
      badge.textContent = rowState.label;
      top.append(name, badge);
      const meta = document.createElement('small');
      meta.textContent = task.id;
      copy.append(top, meta);
      label.append(copy);
      if (taskBucket(task) === 'annotated' || completedMap.has(task.id)) {
        const view = document.createElement('button');
        view.type = 'button';
        view.className = 'anno-store-view';
        view.textContent = '查看效果';
        view.title = '回看该门店已确认点击的截图与位置';
        view.addEventListener('click', (event) => {
          // 门店行本身是 label，阻止它把点击转发成勾选。
          event.preventDefault();
          event.stopPropagation();
          void openHistory(task.id);
        });
        label.append(view);
      }
      list.append(label);
    }
    syncSelection();
  }
  renderTasks._signature = '';

  function syncSelection() {
    if (state?.active) {
      const completed = state.completed?.length || 0;
      const rejected = state.rejected?.length || 0;
      const skipped = state.skipped?.length || 0;
      $('anno-selected-count').textContent =
        `第 ${Math.min(state.index + 1, state.total)}/${state.total} 家 · 完成 ${completed} · 不合格 ${rejected}` +
        (skipped ? ` · 跳过 ${skipped}` : '');
    } else {
      $('anno-selected-count').textContent = `已选 ${selected.size} 家`;
    }
    const locked = Boolean(state?.active);
    const inputs = [...$('anno-store-list').querySelectorAll('input[type=checkbox]')];
    for (const input of inputs) input.disabled = locked;
    $('anno-select-all').checked = inputs.length > 0 && inputs.every((input) => input.checked);
    $('anno-select-all').disabled = locked;
    renderButtons();
  }

  async function loadTasks() {
    try {
      const data = await api('/api/status');
      state = state || {};
      state.__tasks = Array.isArray(data.tasks) ? data.tasks : [];
      tasksLoaded = true;
      renderTasks();
    } catch (error) {
      tasksLoaded = false;
      toast(error.message);
    }
  }

  async function refreshState() {
    try {
      const data = await api('/api/annotation/state');
      applyState(data);
    } catch (error) {
      if (!busy) toast(error.message);
    }
  }

  function applyState(next) {
    const previous = state || {};
    const merged = Object.assign({}, next);
    merged.__tasks = previous.__tasks || [];
    const previousSteps = previous.current_steps || [];
    const nextSteps = merged.current_steps || [];
    if ((merged.task?.id || '') !== stepTaskId || (merged.session_id || '') !== (previous.session_id || '')) {
      stepTaskId = merged.task?.id || '';
      stepCursor = -1;
    }
    if (nextSteps.length && (stepCursor < 0 || stepCursor >= nextSteps.length || nextSteps.length > previousSteps.length)) {
      stepCursor = nextSteps.length - 1;
    }
    if (!nextSteps.length) stepCursor = -1;
    if (merged.active && review) clearReviewState();
    state = merged;
    if (['finished', 'stopped', 'idle'].includes(state.status) && previous.status !== state.status) {
      selected = new Set();
      renderTasks._signature = '';
    }
    renderTasks();
    render();
  }

  function render() {
    if (!state) return;
    if (review) {
      renderReview();
      return;
    }
    const status = state.status || 'idle';
    $('anno-status').textContent = STATUS_LABELS[status] || status;
    $('anno-status').className = 'anno-status ' +
      (['ready', 'clicked'].includes(status) ? 'ready' :
        ['starting', 'opening', 'closing'].includes(status) ? 'busy' :
          status === 'paused' ? 'paused' :
            ['open_failed', 'close_failed', 'error'].includes(status) ? 'error' : '');
    const progress = $('anno-progress');
    if (state.active && state.session_id) {
      progress.hidden = false;
      progress.textContent = `第 ${Math.min(state.index + 1, state.total)}/${state.total} 家`;
    } else {
      progress.hidden = true;
    }
    if (!busy) $('anno-message').textContent = state.message || '';

    const frame = state.frame || null;
    const key = frame ? frame.name : '';
    if (key !== imageKey) {
      if (frame) loadFrame(frame, state.suggestion);
      else {
        image = null;
        imageKey = '';
        box = null;
        clearCanvas();
      }
    } else if (frame) {
      draw();
    }

    if (frame) {
      $('anno-empty').hidden = true;
      $('anno-canvas').hidden = false;
      $('anno-frame-meta').textContent =
        `截图 ${frame.image_width}×${frame.image_height} px · 窗口 ${Math.round(frame.window_width)}×${Math.round(frame.window_height)} points · 换算比例 ${frame.scale_x.toFixed(4)} / ${frame.scale_y.toFixed(4)}`;
    } else {
      $('anno-canvas').hidden = true;
      $('anno-empty').hidden = false;
      $('anno-empty').textContent = ['starting', 'opening'].includes(status)
        ? '正在打开小程序，请稍候…'
        : '开始标注后，当前小程序截图会显示在这里。';
      $('anno-frame-meta').textContent = '';
    }

    renderSuggestion();
    renderDecision();
    renderResult();
    updateMapping();
    renderButtons();
    void maybeAutoDecide();
    const foot = status === 'open_failed'
      ? '小程序打不开通常是二维码过期、无效或该门店已不存在；可点击“标记不合格”并选择原因。'
      : status === 'paused'
        ? '已暂停：当前门店保持打开；点击“继续标注”后可以接着框选。'
        : '点击前会重新截图并校验窗口尺寸；每次点击都会保存前后截图和坐标。';
    if ($('anno-foot').textContent !== foot) $('anno-foot').textContent = foot;
  }

  async function maybeAutoDecide() {
    if (review || busy || deciding) return;
    const status = state?.status;
    if (!state?.active || !['ready', 'clicked'].includes(status)) return;
    const frame = state?.frame;
    if (!frame || state?.decision) return;
    const key = `${state.session_id || ''}|${frame.name}`;
    if (autoDecideFrame === key) return;
    autoDecideFrame = key;
    deciding = true;
    renderButtons();
    try {
      const data = await api('/api/annotation/decide', {method: 'POST', body: '{}'});
      applyState(data);
    } catch (error) {
      // 画面已经切换时后端会拒绝这次建议；保留提示，等下一张画面重新触发。
      if (!String(error.message).includes('画面已切换')) {
        $('anno-message').textContent = '初始判断生成失败：' + error.message;
      }
    } finally {
      deciding = false;
      renderButtons();
    }
  }

  function adoptDecisionBox(decision) {
    if (!decision?.box_px) return;
    box = {...decision.box_px};
    if (decision.target_label) $('anno-target').value = String(decision.target_label).slice(0, 80);
    draw();
    updateMapping();
    renderButtons();
  }

  function renderSuggestion() {
    const panel = $('anno-suggestion');
    const decision = state?.decision;
    const suggestion = decision ? null : state?.suggestion;
    const key = decision
      ? 'decision:' + JSON.stringify(decision) + (state?.frame?.name || '')
      : (suggestion ? JSON.stringify(suggestion) + (state?.frame?.name || '') : '');
    if (key === renderSuggestion._key) return;
    renderSuggestion._key = key;
    panel.textContent = '';
    if (decision) {
      const parts = [];
      parts.push(decision.source_detail || DECISION_SOURCE_LABELS[decision.source] || decision.source || '初始判断');
      if (decision.target_label) parts.push(`目标“${decision.target_label}”`);
      if (decision.box_px) parts.push('蓝色虚线为建议位置，请核对后采纳或重新框选');
      else if (decision.reason) parts.push(decision.reason);
      panel.hidden = false;
      const text = document.createElement('span');
      const title = document.createElement('strong');
      title.textContent = '系统初始判断';
      const body = document.createElement('span');
      body.className = 'anno-suggestion-hint';
      body.textContent = ' · ' + parts.join(' · ');
      text.append(title, body);
      panel.append(text);
      if (decision.box_px) {
        const use = document.createElement('button');
        use.type = 'button';
        use.className = 'btn';
        use.textContent = '采纳建议位置';
        use.addEventListener('click', () => adoptDecisionBox(decision));
        panel.append(use);
      }
      return;
    }
    if (!suggestion) {
      panel.hidden = true;
      return;
    }
    const parts = [];
    if (suggestion.category) parts.push(`建议类别：${CATEGORY_LABELS[suggestion.category] || suggestion.category}`);
    if (typeof suggestion.confidence === 'number') parts.push(`置信度 ${suggestion.confidence}`);
    if (suggestion.matched_text) parts.push(`识别文字“${suggestion.matched_text}”`);
    if (suggestion.note) parts.push(suggestion.note);
    if (suggestion.error) parts.push(suggestion.error);
    if (!parts.length) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;
    const text = document.createElement('span');
    const title = document.createElement('strong');
    title.textContent = '本地快速提示';
    const body = document.createElement('span');
    body.className = 'anno-suggestion-hint';
    body.textContent = ' · ' + parts.join(' · ');
    text.append(title, body);
    panel.append(text);
    if (suggestion.box_px) {
      const apply = document.createElement('button');
      apply.type = 'button';
      apply.className = 'btn';
      apply.textContent = '应用建议框选';
      apply.addEventListener('click', () => applySuggestion(true));
      panel.append(apply);
    }
  }

  const DECISION_SOURCE_LABELS = {
    local_ready: '本地规则 · 菜单就绪',
    local_prepare: '本地规则 · 目标定位',
    local_fused_ready: '本地规则 · 菜单就绪（多尺度 OCR）',
    local_fused_prepare: '本地规则 · 目标定位（多尺度 OCR）',
    model: '模型判断',
    model_failed: '模型未给出可用建议',
    unavailable: '模型不可用'
  };

  /**
   * 展示与批处理同源的只读建议：来源、目标、定位方式、理由与候选。
   * 建议只画虚线框，只有点击“采纳建议位置”才会变成真正的框选。
   */
  function renderDecision() {
    const panel = $('anno-decision');
    const decision = state?.decision;
    const key = decision ? JSON.stringify(decision) + (state?.frame?.name || '') : '';
    if (key === renderDecision._key) return;
    renderDecision._key = key;
    panel.textContent = '';
    if (!decision) {
      panel.hidden = true;
      return;
    }
    panel.hidden = false;

    const head = document.createElement('div');
    head.className = 'anno-decision-head';
    const source = decision.source || 'unknown';
    const tag = document.createElement('span');
    tag.className = `anno-decision-tag ${source}`;
    tag.textContent = DECISION_SOURCE_LABELS[source] || source;
    head.append(tag);
    if (decision.target_label) {
      const label = document.createElement('span');
      label.textContent = `目标“${decision.target_label}”`;
      head.append(label);
    }
    if (decision.action) {
      const action = document.createElement('span');
      action.textContent = `动作 ${decision.action}`;
      head.append(action);
    }
    panel.append(head);

    const rows = [];
    if (decision.source_detail) rows.push(`来源：${decision.source_detail}`);
    if (Array.isArray(decision.chain) && decision.chain.length) {
      rows.push(`判断链：${decision.chain.join(' → ')}` + (decision.hit_stage ? `（命中 ${decision.hit_stage}）` : ''));
    }
    if (decision.method) rows.push(`定位方式：${decision.method}`);
    if (decision.resolution_reason) rows.push(`定位结果：${decision.resolution_reason}`);
    if (decision.reason) rows.push(`理由：${decision.reason}`);
    if (decision.box_px) {
      const b = decision.box_px;
      rows.push(`建议区域：${Math.round(b.x)}, ${Math.round(b.y)} ${Math.round(b.width)}×${Math.round(b.height)} px`);
    }
    if (decision.error) rows.push(`失败原因：${decision.error}`);
    for (const text of rows) {
      const row = document.createElement('div');
      row.className = 'anno-decision-row';
      row.textContent = text;
      panel.append(row);
    }

    if (Array.isArray(decision.candidates) && decision.candidates.length) {
      const details = document.createElement('details');
      const summary = document.createElement('summary');
      summary.textContent = `本地 OCR 候选（${decision.candidates.length}）`;
      const list = document.createElement('ul');
      for (const item of decision.candidates.slice(0, 20)) {
        const li = document.createElement('li');
        li.textContent = `${item.id || ''} ${item.kind || ''} ${item.text || ''} ${item.position || ''}`.trim();
        list.append(li);
      }
      details.append(summary, list);
      panel.append(details);
    }

    if (decision.box_px) {
      const actions = document.createElement('div');
      actions.className = 'anno-decision-actions';
      const use = document.createElement('button');
      use.type = 'button';
      use.className = 'btn';
      use.textContent = '采纳建议位置';
      use.addEventListener('click', () => adoptDecisionBox(decision));
      actions.append(use);
      panel.append(actions);
    }
  }

  function renderResult() {
    const panel = $('anno-result');
    const steps = review
      ? (Array.isArray(review.steps) ? review.steps : [])
      : (Array.isArray(state?.current_steps) ? state.current_steps : []);
    if (!steps.length) {
      renderResult._key = '';
      panel.hidden = true;
      panel.textContent = '';
      return;
    }
    if (review) {
      if (reviewCursor < 0 || reviewCursor >= steps.length) reviewCursor = steps.length - 1;
    } else if (stepCursor < 0 || stepCursor >= steps.length) {
      stepCursor = steps.length - 1;
    }
    const cursor = review ? reviewCursor : stepCursor;
    const step = steps[cursor];
    const scope = review ? `review:${review.session_id || ''}:${review.task?.id || ''}` : 'live';
    const key = `${scope}|${cursor}|${steps.map((item) => `${item.index}:${item.at}`).join(',')}`;
    if (key === renderResult._key) return;
    renderResult._key = key;
    panel.textContent = '';
    panel.hidden = false;

    const nav = document.createElement('div');
    nav.className = 'anno-step-nav';
    const prev = document.createElement('button');
    prev.type = 'button';
    prev.className = 'btn';
    prev.textContent = '上一步';
    prev.disabled = cursor <= 0;
    const position = document.createElement('span');
    const storeName = (review ? review.task?.source_name : state?.task?.source_name) || '当前门店';
    position.textContent = `${storeName} · 第 ${cursor + 1}/${steps.length} 步`;
    const next = document.createElement('button');
    next.type = 'button';
    next.className = 'btn';
    next.textContent = '下一步';
    next.disabled = cursor >= steps.length - 1;
    const move = (delta) => {
      if (review) {
        reviewCursor += delta;
        reviewImageKey = '';
        image = null;
        renderResult._key = '';
        render();
      } else {
        stepCursor += delta;
        renderResult._key = '';
        renderResult();
      }
    };
    prev.addEventListener('click', () => move(-1));
    next.addEventListener('click', () => move(1));
    nav.append(prev, position, next);
    panel.append(nav);

    const head = document.createElement('div');
    head.className = 'anno-result-head';
    const effect = step.effect || {};
    const chip = document.createElement('span');
    const hardNegative = step.hard_negative === true;
    const changed = effect.changed === true;
    const unknown = effect.changed === null || typeof effect.changed === 'undefined';
    chip.className = 'anno-effect ' +
      (hardNegative ? 'negative' : unknown ? 'unknown' : changed ? 'changed' : 'unchanged');
    chip.textContent = hardNegative ? '硬负样本' :
      unknown ? '无法比较画面' : changed ? '画面有变化' :
        '画面几乎无变化，完成后不会归档为有效样本';
    head.append(chip);
    const points = step.center_points || {};
    const meta = document.createElement('span');
    meta.textContent = `第 ${step.index} 次点击 · (${points.x}, ${points.y}) points` +
      (typeof effect.diff_score === 'number' ? ` · 差异 ${effect.diff_score}` : '') +
      (effect.roi_localized && typeof effect.roi_ratio === 'number'
        ? ` · 局部 ${(effect.roi_ratio * 100).toFixed(1)}%` : '');
    head.append(meta);
    panel.append(head);

    const detail = document.createElement('div');
    detail.className = 'anno-step-detail';
    detail.textContent =
      `场景：${CATEGORY_LABELS[step.category] || step.category || '未填'} · ` +
      `目标：${step.target || '未填'}`;
    panel.append(detail);

    const thumbs = document.createElement('div');
    thumbs.className = 'anno-thumbs';
    for (const [role, label] of [['before', '点击前'], ['after', '点击后']]) {
      const item = step[role];
      if (!item) continue;
      const thumb = document.createElement('div');
      thumb.className = 'anno-thumb';
      const caption = document.createElement('span');
      caption.textContent = label;
      const img = document.createElement('img');
      img.alt = label;
      img.src = item.url + '&rev=' + encodeURIComponent(step.at || '');
      thumb.append(caption, img);
      thumbs.append(thumb);
    }
    panel.append(thumbs);
  }

  function renderButtons() {
    const status = state?.status || 'idle';
    const active = Boolean(state?.active);
    const transient = ['starting', 'opening', 'closing'].includes(status);
    const paused = status === 'paused';
    const canClick = !busy && ['ready', 'clicked'].includes(status) && Boolean(box);
    $('anno-start').disabled = busy || active || selected.size === 0;
    $('anno-stop').disabled = busy || !active;
    $('anno-pause').textContent = paused ? '继续标注' : '暂停';
    $('anno-pause').disabled = busy || !active || transient;
    $('anno-skip').disabled = busy || !active || transient || paused;
    $('anno-complete').disabled = busy || !['ready', 'clicked'].includes(status);
    $('anno-reject').disabled = busy || !active || transient || paused;
    $('anno-click').disabled = !canClick;
    $('anno-decide').disabled = busy || deciding || paused || !state?.frame || !['ready', 'clicked'].includes(status);
    $('anno-decide').textContent = deciding ? '正在生成初始判断…' : '生成建议（本地 / 模型）';
    $('anno-refresh').disabled = busy || paused || !active || !state?.frame;
    $('anno-refresh').textContent = busy ? '正在刷新…' : '刷新截图';
    $('anno-apply-suggestion').disabled = busy || !state?.suggestion?.box_px;
    $('anno-retry').hidden = !['open_failed', 'close_failed', 'error'].includes(status);
    $('anno-retry').disabled = busy;
    $('anno-select-all').disabled = busy || active;
    $('anno-exit-review').hidden = !review;
  }

  function setBusy(value, message) {
    busy = value;
    document.querySelector('.anno-stage')?.classList.toggle('anno-busy', value);
    if (value && message) $('anno-message').textContent = message;
    if (!value) document.querySelector('.anno-stage')?.classList.remove('anno-busy');
    renderButtons();
  }

  function loadFrame(frame, suggestion) {
    imageKey = frame.name;
    const img = new Image();
    img.onload = () => {
      if (imageKey !== frame.name) return;
      image = img;
      const canvas = $('anno-canvas');
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      canvas.hidden = false;
      $('anno-empty').hidden = true;
      // 新画面只展示蓝色建议虚线，不把建议框自动塞成绿色的实际框选；
      // 需要时由使用者点“采纳建议位置”或自己拖框。
      box = null;
      applySuggestion(false);
      draw();
      updateMapping();
      renderButtons();
    };
    img.onerror = () => {
      if (imageKey !== frame.name) return;
      image = null;
      clearCanvas();
      toast('标注截图加载失败，请稍后重试或结束标注');
    };
    img.src = frame.url;
  }

  function applySuggestion(includeBox = true) {
    const suggestion = state?.suggestion;
    if (!suggestion) return;
    if (suggestion.category) $('anno-category').value = suggestion.category;
    if (suggestion.target) $('anno-target').value = suggestion.target;
    if (includeBox && suggestion.box_px) box = {...suggestion.box_px};
    draw();
    updateMapping();
    renderButtons();
  }

  function clearCanvas() {
    const canvas = $('anno-canvas');
    const context = canvas.getContext('2d');
    context.clearRect(0, 0, canvas.width, canvas.height);
    $('anno-mapping').textContent = '尚未框选：在截图上按住鼠标拖动即可。';
  }

  function draw() {
    const canvas = $('anno-canvas');
    if (!image || canvas.hidden) return;
    if (canvas.width !== image.naturalWidth || canvas.height !== image.naturalHeight) {
      canvas.width = image.naturalWidth;
      canvas.height = image.naturalHeight;
    }
    const context = canvas.getContext('2d');
    context.clearRect(0, 0, canvas.width, canvas.height);
    context.drawImage(image, 0, 0);
    const line = Math.max(2, Math.round(canvas.width / 400));
    // 建议位置只画蓝色虚线；它不代表已采纳，人工框选仍是最终样本。
    const decisionBox = review
      ? (reviewStep()?.suggestion?.box_px || null)
      : state?.decision?.box_px;
    if (decisionBox) {
      context.save();
      context.strokeStyle = '#2f6fd0';
      context.lineWidth = Math.max(1.5, line * 0.8);
      context.setLineDash([line * 3, line * 3]);
      context.strokeRect(decisionBox.x, decisionBox.y, decisionBox.width, decisionBox.height);
      context.setLineDash([]);
      context.fillStyle = '#2f6fd0';
      context.font = `${Math.max(12, Math.round(canvas.width / 24))}px -apple-system, sans-serif`;
      context.fillText('建议', decisionBox.x + 2, Math.max(14, decisionBox.y - 4));
      context.restore();
    }
    if (!box) return;
    context.save();
    context.fillStyle = 'rgba(22,139,120,.22)';
    context.fillRect(box.x, box.y, box.width, box.height);
    context.strokeStyle = '#0c6d5e';
    context.lineWidth = line;
    context.strokeRect(box.x, box.y, box.width, box.height);
    context.beginPath();
    context.moveTo(box.x + box.width / 2 - line * 4, box.y + box.height / 2);
    context.lineTo(box.x + box.width / 2 + line * 4, box.y + box.height / 2);
    context.moveTo(box.x + box.width / 2, box.y + box.height / 2 - line * 4);
    context.lineTo(box.x + box.width / 2, box.y + box.height / 2 + line * 4);
    context.stroke();
    context.restore();
  }

  function updateMapping() {
    if (review) {
      const step = reviewStep();
      if (!step || !step.box_px) {
        const empty = '该步骤没有记录点击框。';
        if ($('anno-mapping').textContent !== empty) $('anno-mapping').textContent = empty;
        renderButtons();
        return;
      }
      const points = step.center_points || {};
      const text =
        `框选 ${Math.round(step.box_px.x)}, ${Math.round(step.box_px.y)} ` +
        `${Math.round(step.box_px.width)}×${Math.round(step.box_px.height)} px` +
        ` → 实际点击 (${points.x ?? '?'}, ${points.y ?? '?'}) points`;
      if ($('anno-mapping').textContent !== text) $('anno-mapping').textContent = text;
      renderButtons();
      return;
    }
    const frame = state?.frame;
    if (!frame || !box) {
      const empty = '尚未框选：在截图上按住鼠标拖动即可。';
      if ($('anno-mapping').textContent !== empty) $('anno-mapping').textContent = empty;
      renderButtons();
      return;
    }
    const centerX = box.x + box.width / 2;
    const centerY = box.y + box.height / 2;
    const pointX = (centerX * frame.scale_x).toFixed(2);
    const pointY = (centerY * frame.scale_y).toFixed(2);
    const text =
      `框选 ${Math.round(box.x)}, ${Math.round(box.y)} ${Math.round(box.width)}×${Math.round(box.height)} px` +
      ` → 点击 (${pointX}, ${pointY}) points`;
    if ($('anno-mapping').textContent !== text) $('anno-mapping').textContent = text;
  }

  function reviewStep() {
    if (!review) return null;
    const steps = Array.isArray(review.steps) ? review.steps : [];
    if (!steps.length) return null;
    reviewCursor = Math.min(Math.max(0, reviewCursor), steps.length - 1);
    return steps[reviewCursor];
  }

  function loadReviewFrame(step, steps) {
    const before = step.before;
    if (!before || !before.url) {
      image = null;
      box = step.box_px ? {...step.box_px} : null;
      clearCanvas();
      $('anno-canvas').hidden = true;
      $('anno-empty').hidden = false;
      $('anno-empty').textContent = '该步骤没有保存点击前截图。';
      $('anno-frame-meta').textContent = '';
      return;
    }
    $('anno-empty').hidden = true;
    $('anno-canvas').hidden = false;
    const width = before.width || step.image_size?.width || 0;
    const height = before.height || step.image_size?.height || 0;
    $('anno-frame-meta').textContent =
      `截图 ${width}×${height} px · 第 ${reviewCursor + 1}/${steps.length} 步` +
      (typeof step.effect?.diff_score === 'number' ? ` · 差异 ${step.effect.diff_score}` : '') +
      (step.effect?.roi_localized && typeof step.effect.roi_ratio === 'number'
        ? ` · 局部 ${(step.effect.roi_ratio * 100).toFixed(1)}%` : '');
    const key = `${review.session_id || ''}|${step.index}|${step.at || ''}|${before.name}`;
    if (key === reviewImageKey && image) {
      draw();
      updateMapping();
      return;
    }
    reviewImageKey = key;
    const img = new Image();
    img.onload = () => {
      if (reviewImageKey !== key) return;
      image = img;
      const canvas = $('anno-canvas');
      canvas.width = img.naturalWidth;
      canvas.height = img.naturalHeight;
      canvas.hidden = false;
      draw();
      updateMapping();
    };
    img.onerror = () => {
      if (reviewImageKey !== key) return;
      image = null;
      clearCanvas();
      $('anno-canvas').hidden = true;
      $('anno-empty').hidden = false;
      $('anno-empty').textContent = '该步骤的截图已丢失，无法回看。';
    };
    img.src = before.url;
  }

  function renderReview() {
    const steps = Array.isArray(review.steps) ? review.steps : [];
    const step = reviewStep();
    $('anno-status').textContent = step ? '已标注 · 回看' : '已标注';
    $('anno-status').className = 'anno-status review';
    $('anno-progress').hidden = true;
    $('anno-message').textContent = steps.length
      ? `${review.task?.source_name || '该门店'} · 共 ${steps.length} 次确认点击，可切换步骤查看实际效果`
      : `${review.task?.source_name || '该门店'} · 已完成标注，但没有可回看的点击记录（截图可能已清理）`;
    $('anno-suggestion').hidden = true;
    $('anno-decision').hidden = true;
    $('anno-foot').textContent = '回看为只读模式：显示人工确认的点击框与点击前后截图，不会产生新的点击。';
    if (step) {
      $('anno-category').value = step.category || '';
      $('anno-target').value = step.target || '';
      $('anno-hard-negative').checked = step.hard_negative === true;
      box = step.box_px ? {...step.box_px} : null;
      loadReviewFrame(step, steps);
    } else {
      image = null;
      imageKey = '';
      reviewImageKey = '';
      box = null;
      clearCanvas();
      $('anno-canvas').hidden = true;
      $('anno-empty').hidden = false;
      $('anno-empty').textContent = '该门店没有可回看的点击截图。';
      $('anno-frame-meta').textContent = '';
      $('anno-mapping').textContent = '';
    }
    renderResult();
    updateMapping();
    renderButtons();
  }

  function clearReviewState() {
    review = null;
    reviewCursor = 0;
    reviewImageKey = '';
    image = null;
    imageKey = '';
    box = null;
    renderResult._key = '';
  }

  function exitReview() {
    if (!review) return;
    clearReviewState();
    render();
  }

  async function openHistory(taskId) {
    if (busy) return;
    if (state?.active) {
      toast('请先结束当前标注，再回看已标注门店');
      return;
    }
    setBusy(true, '正在读取已标注的实际效果…');
    try {
      const data = await api('/api/annotation/history?task_id=' + encodeURIComponent(taskId));
      review = data;
      reviewCursor = Math.max(0, (data.steps?.length || 0) - 1);
      reviewImageKey = '';
      image = null;
      imageKey = '';
      box = null;
      renderResult._key = '';
      render();
    } catch (error) {
      toast(error.message);
    } finally {
      setBusy(false);
    }
  }

  function canvasPoint(event) {
    const canvas = $('anno-canvas');
    const rect = canvas.getBoundingClientRect();
    const x = Math.min(canvas.width, Math.max(0, (event.clientX - rect.left) * canvas.width / rect.width));
    const y = Math.min(canvas.height, Math.max(0, (event.clientY - rect.top) * canvas.height / rect.height));
    return {x, y};
  }

  function normalizedBox(start, end) {
    const x = Math.min(start.x, end.x);
    const y = Math.min(start.y, end.y);
    return {x, y, width: Math.abs(end.x - start.x), height: Math.abs(end.y - start.y)};
  }

  function bindCanvas() {
    const canvas = $('anno-canvas');
    canvas.addEventListener('pointerdown', (event) => {
      if (review || busy || !state?.frame || !['ready', 'clicked'].includes(state?.status)) return;
      const point = canvasPoint(event);
      drag = point;
      box = {x: point.x, y: point.y, width: 0, height: 0};
      canvas.setPointerCapture(event.pointerId);
      draw();
    });
    canvas.addEventListener('pointermove', (event) => {
      if (!drag) return;
      box = normalizedBox(drag, canvasPoint(event));
      draw();
    });
    const finish = (event) => {
      if (!drag) return;
      const point = canvasPoint(event);
      const result = normalizedBox(drag, point);
      drag = null;
      box = result.width >= 4 && result.height >= 4 ? result : null;
      draw();
      updateMapping();
      renderButtons();
    };
    canvas.addEventListener('pointerup', finish);
    canvas.addEventListener('pointercancel', () => { drag = null; draw(); });
  }

  async function mutate(path, body, message) {
    if (busy) return;
    setBusy(true, message);
    try {
      const data = await api(path, {method: 'POST', body: JSON.stringify(body || {})});
      if (body && body.hard_negative) $('anno-hard-negative').checked = false;
      applyState(data);
      if (data.message) $('anno-message').textContent = data.message;
    } catch (error) {
      $('anno-message').textContent = error.message;
      toast(error.message);
      render();
    } finally {
      setBusy(false);
    }
  }

  function readClickPayload() {
    return {
      box: box,
      // 页面状态由类别在服务端推导，前端只提交一个场景字段。
      category: $('anno-category').value,
      target: $('anno-target').value.trim(),
      hard_negative: $('anno-hard-negative').checked,
      note: ''
    };
  }

  function bindActions() {
    $('anno-start').addEventListener('click', () => {
      const ids = Array.from(selected);
      if (!ids.length) return;
      if (!confirm(`开始人工标注 ${ids.length} 家门店？请确认微信已停留在“文件传输助手”。`)) return;
      if (review) exitReview();
      void mutate('/api/annotation/start', {task_ids: ids}, '正在启动 UiPath 并打开第一家门店，可能需要几十秒…');
    });
    $('anno-exit-review').addEventListener('click', () => exitReview());
    $('anno-click').addEventListener('click', () => {
      if (!box) return;
      const payload = readClickPayload();
      if (!payload.category) { toast('请选择页面 / 场景'); return; }
      if (!confirm(`确认点击框中心（约 ${Math.round(box.x + box.width / 2)}, ${Math.round(box.y + box.height / 2)} px）？`)) return;
      void mutate('/api/annotation/click', payload, '正在点击并保存前后截图…');
    });
    $('anno-apply-suggestion').addEventListener('click', () => applySuggestion(true));
    $('anno-decide').addEventListener('click', () => {
      void mutate('/api/annotation/decide', {}, '正在生成本地/模型建议（模型可能需要几秒）…');
    });
    $('anno-refresh').addEventListener('click', () => {
      void mutate('/api/annotation/refresh', {}, '正在重新截取当前画面…');
    });
    $('anno-complete').addEventListener('click', () => {
      if (!confirm('确认已完成本店标注（已进入菜单）？将归档样本并关闭小程序。')) return;
      void mutate('/api/annotation/complete', {}, '正在归档样本并关闭小程序…');
    });
    $('anno-skip').addEventListener('click', () => {
      if (!confirm('跳过本店？不会归档样本，也不会标记不合格（该店仍留在待处理，可稍后继续标注）。')) return;
      void mutate('/api/annotation/skip', {}, '正在关闭小程序并跳过本店…');
    });
    const rejectDialog = $('anno-reject-dialog');
    const closeRejectDialog = () => {
      if (typeof rejectDialog.close === 'function') rejectDialog.close();
      else rejectDialog.removeAttribute('open');
    };
    $('anno-reject').addEventListener('click', () => {
      if (busy || !state?.active) return;
      $('anno-reject-store').textContent = state.task?.source_name
        ? `当前门店：${state.task.source_name}` : '当前门店';
      $('anno-reject-reason').value = 'invalid_qr';
      $('anno-reject-detail').value = '';
      $('anno-reject-error').hidden = true;
      if (typeof rejectDialog.showModal === 'function') rejectDialog.showModal();
      else rejectDialog.setAttribute('open', '');
    });
    $('anno-reject-cancel').addEventListener('click', closeRejectDialog);
    rejectDialog.addEventListener('close', () => { $('anno-reject-error').hidden = true; });
    $('anno-reject-form').addEventListener('submit', async (event) => {
      event.preventDefault();
      if (busy) return;
      const reason = $('anno-reject-reason').value;
      const detail = $('anno-reject-detail').value.trim();
      if (reason === 'other' && !detail) {
        $('anno-reject-error').textContent = '选择“其他原因”时请填写具体说明';
        $('anno-reject-error').hidden = false;
        return;
      }
      closeRejectDialog();
      setBusy(true, '正在标记不合格并打开下一家…');
      try {
        const data = await api('/api/annotation/reject', {
          method: 'POST', body: JSON.stringify({reason, detail})
        });
        applyState(data);
        if (data.message) $('anno-message').textContent = data.message;
        // 任务已转为不合格，刷新列表与筛选计数。
        renderTasks._signature = '';
        void loadTasks();
      } catch (error) {
        $('anno-message').textContent = error.message;
        toast(error.message);
        render();
      } finally {
        setBusy(false);
      }
    });
    $('anno-retry').addEventListener('click', () => {
      void mutate('/api/annotation/retry', {}, '正在重试…');
    });
    $('anno-pause').addEventListener('click', () => {
      const resume = state?.status === 'paused';
      void mutate(resume ? '/api/annotation/resume' : '/api/annotation/pause', {},
        resume ? '正在继续标注…' : '正在暂停标注…');
    });
    $('anno-stop').addEventListener('click', () => {
      if (!confirm('结束本次人工标注？未完成的门店不会归档。')) return;
      void mutate('/api/annotation/stop', {}, '正在结束标注…');
    });
    $('anno-select-all').addEventListener('change', (event) => {
      const tasks = filteredTasks().slice(0, 300);
      if (event.target.checked) tasks.forEach((task) => selected.add(task.id));
      else tasks.forEach((task) => selected.delete(task.id));
      renderTasks._signature = '';
      renderTasks();
    });
    for (const button of document.querySelectorAll('[data-anno-filter]')) {
      button.addEventListener('click', () => {
        taskFilter = button.dataset.annoFilter || 'all';
        renderTasks._signature = '';
        renderTasks();
      });
    }
  }

  function startPolling() {
    stopPolling();
    pollTimer = setInterval(() => {
      if (document.hidden || currentPage !== 'annotation' || busy) return;
      void refreshState();
    }, 2000);
  }

  function stopPolling() {
    if (pollTimer) clearInterval(pollTimer);
    pollTimer = null;
  }

  function activate() {
    if (!tasksLoaded) void loadTasks();
    void refreshState();
    startPolling();
  }

  initOptions();
  bindCanvas();
  bindActions();
  window.addEventListener('menu:page', (event) => {
    currentPage = event.detail;
    if (event.detail === 'annotation') activate();
    else stopPolling();
  });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) stopPolling();
    else if (currentPage === 'annotation') activate();
  });
  if ($('page-annotation')?.classList.contains('active')) {
    currentPage = 'annotation';
    activate();
  }
})();
