import { readFileSync } from 'node:fs';

// 动作定义只从 action-schema.json 读取：字段校验、宿主方法映射、supportedActions 都由它派生。
export const SCHEMA = JSON.parse(
  readFileSync(new URL('./action-schema.json', import.meta.url), 'utf8'),
);
export const ACTIONS = SCHEMA.actions || {};
export const COORDINATE_SPACE = Number(SCHEMA.coordinate_space) || 1000;

// 带字段名的格式错误：反馈里明确指出缺哪个字段，但不替 Agent 判断页面。
export class ShapeError extends Error {
  constructor(code, field, reason) {
    super(code);
    this.field = field;
    this.reason = reason;
  }
}

export function matchesRule(rule, value) {
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

// 只校验字段白名单、必填项和基础类型；菜单矩形等几何关系仍由专用函数判断。
export function validateArgs(name, args, code = 'invalid_action') {
  const spec = ACTIONS[name];
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
  return spec;
}

function pointOf(args, ctx, required) {
  // 落点只有 coordinate 一种表达：模型给出 0..1000 的点，这里换算成窗口像素。
  const raw = args.coordinate;
  if (raw === undefined) {
    if (required) throw new ShapeError('invalid_action', 'coordinate', 'missing');
    // scroll 允许不给坐标：原地滚动，用当前截图中心。
    return { x: Math.floor(ctx.width / 2), y: Math.floor(ctx.height / 2) };
  }
  const [x, y] = raw;
  if (x < 0 || y < 0 || x >= COORDINATE_SPACE || y >= COORDINATE_SPACE) {
    throw new ShapeError('invalid_action', 'coordinate', 'value');
  }
  return { x: x * ctx.width / COORDINATE_SPACE, y: y * ctx.height / COORDINATE_SPACE };
}

function valueOf(expression, args, ctx, point) {
  if (expression === '$frame') return ctx.frameId;
  if (expression === '$x') return point.x;
  if (expression === '$y') return point.y;
  // 宿主只接受滚轮方向名；幅度由宿主换算成有界格数，符号在这里转成方向。
  if (expression === '$direction') return args.pixels > 0 ? 'Up' : 'Down';
  const indexed = /^(\w+)\[(\d+)\]$/.exec(expression);
  if (indexed) {
    const value = args[indexed[1]];
    if (!Array.isArray(value)) throw new ShapeError('invalid_action', indexed[1], 'value');
    return value[Number(indexed[2])];
  }
  return args[expression];
}

// 把模型参数翻译成宿主方法的参数；坐标仍以当前截图像素为单位，由宿主负责换到窗口坐标。
export function buildHostParams(name, args, ctx) {
  const spec = ACTIONS[name];
  if (!spec?.host) throw new ShapeError('invalid_action', 'action', 'value');
  const point = name === 'left_click' ? pointOf(args, ctx, true)
    : name === 'scroll' ? pointOf(args, ctx, false) : null;
  const params = {};
  for (const [param, expression] of Object.entries(spec.host.map || {})) {
    params[param] = valueOf(expression, args, ctx, point);
  }
  return { method: spec.host.method, params, point };
}

// supportedActions 从 schema 派生：新增动作只要写 host.method，不再单独维护列表。
export function hostMethods() {
  const methods = new Set();
  for (const spec of Object.values(ACTIONS)) {
    if (spec?.host?.method) methods.add(spec.host.method);
  }
  return [...methods];
}

// 门店名称是导航的附带观察，不是动作字段：单独剥离、单独校验，绝不进入宿主参数。
export const OBSERVATION = SCHEMA.observation || {};
export const OBSERVATION_FIELD = OBSERVATION.field || 'observed_store_name';
export const OBSERVATION_MAX_CHARS = Number(OBSERVATION.max_chars) || 40;
// 原始值超过上限直接忽略：不截断，避免把一个错误的半截店名当成确认结果。
const OBSERVATION_MAX_RAW_CHARS = OBSERVATION_MAX_CHARS * 3;
const OBSERVATION_CONTROL_CHARS = /[\u0000-\u001f\u007f]+/g;
// 剥离模型偶发附加在店名尾部的状态标注（如“（已确认）”“(已核对)”）。
const OBSERVATION_STATUS_SUFFIX = /(?:\s*[（(]\s*(?:已确认|确认|已核对|核对|当前门店|门店名称|未确认)\s*[）)])+$/u;
// 明确的泛称/占位词：不是实际品牌或分店名，必须忽略而非写入有效观察。
const GENERIC_STORE_NAMES = new Set([
  '本餐厅', '本店', '本门店', '本店铺', '本商家',
  '当前餐厅', '当前门店', '当前店铺', '当前商家',
  '餐厅', '门店', '店铺', '商家', '餐馆', '饭店', '小店',
  '点餐', '点单', '扫码点餐', '微信点餐', '堂食点餐', '自助点餐', '在线点餐',
  '堂食', '外卖', '自提', '菜单', '菜品菜单', '首页', '小程序', '微信小程序',
  '未知', '未知门店', '无', '暂无', '空', 'null', 'none', 'n/a', 'unknown',
]);
// 兜底提取：仅当模型漏写 observed_store_name 但自己在 reason 中明确陈述可见店名时生效，绝不猜测。
const REASON_STORE_NAME_PATTERNS = [
  /(?:当前)?(?:观察到|识别到|可见|显示)?(?:的)?门店名称为[“「"《]?([^”」"》，。；\n]{2,40})[”」"》]?/u,
  /当前门店为[“「"《]?([^”」"》，。；\n]{2,40})[”」"》]?/u,
  /已进入[“「"《]?([^”」"》，。；\n]{2,36}?)[”」"》]?的(?:菜品)?菜单(?:页面|页|列表)?/u,
];

export function sanitizeObservedStoreName(raw) {
  if (typeof raw !== 'string' || raw.length > OBSERVATION_MAX_RAW_CHARS) return '';
  let name = raw.replace(OBSERVATION_CONTROL_CHARS, ' ').replace(/\s+/g, ' ').trim();
  name = name.replace(/^[“「"《]+|[”」"》]+$/gu, '').trim();
  name = name.replace(OBSERVATION_STATUS_SUFFIX, '').trim();
  if (!name || [...name].length > OBSERVATION_MAX_CHARS) return '';
  if (GENERIC_STORE_NAMES.has(name.toLowerCase())) return '';
  return name;
}

function extractNameFromReason(reason) {
  if (typeof reason !== 'string' || !reason.trim()) return '';
  for (const pattern of REASON_STORE_NAME_PATTERNS) {
    const match = reason.match(pattern);
    if (!match || !match[1]) continue;
    const candidate = sanitizeObservedStoreName(match[1]);
    if (candidate) return candidate;
  }
  return '';
}

// 抽出并校验附带观察字段，返回去掉该字段的动作参数。
// observation 为 { name }、{ revoke: true }（空字符串=显式撤销）或 null（没有有效的新信息）。
export function extractObservation(args) {
  if (!args || typeof args !== 'object' || Array.isArray(args)) return { args, observation: null };
  const hasField = Object.prototype.hasOwnProperty.call(args, OBSERVATION_FIELD);
  const rest = hasField ? { ...args } : args;
  const raw = hasField ? rest[OBSERVATION_FIELD] : undefined;
  if (hasField) delete rest[OBSERVATION_FIELD];
  // 只有原始值恰为空字符串才是“显式撤销”。纯空格/换行/泛称属于无效观察，
  // 不能当成撤销，否则会在模型输出噪声时误清空此前已确认的有效名称。
  if (raw === '') return { args: rest, observation: { revoke: true } };
  if (typeof raw === 'string') {
    const name = sanitizeObservedStoreName(raw);
    if (name) return { args: rest, observation: { name } };
  }
  // 模型漏写 observed_store_name 或填了 null 时，若在 reason 里明确写出了可见门店名，兜底提取。
  if (raw === undefined || raw === null) {
    const fallback = extractNameFromReason(rest.reason);
    if (fallback) return { args: rest, observation: { name: fallback } };
  }
  return { args: rest, observation: null };
}

// 只用于留痕的文本字段缺失时补占位符：gui-plus 实测会输出 reason:null / target_name:null
// 或整段漏掉，坐标与动作本身仍然完整。这里不碰 terminate：终态理由属于业务结论，必须由模型给出。
// 单独开关，便于对比“严格必填”和“补齐留痕字段”两种策略的成功率。
export const FILL_MISSING_TEXT_FIELDS = true;

export function fillMissingTextFields(args) {
  if (!FILL_MISSING_TEXT_FIELDS || !args || typeof args !== 'object' || Array.isArray(args)) return args;
  if (args.action === 'terminate') return args;
  const spec = ACTIONS[args.action];
  if (!spec) return args;
  const next = { ...args };
  for (const key of ['reason', 'target_name']) {
    if (!(key in (spec.required || {})) && !(key in (spec.optional || {}))) continue;
    const value = next[key];
    if (value === null || value === undefined || (typeof value === 'string' && value.trim() === '')) {
      // 点击目标名只用于反馈措辞；模型给了 reason 时直接复用它，比「未提供」更能让下一轮看懂上一轮做了什么。
      const fallback = key === 'target_name' && typeof next.reason === 'string' ? next.reason.trim() : '';
      next[key] = fallback || '未提供';
    }
  }
  return next;
}
