// 无进展识别只反馈客观事实：点击已发送、画面是否变化。不做页面业务判断，也不强制终止。
const SAME_TARGET_RATIO = 0.06;
const SAME_TARGET_NOTICE = '执行反馈：最近两次点击落在同一目标附近，动作均已发送，但截图没有明显变化。'
  + '请先确认是否仍在加载、按钮是否被遮挡或缺少前置步骤；不要继续重复点击该位置，'
  + '可改点按钮中心或其他有效位置，或改用其他入口。';
const NO_STRATEGY_NOTICE = '执行反馈：已有连续 4 次点击后截图没有明显变化。当前入口可能无效，请停止重复点击，'
  + '尝试返回、切换窗口或其他菜单入口；若多种合理尝试仍无进展，请说明已尝试方式和当前阻碍并返回 needs_review。';

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
  }

  // 点击真正发出后调用；frameHash 是这次决策所依据的截图。
  clickSent(frameHash, point) {
    this.pending = { frameHash, point, previousPoint: this.lastPoint };
    this.lastPoint = point;
  }

  // 每次新截图后调用；返回给 Agent 的提示，没有则返回空串。
  observe(frameHash, width, height) {
    const pending = this.pending;
    this.pending = null;
    if (!pending) return '';
    if (frameHash !== pending.frameHash) {
      this.noChangeStreak = 0;
      this.sameTargetStreak = 0;
      return '';
    }
    this.noChangeStreak += 1;
    this.sameTargetStreak = nearby(pending.point, pending.previousPoint, width, height)
      ? this.sameTargetStreak + 1
      : 1;
    if (this.sameTargetStreak === 2) return SAME_TARGET_NOTICE;
    if (this.noChangeStreak === 4) return NO_STRATEGY_NOTICE;
    return '';
  }
}
