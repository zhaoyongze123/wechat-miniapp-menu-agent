import test from 'node:test';
import assert from 'node:assert/strict';

import { extractActionPayload, parseActionPayload, resetRepairFlag, wasRepaired } from '../ui_tars/action-repair.mjs';
import { fillMissingTextFields } from '../ui_tars/action-spec.mjs';

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

test('漏左括号且后面还有 reason 时也能修复', () => {
  resetRepairFlag();
  const payload = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":639, 973],"reason":"点击同意"}}';
  assert.deepEqual(parseActionPayload(payload),
    { action: 'left_click', coordinate: [639, 973], reason: '点击同意' });
  assert.equal(wasRepaired(), true);
});

test('原始输出合法时不标记修复', () => {
  resetRepairFlag();
  parseActionPayload('{"name":"computer_use","arguments":{"action":"left_click","coordinate":[639,973]}}');
  assert.equal(wasRepaired(), false);
});

test('坐标多于两个时仍拒绝', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"left_click","coordinate":118, 873, 12],"reason":"x"}}';
  assert.throws(() => parseActionPayload(payload), SyntaxError);
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

test('修复 menu_box 写成裸四元组的 terminate', () => {
  const payload = '{"name":"computer_use","arguments":{"action":"terminate","outcome":"success","reason":"已进入菜单","menu_box":59,243,940,907}}';
  assert.deepEqual(parseActionPayload(payload), {
    action: 'terminate', outcome: 'success', reason: '已进入菜单', menu_box: [59, 243, 940, 907],
  });
});

test('裸键与已带引号的字段混用时分别修复', () => {
  const payload = '{"name":"computer_use","arguments":{action:"terminate",outcome:"success",reason:"已进入菜单","menu_box":[59,243],[940,907]}}';
  assert.deepEqual(parseActionPayload(payload), {
    action: 'terminate', outcome: 'success', reason: '已进入菜单', menu_box: [59, 243, 940, 907],
  });
});

test('空 arguments 不做归一化，交给 adapter 按格式错误要求补齐 action', () => {
  assert.deepEqual(parseActionPayload('{"name":"computer_use","arguments":{}}'), {});
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

// 以下四条来自 2026-11-17/18 正式批次的实际被拒原文。
test('完整对象后多一个右括号或引号时按完整对象解析', () => {
  const raw = '<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":[497, 817],'
    + '"target_name":"开始点餐","reason":"点击开始点餐按钮"}}"';
  assert.deepEqual(extractActionPayload(raw), {
    action: 'left_click', coordinate: [497, 817], target_name: '开始点餐', reason: '点击开始点餐按钮',
  });
  const raw2 = '<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":[893, 287],'
    + '"target_name":"关闭按钮","reason":"关闭弹窗"}}';
  assert.deepEqual(extractActionPayload(raw2).coordinate, [893, 287]);
});

test('键名缺少开头引号时补齐并继续修复坐标', () => {
  const raw = '<tool_call>{"name":"computer_use","arguments":{action":"left_click", "coordinate":87, 865],'
    + '"target_name":"同意所有政策复选框","reason":"点击复选框"}}';
  assert.deepEqual(extractActionPayload(raw), {
    action: 'left_click', coordinate: [87, 865], target_name: '同意所有政策复选框', reason: '点击复选框',
  });
});

test('字符串值漏掉引号时只在 reason/target_name 且以中文开头时补齐', () => {
  const raw = '<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":312,180,'
    + '"target_name":搜索结果列表中第一个门店条目,"reason":点击该门店进入菜单}}';
  assert.deepEqual(extractActionPayload(raw), {
    action: 'left_click',
    coordinate: [312, 180],
    target_name: '搜索结果列表中第一个门店条目',
    reason: '点击该门店进入菜单',
  });
  // 英文枚举/JSON 字面量不能被加引号，否则会把真实类型错误掩盖成字符串。
  // null 本身是合法 JSON，解析层原样返回，由 schema 校验决定；这里只确认没有被改写。
  assert.deepEqual(parseActionPayload('{"name":"computer_use","arguments":{"action":"wait","time":1,"reason":null}}'),
    { action: 'wait', time: 1, reason: null });
});

test('最外层少一个结尾大括号且不在字符串中间时补齐', () => {
  const raw = '<tool_call>{"name":"computer_use","arguments":{"action":"left_click","coordinate":443, 339,'
    + '"target_name":"页面上方区域","reason":"点击页面上方区域"}';
  assert.deepEqual(extractActionPayload(raw).coordinate, [443, 339]);
  const raw2 = '<tool_call>{"name":"computer_use","arguments":{"action":"wait","time":1.5,"reason":"等待页面加载"}}}';
  assert.deepEqual(extractActionPayload(raw2), { action: 'wait', time: 1.5, reason: '等待页面加载' });
});

test('截断在字符串中间时仍拒绝，不补大括号', () => {
  const raw = '<tool_call>{"name":"computer_use","arguments":{"action":"terminate","outcome":"needs_review","reason":"页面一直加载';
  assert.throws(() => extractActionPayload(raw), /invalid_action/);
});

test('留痕字段缺失时补占位符，但不改 terminate', () => {
  assert.deepEqual(
    fillMissingTextFields({ action: 'left_click', coordinate: [1, 2], reason: null, target_name: null }),
    { action: 'left_click', coordinate: [1, 2], reason: '未提供', target_name: '未提供' },
  );
  assert.deepEqual(
    fillMissingTextFields({ action: 'wait', time: 1 }),
    { action: 'wait', time: 1, reason: '未提供' },
  );
  const terminal = { action: 'terminate', outcome: 'needs_review', reason: null };
  assert.equal(fillMissingTextFields(terminal), terminal);
});
