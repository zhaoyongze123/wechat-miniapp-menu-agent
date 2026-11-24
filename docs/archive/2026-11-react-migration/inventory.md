# #24 Phase 0 功能与前端源码清单

基线 `e6b7ea82079b45f1720ea6dec6411bc29ed5276b`，工作区 `D:\menu-agent-react`，分支 `feat/24-react-migration`。下文源码路径以 `UFS_Menu_Scraper/macos_runner/` 为前缀，行号对应此基线。证据来自静态检查，现场浏览器等效性在对应实施阶段验收。[Issue #24](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/issues/24)

## 路由、页面、弹窗与业务操作

| 编号 / 入口 | 当前可见能力与隐含行为 | 代码锚点与 React 承接 |
| --- | --- | --- |
| I01 全站应用外壳 | 侧栏、页标题、连接/同步状态、Toast、全局任务快捷浮层、深链接和浏览器返回。未知 hash 回到工作台。Hub 隐藏诊断/标注，Worker 隐藏素材/审核。切页关闭队列并切换审核子标签，角色取回后复核可见性。 | `web/index.html:145–165, 833–876, 2345–2367`；AppShell、角色路由守卫、任务快捷入口。 |
| I02 `#overview` Hub 调度 | 当前快照/全部批次范围与批次筛选，状态、排序、关键词、摘要和服务端分页，选中 ID 跨页协调、全选本页、队列移除/清空。分配确认含设备、任务、版本、冲突，成功后移除本次已分配选项。设备简表、门店详情、导入素材跳转、调试视图和折叠偏好。 | `index.html:166–229, 1079–1175, 1250–1492, 2397–2420`；`shared-workbench.js`；HubQueuePage、AssignmentDialog、跨页选择模型。 |
| I03 `#overview` Windows 执行 | 配对引导、已下载任务、本机批次与筛选、排序/勾选和队列顺序、全自动/半自动切换、命名、开始、暂停、恢复、终止、最新轮次和进度。半自动开始截图/判不合格/跳过、原因草稿和 F8/F9。离线可用队列、待补传/历史同步对话框；暂停的全自动轮次在受控条件下可切半自动。 | `index.html:166–226, 290, 1110–1241, 2368–2499`；`shared-workbench.js`；WorkerQueuePage、CaptureControls、ManualCapturePanel、WorkerSyncDialog。旧的扫目录与独立启动按钮只做基线审计，见角色矩阵。 |
| I04 设备与分配入口 | 工作台、设置和任务中心的设备/分配入口共用 `shared-dialog`。配对码生成、到期提示，设备列表、查看详情、改名/备注、解除配对、移除，按设备/状态/关键词分页分配记录，单项/批量回收、回收再分配、展开结果文件。进入详情带设备预选和返回位置。 | `index.html:228–290, 573–598`，`shared-workbench.js:1–640`；DeviceList、DeviceDetailDrawer、PairingDialog、AssignmentRecords、共享 AssignmentDialog。 |
| I05 `#task-center` | 任务名称、批次范围/版本、状态、结果摘要、开始时间及操作。素材分析/采集混排、类型/状态/搜索、统计、页码、跨批次展开、详情和改名。详情需保留原门店名/技术信息，区分旧记录未留存逐店结果；Hub 设备活动显示已上报摘要，Worker 回到本轮。 | `index.html:357–388, 2130–2240`；`web-ui/src/TaskCenter.vue`、`components/TaskDetailDialog.vue`；TaskCenterPage、RunDetailDialog、RunRenameForm、DeviceActivity。 |
| I06 运行面板与快捷浮层 | `task-center.js` 独立轮询活动任务、打开快捷面板/详情、素材任务暂停恢复取消、采集暂停恢复终止，以及失败时跳转重新分析入口。可进入任务中心，查看时带运行或作业身份。打开详情期间刷新，关闭/隐藏清理。 | `web/task-center.js:15–279`；useActiveTasks 与同一详情容器复用，任务中心默认收起。控制按钮按任务种类、状态和角色。 |
| I07 `#rejections` 不合格 | 概览、批次/来源/人工确认/原因/关键词筛选、翻页、刷新、Excel 导出、门店访问标记。详情显示素材/采集来源、原名、原因、截图/候选证据、前后门店；人工确认/取消确认、撤销不合格、打回素材、重新采集语义不同。素材来源的修复跳转携带原批次和门店。 | `index.html:307–332, 1710–1950`；RejectionList、OutcomeEvidenceDrawer、ExportAction、ReviewDispositionForm。 |
| I08 `#review-queue` 待检查 | 批次多选、关键词、原因类型桶及计数、分页、门店选择、重跑、批次通过/撤销通过。基线会从勾选门店推断批次范围，存在跨整批操作误解。Hub 重跑 UI 转分配。待检查与不合格保持分离。 | `index.html:333–356, 1954–2128`；PendingReviewList、独立 BatchApprovalPicker 和 StoreSelectionBar、受控重采交接。 |
| I09 跨页门店抽屉 | 当前结果/历史/诊断证据，门店前后导航、原始二维码、最近错误画面、菜单截图/文件、人工标注历史、操作时间线与翻页、日志导出/清理。抽屉移动到 `body`，防止随旧 section 隐藏；返回焦点，保留筛选与目标。 | `index.html:306–309, 874–876, 1492–1736`；共享 StoreDetailDrawer、EvidenceViewer、统一 OverlayHost。 |
| I10 `#materials` 三栏选码 | 选批次/门店、状态筛选、上一/下一家、候选/原图同步、缩放/旋转、指针手动框选、切换门店清理草稿。系统推荐、选中、确认、跳过/不合格原因、状态恢复、补图追加/替换、门店重分析。 | `index.html:398–492`；`materials.js:260–708, 1110–1184`；MaterialWorkspace、StorePicker、SourceImageCanvas、QrCandidateList、StoreDecisionForm。 |
| I11 素材导入与分析 | 新批次名称/说明、本地服务路径或浏览器目录/文件上传、批次/job 进度、暂停/恢复/取消、失败重试、按门店批量重分析与作业明细。放弃导入时撤销临时上传，部分失败有恢复信息。 | `materials.js:80–219, 687–999`；MaterialImportDialog、UploadController、MaterialJobDetails。 |
| I12 发布、全部批次返工、批次管理 | 发布快照、版本/说明、清除当前选择，全部批次返工筛选与多选，按各自原批次发布。批次编辑/改名、回收站、恢复、永久删除、重新分析、不合格来源入口及操作冲突。 | `materials.js:277–384, 915–1168`；PublishDialog、ReworkPageDialog、BatchManagementDialog；分别跟踪 #20 版本契约。 |
| I13 `#annotation` 人工标注 | 选择可用本机任务和队列，打开微信小程序，截图/框选、坐标映射与预检，模型动作建议、人工确认点击、纠正分类/布局/目标/难负例/备注、动作前后证据、门店历史、完成/跳过/不合格/重试/刷新、暂停/恢复/结束。 | `index.html:493–572`；`annotation.js:828–1199`；AnnotationWorkspace、独立 CanvasController 和可复用 EvidenceViewer。禁止用布局像素直接作为后端坐标。 |
| I14 `#environment` 与连接诊断 | 模型/引擎/微信状态，探测项与诊断事件，检查连接、重连、测试、失败重试、配置跳转、阻塞提示与详情。基线仍有旧系统设置/UiPath 兼容入口，保留 Windows 有效检查及模型诊断。 | `index.html:389–397, 2243–2344`；`environment.py`、`connections.py`；WindowsDiagnostics、ConnectionSummary。 |
| I15 `#settings` 与模型用量 | 模型供应商/兼容接口/模型名/API 密钥保留、连接测试、请求超时/思考模式、截图参数与本机目录/桌面选择器、共享工作台设置。模型用量按日期、来源、模型等筛选、分组、分页、查询详情。用量是设置内入口，无独立 hash。 | `index.html:573–637, 908–940, 2485–2499`；`model-usage.js`；SettingsPage、NativeProfileForm、ModelUsagePanel。 |

## 脚本与迁移方法

| 现状 | React 实施要求 |
| --- | --- |
| `web/index.html` 既含多个 section、跨页弹窗及大量内联样式，又用 `setPage`、`renderStatus`、`loadHubQueue`、`loadRejections` 等函数直接同步 DOM。 | 首先为每个流程记录触发 → 状态 → HTTP → 成功/失败 → 刷新目标。路由负责装配，feature 维护选择和草稿，查询层维护服务端状态。通用组件接收适配后的视图数据与回调。 |
| `shared-workbench.js` 创建工作台实例并直接控制设备、分配、角色数据集和弹窗。 | 提取共享状态查询、配对/分配 API、资格显示适配器；Hub 与 Worker 共用身份和状态标签，保留两个独立工作流。 |
| `materials.js`、`annotation.js` 使用模块内状态、DOM 事件绑定、指针捕获和轮询。 | 图片手势、坐标/边界约束、焦点、上传取消在组件或专用 hook 中显式清理。按当前批次、门店、原图和帧 ID 锁定提交对象；防止迟到响应覆盖新页面。 |
| `model-usage.js` 维护区间、分组、筛选、分页及模态状态。 | 保留为空、费用/用量缺项和查询失败的显示。拆离模型连接测试，统计读取不触发模型调用。 |
| `task-center.js`、`task-center-vue-loader.js` 与 `web-ui/` Vue/Element Plus 试点并存，产出 `web/vue/task-center.js\|css`。 | React 负责自己的 DOM 与请求生命周期。现有两种入口作为过渡版验证参照，最终与 Vue 构建、选择状态、/vue 资源路由统一退场。 |
| `task-center.css`、`materials.css`、`workspace.css`、`model-usage.css`、`annotation.css` 依赖全局选择器、CSS 载入顺序、角色 data 属性和固定高度。 | 新入口只载入 React 样式。使用 Token、语义控件、表格区域与抽屉单独滚动。避免以旧 CSS 隐藏来实现权限。 |

### 事件、状态与更新策略对照

- `window.__consoleBridge` 提供 api、toast、setPage、共享工作台和门店格式化。还有 `__materialsInit`、`__taskCenterInit`、`__taskCenterOpen`、`__loadLegacyHistory`、Vue loader 状态探针。React 以路由、Provider、类型化 API 和显式组件回调接管。过渡入口之间使用 URL 跳转。
- `menu:page` 驱动旧模块显隐/轮询，`materials-open-review` 携带 `batch_id/store_id` 打开素材修复，`batch-workflow` 更新批次标签。对应的 React 行为通过路由参数、查询失效和导航返回状态表达。保留 `popstate`、深链接、Esc 关闭和焦点恢复的效果。
- 旧页在可见时轮询，工作台约 Hub 5 秒/Worker 3 秒；质量各入口约 5 秒，素材作业活跃约 1 秒/其他约 3 秒，经典任务详情约 1.5 秒，Vue 设备活动约 2 秒。React 初期按 [实施计划](phase-plan.md) 统一调度。Worker 后端同步独立于浏览器，不能因关闭页面停止补传。
- 仅将折叠、显示偏好等非敏感信息存浏览器。已查看视觉标记与真实人工确认拆开。选择集合使用稳定 ID，保存失败保留草稿。网络数据带时间戳，响应按请求键和序列归属，切页/关闭取消过期读取。任何批量命令的权威范围来自已确认的 ID 集合。
- 画布验收需要高 DPI、窄屏、原图旋转/缩放、边界框裁切、指针取消、帧更新后过期选择。Canvas 仅管理展示和输入，裁切/点击/资格结果仍由后端裁决。

详见 [接口契约](api-contracts.md)、[角色矩阵](role-matrix.md) 与 [验收映射](phase-plan.md#验收映射)。
