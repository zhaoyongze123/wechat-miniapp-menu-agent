import test from 'node:test';
import assert from 'node:assert/strict';

import { parseActionPayload } from '../ui_tars/action-repair.mjs';

const LEFT_CLICK = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":[118,873]}}';

test('合法 JSON 原样返回 arguments', () => {
  assert.deepEqual(parseActionPayload(LEFT_CLICK), { action: 'left_click', coordinate: [118, 873] });
});

test('修复百炼漏掉坐标两侧方括号的 left_click', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":118, 873}}';
  assert.deepEqual(parseActionPayload(payload), { action: 'left_click', coordinate: [118, 873] });
});

test('修复漏掉左方括号的 left_click', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":118, 873]}}';
  assert.deepEqual(parseActionPayload(payload), { action: 'left_click', coordinate: [118, 873] });
});

test('只修复 left_click，不猜测其他动作的坐标', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"right_click","coordinate":118, 873}}';
  assert.throws(() => parseActionPayload(payload), SyntaxError);
});

test('坐标数量不是两个时拒绝', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":118, 873, 12}}';
  assert.throws(() => parseActionPayload(payload), SyntaxError);
});

test('负数不是可确认的坐标变体，拒绝', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":-118, 873}}';
  assert.throws(() => parseActionPayload(payload), SyntaxError);
});

test('超出归一化坐标空间的数值不是可确认的变体，拒绝', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":1180, 873}}';
  assert.throws(() => parseActionPayload(payload), SyntaxError);
});

test('结构不符的无关 JSON 拒绝', () => {
  assert.throws(() => parseActionPayload('{"foo":1}'), /invalid_action/);
  assert.throws(() => parseActionPayload('{"name":"computer_use","arguments":[]}'), /invalid_action/);
});

test('非点击动作的合法 JSON 原样返回，交由统一字段校验处理', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"scroll","coordinate":[500,500],"pixels":-400}}';
  assert.deepEqual(parseActionPayload(payload).action, 'scroll');
});
