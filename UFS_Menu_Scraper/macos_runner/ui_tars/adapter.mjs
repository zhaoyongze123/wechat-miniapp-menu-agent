import { createInterface } from 'node:readline';
import { appendFileSync } from 'node:fs';
import { createHash } from 'node:crypto';

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
let parseFailures = 0;
let resultEmitted = false;
let failure = null;
let lastRawContent = '';
let pendingNotice = '';
let tracePath = '';
let callSeq = 0;
let currentCall = null;
let actionCount = 0;
const frameHashes = new Map();
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
  const payload = tags[0][1];
  let parsed;
  try {
    parsed = JSON.parse(payload);
  } catch (error) {
    // 百炼实测偶发漏掉点击坐标的左方括号。只接受结构完整、恰好包含两个整数的已知变体。
    const click = payload.match(/^\s*\{\s*"name"\s*:\s*"computer_use"\s*,\s*"arguments"\s*:\s*\{\s*"action"\s*:\s*"left_click"\s*,\s*"coordinate"\s*:\s*(0|[1-9]\d{0,2})\s*,\s*(0|[1-9]\d{0,2})\s*\]\s*\}\s*\}\s*$/);
    if (!click) throw error;
    return { action: 'left_click', coordinate: [Number(click[1]), Number(click[2])] };
  }
  if (!parsed || parsed.name !== 'computer_use' || !parsed.arguments || typeof parsed.arguments !== 'object' || Array.isArray(parsed.arguments)) {
    throw new Error('invalid_action');
  }
  return parsed.arguments;
}

function stopAgent() {
  try { activeAgent?.abort(); } catch { /* fail closed; host owns process deadlines */ }
}

function exactKeys(args, keys) {
  return Object.keys(args).every((key) => keys.includes(key));
}

const NORMALIZED_SCREEN_SIZE = 1000;

function normalizedPoint(value) {
  if (!Array.isArray(value) || value.length !== 2 || value.some((n) => !Number.isFinite(n))) throw new Error('invalid_action');
  const [x, y] = value;
  if (x < 0 || y < 0 || x >= NORMALIZED_SCREEN_SIZE || y >= NORMALIZED_SCREEN_SIZE) throw new Error('invalid_action');
  return { x: x * screenWidth / NORMALIZED_SCREEN_SIZE, y: y * screenHeight / NORMALIZED_SCREEN_SIZE };
}

function menuRegion(value) {
  if (!Array.isArray(value) || value.length !== 4 || value.some((n) => !Number.isFinite(n))) throw new Error('invalid_terminal');
  // 模型偶尔把截图像素高（如 1017）当成归一化坐标；5% 内的越界按边界钳制，超出仍判非法。
  const limit = NORMALIZED_SCREEN_SIZE * 1.05;
  const [x1, y1, x2, y2] = value.map((n) => {
    if (n < -limit || n > limit) throw new Error('invalid_terminal');
    return Math.round(Math.min(NORMALIZED_SCREEN_SIZE, Math.max(0, n)));
  });
  if (x2 <= x1 || y2 <= y1) throw new Error('invalid_terminal');
  const left = x1 * screenWidth / NORMALIZED_SCREEN_SIZE;
  const top = y1 * screenHeight / NORMALIZED_SCREEN_SIZE;
  const right = x2 * screenWidth / NORMALIZED_SCREEN_SIZE;
  const bottom = y2 * screenHeight / NORMALIZED_SCREEN_SIZE;
  return { x: left, y: top, width: right - left, height: bottom - top };
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
      if (!exactKeys(args, ['action', 'coordinate'])) throw new Error('invalid_action');
      const point = normalizedPoint(args.coordinate);
      return { type: 'click', inputs: { point: { raw: point, referenceSystem: 'screen' } } };
    }
    case 'scroll': {
      if (!exactKeys(args, ['action', 'pixels', 'coordinate']) || !Number.isFinite(args.pixels) || args.pixels === 0) throw new Error('invalid_action');
      const point = args.coordinate === undefined
        ? { x: Math.floor(screenWidth / 2), y: Math.floor(screenHeight / 2) }
        : normalizedPoint(args.coordinate);
      return { type: 'scroll', inputs: { point: { raw: point, referenceSystem: 'screen' }, direction: args.pixels > 0 ? 'up' : 'down' } };
    }
    case 'wait': {
      if (!exactKeys(args, ['action', 'time']) || !Number.isFinite(args.time) || args.time <= 0 || args.time > 5) throw new Error('invalid_action');
      return { type: 'wait', inputs: { time: args.time } };
    }
    case 'list_windows': {
      if (!exactKeys(args, ['action'])) throw new Error('invalid_action');
      return { type: 'windows', inputs: {} };
    }
    case 'switch_window': {
      if (!exactKeys(args, ['action', 'title']) || !text(args.title)) throw new Error('invalid_action');
      return { type: 'switch_window', inputs: { title: text(args.title) } };
    }
    case 'read_skill': {
      // Skill 是 Agent 按需加载的参考经验，不改变页面。
      if (!exactKeys(args, ['action', 'name']) || !text(args.name)) throw new Error('invalid_action');
      return { type: 'skill', inputs: { name: text(args.name, 80) } };
    }
    case 'terminate': {
      // 业务只收一个状态、一条理由；success 额外要求菜品区域。字段白名单之外一律视为格式违规。
      if (!exactKeys(args, ['action', 'outcome', 'reason', 'menu_box']) ||
          !['success', 'unqualified', 'needs_review'].includes(args.outcome) || !text(args.reason)) {
        throw new Error('invalid_terminal');
      }
      const next = { kind: 'terminate', outcome: args.outcome, reason: text(args.reason) };
      if (args.outcome === 'success') {
        next.menu_region = menuRegion(args.menu_box);
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

function makeCustomActionParser() {
  return (rawContent) => {
    lastRawContent = typeof rawContent === 'string' ? rawContent : '';
    if (faulted) throw new Error(failure?.code || 'invalid_action');
    try {
      if (!currentFrame || !screenWidth || !screenHeight) throw new Error('no_observation');
      const parsed = parseModelAction(extractAction(rawContent));
      trace({ type: 'action', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame,
              ok: true, action: parsed, ...rawRecord(rawContent) });
      return { rawContent, reasoningContent: '', actions: [parsed] };
    } catch (error) {
      const code = ['invalid_action', 'invalid_terminal', 'disallowed_action', 'no_observation'].includes(error?.message)
        ? error.message
        : 'invalid_action';
      trace({ type: 'action', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame,
              ok: false, error: code, ...rawRecord(rawContent) });
      if ((code === 'invalid_action' || code === 'invalid_terminal') && parseFailures < 2) {
        // 可修正的格式错误给两次反馈机会，让 Agent 按协议改正，而不是整轮终止。
        parseFailures += 1;
        pendingNotice = `上一次输出不符合工具协议（${code}），本次未执行。请只输出一个 computer_use tool_call，`
          + '字段名、类型和取值范围必须完全符合约定，不要添加任何未列出的字段。';
        return { rawContent, reasoningContent: '', actions: [{ type: 'notice', inputs: { message: pendingNotice } }] };
      }
      return reject(code, rawContent);
    }
  };
}

class HostOperator extends Operator {
  async initialize() {}

  supportedActions() {
    return ['click', 'scroll', 'wait', 'windows', 'switch_window', 'skill', 'finished', 'notice'];
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
    const hash = sha256(info.bytes);
    frameHashes.set(hash, currentFrame);
    if (frameHashes.size > 64) frameHashes.delete(frameHashes.keys().next().value);
    trace({ type: 'screenshot', at: now(), frame_id: currentFrame, width: info.width, height: info.height,
            bytes: info.bytes.length, sha256: hash });
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
      actionCount += 1;
      let reply = {};
      if (action.type === 'skill') {
        // 读取 Skill 不改变页面，也不消耗当前帧；Agent 下一轮可继续在同一画面操作。
        const name = text(action.inputs.name);
        if (!name) throw new Error('invalid_action');
        const result = await callHost('skill', { frame_id: currentFrame, name });
        reply = { name };
        pendingNotice = text(result?.content, 6000) || '该 Skill 没有可读内容。';
        trace({ type: 'tool', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame,
                method: 'skill', inputs: sanitized(inputs), ok: true, reply,
                bytes: pendingNotice.length });
        return { status: 'success' };
      }
      actionUsedOnFrame = true;
      if (action.type === 'notice') {
        pendingNotice = text(action.inputs.message, 300) || pendingNotice;
        trace({ type: 'tool', at: now(), call_id: currentCall?.id ?? null, frame_id: currentFrame,
                method: 'notice', inputs: sanitized(inputs), ok: false, error: 'invalid_output',
                feedback: pendingNotice });
        return { status: 'success' };
      }
      if (action.type === 'click') {
        const point = action.inputs.point?.raw;
        if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y)) throw new Error('invalid_action');
        await callHost('click', { frame_id: currentFrame, x: point.x, y: point.y });
      } else if (action.type === 'scroll') {
        const point = action.inputs.point?.raw;
        if (!point || !Number.isFinite(point.x) || !Number.isFinite(point.y) || !['up', 'down'].includes(action.inputs.direction)) throw new Error('invalid_action');
        await callHost('scroll', { frame_id: currentFrame, x: point.x, y: point.y, direction: action.inputs.direction === 'up' ? 'Up' : 'Down' });
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

function emitResult(outcome, reason, extra = {}) {
  if (resultEmitted) return;
  resultEmitted = true;
  trace({ type: 'result', at: now(), outcome, reason: text(reason, 600), extra: sanitized(extra),
          actions: actionCount });
  send({ type: 'result', outcome, reason, frame_id: currentFrame, ...extra });
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
      '终态示例：<tool_call>{"name":"computer_use","arguments":{"action":"terminate","outcome":"success","reason":"...","menu_box":[x1,y1,x2,y2]}}</tool_call>；<tool_call>{"name":"computer_use","arguments":{"action":"terminate","outcome":"needs_review","reason":"..."}}</tool_call>。',
      '安全边界：不要下单、支付、加购物车、领券、登录或切换门店；不要编造坐标，也不要声称截图里没有出现的内容。',
      config.output_contract,
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
