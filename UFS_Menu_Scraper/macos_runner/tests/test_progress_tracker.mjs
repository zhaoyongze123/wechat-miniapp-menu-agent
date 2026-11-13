import test from 'node:test';
import assert from 'node:assert/strict';

import { NoProgressTracker } from '../ui_tars/progress-tracker.mjs';

const A = { x: 500, y: 300 };

test('摘要只给事实：逐点读数与相似画面回访', () => {
  const tracker = new NoProgressTracker();
  const signature = '0f0f0f0f0f0f0f0f';
  tracker.actionSent('h1', { method: 'click', point: { x: 100, y: 200 }, frameId: 1, targetName: '点餐' });
  tracker.observe('h2', { delta: 0, roiDelta: 0, frameId: 2, signature });
  tracker.actionSent('h3', { method: 'click', point: { x: 300, y: 400 }, frameId: 2, targetName: '点餐' });
  tracker.observe('h3', { delta: 0, roiDelta: 0, frameId: 3, signature });
  // 回到相似画面后又点了同一位置一次：合并成一条，但要标出重复次数。
  tracker.actionSent('h4', { method: 'click', point: { x: 100, y: 200 }, frameId: 3, targetName: '点餐' });
  tracker.observe('h5', { delta: 0, roiDelta: 0, frameId: 9, signature });

  const text = tracker.digest(9, signature);
  assert.match(text, /与第 3 帧相似/);
  assert.match(text, /「点餐」\(100,200\) 前后未观察到变化（同一位置重复 1 次）/);
  // 前后帧哈希相同即像素完全相同，与 delta 读数分开表述。
  assert.match(text, /「点餐」\(300,400\) 前后像素完全相同/);
  assert.match(text, /已实际点击 3 次/);
  assert.doesNotMatch(text, /换入口|换目标|建议/);
});

test('证据行先给模型坐标，像素只作附注', () => {
  const tracker = new NoProgressTracker();
  tracker.actionSent('frame-1', {
    method: 'click', point: { x: 490, y: 941 }, pixel: { x: 143, y: 922 },
    reason: '点击开始点餐按钮',
  });
  // 点击原因要能取回，重试提示会用它回述模型意图。
  assert.equal(tracker.pendingReason, '点击开始点餐按钮');
  tracker.observe('frame-2', { delta: 0, roiDelta: 0 });
  assert.match(tracker.evidence(), /left_click\(490,941\)，像素\(143,922\)/);
});

test('单次点击无变化只累计，不提示', () => {
  const tracker = new NoProgressTracker();
  tracker.actionSent('frame-1', { method: 'click', point: A });
  assert.equal(tracker.observe('frame-1', { delta: 0, roiDelta: 0 }), '');
  assert.equal(tracker.noChangeStreak, 1);
});

test('小控件只推动局部变化也算已响应，不累计无进展', () => {
  const tracker = new NoProgressTracker();
  tracker.actionSent('frame-1', { method: 'click', point: A });
  // 典型“选人数”：全局几乎不动（0.4%），落点附近出现选中态（15%）。
  assert.equal(tracker.observe('frame-2', { delta: 0.4, roiDelta: 15 }), '');
  assert.equal(tracker.noChangeStreak, 0);
  assert.equal(tracker.sameTargetStreak, 0);
  assert.match(tracker.evidence(), /局部变化 15%/);
});

test('原分辨率 ROI 下 1% 左右的小控件变化也算已响应', () => {
  const tracker = new NoProgressTracker();
  tracker.actionSent('frame-1', { method: 'click', point: A });
  assert.equal(tracker.observe('frame-2', { delta: 0.2, roiDelta: 1.2 }), '');
  assert.equal(tracker.noChangeStreak, 0);
  assert.match(tracker.evidence(), /局部变化 1.2%/);
});

test('全局变化达到阈值也算已响应并重置连续计数', () => {
  const tracker = new NoProgressTracker();
  tracker.actionSent('frame-1', { method: 'click', point: A });
  tracker.observe('frame-1', { delta: 0, roiDelta: 0 });
  tracker.actionSent('frame-1', { method: 'click', point: A });
  tracker.observe('frame-1', { delta: 0, roiDelta: 0 });
  tracker.actionSent('frame-1', { method: 'click', point: A });
  assert.equal(tracker.observe('frame-2', { delta: 8.6, roiDelta: 0 }), '');
  assert.equal(tracker.noChangeStreak, 0);
  assert.equal(tracker.sameTargetStreak, 0);
});

test('同一目标连续两次点击无变化时只累计，由适配层分阶段提示', () => {
  const tracker = new NoProgressTracker();
  tracker.actionSent('frame-1', { method: 'click', point: A });
  tracker.observe('frame-1', { delta: 0, roiDelta: 0 });
  tracker.actionSent('frame-1', { method: 'click', point: { x: 520, y: 310 } });
  const notice = tracker.observe('frame-1', { delta: 0.2, roiDelta: 0.2 });
  assert.equal(notice, '');
  assert.equal(tracker.sameTargetStreak, 2);
});

test('不同位置连续无变化只累计事实，不替模型下结论', () => {
  const tracker = new NoProgressTracker();
  const points = [{ x: 100, y: 100 }, { x: 700, y: 700 }, { x: 200, y: 800 }];
  const notices = points.map((point) => {
    tracker.actionSent('frame-1', { method: 'click', point });
    return tracker.observe('frame-1', { delta: 0, roiDelta: 0 });
  });
  assert.deepEqual(notices, ['', '', '']);
  assert.equal(tracker.noChangeStreak, 3);
  assert.match(tracker.lastSummary, /连续无变化 3 次/);
});

test('全局与落点都低于阈值时才算无变化', () => {
  const tracker = new NoProgressTracker();
  tracker.actionSent('frame-1', { method: 'click', point: A });
  tracker.observe('frame-2', { delta: 0.4, roiDelta: 0.2 });
  assert.equal(tracker.noChangeStreak, 1);
  assert.match(tracker.evidence(), /全局变化 0.4%/);
  assert.match(tracker.evidence(), /局部变化 0.2%/);
});

test('轻微变化只保留读数，不计连续无变化', () => {
  const tracker = new NoProgressTracker();
  tracker.actionSent('frame-2', { method: 'click', point: A });
  const notice = tracker.observe('frame-3', { delta: 1.8, roiDelta: 0.8 });
  assert.equal(notice, '');
  assert.match(tracker.lastSummary, /轻微变化/);
  assert.equal(tracker.noChangeStreak, 0);
  assert.equal(tracker.sameTargetStreak, 0);
});

test('输入与按键不参与无进展统计，只留证据', () => {
  const tracker = new NoProgressTracker();
  tracker.actionSent('frame-1', { method: 'type_text' });
  assert.equal(tracker.observe('frame-1', { delta: 0, roiDelta: null }), '');
  tracker.actionSent('frame-1', { method: 'key' });
  assert.equal(tracker.observe('frame-1', { delta: 0, roiDelta: null }), '');
  assert.equal(tracker.noChangeStreak, 0);
  assert.match(tracker.evidence(), /不参与无进展统计/);
});

test('滚动按整页变化统计，无变化会累计', () => {
  const tracker = new NoProgressTracker();
  tracker.actionSent('frame-1', { method: 'scroll', point: A });
  tracker.observe('frame-1', { delta: 0, roiDelta: null });
  tracker.actionSent('frame-2', { method: 'scroll', point: A });
  tracker.observe('frame-3', { delta: 5.2, roiDelta: null });
  assert.equal(tracker.noChangeStreak, 0);
  tracker.actionSent('frame-3', { method: 'scroll', point: A });
  tracker.observe('frame-3', { delta: 0.3, roiDelta: null });
  assert.equal(tracker.noChangeStreak, 1);
});

test('点击缺少局部读数时不下无进展结论', () => {
  const tracker = new NoProgressTracker();
  tracker.actionSent('frame-1', { method: 'click', point: A });
  assert.equal(tracker.observe('frame-2', { delta: 0.4, roiDelta: null }), '');
  assert.equal(tracker.noChangeStreak, 0);
  assert.match(tracker.evidence(), /缺少局部读数/);
});

test('没有待比较的动作时不提示，也不留下过期证据', () => {
  const tracker = new NoProgressTracker();
  assert.equal(tracker.observe('frame-1', { delta: 0, roiDelta: 0 }), '');
  assert.equal(tracker.evidence(), '');
  tracker.actionSent('frame-1', { method: 'click', point: A });
  tracker.observe('frame-1', { delta: 0, roiDelta: 0 });
  assert.equal(tracker.observe('frame-1', { delta: 0, roiDelta: 0 }), '');
  assert.equal(tracker.evidence(), '');
});
