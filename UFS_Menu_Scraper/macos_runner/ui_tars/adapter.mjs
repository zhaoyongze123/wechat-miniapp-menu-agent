import { createInterface } from 'node:readline';
import { appendFileSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { extractActionPayload } from './action-repair.mjs';
import { NoProgressTracker } from './progress-tracker.mjs';
import { ACTIONS, COORDINATE_SPACE, ShapeError, buildHostParams, hostMethods, validateArgs } from './action-spec.mjs';
import { PROMPT_VERSION, buildBasePrompt } from './prompts/base.mjs';
import { BUSINESS_PROMPT, BUSINESS_PROMPT_VERSION } from './prompts/business.mjs';

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
// 白屏/加载中/前置页的“最小观察预算”：至少 3 帧、累计 10 秒，避免模型只看一眼就判待检查。
const MIN_REVIEW_SCREENSHOTS = 3;
const MIN_REVIEW_OBSERVATION_MS = 10000;
const MAX_OBSERVATION_DEFERRALS = 1;
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
// 连续空参数次数：只用于提醒模型给动作，不参与失败判定。
let noopCount = 0;
// 每类动作的执行次数：只用于给模型“已尝试什么”的摘要，不参与任何判定。
const attempts = { click: 0, scroll: 0, wait: 0, key: 0, type_text: 0 };
// 步数预算来自启动配置，用于每轮告诉模型还剩多少步。
let maxSteps = 0;
let formatRetries = 0;
let toolRejections = 0;
let screenshotCount = 0;
let firstObservationAt = 0;
let observationDeferrals = 0;
let currentFrameHash = '';
// 当前帧的 64 位 dHash：只用于和本店历史帧比较“是否回到相似画面”。
let currentSignature = '';
let noProgressNotices = 0;
const progress = new NoProgressTracker();
const frameHashes = new Map();
const terminalMarker = 'UFS_UI_TARS_TERMINAL_COMPLETE';
// 两段式反馈只按证据补信息，不替模型判断原因：
// 第一次无变化给实际落点、前后差异、放大图和附近文字框；之后给同一入口的历次位置、读数与回访事实。
const REASON_ECHO_MAX = 80;
const OCR_PRECISION_RADIUS = 120;
const OCR_TARGET_RADIUS = 240;
// 连续空参数说明模型在“只给参数位”的协议里找不到出口；第二次起明确给出三种出口，而不是继续静默等待。
const NOOP_NOTICE = '上一轮没有给出动作，已按等待 1 秒处理。若仍在判断，请输出 wait(时间, 原因)；'
  + '若确认无法推进，请输出 terminate 并说明具体阻碍；若是格式问题，请按协议输出一个合法动作。';
let activeAgent = null;
// 宿主生成的落点放大图（data URL），用于“第一次落点无变化”的参考，不参与坐标换算。
let pendingZoom = '';
// 模型主动请求的局部视图：截图后按 box 裁剪，登记 view_id，并在本轮工具回执里附图。
let pendingZoomRequest = null;
let pendingView = null;
const views = new Map();
let viewSeq = 0;
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

// 观测包：把宿主已知的窗口身份、画面变化、窗口增减压成一行，随每步截图交给 Agent。
// 只整理已有事实，不新增动作或工具。
function frameEnvelope(facts) {
  if (!facts || !facts.window) return '';
  const win = facts.window;
  const parts = [`[Host] ${win.title ? `窗口「${win.title}」` : '当前窗口'} ${win.width}x${win.height}`,
    win.foreground ? '前台=是' : '前台=否'];
  if (facts.image && (facts.image.width !== win.width || facts.image.height !== win.height)) {
    parts.push(`图像 ${facts.image.width}x${facts.image.height}`);
  }
  if (typeof facts.delta === 'number') parts.push(`画面变化 ${facts.delta}%`);
  const created = (facts.new_windows || []).filter(Boolean);
  parts.push(`新窗口：${created.length ? created.join('、') : '无'}`);
  const closed = (facts.closed_windows || []).filter(Boolean);
  if (closed.length) parts.push(`已关闭：${closed.join('、')}`);
  return parts.join(' | ');
}

// 每轮随截图交给模型的“可判断状态”：走到第几步、做过哪些动作、同目标卡了几次。
// 只汇总宿主已经掌握的事实，不预测页面，也不替模型做决定。
function statusSummary() {
  const parts = [];
  if (maxSteps > 0) parts.push(`进度 ${Math.min(actionCount, maxSteps)}/${maxSteps} 步`);
  const tried = [];
  if (attempts.click) tried.push(`点击 ${attempts.click}`);
  if (attempts.scroll) tried.push(`滚动 ${attempts.scroll}`);
  if (attempts.wait) tried.push(`等待 ${attempts.wait}`);
  if (attempts.key) tried.push(`按键 ${attempts.key}`);
  if (attempts.type_text) tried.push(`输入 ${attempts.type_text}`);
  parts.push(tried.length ? `已尝试 ${tried.join('、')}` : '已尝试 无');
  if (progress.sameTargetStreak >= 2) parts.push(`同目标连续无变化 ${progress.sameTargetStreak} 次`);
  return parts.join('；');
}

function windowLabel(item) {
  if (!item || !item.title) return '';
  const size = item.width && item.height ? `(${item.width}x${item.height}${item.foreground ? ', 前台' : ''})` : '';
  return `${item.title}${size}`;
}

function extractAction(raw) {
  return extractActionPayload(raw);
}

function stopAgent() {
  try { activeAgent?.abort(); } catch { /* fail closed; host owns process deadlines */ }
}

// OCR 只是可选参考：宿主侧失败时返回空结果，不能让导航因此中断。
async function nearbyOcrContext(pixel, radius = OCR_PRECISION_RADIUS) {
  try {
    const result = await rpc('ocr_context', {
      frame_id: currentFrame, x: pixel.x, y: pixel.y, radius,
    }, true);
    if (!result || result.__rejected || !Array.isArray(result.items)) return null;
    return result;
  } catch {
    return null;
  }
}

function ocrBody(ocrContext) {
  if (!ocrContext) return '';
  if (!ocrContext.items.length) return '附近未识别到可用文字。';
  return JSON.stringify({ click_point: ocrContext.click_point, items: ocrContext.items });
}

// 第一次无变化：只回传这一步的事实，让模型自己判断有没有点偏、按钮是否可用。
function clickEvidence(reason, step, ocrContext) {
  const why = reason ? `你给出的点击原因：「${reason}」。` : '';
  const body = ocrBody(ocrContext);
  const point = step?.point ? `实际落点 (${step.point[0]},${step.point[1]})（0..1000）` : '实际落点未知';
  const pixel = step?.pixel ? `，窗口像素 (${step.pixel[0]},${step.pixel[1]})` : '';
  const change = `整页变化 ${step?.delta === null || step?.delta === undefined ? '未知' : step.delta}%`
    + (step?.roi_delta === null || step?.roi_delta === undefined ? '' : `，落点邻域变化 ${step.roi_delta}%`);
  return '执行反馈（事实）：上一次 left_click ' + point + pixel + `；${change}。` + why
    + '随附本次落点放大图（2 倍，红叉=实际落点，绿框=识别到的文字框），它不是当前可点击屏幕，坐标仍用整屏 0..1000。'
    + (body ? `附近文字框（0..1000，center 只是文字框中心，不保证是按钮热区）：${body}。` : '')
    + '重试前先检查有什么新证据。';
}

// 之后仍无变化：回传同一入口的历次位置与观察读数，以及是否回到过相似画面。
function clickHistoryEvidence({ reason, targetName, attempts, ocrContext, revisitFrame }) {
  const why = reason ? `你给出的点击原因：「${reason}」。` : '';
  const name = targetName ? `「${targetName}」` : 'left_click';
  const positions = attempts.map((item) => `(${item.point[0]},${item.point[1]})`).join('、');
  const readings = attempts.map((item, index) => {
    const change = item.identical ? '前后像素完全相同'
      : `整页 ${item.delta === null ? '未知' : item.delta}%` + (item.roi === null ? '' : ` / 局部 ${item.roi}%`);
    return `第 ${index + 1} 次${change}`;
  }).join('；');
  const revisit = revisitFrame === null || revisitFrame === undefined
    ? '' : `当前画面与第 ${revisitFrame} 帧相似，说明中途离开后又回到过相似画面。`;
  const outside = containmentNote(attempts.map((item) => item.point), ocrContext, targetName);
  return `执行反馈（事实）：本屏${name}已实际点击 ${attempts.length} 次，位置 ${positions}；各次观察：${readings}。`
    + revisit + outside + why + '重试前先检查有什么新证据。';
}

// 只有拿到可信文字框、并逐个核对过坐标，才补一句“落点在框外”。
function containmentNote(points, ocrContext, targetName) {
  if (!targetName || !points.length || !ocrContext?.items?.length) return '';
  const item = ocrContext.items.find((entry) => {
    const text = String(entry?.text || '');
    return text && (text.includes(targetName) || targetName.includes(text));
  });
  if (!item || !Array.isArray(item.bbox) || item.bbox.length !== 4) return '';
  const [x1, y1, x2, y2] = item.bbox;
  const external = points.every(([x, y]) => x < x1 || x > x2 || y < y1 || y > y2);
  if (!external) return '';
  return `识别到的「${item.text}」文字框为 [${x1},${y1},${x2},${y2}]，以上 ${points.length} 个落点都在框外。`;
}

const NORMALIZED_SCREEN_SIZE = COORDINATE_SPACE;

// 宿主坐标 → 模型坐标：证据行里的位置必须和模型输出的 0..1000 空间一致。
function normalizedPoint(point) {
  return {
    x: point.x * NORMALIZED_SCREEN_SIZE / screenWidth,
    y: point.y * NORMALIZED_SCREEN_SIZE / screenHeight,
  };
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

// 局部视图 → 全窗坐标：模型框的是放大图上的范围，这里按裁剪框线性映射回全窗 0..1000。
// 视图只影响坐标解释，不改变“取框中心”的落点规则。
function resolveViewBox(args) {
  if (args.target_box === undefined || !args.view_id) return args;
  const view = views.get(args.view_id);
  if (!view) throw new ShapeError('invalid_action', 'view_id', 'value');
  const [x1, y1, x2, y2] = view.crop_box;
  const map = (x, y) => [x1 + x / COORDINATE_SPACE * (x2 - x1), y1 + y / COORDINATE_SPACE * (y2 - y1)];
  const [bx1, by1, bx2, by2] = args.target_box;
  const [sx1, sy1] = map(bx1, by1);
  const [sx2, sy2] = map(bx2, by2);
  const { view_id, ...rest } = args;
  trace({ type: 'view_resolve', at: now(), frame_id: currentFrame, view_id,
          view_frame: view.frameId, crop_box: view.crop_box,
          local_box: args.target_box.slice(), screen_box: [sx1, sy1, sx2, sy2],
          stale: view.frameId !== currentFrame });
  return { args: { ...rest, target_box: [sx1, sy1, sx2, sy2] }, view: { id: args.view_id, frameId: view.frameId } };
}

function parseModelAction(rawArgs) {
  const args = normalizedArguments(rawArgs);
  if (!args || typeof args !== 'object' || Array.isArray(args) || typeof args.action !== 'string') throw new Error('invalid_action');
  // noop 必须在 schema 查找之前处理：它是“先不动作”的合法出口，schema 里没有对应的宿主方法。
  // 空 arguments 会被 action-repair 归一化成 noop，如果先查 schema 就会误判成 disallowed_action 并直接终止。
  if (args.action === 'noop') {
    // 模型用空 arguments 表示“先不动、继续观察”；转成一次短暂等待，下一轮重新截图。
    // 连续空参数不是格式错误，但也不能一直静默：第二次起把合法出口直接说出来。
    noopCount += 1;
    if (noopCount >= 2) pendingNotice = [pendingNotice, NOOP_NOTICE].filter(Boolean).join(' ');
    return { type: 'wait', inputs: { params: { seconds: 1 } } };
  }
  if (args.action === 'zoom_region') {
    // 只读观察：不点击不滚动，转成一次短暂等待；截图后按 box 裁剪并登记 view_id。
    validateArgs('zoom_region', args);
    pendingZoomRequest = { box: args.box.slice(), reason: text(args.reason, 120) };
    return { type: 'wait', inputs: { params: { seconds: 0.2 } } };
  }
  const spec = ACTIONS[args.action];
  if (!spec) throw new Error('disallowed_action');
  if (args.action === 'terminate') {
    // 业务只收一个状态、一条理由；success 额外要求菜品区域。字段白名单之外一律视为格式违规。
    validateArgs('terminate', args, 'invalid_terminal');
    // 白屏/加载中/前置页只看一眼就结束是真实存在的“早退”。这里只校验观察过程是否充分，
    // 不替模型判断页面性质；最多退回 3 次，避免异常页面无限循环。
    if (args.outcome === 'needs_review' && observationDeferrals < MAX_OBSERVATION_DEFERRALS
        && (screenshotCount < MIN_REVIEW_SCREENSHOTS
            || Date.now() - firstObservationAt < MIN_REVIEW_OBSERVATION_MS)) {
      observationDeferrals += 1;
      throw new Error('insufficient_observation');
    }
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
  if (!spec.host?.method) throw new Error('disallowed_action');
  // 动作名、字段白名单与宿主方法映射全部来自 action-schema.json，不再逐动作写分支。
  validateArgs(args.action, args);
  // 局部视图只影响 target_box 的解释方式：先换算成全窗框，再走原有取中心与点击链路。
  let hostArgs = args;
  let viewMeta = null;
  if (args.action === 'left_click' && args.view_id) {
    const view = views.get(args.view_id);
    if (!view) throw new ShapeError('invalid_action', 'view_id', 'value');
    if (view.frameId !== currentFrame) {
      // 局部图已过期：不复用旧帧的裁剪范围换算坐标，本次不执行，下一轮拿最新画面重新框选。
      trace({ type: 'view_stale', at: now(), frame_id: currentFrame, view_id: args.view_id,
              view_frame: view.frameId, stale: true });
      pendingNotice = `局部图 ${args.view_id} 来自第 ${view.frameId} 帧，早于当前画面（第 ${currentFrame} 帧），本次未执行。`
        + '已更新当前画面，请基于最新截图重新框选，或直接给 coordinate。';
      return { type: 'notice', inputs: { message: pendingNotice.slice(0, 300) } };
    }
    const resolved = resolveViewBox(args);
    hostArgs = resolved.args;
    viewMeta = resolved.view;
  }
  const { method, params, point } = buildHostParams(args.action, hostArgs, {
    frameId: currentFrame, width: screenWidth, height: screenHeight,
  });
  // point 必须保留：SDK 会据此做坐标归一化，并在执行后用 convertToAgentUIAction 读 point.normalized。
  const inputs = { params, point: point && { raw: point, referenceSystem: 'screen' } };
  // reason 不进入宿主参数，只带给执行层做留痕和失败重试时的意图回述。
  if (args.action === 'left_click') {
    inputs.reason = text(args.reason, 120);
    const targetName = text(args.target_name, 80);
    if (targetName) inputs.target_name = targetName;
    // 框选保留两条事实：模型圈的框、换算后实际执行的落点，回执里要能对照“框在哪、点在哪”。
    if (hostArgs.target_box) inputs.target_box = hostArgs.target_box.slice();
    if (viewMeta) inputs.view = viewMeta;
  }
  return { type: method, inputs };
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
    return (args?.action ? `不支持的动作「${text(args.action, 40)}」. ` : '不支持的动作. ')
      + '可用 left_click / scroll / type_text / key / wait / list_windows / switch_window / terminate / noop。';
  }
  if (code === 'no_observation') return '当前没有可用截图';
  if (args && typeof args === 'object' && !Array.isArray(args) && typeof args.action !== 'string') {
    // 实测模型会连续输出 arguments:{}；把“缺什么”和“无动作/结束怎么写”一起说清楚，让它照着最小结构改。
    return 'arguments 为空或缺少 action：需要动作时给出 action 与参数，例如 '
      + '{"name":"computer_use","arguments":{"action":"left_click","coordinate":[500,500]}}；'
      + '当前不需要动作时输出 {"name":"computer_use","arguments":{"action":"noop"}}；'
      + '要结束时输出 {"name":"computer_use","arguments":{"action":"terminate",'
      + '"outcome":"success|needs_review|unqualified","reason":"...","menu_box":[x1,y1,x2,y2]}}'
      + '（success 才需要 menu_box）。';
  }
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
      // noop 不是“正常动作”，不能在这里清零，否则连续空参数的提醒永远触发不了。
      if (args?.action !== 'noop') noopCount = 0;
      trace({ type: 'action', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame,
              ok: true, action: parsed, ...(args?.action === 'noop' ? { source: 'noop' } : {}),
              ...rawRecord(rawContent) });
      // reason 是模型这一步的判断依据：回填给 SDK 的 reasoningContent，供 UI 与 trace 留痕；
      // 不参与任何判定，也不改变模型下一轮看到的历史（历史用的是 rawContent）。
      return { rawContent, reasoningContent: text(args?.reason, REASON_MAX_CHARS), actions: [parsed] };
    } catch (error) {
      const code = ['invalid_action', 'invalid_terminal', 'disallowed_action', 'no_observation',
        'insufficient_observation'].includes(error?.message)
        ? error.message
        : 'invalid_action';
      trace({ type: 'action', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame,
              ok: false, error: code, ...rawRecord(rawContent) });
      if (code === 'insufficient_observation') {
        // 不计入格式失败预算；只补一次观察机会。
        pendingNotice = `结束判定过早：目前只观察到 ${screenshotCount} 帧、累计不足 10 秒。`
          + '请再等待 3-5 秒观察一帧；白屏、加载中或前置页在画面仍无变化前不要返回 needs_review。';
        trace({ type: 'observation_deferred', at: now(), call_id: currentCall?.id ?? null,
                frame_id: currentFrame, screenshots: screenshotCount, deferrals: observationDeferrals });
        return { rawContent, reasoningContent: '', actions: [{ type: 'notice', inputs: { message: pendingNotice.slice(0, 300) } }] };
      }
      const formatError = ['invalid_action', 'invalid_terminal', 'disallowed_action'].includes(code);
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

class HostOperator extends Operator {
  async initialize() {}

  supportedActions() {
    // 从 schema 的 host.method 派生，新增动作不需要再改这里。
    return [...hostMethods(), 'finished', 'notice'];
  }

  screenContext() {
    return { screenWidth, screenHeight, scaleX: 1, scaleY: 1 };
  }

  async screenshot() {
    // observe() 会清空 pending，所以在截图前保存本次待观测动作的类型。
    const sentMethod = progress.pendingMethod;
    const clicked = sentMethod === 'click' ? progress.pendingPixel : null;
    const clickedReason = text(progress.pendingReason, REASON_ECHO_MAX);
    // 把落点交给宿主：宿主在未标记原帧上算完 delta/roi 后，只有这次点击确实无变化时
    // 才在回传副本上标记落点，并附一张落点放大参考图。标记不改变坐标空间。
    const result = await rpc('screenshot', clicked ? { mark: clicked } : {});
    if (!result || typeof result.base64 !== 'string' || typeof result.frame_id !== 'string' || !result.frame_id) throw new Error('protocol');
    const info = pngInfo(result.base64);
    ({ width: screenWidth, height: screenHeight } = info);
    currentFrame = result.frame_id;
    actionUsedOnFrame = false;
    screenshotCount += 1;
    if (!firstObservationAt) firstObservationAt = Date.now();
    const hash = sha256(info.bytes);
    currentFrameHash = hash;
    frameHashes.set(hash, currentFrame);
    if (frameHashes.size > 64) frameHashes.delete(frameHashes.keys().next().value);
    // 模型请求的局部视图：复用本次截图裁剪，登记 view_id；只改变框选坐标的解释，不改变窗口状态。
    if (pendingZoomRequest) {
      const request = pendingZoomRequest;
      pendingZoomRequest = null;
      try {
        const crop = await rpc('crop_region', { frame_id: currentFrame, box: request.box });
        if (crop && typeof crop.base64 === 'string') {
          viewSeq += 1;
          const viewId = `f${currentFrame}-z${viewSeq}`;
          views.set(viewId, { frameId: currentFrame, crop_box: request.box.slice() });
          if (views.size > 16) views.delete(views.keys().next().value);
          pendingView = { base64: crop.base64, view_id: viewId, crop_box: request.box.slice(),
                          crop: crop.crop ?? null, scale: crop.scale ?? null, reason: request.reason };
        }
      } catch (error) {
        pendingNotice = [pendingNotice, `局部图获取失败：${text(error?.message, 160)}`].filter(Boolean).join('\n');
      }
    }
    // 对比动作前后的截图：delta 是整页差异，roi_delta 是落点邻域差异，hash 只用于识别“字节完全相同”。
    const notice = progress.observe(hash,
      { delta: result.facts?.delta, roiDelta: result.facts?.roi_delta,
        frameId: currentFrame, signature: text(result.facts?.signature, 32) });
    currentSignature = text(result.facts?.signature, 32);
    const evidence = progress.evidence();
    const envelope = frameEnvelope(result.facts);
    const status = statusSummary();
    if (envelope || notice || evidence || status) {
      pendingNotice = [pendingNotice, envelope, status, evidence, notice].filter(Boolean).join(' ');
      if (notice && !envelope) noProgressNotices += 1;
    }
    // 两段式只按证据补信息：第一次给落点/差异/放大图/附近文字框；之后给同一入口的历次读数与回访事实。
    let ocrContext = null;
    let retryHint = '';
    let stage = 0;
    const step = progress.lastStep;
    if (clicked && progress.sameTargetStreak === 1 && progress.noChangeStreak >= 1) {
      stage = 1;
      ocrContext = await nearbyOcrContext(clicked, OCR_PRECISION_RADIUS);
      retryHint = clickEvidence(clickedReason, step, ocrContext);
    } else if (clicked && progress.sameTargetStreak === 2) {
      stage = 2;
      ocrContext = await nearbyOcrContext(clicked, OCR_TARGET_RADIUS);
      retryHint = clickHistoryEvidence({
        reason: clickedReason,
        targetName: step?.target_name || '',
        attempts: progress.attemptsOnScreen(currentSignature, step?.target_name || ''),
        ocrContext,
        revisitFrame: progress.revisitFrame(currentFrame, currentSignature),
      });
    }
    if (retryHint) pendingNotice = [pendingNotice, retryHint].filter(Boolean).join('\n');
    // 放大图只在第一次重试时随文字一起发给模型；第二次不再强调落点。
    const zoom = stage === 1 && typeof result.zoom === 'string' ? result.zoom : '';
    pendingZoom = zoom;
    trace({ type: 'screenshot', at: now(), frame_id: currentFrame, width: info.width, height: info.height,
            bytes: info.bytes.length, sha256: hash,
            window: result.facts?.window ?? null, delta: result.facts?.delta ?? null,
            roi_delta: result.facts?.roi_delta ?? null,
            new_windows: result.facts?.new_windows ?? [],
            retry_stage: stage, zoom_meta: result.zoom_meta ?? null, zoom_sent: Boolean(zoom),
            ocr_context: ocrContext,
            no_change_streak: progress.noChangeStreak, same_target_streak: progress.sameTargetStreak,
            evidence });
    return { base64: result.base64, status: 'success', facts: result.facts ?? null };
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
      // 动作类型计数放到执行成功后（见下方 trace 前），未执行的动作只留痕、不计入“已尝试”。
      // 宿主参数由 schema 的 host.map 生成；这里只保留各方法的回执处理。
      const params = { ...(action.inputs.params || {}), frame_id: currentFrame };
      if (action.type === 'click') {
        const point = action.inputs.point?.raw;
        if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) throw new Error('invalid_action');
        const clicked = await callHost('click', params);
        // 证据行与模型输出使用同一坐标空间：0..1000 在前，像素只作附注。
        progress.actionSent(currentFrameHash, {
          method: 'click',
          point: normalizedPoint(point),
          pixel: { x: point.x, y: point.y },
          reason: text(action.inputs.reason, 120),
          frameId: currentFrame,
          targetName: text(action.inputs.target_name, 80),
          box: Array.isArray(action.inputs.target_box) ? action.inputs.target_box.slice() : null,
        });
        if (clicked?.window_switch?.switched) {
          const switchedTitle = text(clicked.window_switch.title) || '新窗口';
          reply = { window_switch: { title: switchedTitle } };
          // 新窗口是宿主枚举到的确定事实：告诉 Agent 下一帧来自哪里，避免把“原窗口没变化”当成没响应。
          pendingNotice = `点击后另开了小程序窗口「${switchedTitle}」，宿主已切换过去；下一张截图来自新窗口。`;
        }
      } else if (action.type === 'scroll') {
        const point = action.inputs.point?.raw;
        if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) throw new Error('invalid_action');
        // pixels 保持模型给出的符号与绝对值，由宿主换算成有界滚轮格数。
        await callHost('scroll', params);
        progress.actionSent(currentFrameHash, { method: 'scroll', point: normalizedPoint(point) });
      } else if (action.type === 'type_text') {
        const value = text(params.text, 200);
        if (!value) throw new Error('invalid_action');
        await callHost('type_text', { ...params, text: value });
        progress.actionSent(currentFrameHash, { method: 'type_text' });
      } else if (action.type === 'key') {
        const key = text(params.key, 40);
        if (!key) throw new Error('invalid_action');
        await callHost('key', { ...params, key });
        progress.actionSent(currentFrameHash, { method: 'key' });
      } else if (action.type === 'wait') {
        const seconds = params.seconds;
        if (!Number.isFinite(seconds) || seconds <= 0 || seconds > 5) throw new Error('invalid_action');
        await callHost('wait', params);
      } else if (action.type === 'windows') {
        const result = await rpc('windows', params);
        const titles = Array.isArray(result?.windows)
          ? result.windows.map((item) => text(item?.title)).filter(Boolean)
          : [];
        reply = { titles };
        pendingNotice = titles.length
          ? `可切换的小程序窗口：${titles.join('、')}。如需切换，请调用 switch_window(title="窗口名称")。`
          : '当前没有可切换的小程序窗口。';
      } else if (action.type === 'switch_window') {
        const title = text(params.title);
        if (!title) throw new Error('invalid_action');
        const result = await callHost('switch_window', { ...params, title });
        progress.actionSent(currentFrameHash, { method: 'switch_window' });
        reply = { switched: Boolean(result?.switched), error: text(result?.error), titles: result?.titles || [] };
        pendingNotice = result?.switched
          ? `已切换到窗口「${title}」；下一张截图来自该窗口，请重新核对门店。`
          : `切换失败：${text(result?.error) || '目标窗口不存在'}。可用窗口：${(reply.titles || []).join('、') || '无'}。`;
      } else {
        throw new Error('disallowed_action');
      }
      // 只有真正执行成功的动作才计入“已尝试”；过期视图、宿主拒绝和格式错误都不算。
      if (Object.prototype.hasOwnProperty.call(attempts, action.type)) attempts[action.type] += 1;
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

// 整店执行摘要只在本次请求里放一份最新版：先清掉历史残留，再追加当前摘要。
function injectDigest(messages) {
  const marker = '【本店执行记录】';
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const content = messages[index]?.content;
    const head = typeof content === 'string' ? content : content?.[0]?.text;
    if (typeof head === 'string' && head.startsWith(marker)) messages.splice(index, 1);
  }
  const digest = progress.digest(currentFrame, currentSignature);
  if (!digest) return;
  messages.push({ role: 'user', content: `${marker}\n${digest}` });
  trace({ type: 'digest', at: now(), frame_id: currentFrame, digest });
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
    maxSteps = config.max_steps;
    if (typeof config.trace_path === 'string' && config.trace_path.trim()) tracePath = config.trace_path.trim();
    // 提示词分两段：稳定段（协议 + 动作表）在前，业务段在后；两段 hash 一并留痕，便于区分版本问题与门店问题。
    const basePrompt = buildBasePrompt({ outputContract: config.output_contract || '' });
    const systemPrompt = [basePrompt, BUSINESS_PROMPT].join('\n\n');
    const promptMeta = {
      version: PROMPT_VERSION,
      business_version: BUSINESS_PROMPT_VERSION,
      base_sha256: sha256(Buffer.from(basePrompt)),
      business_sha256: sha256(Buffer.from(BUSINESS_PROMPT)),
      base_chars: basePrompt.length,
      business_chars: BUSINESS_PROMPT.length,
    };
    trace({ type: 'session', at: now(), node: process.version, model: model.model, max_steps: config.max_steps,
            reference: reference ? { mime: reference.mime, width: reference.width || null,
                                     height: reference.height || null,
                                     bytes: Buffer.byteLength(reference.base64, 'base64') } : null,
            prompt: promptMeta,
            ...sanitized(typeof config.trace === 'object' && config.trace ? config.trace : {}) });
    const agent = new class extends GUIAgent {
      async onLLMRequest(_id, payload) {
        const request = payload?.request;
        if (!request || typeof request !== 'object') throw new Error('invalid_model_request');
        delete request.thinking;
        request.enable_thinking = false;
        request.stream_options = { ...(request.stream_options || {}), include_usage: true };
        if (Array.isArray(request.messages)) {
          try { injectDigest(request.messages); } catch { /* 摘要失败不影响主链路 */ }
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
        const notice = pendingNotice;
        const zoom = pendingZoom;
        const view = pendingView;
        pendingNotice = '';
        pendingZoom = '';
        pendingView = null;
        if (notice || zoom || view) {
          try {
            // 文字先说明参考图的性质，再附图；两张图都只用于观察，不参与坐标换算。
            const content = [];
            if (notice) content.push({ type: 'text', text: notice });
            if (zoom) {
              content.push({
                type: 'image_url',
                image_url: { url: `data:image/png;base64,${zoom}` },
              });
            }
            if (view) {
              content.push({
                type: 'text',
                text: `局部图 view_id=${view.view_id}（全窗裁剪框 [${view.crop_box.join(',')}]）。`
                  + `在这张图上框选后，用 left_click 的 target_box + view_id="${view.view_id}" 点击。`,
              });
              content.push({
                type: 'image_url',
                image_url: { url: `data:image/png;base64,${view.base64}` },
              });
            }
            const stream = this.getEventStream();
            stream.sendEvent(stream.createEvent('environment_input', {
              description: 'Host feedback',
              content,
              metadata: { type: content.some((item) => item.type === 'image_url') ? 'text+image' : 'text' },
            }));
          } catch { /* notice is advisory; the screenshot still drives the next decision */ }
        }
        // 这一步的结构化事实随工具回执返回：做了什么、到了哪一帧、变了多少。不含任何结论。
        const step = progress.lastStep;
        if (!step) return output;
        return {
          ...output,
          observation: {
            ...step,
            no_change_streak: progress.noChangeStreak,
            same_target_streak: progress.sameTargetStreak,
          },
        };
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
    const observationMeta = `First attached PNG dimensions: ${screenWidth}x${screenHeight} pixels. `
      + 'Coordinates use the normalized 1000x1000 screen space.';
    // 首帧就把窗口清单交给 Agent：省掉“先花几步确认自己看的是哪个窗口”。
    const firstFacts = first.facts ?? null;
    const windowList = (firstFacts?.windows || []).map(windowLabel).filter(Boolean);
    const hostHint = '[Host] 这是桌面窗口截图，不是浏览器。'
      + (windowList.length ? `当前窗口：${windowList.join('、')}。` : '')
      + (firstFacts?.window?.title ? `本图来自「${firstFacts.window.title}」。` : '');
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
      input: `${hostHint}\n${observationMeta}\n\nTask: ${config.instruction}`,
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
