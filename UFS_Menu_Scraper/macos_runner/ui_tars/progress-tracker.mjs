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
// 64 位 dHash 的汉明距离阈值：只用于“是否回到相似画面”的候选，不做像素级相等判断。
const SIMILAR_HAMMING = 8;

function hamming(left, right) {
  if (typeof left !== 'string' || typeof right !== 'string' || left.length !== right.length) return 64;
  let distance = 0;
  for (let index = 0; index < left.length; index += 1) {
    let value = parseInt(left[index], 16) ^ parseInt(right[index], 16);
    while (value) { distance += value & 1; value >>= 1; }
  }
  return distance;
}

// 会产生局部响应的动作：只在落点附近验证。
const LOCAL_METHODS = new Set(['click']);
// 会产生整页位移的动作：按全局 delta 验证。
const PAGE_METHODS = new Set(['scroll']);

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
    // 最近一步的结构化事实：随工具回执发给模型，不做任何“该不该换目标”的判断。
    this.lastStep = null;
    // 本店路线：每次动作后记录“从哪一帧、做了什么、到了哪一帧”，供摘要检索。
    this.route = [];
  }

  reset() {
    this.noChangeStreak = 0;
    this.sameTargetStreak = 0;
  }

  // 会改变界面的动作发出后调用；frameHash 是这次决策所依据的截图。
  // point 是模型输出的 0..1000 归一化坐标，pixel 是实际窗口像素坐标（仅供证据行与标记使用）。
  // reason 是模型对这次点击的中文说明，用于失败重试时回述其意图。
  actionSent(frameHash, { method = 'action', point = null, pixel = null, reason = '',
    frameId = null, targetName = '', box = null } = {}) {
    this.pending = { frameHash, method, point, pixel, reason, frameId, targetName, box,
      previousPoint: this.lastPoint };
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
  observe(frameHash, { delta, roiDelta, frameId = null, signature = '' } = {}) {
    const pending = this.pending;
    this.pending = null;
    if (!pending) {
      this.lastSummary = '';
      this.lastStep = null;
      return '';
    }
    const pageDelta = reading(delta);
    const roi = reading(roiDelta);
    const identical = frameHash === pending.frameHash;
    const name = label(pending.method, pending.point, pending.pixel);
    const facts = `全局变化 ${pageDelta === null ? '未知' : pageDelta}%`
      + (roi === null ? '' : `，局部变化 ${roi}%`);
    // 只记录“做了什么、到了哪、变了多少”，不记录结论。
    this.lastStep = {
      from_frame: pending.frameId,
      to_frame: frameId,
      method: LABELS[pending.method] || pending.method,
      target_name: pending.targetName || '',
      point: pending.point ? [Math.round(pending.point.x), Math.round(pending.point.y)] : null,
      box: pending.box || null,
      pixel: pending.pixel ? [Math.round(pending.pixel.x), Math.round(pending.pixel.y)] : null,
      delta: pageDelta,
      roi_delta: roi,
      page_identical: identical,
    };
    if (frameId !== null) {
      this.route.push({
        fromFrame: pending.frameId,
        toFrame: frameId,
        signature,
        method: pending.method,
        targetName: pending.targetName || '',
        point: pending.point ? [Math.round(pending.point.x), Math.round(pending.point.y)] : null,
        delta: pageDelta,
        roi,
        identical,
      });
      if (this.route.length > 60) this.route.shift();
    }

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
      // 有变化但不明显：不计入连续无变化，只在证据行保留读数。
      this.reset();
      this.lastSummary = `上一步：${name}，${facts}，轻微变化，不计连续无变化`;
      return '';
    }

    this.noChangeStreak += 1;
    // 只有点击有“重复点同一目标”的语义；滚动不参与该判断。坐标已是 0..1000 空间。
    this.sameTargetStreak = pending.method === 'click'
      ? (nearby(pending.point, pending.previousPoint, NORMALIZED_SPACE, NORMALIZED_SPACE)
        ? this.sameTargetStreak + 1 : 1)
      : 0;
    this.lastSummary = `上一步：${name}，${facts}，连续无变化 ${this.noChangeStreak} 次`;
    // 只保留累计事实；是否换入口由模型结合尝试记录自行判断。
    return '';
  }

  // 随每帧观测包一起交给 Agent 的客观证据行；没有待比较动作时为空。
  evidence() {
    return this.lastSummary;
  }

  // 同一入口在本屏的已执行尝试：只给位置和各次观察读数，不含任何建议。
  attemptsOnScreen(signature, targetName = '') {
    if (!signature) return [];
    return this.route.filter((item) => item.method === 'click' && item.point
      && hamming(item.signature, signature) <= SIMILAR_HAMMING
      && (!targetName || item.targetName === targetName));
  }

  // 是否回到过相似画面：返回最近一次相似帧号，没有则为 null。
  revisitFrame(currentFrame, signature) {
    if (!signature) return null;
    const hit = [...this.route].reverse().find((item) => item.signature
      && hamming(item.signature, signature) <= SIMILAR_HAMMING
      && item.toFrame !== currentFrame && currentFrame - item.toFrame > 2);
    return hit ? hit.toFrame : null;
  }

  // 本店执行摘要：只给可以核对的事实——回到哪一帧、这一屏试过哪些位置、各次前后有没有变化。
  // 是否换入口、是否放弃，由模型自己根据这些事实判断。
  digest(currentFrame, currentSignature) {
    if (!currentSignature || this.route.length === 0) return '';
    const similar = (item) => hamming(item.signature, currentSignature) <= SIMILAR_HAMMING;
    const lines = [];
    const revisit = this.revisitFrame(currentFrame, currentSignature);
    if (revisit !== null) lines.push(`当前画面与第 ${revisit} 帧相似。`);
    const screenClicks = this.route.filter((item) => item.method === 'click' && item.point && similar(item));
    if (screenClicks.length) {
      const seen = new Set();
      const items = [];
      for (const item of screenClicks) {
        const key = `${item.targetName}|${item.point[0]},${item.point[1]}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const readout = item.identical ? '前后像素完全相同'
          : item.delta === null ? '缺少变化读数'
            : item.delta < NO_CHANGE_DELTA && (item.roi === null || item.roi < NO_CHANGE_ROI)
              ? '前后未观察到变化' : '前后观察到变化';
        items.push(`${item.targetName ? `「${item.targetName}」` : 'left_click'}(${item.point[0]},${item.point[1]}) ${readout}`);
      }
      lines.push(`这一屏已实际点击 ${screenClicks.length} 次：${items.slice(-6).join('；')}。`);
    }
    return lines.join('\n');
  }
}
