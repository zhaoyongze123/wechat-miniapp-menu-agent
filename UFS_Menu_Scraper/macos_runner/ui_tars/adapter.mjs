import { createInterface } from 'node:readline';
import { appendFileSync, readFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { parseActionPayload } from './action-repair.mjs';
import { NoProgressTracker } from './progress-tracker.mjs';

// SDK 日志可能包含提示词、模型返回值或请求信息。协议输出使用启动时保存的原始写口，SDK 日志静默丢弃。
const protocolWrite = process.stdout.write.bind(process.stdout);
process.stdout.write = () => true;
globalThis.console = Object.freeze({ log() {}, info() {}, warn() {}, error() {}, debug() {} });

const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
const lines = input[Symbol.asyncIterator]();
let nextId = 1;
let currentFrame = '';
let screenWidth = 0;
let screenHeight = 0;
let actionUsedOnFrame = false;
let terminal = null;
let faulted = false;
// 连续格式错误与整个任务的格式错误分开计数：连续合法输出后允许重新获得修正机会。
const CONSECUTIVE_PARSE_FAILURE_LIMIT = 2;
const TOTAL_PARSE_FAILURE_LIMIT = 4;
const REASON_MAX_CHARS = 500;
let parseFailures = 0;
let parseFailuresTotal = 0;
let resultEmitted = false;
let failure = null;
let lastRawContent = '';
let pendingNotice = '';
let tracePath = '';
let callSeq = 0;
let currentCall = null;
let actionCount = 0;
let formatRetries = 0;
let toolRejections = 0;
let screenshotCount = 0;
let currentFrameHash = '';
let noProgressNotices = 0;
const progress = new NoProgressTracker();
const frameHashes = new Map();
// 动作名称、必填/选填字段与基础类型只有这一份定义，Python 侧从同一文件生成输出契约。
const actionSchema = JSON.parse(readFileSync(new URL('./action-schema.json', import.meta.url), 'utf8'));
const terminalMarker = 'UFS_UI_TARS_TERMINAL_COMPLETE';
let activeAgent = null;
const { GUIAgent } = await import('@gui-agent/agent-sdk');
const { Operator } = await import('@gui-agent/shared/base');

function send(message) {
  protocolWrite(`${JSON.stringify(message)}\n`);
}

function now() {
  return new Date().toISOString();
}

function trace(record) {
  if (!tracePath) return;
  try {
    appendFileSync(tracePath, `${JSON.stringify(record)}\n`, { encoding: 'utf8', mode: 0o600 });
  } catch { /* 留痕失败不能影响导航与交接协议 */ }
}

function redacted(value) {
  return value.replace(/sk-[A-Za-z0-9._-]{12,}/g, '[REDACTED]');
}

function sha256(buffer) {
  return createHash('sha256').update(buffer).digest('hex');
}

function rawRecord(value, limit = 262144) {
  // 留痕要能复盘协议问题：原文完整保留，超出上限才截断并显式标注。
  const raw = typeof value === 'string' ? value : '';
  return { raw: raw.slice(0, limit), raw_bytes: Buffer.byteLength(raw), raw_truncated: raw.length > limit };
}

function imageRef(url) {
  let bytes = Buffer.alloc(0);
  try {
    bytes = Buffer.from(url.slice(url.indexOf(',') + 1), 'base64');
  } catch { /* 非法图片数据只保留空引用 */ }
  const hash = sha256(bytes);
  return { type: 'image_ref', frame_id: frameHashes.get(hash) || '', bytes: bytes.length, sha256: hash };
}

function sanitized(value) {
  if (Array.isArray(value)) return value.map(sanitized);
  if (value && typeof value === 'object') {
    if (value.type === 'image_url' && typeof value.image_url?.url === 'string'
        && value.image_url.url.startsWith('data:image/')) {
      return { type: 'image_ref', detail: value.image_url.detail || '', ...imageRef(value.image_url.url) };
    }
    const result = {};
    for (const [key, nested] of Object.entries(value)) {
      if (key === 'apiKey' || key === 'api_key') continue;
      result[key] = sanitized(nested);
    }
    return result;
  }
  if (typeof value === 'string') return redacted(value);
  return value;
}

function pngInfo(base64) {
  const bytes = Buffer.from(base64, 'base64');
  const signature = Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]);
  if (bytes.length < 24 || !bytes.subarray(0, 8).equals(signature)) throw new Error('protocol');
  const width = bytes.readUInt32BE(16);
  const height = bytes.readUInt32BE(20);
  if (!width || !height) throw new Error('protocol');
  return { bytes, width, height };
}

async function readObject() {
  const line = await lines.next();
  if (line.done || typeof line.value !== 'string' || !line.value.trim()) throw new Error('protocol');
  try {
    const value = JSON.parse(line.value);
    if (!value || typeof value !== 'object' || Array.isArray(value)) throw new Error('protocol');
    return value;
  } catch {
    throw new Error('protocol');
  }
}

class HostRejected extends Error {}

async function rpc(method, params, recoverable = false) {
  const id = String(nextId++);
  send({ type: 'request', id, method, params });
  const reply = await readObject();
  if (reply.id !== id || typeof reply.ok !== 'boolean') {
    faulted = true;
    throw new Error('protocol');
  }
  if (!reply.ok) {
    // 可修正的动作被宿主拒绝时，把错误交回 Agent 重新决策，而不是结束整轮会话。
    if (recoverable) return { __rejected: true, error: text(reply.error, 300) || '宿主拒绝执行' };
    faulted = true;
    throw new Error('host_rejected');
  }
  return reply.result;
}

async function callHost(method, params) {
  const result = await rpc(method, params, true);
  if (result?.__rejected) throw new HostRejected(result.error);
  return result;
}

function text(value, max = 1000) {
  return typeof value === 'string' ? value.trim().slice(0, max) : '';
}

function extractAction(raw) {
  const tags = [...raw.matchAll(/<tool_call>([\s\S]*?)<\/tool_call>/gi)];
  if (tags.length !== 1) throw new Error('invalid_action');
  return parseActionPayload(tags[0][1]);
}

function stopAgent() {
  try { activeAgent?.abort(); } catch { /* fail closed; host owns process deadlines */ }
}

const NORMALIZED_SCREEN_SIZE = Number(actionSchema.coordinate_space) || 1000;

function matchesRule(rule, value) {
  switch (rule?.type) {
    case 'point':
      return Array.isArray(value) && value.length === 2 && value.every((n) => Number.isFinite(n));
    case 'box':
      return Array.isArray(value)
        && ((value.length === 4 && value.every((n) => Number.isFinite(n)))
          || (rule.allow_empty === true && value.length === 0));
    case 'number':
      if (!Number.isFinite(value)) return false;
      if (rule.non_zero && value === 0) return false;
      if (Number.isFinite(rule.min) && value < rule.min) return false;
      if (Number.isFinite(rule.exclusive_min) && value <= rule.exclusive_min) return false;
      if (Number.isFinite(rule.max) && value > rule.max) return false;
      return true;
    case 'string':
      return typeof value === 'string' && value.trim().length > 0;
    case 'enum':
      return Array.isArray(rule.values) && rule.values.includes(value);
    default:
      return false;
  }
}

// 带字段名的格式错误：反馈里明确指出缺哪个字段，但不替 Agent 判断页面。
class ShapeError extends Error {
  constructor(code, field, reason) {
    super(code);
    this.field = field;
    this.reason = reason;
  }
}

// 只校验字段白名单、必填项和基础类型；菜单矩形等几何关系仍由专用函数判断。
function checkShape(name, args, code = 'invalid_action') {
  const spec = actionSchema.actions?.[name];
  if (!spec) throw new ShapeError(code, 'action', 'value');
  const required = spec.required || {};
  const optional = spec.optional || {};
  const allowed = new Set(['action', ...Object.keys(required), ...Object.keys(optional)]);
  for (const key of Object.keys(args)) {
    if (!allowed.has(key)) throw new ShapeError(code, key, 'unexpected');
  }
  for (const [field, rule] of Object.entries(required)) {
    if (args[field] === undefined) throw new ShapeError(code, field, 'missing');
    if (!matchesRule(rule, args[field])) throw new ShapeError(code, field, 'value');
  }
  for (const [field, rule] of Object.entries(optional)) {
    if (args[field] !== undefined && !matchesRule(rule, args[field])) throw new ShapeError(code, field, 'value');
  }
}

function normalizedPoint(value) {
  if (!Array.isArray(value) || value.length !== 2 || value.some((n) => !Number.isFinite(n))) throw new ShapeError('invalid_action', 'coordinate', 'value');
  const [x, y] = value;
  if (x < 0 || y < 0 || x >= NORMALIZED_SCREEN_SIZE || y >= NORMALIZED_SCREEN_SIZE) throw new ShapeError('invalid_action', 'coordinate', 'value');
  return { x: x * screenWidth / NORMALIZED_SCREEN_SIZE, y: y * screenHeight / NORMALIZED_SCREEN_SIZE };
}

// 模型偶尔把截图像素当归一化坐标，允许真实 ±5% 越界（-50..1050）后钳制到 0..1000；
// 超出这个范围说明语义已不可确认，交给 Agent 重新输出。原始值只留在 trace 里复盘。
const MENU_BOX_TOLERANCE = 0.05;

function menuRegion(value) {
  if (!Array.isArray(value) || value.length !== 4 || value.some((n) => !Number.isFinite(n))) throw new Error('invalid_terminal');
  const lower = -NORMALIZED_SCREEN_SIZE * MENU_BOX_TOLERANCE;
  const upper = NORMALIZED_SCREEN_SIZE * (1 + MENU_BOX_TOLERANCE);
  const normalized = value.map((n) => {
    if (n < lower || n > upper) throw new Error('invalid_terminal');
    return Math.round(Math.min(NORMALIZED_SCREEN_SIZE, Math.max(0, n)));
  });
  const [x1, y1, x2, y2] = normalized;
  if (x2 <= x1 || y2 <= y1) throw new Error('invalid_terminal');
  const left = x1 * screenWidth / NORMALIZED_SCREEN_SIZE;
  const top = y1 * screenHeight / NORMALIZED_SCREEN_SIZE;
  const right = x2 * screenWidth / NORMALIZED_SCREEN_SIZE;
  const bottom = y2 * screenHeight / NORMALIZED_SCREEN_SIZE;
  return {
    region: { x: left, y: top, width: right - left, height: bottom - top },
    meta: {
      original_menu_box: value.slice(),
      normalized_menu_box: normalized,
      clamped: value.some((n) => n < 0 || n > NORMALIZED_SCREEN_SIZE),
    },
  };
}

function normalizedArguments(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return value;
  const args = { ...value };
  if (typeof args.action === 'string') {
    // 实测模型会把动作写成函数调用形式（如 list_windows()），只去掉空括号后缀。
    args.action = args.action.trim().replace(/\(\s*\)$/, '');
  } else if (['success', 'unqualified', 'needs_review'].includes(args.outcome) && text(args.reason)) {
    // 模型偶发漏掉 action，但终态字段完整；按 terminate 解析，其余字段仍走严格校验。
    args.action = 'terminate';
  }
  return args;
}

function parseModelAction(rawArgs) {
  const args = normalizedArguments(rawArgs);
  if (!args || typeof args !== 'object' || Array.isArray(args) || typeof args.action !== 'string') throw new Error('invalid_action');
  switch (args.action) {
    case 'left_click': {
      checkShape('left_click', args);
      const point = normalizedPoint(args.coordinate);
      return { type: 'click', inputs: { point: { raw: point, referenceSystem: 'screen' } } };
    }
    case 'scroll': {
      checkShape('scroll', args);
      const point = args.coordinate === undefined
        ? { x: Math.floor(screenWidth / 2), y: Math.floor(screenHeight / 2) }
        : normalizedPoint(args.coordinate);
      // 同时保留方向和有符号幅度：幅度由 Python 换算成有界滚轮格数，避免 -100 与 -800 执行结果相同。
      return { type: 'scroll', inputs: { point: { raw: point, referenceSystem: 'screen' }, direction: args.pixels > 0 ? 'up' : 'down', pixels: args.pixels } };
    }
    case 'wait': {
      checkShape('wait', args);
      return { type: 'wait', inputs: { time: args.time } };
    }
    case 'list_windows': {
      checkShape('list_windows', args);
      return { type: 'windows', inputs: {} };
    }
    case 'switch_window': {
      checkShape('switch_window', args);
      return { type: 'switch_window', inputs: { title: text(args.title) } };
    }
    case 'terminate': {
      // 业务只收一个状态、一条理由；success 额外要求菜品区域。字段白名单之外一律视为格式违规。
      checkShape('terminate', args, 'invalid_terminal');
      const next = { kind: 'terminate', outcome: args.outcome, reason: text(args.reason, REASON_MAX_CHARS) };
      if (args.outcome === 'success') {
        const menu = menuRegion(args.menu_box);
        next.menu_region = menu.region;
        // 只进 trace，不作为模型字段，也不改变终态 payload。
        next.menu_meta = menu.meta;
      } else if (args.menu_box !== undefined && !(Array.isArray(args.menu_box) && args.menu_box.length === 0)) {
        // 非 success 带空数组是模板残留，视同未提供；带真实区域仍判非法。
        throw new Error('invalid_terminal');
      }
      terminal = next;
      return { type: 'finished', inputs: { content: terminalMarker } };
    }
    default:
      throw new Error('disallowed_action');
  }
}

function reject(code, rawContent) {
  failure = { code, raw: text(rawContent, 1200) };
  trace({ type: 'rejected', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame,
          code, ...rawRecord(rawContent) });
  // Python 侧把 stderr 落到本店 adapter.log；这里保留原始输出，便于逐条区分是格式还是参数问题。
  try {
    process.stderr.write(`${JSON.stringify({ code, raw: failure.raw })}\n`);
  } catch { /* stderr may be closed during shutdown */ }
  faulted = true;
  stopAgent();
  throw new Error(code);
}

function describeFormatError(code, args) {
  if (code === 'invalid_terminal') {
    if (args?.outcome === 'success' && args?.menu_box === undefined) {
      return 'success 缺少 menu_box，需要 4 个 0..1000 的数字 [x1,y1,x2,y2]，且 x1<x2、y1<y2';
    }
    if (args?.outcome && args.outcome !== 'success' && args?.menu_box !== undefined) {
      return '非 success 终态不要提供 menu_box';
    }
    return '终态字段不完整，或 menu_box 越界/无法形成有效矩形';
  }
  if (code === 'disallowed_action') {
    return args?.action ? `不支持的动作「${text(args.action, 40)}」` : '不支持的动作';
  }
  if (code === 'no_observation') return '当前没有可用截图';
  return '这不是一个合法的 computer_use JSON，或字段缺失、有多余字段、类型不对';
}

function makeCustomActionParser() {
  return (rawContent) => {
    lastRawContent = typeof rawContent === 'string' ? rawContent : '';
    if (faulted) throw new Error(failure?.code || 'invalid_action');
    let args = null;
    try {
      if (!currentFrame || !screenWidth || !screenHeight) throw new Error('no_observation');
      args = extractAction(rawContent);
      const parsed = parseModelAction(args);
      // 合法输出后重置连续错误计数，让 Agent 在任务后段仍有修正机会；总上限仍保留。
      parseFailures = 0;
      trace({ type: 'action', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame,
              ok: true, action: parsed, ...rawRecord(rawContent) });
      return { rawContent, reasoningContent: '', actions: [parsed] };
    } catch (error) {
      const code = ['invalid_action', 'invalid_terminal', 'disallowed_action', 'no_observation'].includes(error?.message)
        ? error.message
        : 'invalid_action';
      trace({ type: 'action', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame,
              ok: false, error: code, ...rawRecord(rawContent) });
      const formatError = code === 'invalid_action' || code === 'invalid_terminal';
      if (formatError) {
        // 先记发生次数：即使这次超出修正上限直接终止，也会计入 format_failures。
        parseFailures += 1;
        parseFailuresTotal += 1;
      }
      if (formatError && parseFailures <= CONSECUTIVE_PARSE_FAILURE_LIMIT
          && parseFailuresTotal <= TOTAL_PARSE_FAILURE_LIMIT) {
        // 错误只解释格式，不替 Agent 判断页面；连续和总计各有上限，避免无限重试。
        formatRetries += 1;
        const detail = error instanceof ShapeError
          ? (error.reason === 'missing' ? `缺少必填字段 ${error.field}`
            : error.reason === 'unexpected' ? `出现了协议未定义的字段 ${error.field}`
              : `字段 ${error.field} 的类型或取值范围不对`)
          : describeFormatError(code, args);
        pendingNotice = `上一次输出不符合工具协议（${code}）：${detail}。`
          + '本次未执行。请只输出一个 computer_use tool_call，字段名、类型和取值范围必须完全符合约定，'
          + '不要添加任何未列出的字段。';
        pendingNotice = pendingNotice.slice(0, 300);
        return { rawContent, reasoningContent: '', actions: [{ type: 'notice', inputs: { message: pendingNotice } }] };
      }
      return reject(code, rawContent);
    }
  };
}

// 点餐小程序导航经验只维护这一份，作为系统提示词常驻；不再提供按需读取的 Skill。
const navigationGuide = `点餐小程序导航经验

目标是浏览本轮门店的菜单并交接。根据截图自主选择动作。浏览期间避免登录、入会、储值、加购、下单、支付或发起外部联系。

先确认扫码进来的是不是点餐码
- 如果打开的是大众点评/美团团购、外卖平台、优惠券或团购详情、门店介绍、公众号文章、支付或开票等非点餐页面，说明这个二维码不是本店点餐码。
- 直接 terminate，outcome=unqualified，reason 写明实际看到的平台与页面，例如“这是大众点评的团购界面，不是点餐码。”；不要在这些页面里继续浏览、登录或下单。
- 如果多次尝试（换入口、返回、切窗口）后仍进不到点餐菜单，也判 unqualified，reason 说明尝试过哪些入口、最后停在什么页面，例如“尝试 5 次后仍停在团购详情页，未进入点餐菜单，二维码可能不是点餐码”。
- 只有白屏、加载慢、网络异常这类“暂时没成功”，才用 needs_review。

一、三个终态怎么分

success：已经出现可直接截图和滚动浏览的真实菜品菜单，通常能看到对应价格，且 \`menu_box\` 能框住当前可见的菜品列表。滚动采集由程序完成。
可以判 success：
- 当前截图已经出现可滚动浏览的菜品名和价格列表时，立即判 success 并给出 \`menu_box\`，不必再滚动或点其他入口确认。
- 一两道菜也算，不要求菜品很多。
- 截图里没有显示店名时，如果开窗和导航过程能对应本轮门店，仍可交接。
- 页面显示的是其他分店时，只要已经出现可截图、可滚动的真实菜品菜单，就照常交接。
- 门店打烊、暂停接单，但菜单仍能浏览，也算 success。
- 顶部横幅、分类导航、底部购物车或角落提示未挡住菜品列表和滚动时，可以交接。
- 覆盖在菜品列表上的广告、会员、储值或活动浮层必须先关闭再交接。
不要判 success：
- 只有欢迎页、“立即点餐”按钮、门店介绍、广告。
- 只有分类名或搜索框，还没有菜品和价格。
- 购物车、订单确认、订单详情、历史订单、已下单成功页、支付页、开票页。
- 页面正在加载或白屏。
- 模态弹窗盖住菜品区域或拦住列表滚动。
- 人数选择页（“选择人数”“开始点餐”“欢迎来到…”）不是菜单页；应先选中人数并进入点餐，再进行判定。

unqualified：要有清楚、可见的证据表明本轮拿不到菜单，而不是“暂时没成功”。
常见证据：打开的是大众点评、美团团购等非点餐平台页面（二维码不是点餐码，reason 写明平台与页面）；多次尝试后仍进不到点餐菜单；明确提示扫码点餐已关闭且无菜单入口；要求到店后再扫码；二维码失效、小程序停止服务或门店未配置菜单；明确写暂无菜单/暂无菜品且重试后仍一样。
不算 unqualified：打烊但菜单可见→success；提示暂时无法下单但有“查看菜单/进入点餐”入口→继续；白屏、加载慢、一般网络异常→有限恢复或观察等待，持续失败返回 needs_review；点击没反应、弹窗关不掉、需要登录、页面归属不明→needs_review；菜品很少、店名不全→不影响 success。

needs_review：合理尝试后仍无法确定——强制手机号+验证码；持续白屏或一直加载；打开的页面与本轮二维码关系不明；工具连续失败或窗口反复被拒绝；弹窗无法消除；停在人数选择、欢迎页等前置页且找不到进入菜单的入口。

二、常见页面怎么处理

- 欢迎页或广告页优先找能直接通向菜品菜单的入口；同时有点餐、会员中心、储值、外卖或商城时，结合按钮与内容选择菜单路径。误入无关页面后优先退出、返回或切回原窗口。
- 全屏广告、会员礼遇、储值海报等有叉号、关闭、跳过入口时先关闭再观察；不要未关闭遮挡就反复点底部导航。
- 门店首页是海报加人数选择时，先按任务人数选中对应数字，确认选中状态，再点底部“开始点餐/去点餐”一次；没有页面变化时先核对按钮位置和选中状态，再决定是否重试，不要连续点海报或空白区域。
- 扫码后直接进入已有订单、订单详情或当前餐桌的点餐记录时，可用返回、关闭、左上角返回箭头离开，或切换底部“菜单/点餐/首页”导航，或找“继续点餐/加菜/再来一单”入口进入菜品列表；全程只浏览，不勾选菜品、不修改订单、不提交、不支付；都无法回到可浏览菜单时返回 needs_review。
- 订单详情、历史订单或下单成功页中的菜名与价格，不是菜单成功交接的充分证据；必须真正回到可滚动的菜品列表才判 success。
- 遇到隐私或授权弹窗时，优先使用可跳过、关闭或拒绝的入口；若只能通过手机号、登录或未授权的个人信息请求，返回 needs_review。
- 系统通知弹窗优先拒绝或关闭。
- 等待应有可见依据，例如加载提示、转场或刚触发的页面变化。等待后重新观察。有稳定的非目标页面时，转向返回或其他入口。
- 点击没反应时，对照动作前后的截图，看按钮是否可用、有没有前置条件、点击是否落在目标上。只有新证据支持时才重试同一位置。
- 点击点餐后，如果原窗口停在加载页、浏览器跳转页或没有明显变化，先用 \`list_windows\` 核对是否另开了微信小程序，再决定是否待检查；窗口标题只是线索，可用 \`switch_window\` 查看合理候选的新截图。

三、操作没有进展时

- 每次操作后先对照新截图，确认页面是否出现预期变化；工具执行成功不代表页面已经响应。
- 没有变化时，先检查是否仍在加载、按钮是否被遮挡、是否缺少前置步骤。只有发现新的依据，才重新点击原目标。
- 同一目标连续两次点击都没有响应时，放弃当前点击方式：可改点按钮中心或其他有效位置，也可尝试其他菜单入口、返回路径或窗口切换。不要反复点击同一区域直到耗尽步数。
- 如果执行反馈指出画面没有变化，把它作为判断依据，而不是必须终止的信号；延迟生效或不可见的变化仍可继续观察。
- 多个合理入口都没有进展时，停止机械重试，说明已经尝试的方式和当前阻碍，返回 needs_review。

四、交接时的注意事项

- \`menu_box\` 只框菜品列表区域，不要包含顶部状态栏、底部购物车栏；不要用接近整窗的框代替菜品区域。
- 只框当前截图中可见的部分，不推测滚动后的区域。
- 理由写实际观察到的内容，例如“已进入菜单，可见菜品与价格”，不要写推测或截图中不存在的内容。`;

class HostOperator extends Operator {
  async initialize() {}

  supportedActions() {
    return ['click', 'scroll', 'wait', 'windows', 'switch_window', 'finished', 'notice'];
  }

  screenContext() {
    return { screenWidth, screenHeight, scaleX: 1, scaleY: 1 };
  }

  async screenshot() {
    const result = await rpc('screenshot', {});
    if (!result || typeof result.base64 !== 'string' || typeof result.frame_id !== 'string' || !result.frame_id) throw new Error('protocol');
    const info = pngInfo(result.base64);
    ({ width: screenWidth, height: screenHeight } = info);
    currentFrame = result.frame_id;
    actionUsedOnFrame = false;
    screenshotCount += 1;
    const hash = sha256(info.bytes);
    currentFrameHash = hash;
    frameHashes.set(hash, currentFrame);
    if (frameHashes.size > 64) frameHashes.delete(frameHashes.keys().next().value);
    // 对比动作前后的截图：只提示“画面是否变化”，是否换策略仍由 Agent 决定。
    const notice = progress.observe(hash, info.width, info.height);
    if (notice && !pendingNotice) {
      pendingNotice = notice;
      noProgressNotices += 1;
    }
    trace({ type: 'screenshot', at: now(), frame_id: currentFrame, width: info.width, height: info.height,
            bytes: info.bytes.length, sha256: hash,
            no_change_streak: progress.noChangeStreak, same_target_streak: progress.sameTargetStreak });
    return { base64: result.base64, status: 'success' };
  }

  async execute({ actions }) {
    const started = Date.now();
    let method = '';
    const inputs = actions?.[0]?.inputs ?? null;
    try {
      if (!Array.isArray(actions) || actions.length !== 1 || !currentFrame || faulted || actionUsedOnFrame) throw new Error('invalid_action');
      const action = actions[0];
      if (!action || !action.inputs || typeof action.type !== 'string') throw new Error('invalid_action');
      method = action.type;
      let reply = {};
      actionUsedOnFrame = true;
      if (action.type === 'notice') {
        // 格式修正不是导航动作；次数已在解析阶段计入 format_retries。
        pendingNotice = text(action.inputs.message, 300) || pendingNotice;
        trace({ type: 'tool', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame,
                method: 'notice', inputs: sanitized(inputs), ok: false, error: 'invalid_output',
                feedback: pendingNotice });
        return { status: 'success' };
      }
      actionCount += 1;
      if (action.type === 'click') {
        const point = action.inputs.point?.raw;
        if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) throw new Error('invalid_action');
        await callHost('click', { frame_id: currentFrame, x: point.x, y: point.y });
        progress.clickSent(currentFrameHash, { x: point.x, y: point.y });
      } else if (action.type === 'scroll') {
        const point = action.inputs.point?.raw;
        if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y) || !['up', 'down'].includes(action.inputs.direction)) throw new Error('invalid_action');
        if (!Number.isFinite(action.inputs.pixels) || action.inputs.pixels === 0) throw new Error('invalid_action');
        // pixels 保持模型给出的符号与绝对值，由宿主换算成有界滚轮格数。
        await callHost('scroll', {
          frame_id: currentFrame, x: point.x, y: point.y,
          direction: action.inputs.direction === 'up' ? 'Up' : 'Down',
          pixels: action.inputs.pixels,
        });
      } else if (action.type === 'wait') {
        const seconds = action.inputs.time;
        if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 5) throw new Error('invalid_action');
        await callHost('wait', { frame_id: currentFrame, seconds });
      } else if (action.type === 'windows') {
        const result = await rpc('windows', { frame_id: currentFrame });
        const titles = Array.isArray(result?.windows)
          ? result.windows.map((item) => text(item?.title)).filter(Boolean)
          : [];
        reply = { titles };
        pendingNotice = titles.length
          ? `可切换的小程序窗口：${titles.join('、')}。如需切换，请调用 switch_window(title="窗口名称")。`
          : '当前没有可切换的小程序窗口。';
      } else if (action.type === 'switch_window') {
        const title = text(action.inputs.title);
        if (!title) throw new Error('invalid_action');
        const result = await callHost('switch_window', { frame_id: currentFrame, title });
        reply = { switched: Boolean(result?.switched), error: text(result?.error), titles: result?.titles || [] };
        pendingNotice = result?.switched
          ? `已切换到窗口「${title}」；下一张截图来自该窗口，请重新核对门店。`
          : `切换失败：${text(result?.error) || '目标窗口不存在'}。可用窗口：${(reply.titles || []).join('、') || '无'}。`;
      } else {
        throw new Error('disallowed_action');
      }
      trace({ type: 'tool', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame, method,
              inputs: sanitized(inputs), ok: true, duration_ms: Date.now() - started, reply,
              feedback: pendingNotice });
      return { status: 'success' };
    } catch (error) {
      if (error instanceof HostRejected) {
        toolRejections += 1;
        pendingNotice = `上一步操作未执行：${error.message}。请重新观察截图，换一个位置或动作。`;
        trace({ type: 'tool', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame, method,
                inputs: sanitized(inputs), ok: false, duration_ms: Date.now() - started,
                error: 'host_rejected', detail: text(error.message, 300), feedback: pendingNotice });
        return { status: 'success' };
      }
      trace({ type: 'tool', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame, method,
              inputs: sanitized(inputs), ok: false, duration_ms: Date.now() - started,
              error: text(error?.message, 120) });
      if (!failure) {
        const code = text(error?.message, 120) || 'action_failed';
        failure = { code, raw: '' };
        try {
          process.stderr.write(`${JSON.stringify({ code, raw: '' })}\n`);
        } catch { /* ignore */ }
      }
      faulted = true;
      stopAgent();
      throw error;
    }
  }
}

// 辅助调用与导航动作分开计数，便于判断格式修正是否挤占导航预算。
function metrics() {
  return {
    model_calls: callSeq,
    screenshots: screenshotCount,
    navigation_actions: actionCount,
    format_retries: formatRetries,
    tool_rejections: toolRejections,
    format_failures: parseFailuresTotal,
    no_progress_notices: noProgressNotices,
  };
}

function emitResult(outcome, reason, extra = {}) {
  if (resultEmitted) return;
  resultEmitted = true;
  const payload = { metrics: metrics(), ...extra };
  trace({ type: 'result', at: now(), outcome, reason: text(reason, 600), extra: sanitized(payload),
          actions: actionCount });
  send({ type: 'result', outcome, reason, frame_id: currentFrame, ...payload });
}

function emitRuntimeReview(reason, diagnostic = {}) {
  emitResult('needs_review', reason, { source: 'runtime', diagnostic: { code: 'runtime', ...diagnostic } });
}

async function main() {
  try {
    const config = await readObject();
    const model = config.model;
    if (!model || typeof model.baseURL !== 'string' || typeof model.apiKey !== 'string' || typeof model.model !== 'string' ||
        typeof config.instruction !== 'string' || !config.instruction.trim() ||
        typeof config.output_contract !== 'string' || !config.output_contract.trim() ||
        !Number.isInteger(config.max_steps) || config.max_steps < 1 || config.max_steps > 100) throw new Error('protocol');
    const reference = config.reference_image;
    if (reference != null && (typeof reference !== 'object' || typeof reference.mime !== 'string' ||
        !/^image\/(png|jpeg)$/.test(reference.mime) || typeof reference.base64 !== 'string' || !reference.base64)) {
      throw new Error('protocol');
    }

    const operator = new HostOperator();
    if (typeof config.trace_path === 'string' && config.trace_path.trim()) tracePath = config.trace_path.trim();
    trace({ type: 'session', at: now(), node: process.version, model: model.model, max_steps: config.max_steps,
            reference: reference ? { mime: reference.mime, width: reference.width || null,
                                     height: reference.height || null,
                                     bytes: Buffer.byteLength(reference.base64, 'base64') } : null,
            ...sanitized(typeof config.trace === 'object' && config.trace ? config.trace : {}) });
    const systemPrompt = [
      '每一轮只做一次观察和一个动作。',
      '只输出一个 tool_call：<tool_call>{"name":"computer_use","arguments":{...}}</tool_call>。坐标与 menu_box 使用当前截图的 1000x1000 归一化空间，左上角为原点。',
      '终态示例：<tool_call>{"name":"computer_use","arguments":{"action":"terminate","outcome":"success","reason":"已进入菜单","menu_box":[50,150,950,950]}}</tool_call>；<tool_call>{"name":"computer_use","arguments":{"action":"terminate","outcome":"unqualified","reason":"这是大众点评的团购界面，不是点餐码"}}</tool_call>；<tool_call>{"name":"computer_use","arguments":{"action":"terminate","outcome":"needs_review","reason":"..."}}</tool_call>。',
      '安全边界：不要下单、支付、加购物车、领券、登录或切换门店；不要编造坐标，也不要声称截图里没有出现的内容。',
      config.output_contract,
      navigationGuide,
    ].join('\n');
    const agent = new class extends GUIAgent {
      async onLLMRequest(_id, payload) {
        const request = payload?.request;
        if (!request || typeof request !== 'object') throw new Error('invalid_model_request');
        delete request.thinking;
        request.enable_thinking = false;
        request.stream_options = { ...(request.stream_options || {}), include_usage: true };
        if (Array.isArray(request.messages)) {
          for (const message of request.messages) {
            if (message?.role === 'user' && typeof message.content === 'string') {
              message.content = [{ type: 'text', text: message.content }];
            }
          }
        }
        const callId = ++callSeq;
        currentCall = { id: callId, started: Date.now(), provider: payload?.provider || '', baseURL: payload?.baseURL || '' };
        trace({ type: 'request', at: now(), call_id: callId, provider: payload?.provider || '',
                base_url: payload?.baseURL || '', request: sanitized(request) });
      }

      async onLLMResponse(_id, payload) {
        const call = currentCall || { id: ++callSeq, started: Date.now() };
        const response = payload?.response || {};
        const choice = response?.choices?.[0] || {};
        const content = choice?.message?.content;
        trace({ type: 'response', at: now(), call_id: call.id, duration_ms: Date.now() - call.started,
                provider: payload?.provider || call.provider, request_id: text(response?.id, 120),
                returned_model: text(response?.model, 120), finish_reason: text(choice?.finish_reason, 40),
                content: typeof content === 'string' ? content : JSON.stringify(content ?? ''),
                usage: response?.usage ?? null, error: null });
      }

      async onLLMStreamingResponse(_id, payload) {
        const chunks = Array.isArray(payload?.chunks) ? payload.chunks : [];
        trace({ type: 'stream', at: now(), call_id: currentCall?.id ?? null, chunks: chunks.length });
      }

      async onAfterToolCall(id, toolCall, result) {
        const output = await super.onAfterToolCall(id, toolCall, result);
        if (pendingNotice) {
          const notice = pendingNotice;
          pendingNotice = '';
          try {
            const stream = this.getEventStream();
            stream.sendEvent(stream.createEvent('environment_input', {
              description: 'Host feedback',
              content: [{ type: 'text', text: notice }],
              metadata: { type: 'text' },
            }));
          } catch { /* notice is advisory; the screenshot still drives the next decision */ }
        }
        return output;
      }
    }({
      operator,
      model: { provider: 'openai-compatible', id: model.model, baseURL: model.baseURL, apiKey: model.apiKey },
      systemPrompt,
      customeActionParser: makeCustomActionParser(),
      normalizeCoordinates: (coords) => {
        if (!coords?.raw || !Number.isFinite(coords.raw.x) || !Number.isFinite(coords.raw.y)) throw new Error('invalid_action');
        if (coords.raw.x < 0 || coords.raw.y < 0 || coords.raw.x >= screenWidth || coords.raw.y >= screenHeight) throw new Error('invalid_action');
        return {
          normalized: {
            ...coords,
            normalized: { x: coords.raw.x / screenWidth, y: coords.raw.y / screenHeight },
          },
        };
      },
      maxLoopCount: config.max_steps,
      loopIntervalInMs: 0,
    });
    activeAgent = agent;
    const first = await operator.doScreenshot();
    if (first.status !== 'success' || !first.base64) throw new Error('screenshot');
    const observationMeta = `First attached PNG dimensions: ${screenWidth}x${screenHeight} pixels. Coordinates use the normalized 1000x1000 screen space.`;
    // 第一张图是当前屏幕，也是唯一可点击的坐标依据；参考图只用于理解任务来源。
    const initialContent = [{ type: 'image_url', image_url: { url: `data:image/png;base64,${first.base64}` } }];
    if (reference) {
      initialContent.push({
        type: 'text',
        text: '下面附一张本轮二维码原图，仅用于理解品牌、桌号、扫码方式等任务背景；它不是当前屏幕，不能作为点击坐标依据，也不要求页面必须出现其中的文字。',
      });
      initialContent.push({
        type: 'image_url',
        image_url: { url: `data:${reference.mime};base64,${reference.base64}` },
      });
    }
    const event = await agent.run({
      input: `${observationMeta}\n\nTask: ${config.instruction}`,
      environmentInput: {
        content: initialContent,
        description: 'Initial desktop screenshot',
        metadata: { type: 'screenshot' },
      },
    });

    if (faulted) {
      const code = failure?.code || 'runtime';
      return emitRuntimeReview(`模型输出被拒绝（${code}）`, { code, raw: failure?.raw || '' });
    }
    if (!terminal || event?.type !== 'assistant_message' || event.content !== terminalMarker || event.finishReason !== 'stop') {
      return emitRuntimeReview(`自动化未到达有效业务终态（已执行 ${actionCount} 个动作，步数上限 ${config.max_steps}）`,
        { code: 'no_terminal', actions: actionCount, max_steps: config.max_steps, raw: lastRawContent.slice(0, 800) });
    }
    if (terminal.outcome === 'needs_review') return emitResult('needs_review', terminal.reason);
    const extra = {};
    if (terminal.menu_region) extra.menu_region = terminal.menu_region;
    emitResult(terminal.outcome, terminal.reason, extra);
  } catch (error) {
    const detail = text(error?.message, 200);
    trace({ type: 'error', at: now(), call_id: currentCall?.id ?? null, code: failure?.code || 'runtime', detail,
            actions: actionCount, ...rawRecord(lastRawContent) });
    if (failure) {
      return emitRuntimeReview(`模型输出被拒绝（${failure.code}）`,
        { code: failure.code, raw: failure.raw || '', detail });
    }
    try {
      process.stderr.write(`${JSON.stringify({ code: 'runtime', detail })}\n`);
    } catch { /* ignore */ }
    emitRuntimeReview('协议或自动化执行失败', { code: 'runtime', detail });
  } finally {
    input.close();
  }
}

main().catch((error) => {
  const detail = text(error?.message, 200);
  trace({ type: 'error', at: now(), code: 'runtime', detail, actions: actionCount });
  emitRuntimeReview('协议或自动化执行失败', { code: 'runtime', detail });
});
