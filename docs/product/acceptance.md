# 功能保真与产品任务验收

本表是交付门槛，不是已经通过的测试报告。以旧前端 e6b7ea8 的 web/index.html、web/task-center.js、web/shared-workbench.js、web-ui 等及迁移 inventory I01–I15、当前 ce2856b 附近源码、原 Issue 为基线。每个旧功能必须在最新 UI 有明确入口或经维护者确认的退场理由；不能只靠 API 仍存在认定功能保留。

## 已知差异（必须处理，非穷尽清单）

| ID | 旧要求/入口 | 当前源码证据 | 目标 |
| --- | --- | --- | --- |
| F01 | #22 §6.2 待检查直接打回 | PendingReviewDrawer 无 onReturnToMaterials，只有 onReject/onRerun | 质量页与任务详情提供直接处置；不要求先伪造不合格结论。当前资格及活动分配保护见 D01 |
| F02 | 待检查当前/历史/诊断分区 | PendingReviewDrawer 主要概要/原因/两图 | 三分区恢复真实可用信息，历史/同步缺失真实提示 |
| F03 | 旧门店详情 drawer-review/reject/rejection undo/materials/retry | WorkerTaskDetailDrawer 主要证据与诊断；HubStoreDetailDrawer 主要当前与分配 | 当前合法任务保留处置，历史与 Worker 权限限制严格区分 |
| F04 | #21 任务详情逐店下钻与本次用户要求 | TaskDetailDrawer 逐店四列，无行激活或操作 | 逐店详情与直接处置入口、上下文返回，见 task-center |
| F05 | web/task-center.js 活动任务 pause/resume/cancel | ActiveTasksPanel 只查看详情，TaskDetailDrawer 主要改名/关闭 | 复用有权的控制，或一步定位真正控制面板，Hub 远程只读 |
| F06 | 旧门店动作时间线/调试导出 | final-migration-report 标记未迁移；WorkerTaskDetailDrawer 后续已恢复部分 | 保留 Worker 已恢复内容，核对 Hub 授权证据/历史入口；不读取未同步本机文件 |
| F07 | Hub 运行区折叠偏好 | final-migration-report 明确未迁移 | 恢复调试区合理记忆 |
| F08 | 旧设备详情→分配记录→返回 | #40 已补设备筛选跳转 | 保留并恢复原设备上下文，不重复实现 |
| F09 | 待检查标记不合格 | #36 已补质量页单条、详情、批量 | 保留原真实流程，向当前任务详情复用，不重复开发 |

资格与实现决策：D01「待检查能否直接打回」的产品决策已由 #22 §6.2 明确为可以直接打回，不得要求先标记不合格。当前后端仅对带不合格结论或 `status=pending` 的任务开放打回，并校验素材绑定、批次内唯一门店匹配及 Hub 当前有效任务；这不等同于所有待检查记录都已具备资格。实现前需用真实状态定义待检查记录到后端任务的映射，并验证活动分配、Worker 已下载任务的处理：有活动分配时须阻止操作或走明确、可审计的回收流程；成功后新分配应被阻止，已下载端的迟到结果只能留档，不能覆盖返工后的当前状态；失败时原任务与分配状态保持。此项是服务端安全与恢复验收，不是待维护者决定是否允许直接打回。详见[质量审核契约](pages/quality.md)。D02 Worker 历史处置：旧版有按钮，但 #24 目标角色矩阵限制配对 Worker 审核写请求；默认保持权限，将 Hub 处置定位到可信共享身份，不能为恢复旧入口扩大权限。如无可信映射只给清晰人工说明。D03 历史记录是否拥有当前有效任务的可信关联：需由后台真实数据决定，无则只读；不是新业务规则。

## 用户情境（以真实操作验收，不按截图好不好看评分）

| 情境 | 应完成的结果与错误边界 |
| --- | --- |
| U01 新批次导入→分析→人工选码→发布 | 导入范围明确、进度真实、选码与发布分离，未发布不可分配 |
| U02 多页选店→分配→查看设备 | 筛选与选择保持，资格/冲突可理解，成功与失败分别反馈 |
| U03 Worker 已下载→断线→继续→补传 | 本机与共享结果区分，重连不丢数据，不伪造同步 |
| U04 半自动→置顶→截图/判退 | 当前店/等待动作首屏，状态变化防错命令，关闭不终止 |
| U05 任务中心→轮次→待检查店→处置 | 无需重搜，证据与历史可查，当前有效身份校验；合法动作直达 |
| U06 待检查→判退/返工/重采→下一项 | 三种动作独立，原因与资格清楚，成功才连续处理，失败保持草稿 |
| U07 返工→同图或换图→发布 | 原店/原批次，像素规则准确，历史不变，不自动重采 |
| U08 批量审核与撤销/回收 | 单店选择、批次范围明确，部分失败保留，结果不混淆 |
| U09 设备配对/改名/回收→历史留档 | 稳定 ID，离线迟到结果正确，返回上下文 |
| U10 人工标注→历史→继续 | 画布坐标正确，建议与实际动作分离，历史只读，草稿保护 |
| U11 设置→测试→保存→导航 | 角色与保存语义、错误保留、浏览器前后退保护 |
| U12 模型用量→分组下钻→返回 | 未知/0区分，条件/范围保留，真实记录，不泄露密钥 |
| U13 更新与存储治理 | 一次授权完成下载/等待/安装/重启/重连；安全资格、回滚与保护版本 |
| U14 诊断失败→恢复 | 真实阻塞与下一步，运行时不抢占桌面 |

实施时按页面补齐所有真实按钮、隐藏菜单、弹层、角色变体，记录：原入口/行为、当前入口/行为、最终入口/状态/权限、实现证据、验收结果或经批准退场。来源为历史需求和源码，不另写重复长报告。每项必须真实可发现、可执行或说明合法阻塞，不接受「接口存在」或「测试没失败」。

浏览器验收使用合成/脱敏隔离环境，至少检查各页面首屏、正常/空/失败/禁用、长文本、窄屏与键盘，以及高频任务的连续操作、返回位置。共用 #58 组件/模式的证据；仅在实际受影响时重跑平台/OTA/真机。按 GitLab 方法记录情境是否完成、用户需额外导航/重复输入的位置、错误能否恢复，不编造实测时长和主观分数。最终无未经批准的功能丢失、危险操作歧义、无法找到的主任务或未解释的设计例外；维护者实际体验后确认。

## 功能入口完整性矩阵（F01–F09）

状态口径：**已核对** = 入口可发现、动作可执行，且已用本地自动化用例或代码路径核对；**部分** = 主路径可用但存在明确限制；**需真实环境** = 必须在隔离 Windows 采集机/共享后台实际执行，当前生产冻结未执行。不得以“接口存在”替代入口核对。

| ID | 原入口/行为 | 当前入口 | 状态 | 证据 |
| --- | --- | --- | --- | --- |
| F01 | 待检查直接打回素材清洗 | 质量审核待检查抽屉脚部「打回素材清洗」；任务中心逐店「就地处置」；Hub 门店详情「就地处置」 | 已核对 | `features/quality/PendingReviewDrawer.tsx`、`ReviewDispositionDialog.tsx`；后端资格与活动分配校验 `menu_capture/materials/rejections.py::return_blocker`；`tests/unit/test_rejections_batch.py`、`PendingReviewDrawer.test.tsx`、`ReviewDispositionDialog.test.tsx` |
| F02 | 待检查当前/历史/诊断分区 | 三分区 SegmentedControl；历史缺轮次、部分同步、动作记录被清理均如实提示 | 已核对 | `PendingReviewContent`；`PendingReviewDrawer.test.tsx` |
| F03 | 旧门店详情 drawer 的 review/reject/undo/materials/retry | Hub 门店详情保留只读证据、同步状态、二维码/最近画面与「就地处置」；历史轮次只读 | 已核对 | `features/hub/HubStoreDetailDrawer.tsx`；`HubStoreDetailDrawer.test.tsx` |
| F04 | 任务详情逐店下钻 | 任务中心逐店详情弹窗 →「就地处置」打开同一任务上下文的处置弹窗，不跳转页面 | 已核对 | `features/task-center/TaskDetailDrawer.tsx`、`ReviewDispositionDialog.tsx`；`TaskDetailDrawer.test.ts`、`ReviewDispositionDialog.test.tsx` |
| F05 | 活动任务 pause/resume/cancel | 任务中心活动任务：素材任务暂停/继续/终止；Worker 本机采集可控制；Hub 远程只读 | 部分 | `features/task-center/ActiveTasksPanel.tsx`；后端任务控制已有集成用例；前端控制按钮暂无独立组件用例 |
| F06 | 门店动作时间线/调试导出 | Worker 任务详情保留动作记录与调试包导出；Hub 不读取未同步的本机文件 | 已核对 | `features/worker/WorkerTaskDetailDrawer.tsx`；`WorkerTaskDetailDrawer.test.tsx` |
| F07 | Hub 运行区折叠偏好 | Hub 调试视图开关写入本机偏好，刷新后保留 | 已核对 | `features/hub/HubWorkbenchPage.tsx`（`hub-debug-view`） |
| F08 | 设备详情→分配记录→返回 | 设备详情「查看分配记录」带 `from=devices`，分配页显示「返回采集电脑」 | 已核对 | `features/hub/WorkerDetailDrawer.tsx`、`HubAssignmentsPage.tsx`；`HubDevicesPage.test.tsx` |
| F09 | 待检查标记不合格 | 质量审核行内、批量选中、详情抽屉均可提交；任务详情就地处置复用同一动作 | 已核对 | `features/quality/QualityPage.tsx`、`RejectPendingDialog.tsx`；`tests/unit/test_rejections_batch.py` |

### 页面模式层落地

公共列表页模板 `components/ListPageLayout.tsx` 固定「标题 → 概览 → 筛选 → 批量条 → 列表 → 分页」层级，已用于：

- 分配记录：`features/hub/HubAssignmentsPage.tsx`（含筛选、批量条、分页全部通过模板插槽）
- 素材清洗：`features/materials/MaterialsPage.tsx`（页面头与视图切换）

尚未迁移的页面及原因（不为了统一而破坏业务结构）：质量审核同页包含两条独立列表与批次范围选择；采集工作台同时承载运行面板、半自动面板与队列；任务中心以轮次列表 + 弹窗为主。后续这些页面调整结构时优先复用模板，而不是新增同类布局。

## U01–U14 验收记录

记录口径：**已核对** = 入口、主路径与错误/边界状态已按代码路径与本地自动化核对；**需真实环境** = 必须连接真实采集机/共享端验收，当前冻结未执行；**未覆盖** 明确列出，不用“测试通过”代替。

| 情境 | 主路径入口 | 已核对的边界 | 状态与说明 |
| --- | --- | --- | --- |
| U01 导入→分析→选码→发布 | 素材清洗：新建批次/批次管理 → 门店列表 → 选码 → 发布 | 导入/创建中锁定关闭；分析只关 UI 不取消后台；未发布不可分配 | 已核对（`MaterialsPage`、`ReworkPanel`、`ImportBatchDialog`、`PublishDialog`；`tests/unit/test_materials.py`、`test_material_jobs.py`） |
| U02 多页选店→分配→查看设备 | 采集工作台勾选 → 分配弹窗 → 设备详情 | 筛选/选择保留、冲突排除、逐店结果反馈 | 已核对（`HubAssignmentsPage`、`AssignDialog`、`tests/integration/test_shared_workbench.py`） |
| U03 已下载→断线→继续→补传 | Worker 工作台同步状态、待补传 | 本机与共享结果区分，不伪造同步百分比 | 已核对（`WorkerSyncDialog`、`tests/integration/test_shared_workbench.py`）；断网重连需真实环境 |
| U04 半自动→置顶→截图/判退 | ManualPanel + 远程控制 PiP | 当前店/等待动作首屏、关闭不终止、失败保留 | 部分：桌面置顶与远程声音需真实 Windows 采集机 |
| U05 任务中心→轮次→待检查店→处置 | 任务中心 → 轮次详情 → 逐店 → 就地处置 | 无需重搜门店；处置后仍停留原任务详情；证据/历史可查 | 已核对（`TaskDetailDrawer`、`ReviewDispositionDialog`） |
| U06 待检查→判退/返工/重采→下一项 | 质量审核详情三动作：标记不合格、打回素材清洗、重跑分配 | 三动作独立；原因必填；部分失败保留可重试；后端拒绝活动分配 | 已核对（`PendingReviewDrawer`、`RejectPendingDialog`、`ReviewDispositionDialog`、`tests/unit/test_rejections_batch.py`、`tests/integration/test_review_queue.py`） |
| U07 返工→同图或换图→发布 | 跨批次返工面板 | 原店/原批次、历史不变、不自动重采 | 已核对（`ReworkPanel`、`tests/unit/test_materials.py`）；像素级识别需真实截图 |
| U08 批量审核与撤销/回收 | 质量审核批量条、分配记录批量回收 | 选择范围说明、部分失败保留、逐条结果 | 已核对（`QualityPage`、`HubAssignmentsPage`、`tests/unit/test_rejections_batch.py`） |
| U09 配对/改名/回收→历史留档 | 设备页配对、备注、分配回收 | 稳定 ID；回收确认说明涉及设备与迟到结果留档 | 已核对（`WorkerDetailDrawer`、`HubAssignmentsPage`、`tests/integration/test_direct_connection.py`）；真实离线迟到需环境 |
| U10 标注→历史→继续 | 标注页画布与历史 | 坐标映射、草稿保护、历史只读 | 部分：历史只读与坐标逻辑已核对；建议/实际动作分离需真实截图验证 |
| U11 设置→测试→保存→导航 | 设置中心分类、吸底保存、测试连接 | 未保存修改拦截站内跳转、后退/前进与刷新 | 已核对（`SettingsPage`、`UnsavedChangesProvider.test.tsx`、`features/settings/format.test.ts`） |
| U12 模型用量→分组下钻→返回 | 设置/诊断入口 → 模型用量弹窗 | 分组下钻、条件保留、未知/0 区分、不泄露密钥 | 已核对（`ModelUsageDialog`、`tests/unit/test_model_usage.py`、`test_model_usage_integration.py`、`features/model-usage/format.test.ts`） |
| U13 更新与存储治理 | 设置中心软件更新 | 首屏一次授权：立即更新/空闲后更新→下载→等待采集结束→安装→重启→自动重连；页面关闭后后台继续；安装开始后不可取消；失败可重试并展示真实错误；技术细节收进高级设置。仍区分当前/目标/下载/安装，保留数量 2–5 且保护运行中与回滚目标；清理先预览后确认 | 已核对（`UpdatesPanel`、`features/updates/UpdatesPanel.test.tsx`、`tests/unit/test_updates.py`）；真实浏览器 + 隔离环境 OTA 全流程待解冻后按 Issue #32 验收条件产出，生产冻结未发布 |
| U14 诊断失败→恢复 | 诊断页只读检查 + 复制降级 | 真实阻塞与下一步；不抢占桌面 | 已核对（`DiagnosticsPage`、`tests/unit/test_environment.py`）；平台差异需真实环境 |

### 仍需真实环境或后续补强的项

1. U04 桌面置顶、远程声音与 U10 建议/实际动作分离，需要隔离 Windows 采集机实机验收；当前冻结未执行。
2. U03 断网重连补传需要真实网络切换；现有证据为共享端集成用例与代码路径，不宣称已实测。
3. F05 活动任务控制虽有后端集成用例，前端控制按钮缺少独立组件级用例；本轮未补，避免为凑数新增低价值测试。
4. 浏览器逐像素视觉基线未做；关键路径由单测、Storybook 交互断言与生产构建覆盖。
