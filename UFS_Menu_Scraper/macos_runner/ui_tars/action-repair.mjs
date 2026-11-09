// 只修复已经确认、语义唯一的格式变体。修复结果仍由 adapter 的统一字段、类型和坐标校验处理；
// 无法确定原始意图的输出必须拒绝，不能猜测坐标后执行。
const CLICK_WITHOUT_BRACKETS = /^\s*\{\s*"name"\s*:\s*"computer_use"\s*,\s*"arguments"\s*:\s*\{\s*"action"\s*:\s*"left_click"\s*,\s*"coordinate"\s*:\s*(0|[1-9]\d{0,2})\s*,\s*(0|[1-9]\d{0,2})\s*\]?\s*\}\s*\}\s*$/;

export function parseActionPayload(payload) {
  let parsed;
  try {
    parsed = JSON.parse(payload);
  } catch (error) {
    // 百炼实测偶发漏掉点击坐标的方括号。只接受结构完整、恰好包含两个整数的 left_click 变体。
    const click = payload.match(CLICK_WITHOUT_BRACKETS);
    if (!click) throw error;
    return { action: 'left_click', coordinate: [Number(click[1]), Number(click[2])] };
  }
  if (!parsed || parsed.name !== 'computer_use' || !parsed.arguments || typeof parsed.arguments !== 'object' || Array.isArray(parsed.arguments)) {
    throw new Error('invalid_action');
  }
  return parsed.arguments;
}
