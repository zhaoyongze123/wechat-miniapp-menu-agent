import test from 'node:test';
import assert from 'node:assert/strict';

import {
  ACTIONS, OBSERVATION_FIELD, OBSERVATION_MAX_CHARS, ShapeError, buildHostParams,
  extractObservation, hostMethods, validateArgs,
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
  const args = { action: 'left_click', coordinate: [500, 250], reason: '点击开始点餐按钮', target_name: '开始点餐' };
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
    (error) => error instanceof ShapeError && error.field === 'reason' && error.reason === 'missing',
  );
  assert.throws(
    () => buildHostParams('left_click', { action: 'left_click', reason: '点击按钮' }, CTX),
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

test('left_click 必填 coordinate，执行时用点坐标', () => {
  const args = { action: 'left_click', coordinate: [726, 965], reason: '点击同意并继续', target_name: '同意' };
  validateArgs('left_click', args);
  const call = buildHostParams('left_click', args, CTX);
  assert.equal(call.method, 'click');
  // CTX 是 1000x500：点 (726, 965) → 截图坐标 (726, 482.5)。
  assert.deepEqual(call.params, { frame_id: 'frame-1', x: 726, y: 482.5 });
  assert.deepEqual(call.point, { x: 726, y: 482.5 });
});

test('left_click 缺少 coordinate 按字段报错，target_box 已下线', () => {
  assert.throws(
    () => validateArgs('left_click', { action: 'left_click', reason: 'x' }),
    (error) => error instanceof ShapeError && error.field === 'coordinate' && error.reason === 'missing',
  );
  assert.throws(
    () => validateArgs('left_click', { action: 'left_click', target_box: [4, 5, 60, 70], reason: 'x' }),
    (error) => error instanceof ShapeError && error.field === 'target_box',
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

test('附带门店名称被剥离，不进入动作参数或宿主参数', () => {
  const args = { action: 'left_click', coordinate: [10, 20], reason: '点击分类', target_name: '分类',
                 [OBSERVATION_FIELD]: '东方饺子王（北礼士路店）' };
  const { args: cleaned, observation } = extractObservation(args);
  assert.deepEqual(observation, { name: '东方饺子王（北礼士路店）' });
  assert.equal(OBSERVATION_FIELD in cleaned, false);
  validateArgs('left_click', cleaned);
  assert.deepEqual(buildHostParams('left_click', cleaned, CTX).params,
                   { frame_id: 'frame-1', x: 10, y: 10 });
});

test('空字符串是显式撤销；省略或非法值都表示没有新信息', () => {
  assert.deepEqual(extractObservation({ action: 'noop', [OBSERVATION_FIELD]: '' }).observation,
                   { revoke: true });
  assert.equal(extractObservation({ action: 'noop' }).observation, null);
  assert.equal(extractObservation({ action: 'noop', [OBSERVATION_FIELD]: 42 }).observation, null);
  assert.equal(extractObservation({ action: 'noop', [OBSERVATION_FIELD]: 'x'.repeat(OBSERVATION_MAX_CHARS + 1) }).observation,
               null);
});

test('纯空白/换行/控制字符清洗后为空按无效观察处理，不撤销已确认名称', () => {
  for (const raw of ['   ', '\n', '\t\r\n', '\u0000\u0007', ' \n\u0008 ']) {
    assert.equal(extractObservation({ action: 'noop', [OBSERVATION_FIELD]: raw }).observation, null, JSON.stringify(raw));
  }
});

test('附带门店名称清洗控制字符与空白并限长，超长不截断', () => {
  const messy = extractObservation({ action: 'noop', [OBSERVATION_FIELD]: '  老王\n饭店\t(二店)  ' });
  assert.deepEqual(messy.observation, { name: '老王 饭店 (二店)' });
  const atLimit = extractObservation({ action: 'noop', [OBSERVATION_FIELD]: '店'.repeat(OBSERVATION_MAX_CHARS) });
  assert.equal(atLimit.observation.name.length, OBSERVATION_MAX_CHARS);
  // 非法附带值不能把原本合法的动作判成格式错误。
  const { args } = extractObservation({ action: 'left_click', coordinate: [1, 2], reason: 'x',
                                        target_name: 'x', [OBSERVATION_FIELD]: ['bad'] });
  validateArgs('left_click', args);
});
