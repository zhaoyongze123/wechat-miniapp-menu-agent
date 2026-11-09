# 正式执行修复 + 标注决策建议（已完成）

## 一、修复清单（对应用户确认的 7 个问题）

- [x] **问题 1：页面变化被误判为“没反应”**
  - `uipath_runner._evaluate_page_change`：全图平均差之外增加“点击目标周围 ROI 的差异像素占比”。
  - 阈值：ROI 内 ≥2% 像素变化即算生效；全图 ≥5% 仍算整页跳转。
  - ROI 以实际点击位置为中心，尺寸随目标高度收缩（24–40px pad，最小 48×48）。
  - 复选框勾选（约 10×10 px）现在能被识别为 `local_target_change`。

- [x] **问题 2：复选框小方框定位失效 / target_box 未换算**
  - `scene_agent._convert_coordinates` 现在同步换算 `target_box`（此前归一化值被当像素用）。
  - `_resolve_click_coordinates` 不再要求模型返回 `target_box`；缺失时用同帧 OCR 标签行补位（`_locate_consent_label_box`）。
  - 精定位结果记录 `label_box_source`（`model_target_box` / `local_ocr_label_box`）。

- [x] **问题 3：本地授权规则 100% 弃权**
  - 本地精确匹配的置信度阈值 0.8 → 0.3（与坐标解析一致），覆盖 Apple Vision 对短按钮 0.3 的输出。
  - 入口词表补齐「开始点单 / 开始点菜」。
  - 正向按钮被 OCR 漏检时，用「拒绝/不同意」同一行右侧几何推算（`_paired_consent_target`），推断点周围若有文字则放弃。
  - `choose_exact` 增加“唯一包含”兜底（如 OCR 把「1人开始点餐」并成一行），并强制排除否定文案（「不同意授权」不会被点）。
  - 授权场景识别补充「同意并继续 / 同意并授权 / 同意并开始使用」；单行正向文案（如「授权后进入」）本地唯一确认后可直接点。

- [x] **问题 4：熔断按坐标不按文字**
  - 改为按规范化文字计数，阈值 2 次；位置差 >48px 视为不同目标并重新计数。
  - 结果：同一「允许」最多点 2 次即停；同页两个同名不同位置按钮仍可继续。

- [x] **问题 5：授权顺序（先勾选再同意）**
  - `_local_prepare_action` 授权分支先找「我已了解/我已阅读并同意…」复选框并点小方框，再点「同意」。
  - `consent_checked` 状态由画面变化确认后置位，避免重复勾选。
  - 复选框标签识别排除否定语义（「不同意相关协议」不会被当成复选框标签）。

- [x] **问题 6：等待只给 2 次**
  - 改为时间预算：连续 wait 上限 5 次或累计 20 秒；每次 wait 后截图比对，页面变化即重置预算。

- [x] **问题 7：两个静态误判**
  - `_TRANSACTION_TEXT` 移除「价格」（菜单与隐私说明页都会出现）。
  - `_is_people_count_choice` 的 reason 判定放宽到「已选 2 人 / 选择 2 位」。
  - 人数页识别支持“无提示词、只有 1–12 网格”的情况；价格数字（>12）不参与判定。

## 二、人工标注：接入批处理决策，只给建议不点击

- [x] 新增 `POST /api/annotation/decide`（`AnnotationSession.decide`）
  - 顺序与批处理一致：本地就绪检查 → 本地目标定位 → 模型判断。
  - 全程只读：不调用任何点击动作。
  - 返回来源、动作、目标文案、建议区域、定位方式、定位结果、理由、OCR 候选、页面标记、失败原因。
- [x] 前端：新增「生成建议（本地 / 模型）」按钮与建议面板；建议位置以蓝色虚线框叠加，只有点「采纳建议位置」才会变成正式框选。
- [x] 归因：每次确认点击记录建议快照与人工最终位置
  - `adopted`：中心点偏差 ≤24px 视为采纳
  - `deviation_px`：偏差像素
  - `attribution`：`model_wrong` / `local_rule_wrong` / `coordinate_or_target_wrong` / `suggestion_from_other_frame` / `no_suggestion` / `none`
- [x] manifest 升级到第 2 版，新增 6 列：`suggestion_source / suggestion_label / suggestion_method / adopted / deviation_px / attribution`；旧 manifest 自动改名 `manifest-legacy-*.csv`，不会错位写入。

## 三、验证

- [x] 新增测试
  - 页面变化：复选框小变化可检出、无变化不误报、整页跳转仍检出（3 项）
  - target_box 归一化换算（1 项）
  - 授权本地规则：先勾选、勾选后跳过、低置信度正向按钮、拒绝按钮几何兜底、否定正文不误判（4 项）
  - 标注归因与 manifest 列（6 项）
- [x] 全量测试：`382 passed, 8 failed`
  - 8 项失败均为既有基线（`_automatic_staging_dir` 缺失、门店名称剪贴板接口未接入、window discovery、test_connections 预检），本次未新增失败；同时修好了原来失败的 3 项本地规则测试。
- [x] `node --check web/annotation.js` 通过
- [x] 服务重启后 `GET /api/session` 200，`POST /api/annotation/decide` 返回 400（无会话时的预期），`GET /api/annotation/state` 200

## 四、未纳入本次（独立问题）

- 微信图片发送失败、门店名 OCR 未识别、门店真实不可用：属于独立链路，未在本次改动范围。
- 截图宽度只有 310px：未调整窗口/分辨率，避免影响既有生产几何。
