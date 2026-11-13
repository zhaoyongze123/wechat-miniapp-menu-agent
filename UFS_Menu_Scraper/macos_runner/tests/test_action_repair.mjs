import test from 'node:test';
import assert from 'node:assert/strict';

import { extractActionPayload, parseActionPayload } from '../ui_tars/action-repair.mjs';

const LEFT_CLICK = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":[118,873]}}';

test('合法 JSON 原样返回 arguments', () => {
  assert.deepEqual(parseActionPayload(LEFT_CLICK), { action: 'left_click', coordinate: [118, 873] });
});

test('修复百炼漏掉坐标两侧方括号的 left_click', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":118, 873}}';
  assert.deepEqual(parseActionPayload(payload), { action: 'left_click', coordinate: [118, 873] });
});

test('修复漏掉坐标方括号的 left_click，并保留 reason', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":315, 950,'
    + '"reason":"点击目标按钮"}}';
  assert.deepEqual(parseActionPayload(payload), {
    action: 'left_click', coordinate: [315, 950], reason: '点击目标按钮',
  });
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

test('修复 menu_box 输出成两个角点的 terminate', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"terminate","outcome":"success","reason":"已进入菜单","menu_box":[59,243],[940,907]}}';
  assert.deepEqual(parseActionPayload(payload), {
    action: 'terminate', outcome: 'success', reason: '已进入菜单', menu_box: [59, 243, 940, 907],
  });
});

test('修复两边都没有引号的裸键 terminate，且不改动 reason 里的逗号和冒号', () => {
  const payload = '{"name":"computer_use","arguments":{action:"terminate",outcome:"needs_review",reason:"页面停留, 无入口: 无法继续"}}';
  assert.deepEqual(parseActionPayload(payload), {
    action: 'terminate', outcome: 'needs_review', reason: '页面停留, 无入口: 无法继续',
  });
});

test('裸键与已带引号的字段混用时分别修复', () => {
  const payload = '{"name":"computer_use","arguments":{action:"terminate",outcome:"success",reason:"已进入菜单","menu_box":[59,243],[940,907]}}';
  assert.deepEqual(parseActionPayload(payload), {
    action: 'terminate', outcome: 'success', reason: '已进入菜单', menu_box: [59, 243, 940, 907],
  });
});

test('空 arguments 归一化为 noop，表示继续观察而不是格式失败', () => {
  assert.deepEqual(parseActionPayload('{"name":"computer_use","arguments":{}}'), { action: 'noop' });
});

test('闭合标签被流式截断（</to）时，完整动作对象仍可解析', () => {
  const raw = '<tool_call>{"name":"computer_use","arguments":{"action":"terminate","outcome":"needs_review","reason":"订单详情页，不是菜单"}}</to';
  assert.deepEqual(extractActionPayload(raw), {
    action: 'terminate', outcome: 'needs_review', reason: '订单详情页，不是菜单',
  });
});

test('点击写成 {"x":70,y":34} 的畸形对象时补成 coordinate', () => {
  const raw = '<tool_call>{"name":"computer_use","arguments":{"action":"left_click",{"x":70, y":34 "}}';
  assert.deepEqual(extractActionPayload(raw), { action: 'left_click', coordinate: [70, 34] });
});

test('坐标后多一个悬空引号时补成 coordinate', () => {
  const raw = '<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":70,44 "}}';
  assert.deepEqual(extractActionPayload(raw), { action: 'left_click', coordinate: [70, 44] });
  const withArray = '<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":[70,44] "}}';
  assert.deepEqual(extractActionPayload(withArray), { action: 'left_click', coordinate: [70, 44] });
});

test('结构不符的无关 JSON 拒绝', () => {
  assert.throws(() => parseActionPayload('{"foo":1}'), /invalid_action/);
  assert.throws(() => parseActionPayload('{"name":"computer_use","arguments":[]}'), /invalid_action/);
});

test('非点击动作的合法 JSON 原样返回，交由统一字段校验处理', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"scroll","coordinate":[500,500],"pixels":-400}}';
  assert.deepEqual(parseActionPayload(payload).action, 'scroll');
});

test('漏掉结尾闭合标签但正文完整时接受', () => {
  const raw = '<tool_call>{"name":"computer_use","arguments":{"action":"terminate","outcome":"needs_review","reason":"页面报错，无法进入菜单"}}\r\n';
  assert.deepEqual(extractActionPayload(raw), {
    action: 'terminate', outcome: 'needs_review', reason: '页面报错，无法进入菜单',
  });
});

test('漏掉闭合标签且正文不可解析时拒绝', () => {
  assert.throws(() => extractActionPayload('<tool_call>{"name":"computer_use","arguments":{"action":'), /invalid_action/);
});

test('两个闭合块时拒绝，避免把多次动作合并成一个', () => {
  const raw = '<tool_call>{"name":"computer_use","arguments":{"action":"wait","time":1}}</tool_call>'
    + '<tool_call>{"name":"computer_use","arguments":{"action":"wait","time":2}}</tool_call>';
  assert.throws(() => extractActionPayload(raw), /invalid_action/);
});

test('动作后跟围栏 json 说明块时接受（实测重复输出 menu_box）', () => {
  const raw = '<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":[500,500]}}\r\n\r\n```json\n{"menu_box":[40,230,960,970]}\n```';
  assert.deepEqual(extractActionPayload(raw), { action: 'left_click', coordinate: [500, 500] });
});

test('动作后跟普通解释文本或第二个动作对象时仍拒绝', () => {
  const action = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":[500,500]}}';
  assert.throws(() => extractActionPayload(`<tool_call>${action}\n我觉得应该点这里`), /invalid_action/);
  assert.throws(() => extractActionPayload(`<tool_call>${action}\n${action}`), /invalid_action/);
});
