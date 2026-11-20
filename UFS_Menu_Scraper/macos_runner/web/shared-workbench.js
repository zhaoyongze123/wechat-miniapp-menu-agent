/** 共享工作台连接状态、设备配对及门店分配界面。采集动作仍由本机工作台发起。 */
(() => {
  'use strict';

  const get = id => document.getElementById(id);
  const labels = {
    assigned: '待下载',
    ready: '已下载',
    pending_upload: '待上传',
    completed: '后台已确认',
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

  window.createSharedWorkbench = ({api, toast, formatStore, onChange, onAssigned}) => {
    let status = null;
    let refreshRequest = null;
    let selectedTaskIds = [];
    let selectedTasks = new Map();
    let conflicts = new Set();
    let working = false;
    let workerSignature = '';
    let assignmentSignature = '';
    let view = '';
    let selectedDeviceId = '';
    let recordsVersion = 0;
    let recordsRequest = null;
    let recordsTimer = null;
    let recordRows = [];
    let reassignTasks = [];
    const selectedRecords = new Set();
    let pairExpiry = null;
    let activeTasks = new Map();
    const dialog = get('shared-dialog');
    const workerSelect = get('shared-assign-worker');
    const recordDevice = get('shared-record-device');
    const recordStatus = get('shared-record-status');
    const recordSearch = get('shared-record-search');
    const recordPageSize = 30;
    let filterDevice = '';
    let recordPage = 1;

    const reclaimable = record => ['assigned', 'ready'].includes(record.status);
    const storeName = value => formatStore(value || '').name;

    const role = () => status?.role || '';
    const assignments = () => Array.isArray(status?.assignments) ? status.assignments : [];
    const workers = () => Array.isArray(status?.workers) ? status.workers : [];
    const currentDevice = () => workers().find(worker => worker.id === selectedDeviceId);
    const devicePath = id => `/api/shared/workers/${encodeURIComponent(id)}`;
    const deviceState = device => device.revoked_at ? '已解除配对' : online(device) ? '在线' : '离线';
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

    function online(device) {
      const seen = Date.parse(device?.last_seen || '');
      return Number.isFinite(seen) && Date.now() - seen < 60000;
    }

    function syncAssignmentSubmit() {
      const target = workers().find(worker => worker.id === workerSelect.value);
      get('shared-offline-target').hidden = !target || online(target);
      const submit = get('shared-assign-submit');
      const available = selectedTaskIds.length - conflicts.size;
      submit.textContent = conflicts.size ? `分配剩余 ${available} 家` : '确认分配';
      submit.disabled = working || !available || !workerSelect.value;
    }

    function renderDeviceOptions() {
      const devices = workers();
      const workerState = JSON.stringify(devices.map(device => [device.id, device.name, device.revoked_at,
        device.archived_at, online(device)]));
      if (workerState === workerSignature) return;
      workerSignature = workerState;
      const active = devices.filter(device => !device.revoked_at && !device.archived_at);
      const selected = workerSelect.value;
      workerSelect.replaceChildren(new Option('请选择采集电脑', ''), ...active.map(device =>
        new Option(`${device.name} · ${deviceState(device)}`, device.id)));
      if (active.some(device => device.id === selected)) workerSelect.value = selected;
      recordDevice.replaceChildren(new Option('全部采集电脑', ''), ...devices.map(device =>
        new Option(device.archived_at ? `${device.name}（已移除）` : device.name, device.id)));
      recordDevice.value = filterDevice;
      get('shared-assign-empty').hidden = !!active.length;
      syncAssignmentSubmit();
    }

    function renderManagement() {
      if (view === 'devices') {
        const devices = workers().filter(device => !device.archived_at);
        const records = assignments().filter(reclaimable);
        const signature = JSON.stringify([devices.map(device => [device.id, device.name, deviceState(device)]),
          records.map(record => record.worker_id)]);
        const list = get('shared-device-list');
        if (list.dataset.signature === signature) return;
        list.dataset.signature = signature;
        setText('shared-device-total', `${devices.length} 台设备`);
        get('shared-devices-empty').hidden = !!devices.length;
        list.replaceChildren(...devices.map(device => {
          const row = node('li', null, 'shared-row');
          const count = records.filter(record => record.worker_id === device.id).length;
          const detail = node('button', '查看详情', 'btn');
          detail.type = 'button';
          detail.setAttribute('aria-label', `查看 ${device.name} 的详情`);
          detail.addEventListener('click', () => showView('detail', device.id));
          row.append(node('strong', device.name), node('span', `${deviceState(device)} · 已分配 ${count} 家`), detail);
          return row;
        }));
      }
      if (view !== 'detail') return;
      const device = currentDevice();
      if (!device) return;
      const records = assignments().filter(record => record.worker_id === device.id && reclaimable(record));
      const facts = [
        ['连接状态', deviceState(device)],
        ['最近连接', formatTime(device.last_seen)],
        ['当前分配', `${records.length} 家 · 已下载 ${records.filter(record => record.status === 'ready').length} 家`],
        ['上次报告待上传', `${device.pending_uploads ?? 0} 家`],
        ['配对时间', formatTime(device.paired_at, '既有设备未记录')],
        ...(device.description ? [['备注', device.description]] : []),
        ...(device.revoked_at ? [['解除配对时间', formatTime(device.revoked_at)]] : []),
      ];
      const details = get('shared-device-facts');
      const signature = JSON.stringify(facts);
      if (details.dataset.signature !== signature) {
        details.dataset.signature = signature;
        details.replaceChildren(...facts.map(([label, value]) => {
          const item = node('div');
          item.append(node('dt', label), node('dd', value));
          return item;
        }));
      }
      setText('shared-dialog-title', device.name);
      get('shared-device-revoke').hidden = !!device.revoked_at;
      get('shared-device-revoke').disabled = working || !!records.length;
      get('shared-device-archive').hidden = !device.revoked_at || !!device.archived_at;
      get('shared-device-archive').disabled = working;
      get('shared-device-edit').hidden = !!device.archived_at || !get('shared-device-edit-form').hidden;
      setText('shared-device-lifecycle-help', device.revoked_at
        ? '从设备列表移除后，仍可在分配记录里查看历史。'
        : records.length ? '还有未结束的分配，请等待同步或先回收任务。'
          : '解除配对后，这台电脑无法继续下载或上传。离线时可能还有未上传的本机结果。');
    }

    function syncRecordSelection() {
      const eligible = recordRows.filter(reclaimable);
      const boxes = get('shared-assignment-list').querySelectorAll('input[data-record]');
      for (const box of boxes) box.checked = selectedRecords.has(box.dataset.record);
      const all = get('shared-record-select-page');
      const selected = eligible.filter(record => selectedRecords.has(record.id)).length;
      all.checked = !!eligible.length && selected === eligible.length;
      all.indeterminate = selected > 0 && selected < eligible.length;
      all.disabled = working;
      get('shared-record-bulk').hidden = !eligible.length;
      const button = get('shared-record-reclaim');
      button.textContent = selected ? `回收选中 ${selected} 家` : '回收选中任务';
      button.disabled = working || !selected;
    }

    function renderRecords(result) {
      recordRows = result.records || [];
      for (const id of selectedRecords) {
        if (!recordRows.some(record => record.id === id && reclaimable(record))) selectedRecords.delete(id);
      }
      recordPage = result.page;
      const signature = JSON.stringify(result);
      if (signature !== assignmentSignature) {
        assignmentSignature = signature;
        get('shared-assignment-list').replaceChildren(...recordRows.map(record => {
          const row = node('li', null, 'shared-row shared-assignment-row');
          const identity = node('div', null, 'shared-assignment-name');
          const store = formatStore(record.store || '');
          const title = node('strong', store.name || record.task_id);
          title.title = record.store || record.task_id;
          const name = store.name || record.task_id;
          const box = node('input');
          box.type = 'checkbox';
          box.dataset.record = record.id;
          box.setAttribute('aria-label', `选择回收 ${name}`);
          box.addEventListener('change', () => {
            if (box.checked) selectedRecords.add(record.id);
            else selectedRecords.delete(record.id);
            syncRecordSelection();
          });
          identity.append(title, node('span', [store.code, record.batch_name, record.revision].filter(Boolean).join(' · ')));
          row.append(reclaimable(record) ? box : node('span'), identity,
            node('span', record.worker_name || record.worker_id, 'shared-assignment-worker'),
            node('span', record.disposition === 'history' ? '旧结果留档' : labels[record.status] || '状态待确认', 'shared-assignment-status'));
          if (reclaimable(record)) {
            const button = node('button', '回收', 'btn');
            button.type = 'button';
            button.setAttribute('aria-label', `回收 ${name}`);
            button.addEventListener('click', () => void reclaimRecords([record.id], button));
            row.append(button);
          }
          if (record.disposition === 'history') row.append(historyDetails(record));
          return row;
        }));
      }
      get('shared-assignments-empty').hidden = !!recordRows.length;
      setText('shared-assignments-empty', '没有符合条件的分配记录。');
      get('shared-record-pager').hidden = !result.total;
      setText('shared-record-summary', `第 ${recordPage} / ${result.pages} 页 · ${result.total} 条`);
      get('shared-record-prev').disabled = recordPage === 1;
      get('shared-record-next').disabled = recordPage === result.pages;
      syncRecordSelection();
    }

    function refreshRecords(reset = false) {
      if (view !== 'records' || !dialog.open) return;
      if (!reset && recordsRequest) return recordsRequest;
      const version = ++recordsVersion;
      const params = new URLSearchParams({worker_id: filterDevice, phase: recordStatus.value,
        q: recordSearch.value, page: String(recordPage), size: String(recordPageSize)});
      recordsRequest = (async () => {
        try {
          const result = await api('/api/shared/assignments?' + params);
          if (version === recordsVersion && dialog.open && view === 'records') {
            setError('shared-record-error', '');
            renderRecords(result);
          }
        } catch (error) {
          if (version === recordsVersion && dialog.open && view === 'records') {
            get('shared-assignments-empty').hidden = true;
            setError('shared-record-error', error.message);
          }
        } finally {
          if (version === recordsVersion) recordsRequest = null;
        }
      })();
      return recordsRequest;
    }

    function clearRecords() {
      recordsVersion++;
      recordsRequest = null;
      recordRows = [];
      assignmentSignature = '';
      selectedRecords.clear();
      get('shared-assignment-list').replaceChildren();
      get('shared-record-pager').hidden = true;
      get('shared-assignments-empty').hidden = false;
      setText('shared-assignments-empty', '正在读取分配记录…');
      setError('shared-record-error', '');
      syncRecordSelection();
    }

    async function reclaimRecords(ids, button) {
      const devices = [...new Set(recordRows.filter(row => ids.includes(row.id)).map(row => row.worker_name))];
      const names = devices.filter(Boolean).join('、');
      if (working || !ids.length) return;
      setError('shared-dialog-error', '');
      if (!window.confirm(`回收${names ? ' ' + names + ' 的' : ''} ${ids.length} 家门店？采集端随后补传的旧结果将留档。`)) return;
      working = true;
      button.disabled = true;
      syncRecordSelection();
      try {
        const result = await post('/api/shared/assignments/reclaim', {assignment_ids: ids});
        reassignTasks = result.tasks;
        const reassign = get('shared-record-reassign');
        reassign.hidden = !reassignTasks.length;
        reassign.textContent = `重新分配 ${reassignTasks.length} 家`;
        selectedRecords.clear();
        await refreshRequest;
        await refresh();
        await refreshRecords(true);
        onChange();
        toast(`已回收 ${result.count} 家门店`);
      } catch (error) {
        setError('shared-dialog-error', error.message);
      } finally {
        working = false;
        button.disabled = false;
        syncRecordSelection();
      }
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
      rolePill.textContent = mode === 'hub' ? '共享控制台' : status?.credential_invalid ? '配对已失效' : status?.paired ? (status.online ? '在线' : '离线') : '未连接';
      const connectHint = get('worker-connect-hint');
      if (connectHint) connectHint.hidden = !(mode === 'worker' && !status?.paired);
      if (mode === 'hub') {
        const active = assignments().filter(reclaimable).length;
        const paired = workers().filter(device => !device.revoked_at && !device.archived_at).length;
        setText('shared-heading', '共享工作台');
        setText('shared-summary', `${paired} 台已配对 · ${active} 家已分配`);
        setText('shared-help', '配对后即可分配门店。离线电脑会在重新连接后下载任务。');
        if (dialog.open) {
          renderDeviceOptions();
          renderManagement();
        }
      } else if (mode === 'worker') {
        const ready = assignments().filter(item => item.status === 'ready').length;
        const pending = status.pending_uploads ?? assignments().filter(item => item.status === 'pending_upload').length;
        setText('shared-heading', status.credential_invalid ? '共享工作台配对已失效'
          : !status.paired ? '连接共享工作台' : status.online ? '共享工作台已连接' : '共享工作台离线');
        setText('shared-summary', status.paired
          ? status.credential_invalid ? `${pending} 家结果保存在本机` : `${ready} 家可离线采集 · ${pending} 家待上传`
          : '从共享工作台管理员处获取地址和配对码。');
        setText('shared-help', status.credential_invalid ? '请联系管理员核对设备配对状态。' : status.paired
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
          const result = await api('/api/shared/status?include_history=false');
          if (!['standalone', 'hub', 'worker'].includes(result?.role)) throw new Error('无法识别工作台模式');
          status = result;
          render();
          void refreshRecords();
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
      if (role() === 'worker') return !!status?.paired && !status.credential_invalid && assignment?.status === 'ready';
      return role() === 'standalone';
    }

    function assignmentFor(taskId) {
      const record = activeTasks.get(String(taskId));
      if (!record) return null;
      const name = workers().find(worker => worker.id === record.worker_id)?.name;
      return {...record, workerName: name || record.worker_id, phase: labels[record.status],
        label: [labels[record.status], name].filter(Boolean).join(' · ')};
    }

    function showView(next, deviceId = '', ids = [], tasks = []) {
      if (role() !== 'hub') return;
      view = next;
      selectedDeviceId = deviceId;
      selectedTaskIds = [...new Set(ids.map(String))];
      get('shared-assign-form').hidden = view !== 'assign';
      get('shared-manage-panel').hidden = view === 'assign';
      get('shared-devices-panel').hidden = view !== 'devices';
      get('shared-device-detail').hidden = view !== 'detail';
      get('shared-device-edit-form').hidden = true;
      get('shared-record-panel').hidden = view !== 'records';
      get('shared-record-reassign').hidden = true;
      reassignTasks = [];
      const title = get('shared-dialog-title');
      title.textContent = view === 'assign' ? `分配 ${selectedTaskIds.length} 家门店`
        : view === 'records' ? '门店分配记录' : '采集电脑';
      if (view === 'records') {
        filterDevice = deviceId;
        recordDevice.value = filterDevice;
        recordDevice.hidden = !!deviceId;
        get('shared-record-back').hidden = !deviceId;
        recordStatus.value = '';
        recordSearch.value = '';
        recordPage = 1;
        clearRecords();
      }
      selectedTasks = new Map(tasks.map(task => [String(task.id), task]));
      conflicts = new Set();
      get('shared-assign-conflicts').hidden = true;
      get('shared-assign-conflicts').replaceChildren();
      const batchCounts = new Map();
      const selected = selectedTaskIds.map(id => {
        const task = selectedTasks.get(id);
        const batch = task?.batch_name || '批次未记录';
        batchCounts.set(batch, (batchCounts.get(batch) || 0) + 1);
        return node('li', [storeName(task?.source_name) || id, batch, task?.version].filter(Boolean).join(' · '), 'shared-row');
      });
      get('shared-assignment-batches').textContent = [...batchCounts].map(([name,count]) => `${name} ${count} 家`).join(' · ');
      get('shared-selection-details').open = false;
      get('shared-selection-list').replaceChildren(...selected);
      setError('shared-dialog-error', '');
      if (!dialog.open) dialog.showModal();
      renderDeviceOptions();
      if (view === 'assign') workerSelect.value = '';
      renderManagement();
      syncAssignmentSubmit();
      title.focus();
      if (view === 'records') void refreshRecords(true);
    }

    get('shared-manage').addEventListener('click', () => showView('devices'));
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
    get('shared-dialog-close').addEventListener('click', () => {
      if (!working) dialog.close();
    });
    get('shared-device-back').addEventListener('click', () => showView('devices'));
    get('shared-device-tasks').addEventListener('click', () => showView('records', selectedDeviceId));
    get('shared-record-back').addEventListener('click', () => showView('detail', selectedDeviceId));
    get('shared-device-edit').addEventListener('click', () => {
      const device = currentDevice();
      if (!device) return;
      get('shared-device-name').value = device.name;
      get('shared-device-description').value = device.description || '';
      get('shared-device-edit-form').hidden = false;
      renderManagement();
      get('shared-device-name').focus();
    });
    get('shared-device-edit-cancel').addEventListener('click', () => {
      if (working) return;
      get('shared-device-edit-form').hidden = true;
      renderManagement();
      get('shared-device-edit').focus();
    });
    get('shared-device-edit-form').addEventListener('submit', async event => {
      event.preventDefault();
      const device = currentDevice();
      if (!device || working) return;
      const save = get('shared-device-edit-save');
      working = true;
      save.disabled = true;
      setError('shared-dialog-error', '');
      try {
        await api(devicePath(device.id), {method: 'PATCH', body: JSON.stringify({
          name: get('shared-device-name').value, description: get('shared-device-description').value,
        })});
        get('shared-device-edit-form').hidden = true;
        await refreshRequest;
        await refresh();
        onChange();
        get('shared-device-edit').focus();
        toast('设备信息已保存');
      } catch (error) {
        setError('shared-dialog-error', error.message);
      } finally {
        working = false;
        save.disabled = false;
        renderManagement();
      }
    });
    for (const action of ['revoke', 'archive']) {
      get('shared-device-' + action).addEventListener('click', async () => {
        const device = currentDevice();
        if (!device || working) return;
        const revoke = action === 'revoke';
        const message = revoke
          ? `解除「${device.name}」的配对？该电脑不能再下载任务或补传结果，历史记录会保留。`
          : `从设备列表移除「${device.name}」？它的历史分配和采集结果会保留。`;
        if (!window.confirm(message)) return;
        working = true;
        setError('shared-dialog-error', '');
        renderManagement();
        try {
          await post(`${devicePath(device.id)}/${action}`);
          await refreshRequest;
          await refresh();
          if (!revoke) showView('devices');
          onChange();
          toast(revoke ? '已解除配对' : '已从设备列表移除');
        } catch (error) {
          setError('shared-dialog-error', error.message);
        } finally {
          working = false;
          renderManagement();
        }
      });
    }
    get('shared-record-select-page').addEventListener('change', event => {
      selectedRecords.clear();
      if (event.target.checked) {
        for (const record of recordRows.filter(reclaimable)) selectedRecords.add(record.id);
      }
      syncRecordSelection();
    });
    get('shared-record-reclaim').addEventListener('click', event =>
      void reclaimRecords([...selectedRecords], event.currentTarget));
    get('shared-record-reassign').addEventListener('click', () => {
      showView('assign', '', reassignTasks.map(task => task.id), reassignTasks);
    });
    workerSelect.addEventListener('change', syncAssignmentSubmit);
    for (const [element, event] of [[recordDevice, 'change'], [recordStatus, 'change'], [recordSearch, 'input']]) {
      element.addEventListener(event, () => {
        filterDevice = recordDevice.value;
        recordPage = 1;
        clearRecords();
        clearTimeout(recordsTimer);
        recordsTimer = setTimeout(() => void refreshRecords(true), event === 'input' ? 180 : 0);
      });
    }
    for (const [id, delta] of [['shared-record-prev', -1], ['shared-record-next', 1]]) {
      get(id).addEventListener('click', () => {
        recordPage += delta;
        clearRecords();
        void refreshRecords(true);
        get('shared-record-panel').scrollIntoView({block: 'nearest'});
      });
    }
    dialog.addEventListener('cancel', event => { if (working) event.preventDefault(); });
    get('shared-assign-form').addEventListener('submit', async event => {
      event.preventDefault();
      if (working || !selectedTaskIds.length || !workerSelect.value) return;
      working = true;
      syncAssignmentSubmit();
      get('shared-dialog-close').disabled = true;
      setError('shared-dialog-error', '');
      const taskIds = selectedTaskIds.filter(id => !conflicts.has(id));
      const worker = workers().find(candidate => candidate.id === workerSelect.value);
      try {
        await post('/api/shared/assignments', {worker_id: workerSelect.value, task_ids: taskIds});
        await refresh();
        onAssigned(taskIds);
        dialog.close();
        toast(`已将 ${taskIds.length} 家门店分给 ${worker?.name || '所选电脑'}`);
      } catch (error) {
        setError('shared-dialog-error', error.message);
        if (Array.isArray(error.conflicts)) {
          const list = get('shared-assign-conflicts');
          const labelsByReason = {
            materials: '待重新选码', revision: '发布版本已变化', unavailable: '当前状态不可采集',
            source: '发布二维码缺失或已变化',
          };
          const rows = new Map([...list.children].map(row => [row.dataset.taskId, row]));
          for (const item of error.conflicts) {
            if (!selectedTaskIds.includes(item.task_id)) continue;
            const name = storeName(selectedTasks.get(item.task_id)?.source_name) || item.task_id;
            const reason = item.reason === 'assigned' ? `已分配给 ${item.worker_name}` : labelsByReason[item.reason];
            const row = node('li', `${name} · ${reason || '状态已变化'}`);
            row.dataset.taskId = item.task_id;
            rows.set(item.task_id, row);
          }
          conflicts = new Set(rows.keys());
          list.replaceChildren(...rows.values());
          list.hidden = !rows.size;
        }
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

    // workers/assignments 供工作台首页设备行与采集端同步详情复用同一份状态快照。
    return {role, refresh, allowsTask, assignmentFor, workers, assignments, snapshot: () => status,
      showAssignments: (ids, tasks) => showView('assign', '', ids, tasks),
      showManager: workerId => showView(workerId ? 'detail' : 'devices', workerId),
      showRecords: () => showView('records')};
  };
})();
