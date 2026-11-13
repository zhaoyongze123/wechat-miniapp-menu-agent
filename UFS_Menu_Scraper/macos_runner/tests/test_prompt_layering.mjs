import test from 'node:test';
import assert from 'node:assert/strict';

import { PROMPT_VERSION, buildBasePrompt } from '../ui_tars/prompts/base.mjs';
import { BUSINESS_PROMPT, BUSINESS_PROMPT_VERSION } from '../ui_tars/prompts/business.mjs';

test('稳定段只含协议与动作表，同一输入逐字一致', () => {
  const contract = 'left_click：必填 coordinate=[x,y]；不得有其他字段。';
  const first = buildBasePrompt({ outputContract: contract });
  const second = buildBasePrompt({ outputContract: contract });
  assert.equal(first, second);
  assert.match(first, /tool_call/);
  assert.match(first, /left_click：必填/);
  assert.doesNotMatch(first, /点餐小程序导航经验/);
  assert.ok(Number.isInteger(PROMPT_VERSION));
});

test('业务段单独维护并带版本号', () => {
  assert.match(BUSINESS_PROMPT, /点餐小程序导航经验/);
  assert.match(BUSINESS_PROMPT, /死规则一/);
  assert.ok(Number.isInteger(BUSINESS_PROMPT_VERSION));
});
