/** 共享工作台连接状态、设备配对及门店分配界面。采集动作仍由本机工作台发起。 */
(() => {
  'use strict';

  const get = id => document.getElementById(id);
  const labels = {
    assigned: '待下载',
    ready: '已下载',
    pending_upload: '待上传',
    completed: '已同步',
    reclaimed: '已收回',
  };

  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text != null) item.textContent = text;
    if (className) item.className = className;
    return item;
  }

  function formatTime(value, fallback = '尚未上线') {
    if (!value) return fallback;
    const date = new Date(value);
    return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString('zh-CN');
  }

  window.createSharedWorkbench = ({api, toast, onChange, onAssigned}) => {
    let status = null;
    let refreshRequest = null;
    let selectedTaskIds = [];
    let working = false;
    let workerSignature = '';
    let assignmentSignature = '';
    let pairExpiry = null;
    let activeTasks = new Map();
    const dialog = get('shared-dialog');
    const workerSelect = get('shared-assign-worker');

    const role = () => status?.role || '';
    const assignments = () => Array.isArray(status?.assignments) ? status.assignments : [];
    const workers = () => Array.isArray(status?.workers) ? status.workers : [];
    const post = (path, body = {}) => api(path, {method: 'POST', body: JSON.stringify(body)});

    function setError(id, text) {
      const element = get(id);
      if (element.textContent !== (text || '')) element.textContent = text || '';
      element.hidden = !text;
    }

    function setText(id, text) {
      const element = get(id);
      if (element.textContent !== text) element.textContent = text;
    }

    function syncAssignmentSubmit() {
      get('shared-assign-submit').disabled = working || !selectedTaskIds.length || !workerSelect.value;
    }

    function renderDialogLists() {
      const devices = workers();
      const records = assignments();
      const byId = new Map(devices.map(device => [device.id, device]));
      const workerState = JSON.stringify(devices);
      if (workerState !== workerSignature) {
        workerSignature = workerState;
        const selectedDevice = workerSelect.value;
        const options = devices.map(device => new Option(device.name || device.id, device.id));
        workerSelect.replaceChildren(new Option('请选择采集电脑', ''), ...options);
        if (devices.some(device => device.id === selectedDevice)) workerSelect.value = selectedDevice;
        get('shared-assign-empty').hidden = devices.length > 0;
        const deviceRows = devices.map(device => {
          const row = node('li', null, 'shared-row');
          row.append(
            node('strong', device.name || device.id),
            node('span', `最后在线 ${formatTime(device.last_seen)}`),
            node('span', `待上传 ${device.pending_uploads ?? 0} 家`),
          );
          return row;
        });
        get('shared-device-list').replaceChildren(...deviceRows);
        get('shared-devices-empty').hidden = deviceRows.length > 0;
        syncAssignmentSubmit();
      }

      const recordState = JSON.stringify([records, devices.map(device => [device.id, device.name])]);
      if (recordState === assignmentSignature) return;
      assignmentSignature = recordState;

      const assignmentRows = records.map(record => {
        const row = node('li', null, 'shared-row shared-assignment-row');
        const identity = node('div', null, 'shared-assignment-name');
        identity.append(node('strong', record.store || record.task_id));
        const detail = [record.batch_name || record.batch_id, record.revision].filter(Boolean).join(' · ');
        if (detail) identity.append(node('span', detail));
        row.append(identity, node('span', byId.get(record.worker_id)?.name || record.worker_id || '—'),
          node('span', record.disposition === 'history' ? '旧结果待检查' : labels[record.status] || '状态待确认'));
        if (['assigned', 'ready'].includes(record.status)) {
          const reclaim = node('button', '收回', 'btn');
          reclaim.type = 'button';
          reclaim.setAttribute('aria-label', `收回 ${record.store || record.task_id}`);
          reclaim.addEventListener('click', () => void reclaimAssignment(record, reclaim));
          row.append(reclaim);
        }
        if (record.disposition === 'history') row.append(historyDetails(record));
        return row;
      });
      get('shared-assignment-list').replaceChildren(...assignmentRows);
      get('shared-assignments-empty').hidden = assignmentRows.length > 0;
      syncAssignmentSubmit();
    }

    function historyDetails(record) {
      const details = node('details', null, 'shared-history');
      const files = node('div', null, 'shared-file-links');
      details.append(node('summary', '查看补传结果'), node('p', record.result_message || '该结果仅保留为历史记录。'), files);
      let loaded = false;
      details.addEventListener('toggle', async () => {
        if (!details.open || loaded) return;
        loaded = true;
        const base = `/api/shared/results/${encodeURIComponent(record.id)}/files`;
        try {
          const result = await api(base);
          files.replaceChildren();
          for (const path of result.files || []) {
            const parts = String(path).split('/');
            if (parts.some(part => !part || part === '.' || part === '..')) continue;
            const link = node('a', parts.at(-1));
            link.href = `${base}/${parts.map(encodeURIComponent).join('/')}`;
            link.target = '_blank';
            link.rel = 'noopener';
            files.append(link);
          }
          if (!files.childElementCount) files.textContent = '没有可查看的文件';
        } catch (error) {
          loaded = false;
          files.textContent = error.message;
        }
      });
      return details;
    }

    function render() {
      const mode = role();
      document.body.dataset.workbenchRole = mode;
      get('shared-workbench').hidden = !mode || mode === 'standalone';
      get('shared-manage').hidden = mode !== 'hub';
      get('shared-refresh').hidden = !mode || mode === 'standalone';
      document.body.dataset.workbenchPaired = String(!!status?.paired);
      setError('shared-sync-error', mode === 'worker' ? status.error : '');
      get('shared-connect-form').hidden = mode !== 'worker' || !!status?.paired;
      const rolePill = get('shared-role-pill');
      rolePill.hidden = !mode || mode === 'standalone';
      rolePill.textContent = mode === 'hub' ? '共享控制台' : status?.paired ? (status.online ? '在线' : '离线') : '未连接';
      const connectHint = get('worker-connect-hint');
      if (connectHint) connectHint.hidden = !(mode === 'worker' && !status?.paired);
      if (mode === 'hub') {
        const active = assignments().filter(item => ['assigned', 'ready'].includes(item.status)).length;
        setText('shared-heading', '共享工作台');
        setText('shared-summary', `${workers().length} 台已配对 · ${active} 家已分配`);
        setText('shared-help', '生成一次性配对码，把门店分配给已连接的采集电脑；离线补传的结果会保留在分配记录中。');
        if (dialog.open) renderDialogLists();
      } else if (mode === 'worker') {
        const ready = assignments().filter(item => item.status === 'ready').length;
        const pending = status.pending_uploads ?? assignments().filter(item => item.status === 'pending_upload').length;
        setText('shared-heading', !status.paired ? '连接共享工作台' : status.online ? '共享工作台已连接' : '共享工作台离线');
        setText('shared-summary', status.paired
          ? `${ready} 家可离线采集 · ${pending} 家待上传`
          : '从共享工作台管理员处获取地址和配对码。');
        setText('shared-help', status.paired
          ? '本机已加入共享工作台：任务由工作台分配，断网时可继续采集已下载门店。'
          : '输入共享工作台地址、电脑名称和一次性配对码，即可加入共享采集。');
      }
      activeTasks = new Map(assignments().filter(item =>
        ['assigned', 'ready', 'pending_upload'].includes(item.status)).flatMap(item =>
        [item.task_id, item.current_task_id].filter(Boolean).map(taskId => [String(taskId), item])));
    }

    async function refresh() {
      if (refreshRequest) return refreshRequest;
      refreshRequest = (async () => {
        try {
          const includeHistory = dialog.open && !get('shared-manage-panel').hidden;
          const result = await api('/api/shared/status?include_history=' + includeHistory);
          if (!['standalone', 'hub', 'worker'].includes(result?.role)) throw new Error('无法识别工作台模式');
          status = result;
          render();
        } catch (error) {
          get('shared-workbench').hidden = false;
          setError('shared-sync-error', `共享状态读取失败：${error.message}`);
        } finally {
          refreshRequest = null;
        }
      })();
      return refreshRequest;
    }

    function allowsTask(task) {
      const assignment = activeTasks.get(String(task.id));
      if (role() === 'hub') return !assignment;
      if (role() === 'worker') return !!status?.paired && assignment?.status === 'ready';
      return role() === 'standalone';
    }

    function assignmentFor(taskId) {
      const record = activeTasks.get(String(taskId));
      if (!record) return null;
      const name = workers().find(worker => worker.id === record.worker_id)?.name;
      return {...record, label: [labels[record.status], name].filter(Boolean).join(' · ')};
    }

    function showDialog(mode, ids = []) {
      if (role() !== 'hub') return;
      selectedTaskIds = [...new Set(ids.map(String))].filter(id => !activeTasks.has(id));
      get('shared-dialog-title').textContent = mode === 'assign' ? '分配已选门店' : '采集电脑和分配记录';
      get('shared-assign-form').hidden = mode !== 'assign';
      get('shared-manage-panel').hidden = mode === 'assign';
      get('shared-assignment-count').textContent = `已选 ${selectedTaskIds.length} 家门店`;
      setError('shared-dialog-error', '');
      if (!dialog.open) dialog.showModal();
      renderDialogLists();
      syncAssignmentSubmit();
      if (mode === 'manage') {
        void Promise.resolve(refreshRequest).then(() => {
          if (dialog.open && !get('shared-manage-panel').hidden) return refresh();
        });
      }
    }

    async function reclaimAssignment(record, button) {
      if (!window.confirm(`收回「${record.store || record.task_id}」？离线电脑补传的旧结果会留档待检查。`)) return;
      button.disabled = true;
      setError('shared-dialog-error', '');
      try {
        await post(`/api/shared/assignments/${encodeURIComponent(record.id)}/reclaim`);
        await refresh();
        onChange();
        toast('门店已收回');
      } catch (error) {
        setError('shared-dialog-error', error.message);
      } finally {
        button.disabled = false;
      }
    }

    get('shared-manage').addEventListener('click', () => showDialog('manage'));
    get('shared-refresh').addEventListener('click', async event => {
      const button = event.currentTarget;
      button.disabled = true;
      try {
        await refresh();
        if (get('shared-sync-error').hidden) toast('共享工作台状态已刷新');
      } finally {
        button.disabled = false;
      }
    });
    get('shared-dialog-close').addEventListener('click', () => dialog.close());
    workerSelect.addEventListener('change', syncAssignmentSubmit);
    dialog.addEventListener('cancel', event => { if (working) event.preventDefault(); });
    get('shared-assign-form').addEventListener('submit', async event => {
      event.preventDefault();
      if (working || !selectedTaskIds.length || !workerSelect.value) return;
      working = true;
      syncAssignmentSubmit();
      get('shared-dialog-close').disabled = true;
      setError('shared-dialog-error', '');
      const taskIds = selectedTaskIds.slice();
      const worker = workers().find(candidate => candidate.id === workerSelect.value);
      try {
        await post('/api/shared/assignments', {worker_id: workerSelect.value, task_ids: taskIds});
        await refresh();
        onAssigned(taskIds);
        dialog.close();
        toast(`已将 ${taskIds.length} 家门店分给 ${worker?.name || '所选电脑'}`);
      } catch (error) {
        setError('shared-dialog-error', error.message);
      } finally {
        working = false;
        get('shared-dialog-close').disabled = false;
        syncAssignmentSubmit();
      }
    });

    get('shared-pair-create').addEventListener('click', async event => {
      const button = event.currentTarget;
      button.disabled = true;
      setError('shared-dialog-error', '');
      try {
        const result = await post('/api/shared/pairing');
        get('shared-pair-code').value = result.code;
        get('shared-pair-expiry').textContent = `有效期至 ${formatTime(result.expires_at, '由后台管理')}`;
        get('shared-pair-result').hidden = false;
        get('shared-pair-code').focus();
        get('shared-pair-code').select();
        clearTimeout(pairExpiry);
        const remaining = Date.parse(result.expires_at) - Date.now();
        if (Number.isFinite(remaining) && remaining <= 2147483647) {
          pairExpiry = setTimeout(() => {
            get('shared-pair-code').value = '';
            get('shared-pair-result').hidden = true;
          }, Math.max(0, remaining));
        }
      } catch (error) {
        setError('shared-dialog-error', error.message);
      } finally {
        button.disabled = false;
      }
    });

    get('shared-connect-form').addEventListener('submit', async event => {
      event.preventDefault();
      const button = get('shared-connect-submit');
      button.disabled = true;
      setError('shared-connect-error', '');
      try {
        await post('/api/shared/connect', {
          url: get('shared-hub-url').value.trim(),
          code: get('shared-worker-code').value.trim(),
          name: get('shared-worker-name').value.trim(),
        });
        get('shared-worker-code').value = '';
        await refresh();
        onChange();
        toast('采集电脑已配对');
      } catch (error) {
        setError('shared-connect-error', error.message);
      } finally {
        button.disabled = false;
      }
    });

    return {role, refresh, allowsTask, assignmentFor, showAssignments: ids => showDialog('assign', ids)};
  };
})();
