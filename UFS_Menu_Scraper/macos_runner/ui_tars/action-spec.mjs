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
  // 点与框是互斥的两种表达，同时出现说明模型对落点没有把握，交回格式重试而不是猜一个。
  if (args.coordinate !== undefined && args.target_box !== undefined) {
    throw new ShapeError('invalid_action', 'target_box', 'value');
  }
  if (args.target_box !== undefined) {
    const [x1, y1, x2, y2] = args.target_box;
    if (x2 <= x1 || y2 <= y1
        || [x1, y1, x2, y2].some((n) => n < 0 || n > COORDINATE_SPACE)) {
      throw new ShapeError('invalid_action', 'target_box', 'value');
    }
    // 模型只圈出控件范围，落点由框中心决定：不再要求它猜一个精确像素。
    const cx = (x1 + x2) / 2;
    const cy = (y1 + y2) / 2;
    return { x: cx * ctx.width / COORDINATE_SPACE, y: cy * ctx.height / COORDINATE_SPACE };
  }
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
