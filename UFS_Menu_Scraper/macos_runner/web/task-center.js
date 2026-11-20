/* 统一任务中心：素材分析与采集运行共用一个入口，轮询 /api/tasks/active。 */
(function () {
  'use strict';
  const STATUS = {
    queued: '排队中', running: '运行中', pause_requested: '正在暂停', paused: '已暂停',
    interrupted: '已中断', completed: '已完成', failed: '失败', cancelled: '已取消',
  };
  const SOURCE = { materials: '素材', capture: '采集' };
  const POLL_MS = 2000;

  window.__taskCenterInit = function init({ api, toast, setPage }) {
    const panel = document.createElement('section');
    panel.className = 'task-center';
    panel.setAttribute('aria-label', '任务中心');
    panel.innerHTML = `
      <div class="task-center-head">
        <div><strong>任务中心</strong> <span data-count></span></div>
        <button class="task-center-toggle" type="button" data-toggle aria-label="收起任务中心">收起</button>
      </div>
      <div class="task-center-list" data-list></div>`;
    document.body.append(panel);
    const list = panel.querySelector('[data-list]');
    const countLabel = panel.querySelector('[data-count]');
    const toggle = panel.querySelector('[data-toggle]');
    toggle.addEventListener('click', () => {
      const collapsed = panel.classList.toggle('collapsed');
      toggle.textContent = collapsed ? '展开' : '收起';
    });
    // 侧边栏「任务中心」入口：展开面板并高亮，避免面板被其他浮层遮住时找不到。
    const openPanel = () => {
      panel.classList.remove('collapsed');
      toggle.textContent = '收起';
      panel.classList.add('flash');
      setTimeout(() => panel.classList.remove('flash'), 1500);
    };
    window.__taskCenterOpen = openPanel;
    document.querySelectorAll('[data-task-center]').forEach((btn) => btn.addEventListener('click', openPanel));

    const dialog = document.createElement('dialog');
    dialog.className = 'task-detail';
    dialog.innerHTML = `
      <div class="task-detail-body">
        <h2 data-title></h2>
        <div class="task-detail-bar"><i data-fill></i></div>
        <div class="task-detail-meta">
          <div>进度<b data-progress></b></div>
          <div>状态<b data-status></b></div>
          <div>当前门店<b data-current></b></div>
          <div>预计剩余<b data-eta></b></div>
          <div>失败<b data-failed></b></div>
          <div>排队位次<b data-queue></b></div>
        </div>
        <p class="task-sub" data-message></p>
        <ul class="task-events" data-events></ul>
        <div class="task-detail-actions">
          <button class="task-btn" type="button" data-open>打开页面</button>
          <button class="task-btn" type="button" data-pause>暂停</button>
          <button class="task-btn primary" type="button" data-resume>继续</button>
          <button class="task-btn" type="button" data-retry>重试失败门店</button>
          <button class="task-btn danger" type="button" data-cancel>取消任务</button>
          <button class="task-btn primary" type="button" data-close>关闭</button>
        </div>
      </div>`;
    document.body.append(dialog);
    const q = (name) => dialog.querySelector(`[data-${name}]`);
    let current = null;
    let eventTimer = null;
    let lastTasks = [];

    function formatDuration(seconds) {
      const value = Math.max(0, Number(seconds) || 0);
      if (!value) return '—';
      if (value < 60) return `${Math.max(1, Math.round(value))} 秒`;
      if (value < 3600) return `${Math.round(value / 60)} 分钟`;
      return `${(value / 3600).toFixed(1)} 小时`;
    }

    function percentOf(task) {
      const total = Number(task?.progress?.total) || 0;
      const done = Number(task?.progress?.done) || 0;
      return total ? Math.min(100, Math.round(done / total * 100)) : 0;
    }

    function subtitle(task) {
      const parts = [];
      if (task.progress?.total) parts.push(`${task.progress.done} / ${task.progress.total} 家`);
      if (task.extra?.stage) parts.push(task.extra.stage);
      if (task.extra?.current) parts.push(`当前：${task.extra.current}`);
      return parts.join(' · ');
    }

    function renderList(tasks) {
      lastTasks = tasks;
      // 面板常驻：没有任务时也保留入口，避免用户既看不到历史任务也找不到暂停/取消。
      panel.hidden = false;
      list.replaceChildren();
      countLabel.textContent = tasks.length ? `${tasks.length} 个任务` : '暂无进行中的任务';
      if (!tasks.length) {
        const empty = document.createElement('div');
        empty.className = 'task-empty';
        empty.textContent = '当前没有运行中的任务；最近 10 分钟内结束的任务会保留在这里。';
        list.append(empty);
        return;
      }
      for (const task of tasks) {
        const row = document.createElement('button');
        row.type = 'button';
        row.className = 'task-row';
        const source = document.createElement('span');
        source.className = `task-source ${task.source}`;
        source.textContent = SOURCE[task.source] || task.source;
        const main = document.createElement('span');
        main.className = 'task-main';
        const title = document.createElement('span');
        title.className = 'task-title';
        title.textContent = task.title || task.id;
        const sub = document.createElement('span');
        sub.className = 'task-sub';
        sub.textContent = subtitle(task) || task.kind;
        const bar = document.createElement('span');
        bar.className = 'task-progress';
        const fill = document.createElement('i');
        fill.style.width = `${percentOf(task)}%`;
        bar.append(fill);
        main.append(title, sub, bar);
        const status = document.createElement('span');
        status.className = `task-status ${task.status}`;
        status.textContent = STATUS[task.status] || task.status;
        row.append(source, main, status);
        row.addEventListener('click', () => openDetail(task));
        list.append(row);
      }
    }

    function renderDetail(job) {
      const task = job.source ? job : current;
      q('title').textContent = task.title || task.id;
      const percent = percentOf(task);
      q('fill').style.width = `${percent}%`;
      q('progress').textContent = task.progress?.total
        ? `${task.progress.done} / ${task.progress.total}（${percent}%）` : '—';
      q('status').textContent = STATUS[task.status] || task.status;
      q('current').textContent = job.current_store_name || task.extra?.current || '—';
      q('eta').textContent = formatDuration(job.eta_seconds ?? task.extra?.eta_seconds);
      q('failed').textContent = String(job.failed_stores ?? task.extra?.failed ?? 0);
      q('queue').textContent = Number(task.extra?.queue_position) ? `第 ${task.extra.queue_position} 位` : '—';
      q('message').textContent = job.message || job.error || '';
      const events = Array.isArray(job.events) ? job.events : [];
      q('events').replaceChildren();
      for (const event of events) {
        const item = document.createElement('li');
        const time = document.createElement('time');
        time.textContent = String(event.time || '').slice(11, 19);
        const text = document.createElement('span');
        text.className = event.level || 'info';
        text.textContent = event.message;
        item.append(time, text);
        q('events').append(item);
      }
      const isMaterial = task.source === 'materials';
      const isCapture = task.source === 'capture';
      const open = ['queued', 'running', 'pause_requested', 'paused', 'interrupted'].includes(job.status);
      // 采集侧复用同一套按钮：暂停映射 /api/stop，继续映射 /api/resume，取消映射 /api/terminate。
      q('pause').hidden = !((isMaterial && job.status === 'running') || (isCapture && job.status === 'running'));
      q('resume').hidden = !(
        (isMaterial && ['paused', 'interrupted'].includes(job.status))
        || (isCapture && ['paused', 'blocked', 'interrupted'].includes(job.status))
      );
      q('cancel').hidden = !(
        (isMaterial && open)
        || (isCapture && ['running', 'paused', 'blocked', 'interrupted'].includes(job.status))
      );
      q('retry').hidden = !(isMaterial && Number(job.failed_stores || job.counts?.failed));
      q('cancel').textContent = isCapture ? '终止任务' : '取消任务';
    }

    async function openDetail(task) {
      current = task;
      renderDetail(task);
      dialog.showModal();
      await refreshDetail();
      if (task.source === 'materials') {
        clearInterval(eventTimer);
        eventTimer = setInterval(() => { if (dialog.open) void refreshDetail(); }, 1500);
      }
    }

    async function refreshDetail() {
      if (!current || current.source !== 'materials') return;
      try {
        const job = await api(`/api/materials/jobs/${encodeURIComponent(current.id)}?events=1`);
        renderDetail({
          ...job,
          source: 'materials',
          title: current.title,
          progress: { done: job.done_stores, total: job.total_stores },
          extra: { ...current.extra },
        });
      } catch (err) {
        q('message').textContent = err.message;
      }
    }

    async function act(action) {
      if (!current) return;
      const base = `/api/materials/jobs/${encodeURIComponent(current.id)}`;
      try {
        if (current.source === 'capture') {
          if (action === 'cancel' && !window.confirm('终止当前采集任务？已采集的门店会保留。')) return;
          const route = { pause: '/api/stop', resume: '/api/resume', cancel: '/api/terminate' }[action];
          if (!route) return;
          await api(route, { method: 'POST', body: '{}' });
        } else if (action === 'retry') {
          await api(`/api/materials/batches/${encodeURIComponent(current.extra?.batch_id || '')}/retry-failed`, { method: 'POST', body: '{}' });
        } else {
          await api(`${base}/${action}`, { method: 'POST', body: '{}' });
        }
        toast('操作已提交');
        await poll();
        await refreshDetail();
      } catch (err) {
        toast(err.message);
      }
    }

    q('pause').addEventListener('click', () => void act('pause'));
    q('resume').addEventListener('click', () => void act('resume'));
    q('cancel').addEventListener('click', () => void act('cancel'));
    q('retry').addEventListener('click', () => void act('retry'));
    q('open').addEventListener('click', () => {
      const page = current?.source === 'capture' ? 'overview' : 'materials';
      if (typeof setPage === 'function') setPage(page);
      else window.location.hash = `#${page}`;
      dialog.close();
    });
    q('close').addEventListener('click', () => dialog.close());
    dialog.addEventListener('close', () => {
      clearInterval(eventTimer);
      eventTimer = null;
      current = null;
    });

    let timer = null;
    let polling = false;
    async function poll() {
      if (document.hidden || polling) return schedule();
      polling = true;
      try {
        const data = await api('/api/tasks/active');
        renderList(Array.isArray(data.tasks) ? data.tasks : []);
        if (dialog.open && current) {
          const latest = lastTasks.find((task) => task.id === current.id);
          if (!latest) { dialog.close(); }
          else if (current.source === 'capture') { current = latest; renderDetail(latest); }
          else { current = latest; }
        }
      } catch (err) {
        // 任务中心失败不影响主流程，下一轮继续。
      } finally {
        polling = false;
        schedule();
      }
    }
    function schedule() {
      clearTimeout(timer);
      timer = setTimeout(() => void poll(), POLL_MS);
    }
    document.addEventListener('visibilitychange', () => { if (!document.hidden) void poll(); });
    void poll();
    return { refresh: poll };
  };

  // 主脚本先执行并暴露桥接对象；本文件在其后加载，直接启动，无需页面再写初始化代码。
  if (window.__consoleBridge) window.__taskCenterInit(window.__consoleBridge);
})();
