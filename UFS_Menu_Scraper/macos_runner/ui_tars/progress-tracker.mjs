// 无进展识别只反馈客观事实：动作已发出、整页变化多少、落点附近变化多少。不做页面业务判断，也不强制终止。
// 全局 delta 会被大面积静态内容稀释，不能单独当作“动作是否生效”的判据；
// 因此点击同时看落点邻域的 roiDelta，输入类动作只留证据、不参与无进展统计。
export const SAME_TARGET_RATIO = 0.06;
// delta < NO_CHANGE_DELTA：整页视为没动（光标、动画、加载噪声都在这个区间内）。
export const NO_CHANGE_DELTA = 1;
// delta >= CHANGE_DELTA：整页已响应，清零连续计数。
export const CHANGE_DELTA = 3;
// 点击落点邻域达到该值即视为控件已响应；ROI 已按原分辨率裁剪，24px 控件的选中态约占 1% 以上。
export const ROI_CHANGE_DELTA = 1;
// 邻域低于该值才认为控件本身没有动。
export const NO_CHANGE_ROI = 0.4;
// 连续无变化的操作次数达到该值时，提示换入口/换策略。
export const NO_STRATEGY_STREAK = 3;

// 会产生局部响应的动作：只在落点附近验证。
const LOCAL_METHODS = new Set(['click']);
// 会产生整页位移的动作：按全局 delta 验证。
const PAGE_METHODS = new Set(['scroll']);

const NO_STRATEGY_NOTICE = `执行反馈：已有连续 ${NO_STRATEGY_STREAK} 次操作后落点附近和整页都没有明显变化。当前入口可能无效，请停止重复操作，`
  + '尝试返回、切换窗口或其他菜单入口；若多种合理尝试仍无进展，请说明已尝试方式和当前阻碍并返回 needs_review。';
const SLIGHT_CHANGE_NOTICE = '执行反馈：上一步已执行，画面只有轻微变化，可能仍在加载或只是光标/动画。'
  + '请先确认页面是否已经响应；不要重复点击同一位置。';

const LABELS = { click: 'left_click' };
// 与模型输出保持同一坐标空间：证据行先给 0..1000 归一化值，像素只作附注。
const NORMALIZED_SPACE = 1000;

function label(method, point, pixel) {
  const name = LABELS[method] || method;
  if (!point) return name;
  const normalized = `${name}(${Math.round(point.x)},${Math.round(point.y)})`;
  return pixel ? `${normalized}，像素(${Math.round(pixel.x)},${Math.round(pixel.y)})` : normalized;
}

function reading(value) {
  if (typeof value !== 'number' || !Number.isFinite(value)) return null;
  return Math.round(value * 10) / 10;
}

function nearby(left, right, width, height) {
  if (!left || !right) return false;
  return Math.abs(left.x - right.x) <= width * SAME_TARGET_RATIO
    && Math.abs(left.y - right.y) <= height * SAME_TARGET_RATIO;
}

export class NoProgressTracker {
  constructor() {
    this.pending = null;
    this.lastPoint = null;
    this.noChangeStreak = 0;
    this.sameTargetStreak = 0;
    this.lastSummary = '';
  }

  reset() {
    this.noChangeStreak = 0;
    this.sameTargetStreak = 0;
  }

  // 会改变界面的动作发出后调用；frameHash 是这次决策所依据的截图。
  // point 是模型输出的 0..1000 归一化坐标，pixel 是实际窗口像素坐标（仅供证据行与标记使用）。
  // reason 是模型对这次点击的中文说明，用于失败重试时回述其意图。
  actionSent(frameHash, { method = 'action', point = null, pixel = null, reason = '' } = {}) {
    this.pending = { frameHash, method, point, pixel, reason, previousPoint: this.lastPoint };
    if (point) this.lastPoint = point;
  }

  // 兼容旧调用点：点击就是 actionSent 的一种。
  clickSent(frameHash, point, pixel = null) {
    this.actionSent(frameHash, { method: 'click', point, pixel });
  }

  // 适配层用：本次待观测动作是否点击、以及需要在截图上标记的像素落点。
  get pendingMethod() {
    return this.pending?.method || '';
  }

  get pendingPixel() {
    return this.pending?.pixel || null;
  }

  // 适配层用：本次待观测点击的模型理由；重试提示里回述它，帮助模型区分“落点偏了”和“目标选错”。
  get pendingReason() {
    return this.pending?.reason || '';
  }

  // 每次新截图后调用。delta 是整页差异，roiDelta 是上一次点击落点附近的差异，均为百分比。
  observe(frameHash, { delta, roiDelta } = {}) {
    const pending = this.pending;
    this.pending = null;
    if (!pending) {
      this.lastSummary = '';
      return '';
    }
    const pageDelta = reading(delta);
    const roi = reading(roiDelta);
    const identical = frameHash === pending.frameHash;
    const name = label(pending.method, pending.point, pending.pixel);
    const facts = `全局变化 ${pageDelta === null ? '未知' : pageDelta}%`
      + (roi === null ? '' : `，局部变化 ${roi}%`);

    // 输入、按键、切窗口在语义上不保证画面变化：只留证据，不累计无进展。
    if (!LOCAL_METHODS.has(pending.method) && !PAGE_METHODS.has(pending.method)) {
      this.lastSummary = `上一步：${name}，${facts}（不参与无进展统计）`;
      return '';
    }

    // 点击优先看落点邻域；整页变化同样算响应（弹窗、跳页、新窗口）。
    const localResponse = roi === null ? null : roi >= ROI_CHANGE_DELTA;
    const pageResponse = !identical && (pageDelta === null || pageDelta >= CHANGE_DELTA);
    if (localResponse === true || pageResponse) {
      this.reset();
      this.lastSummary = `上一步：${name}，${facts}，连续无变化 0 次`;
      return '';
    }
    if (localResponse === null && LOCAL_METHODS.has(pending.method)) {
      // 点击没有局部读数（窗口尺寸变化、切窗口等）时不下“无进展”结论，避免误判小控件。
      this.reset();
      this.lastSummary = `上一步：${name}，${facts}，缺少局部读数，本次不计无进展`;
      return '';
    }

    const slight = (!identical && pageDelta !== null && pageDelta >= NO_CHANGE_DELTA)
      || roi >= NO_CHANGE_ROI;
    if (slight) {
      // 有变化但不明显：不累计无进展次数，只提醒确认页面是否响应。
      this.reset();
      this.lastSummary = `上一步：${name}，${facts}，轻微变化，不计连续无变化`;
      return SLIGHT_CHANGE_NOTICE;
    }

    this.noChangeStreak += 1;
    // 只有点击有“重复点同一目标”的语义；滚动不参与该判断。坐标已是 0..1000 空间。
    this.sameTargetStreak = pending.method === 'click'
      ? (nearby(pending.point, pending.previousPoint, NORMALIZED_SPACE, NORMALIZED_SPACE)
        ? this.sameTargetStreak + 1 : 1)
      : 0;
    this.lastSummary = `上一步：${name}，${facts}，连续无变化 ${this.noChangeStreak} 次`;
    // 同一目标的无变化提示由适配层按“第一次修落点、第二次换目标”分阶段给出，
    // 这里只负责累计事实，避免两套文案互相冲突。
    if (this.noChangeStreak === NO_STRATEGY_STREAK) return NO_STRATEGY_NOTICE;
    return '';
  }

  // 随每帧观测包一起交给 Agent 的客观证据行；没有待比较动作时为空。
  evidence() {
    return this.lastSummary;
  }
}
