# 任务清单：修复“只截 1-3 页”与杜绝假成功

执行顺序固定，每完成一项立即更新状态并向用户汇报。

## T1 滚动兜底：`_capture_menu` 识别并关闭浮层

- [x] 新增 `UiPathRunner._clear_scroll_overlay(frame, selector, output_dir) -> bool`
  - `locate_close_icon(path, width/2, height*0.5)`，`score >= 0.8`
  - 位置过滤：`0.06W <= x <= 0.94W`，`0.10H <= y <= 0.92H`
  - 点击坐标换算 `x * window_width / width`、`y * window_height / height`，点击后 `noop 1.2s`
  - 写入 `clear_scroll_overlay` 调试 trace
- [x] `_capture_menu` 的 `difference < 0.7` 分支：先尝试关闭浮层；成功则重新 `scroll_capture` 并 `continue`
- [x] 单次采集最多尝试 6 次；用尽抛 `NeedsReview("滚动多次被浮层打断，菜单未确认到底", "overlay_blocked")`

验收：毛头老爹 / 偏爱炒鸡 / 满溢醉 / 唐宫 复跑后页数 > 3，且不出现“滚动无变化直接发布”。

## T2 准备阶段确定性关闭三类阻塞提示

- [x] `_local_ready_preflight` 新增：`小伙伴已在美团在线点菜|留在小程序` → 点 `留在小程序`
- [x] 新增：`待支付的订单` + `继续加购` → 点 `继续加购`
- [x] 新增：`同意此应用使用你的地理位置|申请使用你的地理位置|获取你的地理位置|需开启.*位置` → 依次点 `拒绝` / `允许` / `同意`
- [x] 三条均用 `_find_ocr_target_detail(..., min_confidence=0.3)`，定位失败返回 `None` 交给模型

验收：郭姐 / 滋啫 / 富春 / 财记 / 滩羊铺子 复跑后能继续滚动采完整菜单。

## T3 发布前兜底，杜绝假成功

- [x] `_CAPTURE_ORDER_TEXT` 增加 `下单成功|坐等开吃|订单已提交`
- [x] 弹窗守卫放宽到 `len(pages) <= 3`，关键词增加 `留在小程序|待支付的订单|同意此应用使用你的地理位置|当餐免单|会员卡办理|储值|开卡赠送`
- [x] `len(pages) <= 3` 时逐页 OCR（最多 3 页），命中即 `NeedsReview(..., "overlay_blocked")`

验收：细记港九 / 辣不怂 / 小厨娘 → `needs_retry / existing_order`；云小孃 / 汉唐 → 不再以单页发布。

## T4 单元测试

- [x] `tests/test_uipath_runner.py` 新增 3 条：订单页 `existing_order`、2 页浮层 `overlay_blocked`、顶部关闭键不触发 `_clear_scroll_overlay`
- [x] `pytest tests/ -q` 基线保持 8 failed / 420 passed / 1 skipped，新增用例全绿

## T5 重启服务并回归

- [ ] `launchctl kickstart -k gui/$(id -u)/com.yipu.menu-capture`
- [ ] 回归 8 家：E00397、E00724、E00514、ZA0077764、ZA0076852、E01516、ZA0077145、E01453
- [ ] 复跑剩余 18 家 1-3 页门店
- [ ] 输出分布表：captured / needs_review / needs_retry / 不合格；单列仍 ≤3 页门店与原因
