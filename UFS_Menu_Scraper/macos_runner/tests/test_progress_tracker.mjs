import test from 'node:test';
import assert from 'node:assert/strict';

import { NoProgressTracker } from '../ui_tars/progress-tracker.mjs';

const SIZE = [1000, 1000];
const A = { x: 500, y: 300 };

test('单次点击无变化只累计，不提示', () => {
  const tracker = new NoProgressTracker();
  tracker.clickSent('frame-1', A);
  assert.equal(tracker.observe('frame-1', ...SIZE), '');
  assert.equal(tracker.noChangeStreak, 1);
});

test('同一目标连续两次点击无变化时提示换位置或换入口', () => {
  const tracker = new NoProgressTracker();
  tracker.clickSent('frame-1', A);
  tracker.observe('frame-1', ...SIZE);
  tracker.clickSent('frame-1', { x: 520, y: 310 });
  const notice = tracker.observe('frame-1', ...SIZE);
  assert.match(notice, /同一目标/);
  assert.match(notice, /没有明显变化/);
  assert.equal(tracker.sameTargetStreak, 2);
});

test('不同位置连续无变化到 4 次时提示放弃当前入口', () => {
  const tracker = new NoProgressTracker();
  const points = [{ x: 100, y: 100 }, { x: 700, y: 700 }, { x: 200, y: 800 }, { x: 800, y: 200 }];
  const notices = points.map((point) => {
    tracker.clickSent('frame-1', point);
    return tracker.observe('frame-1', ...SIZE);
  });
  assert.equal(notices[0], '');
  assert.equal(notices[1], '');
  assert.equal(notices[2], '');
  assert.match(notices[3], /连续 4 次点击/);
  assert.match(notices[3], /needs_review/);
});

test('画面变化后重置连续计数', () => {
  const tracker = new NoProgressTracker();
  tracker.clickSent('frame-1', A);
  tracker.observe('frame-1', ...SIZE);
  tracker.clickSent('frame-1', A);
  tracker.observe('frame-1', ...SIZE);
  tracker.clickSent('frame-1', A);
  assert.equal(tracker.observe('frame-2', ...SIZE), '');
  assert.equal(tracker.noChangeStreak, 0);
  assert.equal(tracker.sameTargetStreak, 0);
});

test('没有待比较的点击时不提示（等待或滚动不参与）', () => {
  const tracker = new NoProgressTracker();
  assert.equal(tracker.observe('frame-1', ...SIZE), '');
  tracker.clickSent('frame-1', A);
  tracker.observe('frame-1', ...SIZE);
  assert.equal(tracker.observe('frame-1', ...SIZE), '');
});
