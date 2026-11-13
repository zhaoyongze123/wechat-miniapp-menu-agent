import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ACTIONS, ShapeError, buildHostParams, hostMethods, validateArgs,
} from '../ui_tars/action-spec.mjs';

const CTX = { frameId: 'frame-1', width: 1000, height: 500 };

test('schema 里每个动作都有模型可见的 doc；除 terminate/noop 外都有宿主映射', () => {
  for (const [name, spec] of Object.entries(ACTIONS)) {
    assert.equal(typeof spec.doc, 'string', name);
    assert.ok(spec.doc.length > 0, name);
    if (name === 'terminate' || name === 'noop') assert.equal(spec.host, null, name);
    else assert.ok(spec.host?.method, name);
  }
});

test('switch_window 必须带 reason，但 reason 不进入宿主参数', () => {
  assert.throws(
    () => validateArgs('switch_window', { action: 'switch_window', title: '门店B' }),
    (error) => error instanceof ShapeError && error.field === 'reason' && error.reason === 'missing',
  );
  assert.deepEqual(buildHostParams('switch_window',
    { action: 'switch_window', title: '门店B', reason: '切到门店B核对菜单' }, CTX).params,
  { frame_id: 'frame-1', title: '门店B' });
});

test('left_click 映射到宿主 click 并保留截图坐标', () => {
  const args = { action: 'left_click', coordinate: [500, 250], reason: '点击开始点餐按钮' };
  validateArgs('left_click', args);
  const call = buildHostParams('left_click', args, CTX);
  assert.equal(call.method, 'click');
  assert.deepEqual(call.params, { frame_id: 'frame-1', x: 500, y: 125 });
  assert.deepEqual(call.point, { x: 500, y: 125 });
});

test('left_click 必须带 reason，但 reason 不进入宿主参数', () => {
  assert.throws(
    () => validateArgs('left_click', { action: 'left_click', coordinate: [10, 10] }),
    (error) => error instanceof ShapeError && error.field === 'reason' && error.reason === 'missing',
  );
  const call = buildHostParams('left_click',
    { action: 'left_click', coordinate: [10, 10], reason: '点击关闭弹窗' }, CTX);
  assert.deepEqual(call.params, { frame_id: 'frame-1', x: 10, y: 5 });
});

test('scroll 不传坐标时使用截图中心，符号转成宿主方向', () => {
  const down = buildHostParams('scroll', { action: 'scroll', pixels: -800 }, CTX);
  assert.equal(down.method, 'scroll');
  assert.deepEqual(down.params, {
    frame_id: 'frame-1', x: 500, y: 250, direction: 'Down', pixels: -800,
  });
  const up = buildHostParams('scroll', { action: 'scroll', pixels: 300, coordinate: [10, 20] }, CTX);
  assert.equal(up.params.direction, 'Up');
  assert.deepEqual(up.point, { x: 10, y: 10 });
});

test('wait/scroll/key/type_text 都必须带 reason，但 reason 不进入宿主参数', () => {
  for (const args of [
    { action: 'wait', time: 1 },
    { action: 'scroll', pixels: -300 },
    { action: 'key', key: 'esc' },
    { action: 'type_text', text: '米饭' },
  ]) {
    assert.throws(
      () => validateArgs(args.action, args),
      (error) => error instanceof ShapeError && error.field === 'reason' && error.reason === 'missing',
      args.action,
    );
  }
  assert.deepEqual(buildHostParams('scroll',
    { action: 'scroll', pixels: 300, reason: '向上滚动查看完整菜单' }, CTX).params,
  { frame_id: 'frame-1', x: 500, y: 250, direction: 'Up', pixels: 300 });
});

test('必填字段缺失、字段多余、取值非法都给出精确字段名', () => {
  assert.throws(
    () => validateArgs('left_click', { action: 'left_click' }),
    (error) => error instanceof ShapeError && error.field === 'coordinate' && error.reason === 'missing',
  );
  assert.throws(
    () => validateArgs('left_click', { action: 'left_click', coordinate: [1, 2], reason: 'x', x: 3 }),
    (error) => error instanceof ShapeError && error.field === 'x' && error.reason === 'unexpected',
  );
  assert.throws(
    () => validateArgs('type_text', { action: 'type_text', text: '   ' }),
    (error) => error instanceof ShapeError && error.field === 'text' && error.reason === 'value',
  );
  assert.throws(
    () => validateArgs('wait', { action: 'wait', time: 9 }),
    (error) => error instanceof ShapeError && error.field === 'time' && error.reason === 'value',
  );
});

test('坐标超出归一化空间时按字段报错', () => {
  assert.throws(
    () => buildHostParams('left_click', { action: 'left_click', coordinate: [1200, 10] }, CTX),
    (error) => error instanceof ShapeError && error.field === 'coordinate',
  );
});

test('supportedActions 由 schema 派生，宿主方法不重复', () => {
  const methods = hostMethods();
  assert.deepEqual(methods.sort(), ['click', 'key', 'scroll', 'switch_window', 'type_text', 'wait', 'windows']);
});
