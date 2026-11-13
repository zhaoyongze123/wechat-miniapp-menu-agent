// 只修复已经确认、语义唯一的格式变体。修复结果仍由 adapter 的统一字段、类型和坐标校验处理；
// 无法确定原始意图的输出必须拒绝，不能猜测坐标后执行。
const CLICK_WITHOUT_BRACKETS = /^\s*\{\s*"name"\s*:\s*"computer_use"\s*,\s*"arguments"\s*:\s*\{\s*"action"\s*:\s*"left_click"\s*,\s*"coordinate"\s*:\s*(0|[1-9]\d{0,2})\s*,\s*(0|[1-9]\d{0,2})\s*\]?\s*\}\s*\}\s*$/;
// 百炼实测还会漏掉字段名开头的引号（如 ,menu_box":[40,230,960,970]）；键名与结构唯一，可安全补齐。
const UNQUOTED_KEY = /([{,]\s*)([A-Za-z_][A-Za-z0-9_]*)"\s*:/g;

// 实测 gui-plus 会输出两边都没有引号的裸键（{action:"terminate",...}）。
// 只在“对象键位置”补引号，并跳过字符串内容，避免误改 reason 里的中文、逗号和冒号。
function quoteBareKeys(text) {
  let output = '';
  let inString = false;
  let escaped = false;
  let expectKey = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (inString) {
      output += char;
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') {
      inString = true;
      expectKey = false;
      output += char;
      continue;
    }
    if (char === '{' || char === ',') {
      output += char;
      expectKey = true;
      continue;
    }
    if (char === ':') {
      output += char;
      expectKey = false;
      continue;
    }
    if (expectKey) {
      const match = text.slice(index).match(/^([A-Za-z_][A-Za-z0-9_]*)\s*:/);
      if (match) {
        output += `"${match[1]}":`;
        index += match[0].length - 1;
        expectKey = false;
        continue;
      }
      // 数组里的数字/布尔值不是键，遇到非空白字符就退出“等待键”状态。
      if (!/\s/.test(char)) expectKey = false;
    }
    output += char;
  }
  return output;
}
// 百炼实测会把 menu_box 输出成两个角点（"menu_box":[59,243],[940,907]）；角点顺序唯一，可展开成扁平四元组。
const MENU_BOX_AS_POINTS = /("menu_box"\s*:\s*)\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]\s*,\s*\[\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\]/;
// 实测 gui-plus 会把点击写成 {"x":70, y":34}，或坐标后多一个悬空引号；结构仍然唯一。
const XY_CLICK_AS_OBJECT = /"left_click"\s*,\s*\{\s*"?x"?\s*:\s*(\d{1,4})\s*,\s*"?y"?\s*:\s*(\d{1,4})\s*"?\s*\}/;
const STRAY_QUOTE_BEFORE_CLOSE = /([\]\d])\s*"\s*(\}\s*\})$/;
// 坐标漏方括号但后面还跟着 reason 等字段：只补坐标括号，保留其余字段，避免整条动作被丢掉。
const LEFT_CLICK_ACTION = /"action"\s*:\s*"left_click"/;
const COORDINATE_WITHOUT_BRACKETS = /"coordinate"\s*:\s*(0|[1-9]\d{0,2}|1000)\s*,\s*(0|[1-9]\d{0,2}|1000)\s*(?!,\s*\d|\]\s*)/;

export function parseActionPayload(payload) {
  let parsed;
  try {
    parsed = JSON.parse(payload);
  } catch (error) {
    // 百炼实测偶发漏掉点击坐标的方括号。只接受结构完整、恰好包含两个整数的 left_click 变体。
    const click = payload.match(CLICK_WITHOUT_BRACKETS);
    if (click) return { action: 'left_click', coordinate: [Number(click[1]), Number(click[2])] };
    const coordinateFixed = LEFT_CLICK_ACTION.test(payload)
      ? payload.replace(COORDINATE_WITHOUT_BRACKETS, '"coordinate":[$1,$2]')
      : payload;
    const objectFixed = coordinateFixed.replace(XY_CLICK_AS_OBJECT, '"left_click","coordinate":[$1,$2]}');
    const repaired = quoteBareKeys(objectFixed)
      .replace(UNQUOTED_KEY, '$1"$2":')
      .replace(STRAY_QUOTE_BEFORE_CLOSE, '$1$2')
      .replace(MENU_BOX_AS_POINTS, '$1[$2,$3,$4,$5]');
    if (repaired === payload) throw error;
    try {
      parsed = JSON.parse(repaired);
    } catch {
      const fallback = repaired.match(CLICK_WITHOUT_BRACKETS);
      if (!fallback) throw error;
      return { action: 'left_click', coordinate: [Number(fallback[1]), Number(fallback[2])] };
    }
  }
  if (!parsed || parsed.name !== 'computer_use' || !parsed.arguments || typeof parsed.arguments !== 'object' || Array.isArray(parsed.arguments)) {
    throw new Error('invalid_action');
  }
  // 实测 gui-plus 在连续点击无变化后会输出 arguments:{} 表示“先不做动作、继续观察”。
  // 空对象没有动作语义，归一化为 noop，由 adapter 转成一次短暂等待，避免整店被判格式失败。
  if (Object.keys(parsed.arguments).length === 0) return { action: 'noop' };
  return parsed.arguments;
}

const CLOSED_TOOL_CALL = /<tool_call>([\s\S]*?)<\/tool_call>/gi;
const OPEN_TOOL_CALL = /<tool_call>([\s\S]*)$/i;

// 取出开头第一个括号配平的 JSON 对象；用于裁掉动作之后多余的说明文本。
function leadingJsonObject(text) {
  let depth = 0;
  let inString = false;
  let escaped = false;
  for (let index = 0; index < text.length; index += 1) {
    const char = text[index];
    if (inString) {
      if (escaped) escaped = false;
      else if (char === '\\') escaped = true;
      else if (char === '"') inString = false;
      continue;
    }
    if (char === '"') inString = true;
    else if (char === '{') depth += 1;
    else if (char === '}') {
      depth -= 1;
      if (depth === 0) return text.slice(0, index + 1);
    }
  }
  return '';
}

// 动作对象之后只允许空白、一个围栏代码块，或一个被截断的闭合标签（实测流式输出会停在 </to）。
// 若后面还有第二个动作对象或其它解释文本，说明响应有歧义，仍按协议错误处理。
function parseWithTrailingNote(body) {
  try {
    return parseActionPayload(body);
  } catch (error) {
    const head = leadingJsonObject(body);
    if (!head) throw error;
    const rest = body.slice(head.length).trim();
    const truncatedTag = rest && '</tool_call>'.startsWith(rest);
    if (rest && !/^```[\s\S]*$/.test(rest) && !truncatedTag) throw error;
    return parseActionPayload(head);
  }
}

// 只接受“一个闭合块”，或“一个开标签 + 剩余内容能完整解析”的漏闭合变体；
// 两个以上块或多出无法解释的文本一律拒绝，避免把多次动作合并成一个。
export function extractActionPayload(rawContent) {
  const raw = typeof rawContent === 'string' ? rawContent : '';
  const tags = [...raw.matchAll(CLOSED_TOOL_CALL)];
  if (tags.length === 1) return parseWithTrailingNote(tags[0][1]);
  if (tags.length === 0) {
    const open = raw.match(OPEN_TOOL_CALL);
    if (open) {
      try {
        return parseWithTrailingNote(open[1].trim());
      } catch { /* 漏闭合且正文不可解析，按原协议错误处理 */ }
    }
  }
  throw new Error('invalid_action');
}
