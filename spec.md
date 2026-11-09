# 修复“只截 1-3 页”与杜绝假成功

## 1. 问题与证据

输出目录 `/Users/mac/项目/意谱科技/小程序截图项目/输出/截图输出` 中，103 家 `captured` 门店有 **26 家只有 1-3 张截图**，却仍被发布为成功（假成功）。

对 26 家首页做本地 OCR 后，根因分为五类：

| 类型 | 表现 | 代表门店 |
| --- | --- | --- |
| A 滚动被浮层吞掉 | `_capture_menu` 把“滚动后画面不变”当成“已到底”，直接发布 | 毛头老爹、偏爱炒鸡、三酉星、满溢醉 |
| B 平台/订单提示浮层 | “小伙伴已在美团在线点菜…前往在线点/留在小程序”、“您有一笔待支付的订单…继续加购/立即支付” | 郭姐 E00397、滋啫 E00724、富春 E00514、财记 |
| C 地理位置授权 | “同意此应用使用你的地理位置 / 拒绝 / 允许” | 滩羊铺子 ZA0077764 |
| D 协议弹窗 | 《隐私协议》《用户服务协议》遮挡且吞掉滚动 | 云小孃 ZA0076852、汉唐 E01516 |
| E 订单详情页 | “下单成功，坐等开吃 / 订单编号” | 细记港九 ZA0077145、辣不怂 ZA0077890、小厨娘 ZA0078128 |
| F 全屏海报浮层 | “暑期海鲜节”等无文字关闭键的海报 | 唐宫 E01453 |

其中 A 是假成功的核心：浮层吞掉滚动 → 两次画面一致 → 被当作菜单到底。

## 2. 代码现状

- `menu_capture/uipath_runner.py`
  - `_capture_menu`（约 5283 行）：相邻帧 `image_difference < 0.7` 直接 `return pages`，没有任何“是不是被浮层挡住”的判断。
  - `_local_ready_preflight`（约 3643 行）：已处理会员充值弹窗、优惠券浮层、协议弹窗，但**没有**美团在线点菜提示、待支付订单提示、地理位置授权。
  - `_verify_captured_menu`（约 5248 行）：订单页正则只有 `订单明细|待支付|已下单|订单编号`，未覆盖“下单成功，坐等开吃”；弹窗守卫仅在 `len(pages) <= 1` 时生效，且缺少本轮新增的浮层关键词。
  - 已有可复用能力：`locate_close_icon(path, anchor_x, anchor_y)`（整页找唯一圆形 X，返回 `center_image_px`/`score`）、`_find_ocr_target_detail(rows, image_size, label, min_confidence=0.3)`（3x OCR 坐标自动 /3 还原）、`_sheet_close_point`。
- `menu_capture/page_state.py`：`has_menu_evidence(rows)` 以价格锚点判断菜单可见。
- 服务当前 PID 82792，**本轮修复尚未重启加载**。

## 3. 变更设计（精确到函数）

### 3.1 `menu_capture/uipath_runner.py`

**(1) `_capture_menu`：滚动无变化时先尝试关浮层，再判断到底**

- 在 `if difference is not None and difference < 0.7:` 分支内，先调用新方法 `self._clear_scroll_overlay(frame, selector, output_dir)`：
  - 用 `locate_close_icon(Path(frame["path"]), frame["width"] / 2, frame["height"] * 0.5)` 以画面中心为锚找关闭键；
  - 仅接受 `score >= 0.8`，且中心点满足 `6% <= x <= 94%`、`10% <= y <= 92%`（排除微信窗口关闭键与底部导航）；
  - 命中后按 `x * window_width / width`、`y * window_height / height` 换算成窗口坐标点击，`noop 1.2s`，返回 `True`。
- 返回 `True` 时不 `return pages`，而是立即再发一次 `scroll_capture` 并 `continue`（当前这帧本来就没保存，不会产生重复页）。
- 单次采集最多尝试 6 次，且同一坐标只点一次（菜单里的圆形图标可能被误识别）；若已采到 ≥4 页则发布现有页数，只有 ≤3 页时抛 `NeedsReview("滚动多次被浮层打断，菜单未确认到底", "overlay_blocked")`。
- 追加 `self._trace("clear_scroll_overlay", ...)` 记录命中的关闭键坐标与分数。

**(2) `_local_ready_preflight`：确定性关闭三类阻塞提示**

在现有 `ready_text` 分支中按顺序新增：

1. `小伙伴已在美团在线点菜` 或 `留在小程序` → OCR 精确定位 `留在小程序` 点击（`target_label="留在小程序"`，`overlay_visible=True`）；定位不到返回 `None` 交给模型。
2. `待支付的订单` 且 `继续加购` → 点击 `继续加购`（`_apply_scene_action` 的 `back_to_menu` 白名单已含“继续加购”，不会误判交易）。
3. `同意此应用使用你的地理位置|申请使用你的地理位置|获取你的地理位置|需开启.{0,6}位置` → 依次找 `拒绝`、`允许`、`同意`，命中即点（优先拒绝，不授予定位）。

三条均走 `_find_ocr_target_detail(..., min_confidence=0.3)`，坐标已是 image px。

**(3) `_verify_captured_menu`：发布前兜底，杜绝假成功**

- `_CAPTURE_ORDER_TEXT` 扩充为 `订单明细|待支付|已下单|订单编号|下单成功|坐等开吃|订单已提交`。
- 弹窗守卫从 `len(pages) <= 1` 放宽到 `len(pages) <= 3`，关键词增加：
  `留在小程序|待支付的订单|同意此应用使用你的地理位置|当餐免单|会员卡办理|储值|开卡赠送`（保留原有协议/优惠券词）。
- 新增：`len(pages) <= 3` 时逐页 OCR（最多 3 页），任一页命中上述关键词 → `NeedsReview(..., "overlay_blocked")`。
- 全部命中不到才允许发布。

### 3.2 `tests/test_uipath_runner.py`

新增 3 条纯函数/静态方法测试：

1. `_verify_captured_menu` 对“下单成功，坐等开吃”抛 `NeedsRetry(existing_order)`（用假 ocr_reader + 临时目录）。
2. `_verify_captured_menu` 对 2 页且首页含“小伙伴已在美团在线点菜”抛 `NeedsReview(overlay_blocked)`。
3. `_clear_scroll_overlay` 的坐标过滤：构造关闭键在顶部 5% 的图，断言不点击（返回 False）。

## 4. 验证步骤

1. `.venv/bin/python -m pytest tests/ -q`，基线必须是 **8 failed / 417 passed / 1 skipped**（既有失败与本次无关），新增 3 条通过。
2. `launchctl kickstart -k gui/$(id -u)/com.yipu.menu-capture`，确认 `http://127.0.0.1:8765/` 可用、令牌刷新。
3. 先跑 8 家代表回归：
   - E00397 郭姐、E00724 滋啫（B 类）
   - E00514 富春（B 类待支付）
   - ZA0077764 滩羊铺子（C 类）
   - ZA0076852 云小孃、E01516 汉唐（D 类）
   - ZA0077145 细记港九（E 类，应 `needs_retry/existing_order`）
   - E01453 唐宫（F 类）
   - 判定标准：B/C/D/F 应完成多页且无浮层；E 应 `needs_retry`，不得 `captured`。
4. 再跑剩余 18 家 1-3 页门店，逐家核对页数 ≥ 4 或状态合理。
5. 全量重跑后输出一张分布表：`captured / needs_review / needs_retry / 不合格`，并单独列出仍 ≤3 页的门店及原因。

## 5. 不做的事

- 不改门店身份校验（`_store_matches` / `_identity_mismatch`）与本轮之前的 store_mismatch 口径。
- 不把扫码进订单页判成门店不合格，仍是 `needs_retry / existing_order`。
- 不删除、不覆盖已有成功输出（除发布前 `_verify_captured_menu` 拦截导致重新采集）。
- 不新增独立文档或临时脚本；修复全部落在现有文件内。

## 6. 风险与边界

- `locate_close_icon` 可能把海报里的圆形装饰误判为关闭键：用 `score >= 0.8` + 位置带 + 低饱和圆底过滤，并且只在“滚动已无变化”时触发，误点最多导致该店 `needs_review`，不会产生假成功。
- 点击“继续加购”属于回菜单导航，不提交订单（`back_to_menu` 白名单已有）。
- 若某店菜单确实只有 1 页，`_verify_captured_menu` 只有在出现浮层/协议/订单关键词时拦截，纯短菜单仍正常发布。
