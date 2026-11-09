# 正式执行失败归因与修复规格

## 1. 结论先行

用 2026-11-06 的真实运行数据（`data/tasks.sqlite3` 的 `model_calls`、`run_actions`、`tasks`，以及 `data/action-evidence` 的逐动作截图）核对后：

1. **不是模型选错，也不是模型服务不可用。** 当天 226 次模型调用全部 `status=success`，平均 6490ms，最长 13739ms，43 个任务用过模型。没有任何一次 HTTP/解析层面的失败。
2. **坐标换算不是今天的主因。** 11-06 坐标类失败 0 次；11-05 有 4 次（3 次“模型坐标超出当前截图”、1 次“归一化坐标超出协议范围”），占比很小。
3. **真正的失败主因是“点到了不可点击的同名文字/被遮挡入口，然后反复点同一个”。**
   - 11-06 共完成 48 个任务：21 成功、24 真实失败、3 条人工标注记录。
   - 24 条失败里：同一目标连续无变化 6、连续等待 4、门店真实不可用 4、微信图片发送失败 2、门店名 OCR 未识别 2、小窗不唯一 1、UiPath 环境 1、系统通知 1、准备动作上限 1、ready 证据不足 1、人数页未确认 1。
   - 其中 10 条（无变化 + 等待）都是同一个交互闭环问题，占失败的 42%。
4. **物证（干饭湘 ZA0071548，10:36–10:37）：** 点击“点餐”后弹出微信订阅消息弹窗，弹窗内真实按钮是“拒绝 / 允许”；弹窗上层还叠了一层引导浮层，浮层里也印着“允许”并带箭头。OCR 候选里因此出现**两个“允许”**，模型/本地规则选到了引导浮层里的那个，点击无效 → 再选同一个 → 循环 16 次才被熔断。同一页面点击被弹窗遮挡的“点餐”同样无效。
5. 所以：**模型识别页面的能力足够，瓶颈在执行侧的三件事**——同名目标的去重与可点击性判断、点击后的效果验证、等待预算。

## 2. 现状证据

| 指标 | 数值 |
| --- | --- |
| 11-06 模型调用 | 226 次，成功率 100%，均值 6.49s |
| 11-06 完成任务 | 48：captured 21 / needs_review 24 / 标注 3 |
| 11-06 坐标类失败 | 0 |
| 11-05 坐标类失败 | 4（越界 3、归一化超范围 1） |
| 失败最多的动作序列 | 荣食方：点餐 → 允许 ×16；干饭湘：点餐 → 允许 ×2，重试 3 轮 |

相关代码现状：

- `menu_capture/target_candidates.py::build_text_candidates` 只按文字长度/尺寸过滤，不区分“按钮”与“引导文案”。
- `menu_capture/uipath_runner.py::_local_prepare_action` 授权分支用 `choose_exact(...)` 要求文字唯一；出现两个“允许”时直接放弃，交给模型猜。
- `menu_capture/uipath_runner.py::_prepare_until_ready` 的重复点击熔断（`_same_prepare_action_target`）只有“标签 + 坐标 ≤24px”才算同一目标，坐标轻微抖动就不计数，因此可能点 10 多次。
- `_is_authorization_target`（约 L3071）已定义但**从未被调用**，授权目标的专项保护没有接上。
- “连续等待两次”固定按次数停止（`model_wait_count > 2`），没有时间预算，慢加载页会被过早判死。

## 3. 目标与非目标

目标：

- 同一文字目标在“点击后无变化”的情况下，最多再点 1 次就必须停止。
- 授权/订阅弹窗中，只有“允许”与“拒绝”成对出现时才点“允许”；不点教程/引导浮层里的“允许”。
- 点完授权按钮后，如果弹窗仍在，立即停止并给出可读原因，不再空转。
- 等待页给出时间预算，而不是固定两次。
- 保留现有坐标越界拒绝逻辑，不做危险的自动纠偏。

非目标：

- 不更换模型、不改提示词、不改 `enable_thinking`（数据不支持“模型选错”）。
- 不改窗口宽度/截图分辨率（310px 宽偏小是事实，但改动风险大，另立需求）。
- 不处理门店真实不可用、微信发送失败、门店名 OCR（这些是独立问题，见 `tasks.md` 的后续项）。

## 4. 设计（decision-complete）

### 4.1 过滤引导浮层文字候选

文件：`menu_capture/target_candidates.py`

- 新增模块级正则：

```python
_GUIDE_TEXT = re.compile(r"点击|长按|请点击|按下|指引|教程|提示框|箭头|戳这|点这里")
```

- 在 `build_text_candidates` 的候选循环中，先做“精确按钮豁免”：`_compact(text)` 若完全等于 `_BUTTON_HINTS` 中某一项（如“允许”“同意”“确定”），照常保留；否则若 `_GUIDE_TEXT.search(text)` 命中，则 `continue`。
- 目的：丢掉“点击 允许”“①点击此处”这类引导文案，保留独立按钮“允许”。
- 不做：不按坐标猜测浮层区域，避免误删真实按钮。

### 4.2 授权按钮必须与“拒绝”成对

文件：`menu_capture/uipath_runner.py`

- 新增模块级 helper：

```python
def consent_button_pairs(rows, image_size, positive_labels, negative_labels=("拒绝", "不同意", "暂不", "取消")) -> list[dict]:
    """在 image px 空间返回与否定按钮同一行、且位于其右侧的正向按钮框。"""
```

判定：两行中心 y 差 ≤ 24px；正向框中心 x 比否定框中心 x 大 0–120px；两者都落在图内。返回正向框（含 `center_image_px`）。

- `_local_prepare_action` 的 `choose_exact` 增加可选参数 `filter_fn=None`。授权分支调用时传入 `filter_fn`：先用 `_find_ocr_target_detail` 拿到 `candidates`（ambiguous 时已包含多个），再取 `consent_button_pairs` 命中的框。
- 若过滤后恰好 1 个 → 正常返回 `SceneDecision(click, x/y=该框中心, consent_required=...)`；若 0 个 → 返回 `None`，并在 `_last_target_resolution` 记录 `reason="consent_button_pair_not_found"`。
- 后续由 4.3 的熔断兜底，避免模型继续点浮层文字。

### 4.3 同标签无效果熔断（按文字，不按坐标）

文件：`menu_capture/uipath_runner.py::_prepare_until_ready`

- 循环外新增 `no_effect_labels: dict[str, int] = {}`。
- 在 `page_check` 判定 `changed=False` 后：

```python
label_key = self._normalize_ocr_text(decision.target_label)
if label_key and not re.fullmatch(r"\d{1,2}(?:人|位)?", label_key):
    no_effect_labels[label_key] = no_effect_labels.get(label_key, 0) + 1
    if no_effect_labels[label_key] >= 2:
        raise NeedsReview("同一文字目标连续两次点击无明显变化；可能选中了提示文字或被遮挡入口")
```

- 页面发生 `changed=True` 时清空 `no_effect_labels`。
- 保留原 `_same_prepare_action_target` 判定不动，作为更严格的补充。

### 4.4 授权点击结果验证

文件：`menu_capture/uipath_runner.py::_prepare_until_ready`

- 点击前记录 `pending_consent_click = decision if self._is_authorization_target(decision) else None`（复用现有未调用的 L3071 方法）。
- 下一轮 OCR 结果拿到后（`ocr_result` 已计算处）判断：

```python
if pending_consent_click is not None:
    text = " ".join(self._normalize_ocr_text(str(row.get("text", ""))) for row in ocr_result[1])
    if ("拒绝" in text and "允许" in text) or ("申请" in text and "发送一次" in text):
        raise NeedsReview("授权/订阅弹窗点击后仍存在；已停止重复点击，请人工确认")
    pending_consent_click = None
```

- 只在“拒绝+允许”成对或订阅文案仍在时触发，避免把普通页面里的“允许”误判。

### 4.5 等待预算改为时间制

文件：`menu_capture/uipath_runner.py::_prepare_until_ready`

- 新增 `wait_started_at: float | None = None`。
- `decision.action == "wait"` 时：首次记录 `wait_started_at = time.monotonic()`；停止条件从 `model_wait_count > 2` 改为：

```python
if model_wait_count > 4 or (wait_started_at and time.monotonic() - wait_started_at >= 20):
    raise NeedsReview("页面连续等待超过时间预算仍未形成可执行判断；已停止并保留复核")
```

- 页面发生变化时 `model_wait_count = 0`、`wait_started_at = None`。

### 4.6 不改动的部分

- `menu_capture/scene_agent.py` 的坐标协议与越界拒绝保持原样；11-05 的 4 次越界属于模型偶发输出，现有“拒绝并复核”是正确安全行为。
- `config.local.json` 的 `model`、`auto_authorize=true` 不变。

## 5. 风险与约束

- `_GUIDE_TEXT` 可能误过滤真实按钮（如“点击查看”）；用 `_BUTTON_HINTS` 精确豁免 + 单测覆盖。
- 成对过滤在“没有拒绝按钮”的普通授权页会返回 `None`，仍交给模型决策；由 4.3/4.4 兜底，不会无限循环。
- 等待预算放宽会增加单店最长耗时；仍受 `max_agent_steps=18`、`max_task_seconds=900` 约束。

## 6. 验收标准

1. `pytest tests/test_target_candidates.py` 通过，新增用例：
   - 同一帧存在“点击 允许”和独立“允许”时，只保留后者。
2. `pytest tests/test_uipath_runner.py -k "consent or repeat or wait or authorization"` 通过，新增用例：
   - 授权弹窗“拒绝+允许”成对 → 选中“允许”按钮中心。
   - 只有引导浮层“允许”、没有“拒绝” → 不产生点击决策。
   - 同一标签坐标抖动两次且无变化 → 第 2 次后 `NeedsReview`。
   - 点击“允许”后弹窗仍在（OCR 仍含拒绝+允许）→ `NeedsReview`，不执行第二次点击。
   - 连续 wait 2 次后页面变化 → 继续；连续 wait 5 次 → 停止。
3. 手工回归 3 家店：干饭湘 `ZA0071548`、荣食方 `ZA0071937`、广州乡下饭庄 `ZA0071668`。
   - 通过：不再出现同一“允许”点击 >2 次；结果要么 `captured`，要么在 20s 内以明确原因 `needs_review`。
4. 不引入新的全量测试失败（`tests/test_uipath_runner.py` 的既有失败项保持基线，不在本次修复范围）。
