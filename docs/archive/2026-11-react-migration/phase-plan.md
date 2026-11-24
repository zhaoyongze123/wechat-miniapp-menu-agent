# #24 Phase 0 架构评审与 React 全量迁移执行计划

## 1. 目标与基线

| 项目 | Phase 0 结论 |
| --- | --- |
| 指定工作区 | `D:\menu-agent-react`，分支 `feat/24-react-migration`，初始工作树干净。 |
| 统一提交 | `e6b7ea82079b45f1720ea6dec6411bc29ed5276b`。较 `c5e9c34` 只新增 Phase -1 报告，用该祖先的等同源树交叉校验源码。 |
| PR #19 | 工作区 origin 和 PR 均为 `zhaoyongze123/wechat-miniapp-menu-agent`。目标 `main`，合并提交 `c5e9c34f5e69be048b4df9a97ff3befdc376db71` 是基线祖先。测试和 CI 是历史证据。[PR #19](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/pull/19) |
| 已知失败 | Phase -1 Windows 记录 31 passed、1 failed，`test_pending_reviews_include_batch_and_approval` 预期 1、实际 0。原因与处置见 [风险登记](risk-register.md)。Phase 0 未重新执行测试或部署。 |
| 支持角色 | Hub 与 Windows 原生 Worker。基线残有 Standalone、UiPath 兼容入口，用户已确认两角色目标。React 采用此范围。旧执行器清理由独立变更验收。 |
| 实装版本 | 本轮只读核验 Windows 运行包 0.1.2，Hub 健康接口可达。双端验收之前记录实际 Worker 发布版号、Hub 镜像摘要与 React 构建 ID，不能用 Git 提交推定部署版本。 |

主规格为 [#24](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/issues/24)。[功能清单](inventory.md)、[接口契约](../../architecture/api-contracts.md)、[角色矩阵](../../architecture/role-matrix.md) 和 [风险登记](risk-register.md)是阶段交付索引。Phase 0 交付规划供评审，Phase 1 经批准后启动。

## 2. 技术决策

### 工程、页面所有权与状态

- 在 `UFS_Menu_Scraper/macos_runner/web-react/` 新建唯一目标前端工程。React 19、TypeScript strict、Vite、Tailwind CSS 4。目录 `src/app` 管壳、路由和 Provider，`src/api` 管传输/DTO/适配，`src/features` 管跨组件业务状态，`src/components` 管通用控件，`src/styles` 管 Token。业务实现通过真实 API 使用 Python 事实。
- 使用 React Router hash 路由保留 `#overview`、`#materials`、`#rejections`、`#review-queue`、`#task-center`、`#settings`、`#environment`、`#annotation`。一期只在 `/?ui=react#task-center` 显式加载 React。未迁移入口走同源 URL 与旧 hash，已迁移入口留在 React。每轮只挂载一个页面实现。遇未知/无权限 hash 回到角色对应工作台并解释。保存 URL 筛选和列表返回信息，关闭详情恢复分页、滚动与触发焦点。
- Fetch 客户端使用现有 `/api/session`，操作令牌只放内存。JSON、原始上传、附件、失败和超时分流。DTO 显式表示未知、缺项与版本能力。角色 Context、选中/草稿/弹窗局部状态或 feature reducer、服务端状态 TanStack Query。禁止非幂等写请求自动重试，服务端明确拒绝旧令牌时的换新规则见 API 文档。
- 查询键包含角色、稳定 ID、筛选、排序、分页和范围。工作台默认 Hub 5 秒/Worker 3 秒读可见页，运行详情 5 秒，终态与关闭停止，素材作业保留活跃期 1 秒级进度。任务中心和快捷入口共享活动查询，页面隐藏时暂停 UI 轮询，再可见时立即查一次。慢响应按 AbortSignal 和请求归属隔离，失败保留最近有效数据及时间。Worker 后端心跳与待上传包不受浏览器切页影响。
- 对 [功能清单](inventory.md) 每项先建立“触发 → 当前对象/状态 → API → 成功或失败 → 回读/焦点”的回归用例。把 Bridge 和旧事件改为路由、回调与查询失效。名称只用于显示，动作按长期 ID。Canvas 管指针、状态与原图坐标映射，确认绑定具体门店/会话和帧，切图/切店或拖动取消时清理。后端决定实际内容、执行状态和资格。复核、返工、撤销、重采及批次审核各自保留不同业务动作。

### 统一组件规范

面向高频桌面业务。保留现有黑白／青绿工作台识别度。主按钮与危险操作层级清晰，技术详情按需展开。以语义 Token 管字体、行高、留白、线条、提示状态、焦点、表层与圆角。状态含文字或图标，不能只有颜色。表格区域可以横向滚动，抽屉头脚固定且内容独立滚动。标题只表达功能，页面/卡片/弹窗/模块标题下不默认添加说明；必要提示按就近原则放在字段、按钮、确认框、状态区或空状态内，详见 [UI 设计规范](../../design-system/react-migration-spec.md)。

| 层 | 实施内容 | 验收 |
| --- | --- | --- |
| 基础控件 | Apps SDK UI 的 Button、Badge、Alert、Checkbox、Input、Select、Menu、Popover、Tooltip、SegmentedControl。Tailwind 4 引入官方 CSS、扫描实际包内 class。 | 官方包当前声明 React 18/19、Tailwind ^4.0.10 peers 与 CSS/组件子路径，MIT。安装锁定版本，在普通同源浏览器验证，无 ChatGPT 宿主依赖和在线字体。[官方 README](https://github.com/openai/apps-sdk-ui#readme) |
| 共用组件 | DataTable、AppPagination、FilterBar、SelectionBar、AppDialog、AppDrawer、StatusBadge、StatePanel、EvidenceViewer、ConfirmActionDialog。语义 HTML 表格，Dialog/Drawer 基于无样式 Radix Dialog 组件。 | 组件通过类型化 props 接受行、列、选择和回调。服务端分页共用页码语义。Dialog/Drawer 支持 Escape、焦点圈定与回焦，确认窗口必须显示对象和范围。 |
| 业务复用 | RunSummary、RunDetail、DeviceDetail、AssignmentRecords、StoreDetail、BatchApprovalPicker、CaptureControls、ImageCoordinateCanvas。 | Feature 适配查询字段，组件展示不同事实域。任务状态、处理结果、审核、分配及上报时效有独立枚举。 |
| 可用性 | 加载、空数据、无匹配、失败、部分成功、权限、离线旧数据、长值展示。 | 960×720、1366×768、1920×1080、125% 缩放及键盘验证。查按钮遮挡、重复滚动、长字段与弹窗焦点。 |

### 制品与静态服务

- Vite 产物含极简 HTML、内容哈希 JS/CSS 与 manifest，装配至 `web/react/`。资源基址 `/react/assets/`。FastAPI 对 React 入口返回 React HTML，静态处理校验 manifest、后缀、路径归属和 MIME。仅哈希构建资源长缓存，HTML/API/session 与业务附件保持 no-store。路径异常、资源不存在不可回落为 HTML。
- CI 使用固定 Node 和 npm 锁文件，`npm ci`、类型、单测、构建与 manifest/外链检查。一次制品打包按同一 Git SHA 交给 Windows installer 和 Hub Docker。两端记录相同的 manifest 摘要及前端 build ID，缺制品直接失败。系统级 Node、npm、CDN 和在线字体不纳入前端运行依赖，Windows 内置 Node 仍供 GUI Agent 使用。
- 试点记录大批次下首屏、请求字节、频率、筛选延迟及证据预览。后续用相同数据和视口对比，不将当前包体或请求数当作无需验证的默认。

## 3. 分阶段交付、文件与门槛

| 阶段 | 实施次序与文件 | 交付与通过条件 |
| --- | --- | --- |
| 0 只读分析 | 在指定 Windows 工作区核对祖先、版本与角色，按 [清单](inventory.md) 归类页面、操作与异步状态。 | 五份规划文档和已知限制。架构评审批准前不启 React 编码。 |
| 1A React 底座与任务中心切片 | 先建 `web-react/`、路由、API、组件、Token 和真实任务中心。随后接入 `menu_capture/server.py` 静态服务、`deploy/windows/build.ps1`、`deploy/Dockerfile.hub` 及现有 Windows release workflow。 | Hub 和 Worker 同构建打开预览，用真实 active/history/detail/rename 验证列表、统计、搜索、分页、改名、详情与回滚。构建、角色、静态缓存和断网通过后，才启动其他产品化 UI。 |
| 1B #21 任务中心与全局数据 | 在 `features/task-center` 完成产品交互。独立后端 PR 增量同步和历史索引，审查 `shared.py`、`shared_worker.py` 与任务历史查询；按设备+本机 run ID 幂等、服务端筛选分页。设备/分配详情用同一公共组件。 | 用两台真实设备及 Hub 素材分析验证历史、时效、同 ID、部分同步/离线/补齐。旧 Worker 继续工作。初版远端名称由 Worker 提供，Hub 对镜像只读，后续双向改名需另审。 |
| 2 Hub 工作台与共享调度 | `features/hub-workbench`、`features/devices` 提取旧批次范围、跨页选项和共享弹窗。复用 #21 分配、设备与详情组件，保留选择与数据刷新逻辑。 | [#23](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/issues/23) 双批次筛选、300 家上限、分配事务冲突、设备、回收/历史通过。与 #20 有关的单店隔离在业务契约交付后联合验收。 |
| 3 素材清洗及质量审核 | `features/materials`、`features/quality`，按导入→浏览/选码→复核→发布、打回→原批次重新选码→增量发布两个流实现。先提取上传/Canvas、批次和门店选择与证据，导出筛选与 #20 店级版本协议在独立后端 PR 验收。 | [#20](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/issues/20) 的素材 UI 与模型分轨，[#22](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/issues/22) 审核语义与 #3 交界案例逐项验收。未有店级契约时维持真实快照标签。 |
| 4 Worker 与辅助入口 | `features/worker-workbench`、`features/settings`、`features/diagnostics`、`features/annotation`、`features/model-usage`。先配对与本机队列，再全自动/半自动、同步与标注、模型用量及隐藏弹窗。 | Windows 全自动/半自动、离线采集与重连、设置/诊断、标注与图像、暂停恢复与终止、同步详情全部可复现。Hub 设置和模型用量亦通过。 |
| 5 默认切换与旧实现退场 | 根入口改为 React 极简 SPA。完成清单后删 Vue/Element Plus、`web-ui/` Vue 原文件和 `web/vue/` 产物、Vue loader、替换完毕的旧脚本/内联业务 HTML/CSS、桥接和 /vue 路由。 | 双端安装升级、真实业务、所有深链、无 CDN、数据与回滚全部通过。复核源码和发布包旧业务引用归零，引导容器仍保留。生产切换单独授权、避开活跃采集。 |

## 4. 验收映射

| 编号与产品要求 | 对应盘点 | 测试验收 |
| --- | --- | --- |
| V01 #24 底座和路由 | I01、I05、I06 | 双端同构建，真实任务中心预览进入/返回、旧 hash、刷新、浏览器前进后退与权限拦截。React 与旧 DOM/轮询互斥。 |
| V02 #21 任务页完整性 | I05/I06 | 按名称、批次或 ID 查询，设备/类型/状态组合、统计与全局分页同口径，20/50/100 页档。跨批次可查版本，长名可见；完成状态与逐店结果分开。运行/历史改名、失败保留输入、ID 不变。关闭详情回到原条件页码及滚动。 |
| V03 #21 Hub 全局历史 | I04/I05、R04 | 两台 Worker + Hub 分析，设备同名与本机同 ID 不串联。历史可见、角色清晰，上报时效、断线补传、已完成/已同步、未记录与未同步各自正确。素材任务首屏摘要及失败项优先。 |
| V04 #23 Hub 主页 | I02、I04 | 两级批次范围可辨，统计与列表对齐且原差异有解释。默认约 50 家/页，全选本页、跨页保留、清空、300 上限。确认核对有效选择、设备、版本及原因。后端事务冲突不误报成功，已分配与已下载分开。 |
| V05 #21/#23 设备分配 | I04、I09 | 设备最近在线、已下载、待上传及摘要时效。编辑名称/备注保持身份，取消不保存，失败保留输入。设备查看分配记录保留预选，单项/批量回收结果、解除配对/移除、历史与授权符合规则。 |
| V06 #20 素材体验 | I10–I12 | 三栏浏览、筛选无匹配、原图/候选/缩放/旋转/手工框选、补图和分析、推荐/选中/确认/已发布/资格区分。批次改名 ID 不动；清除选择、撤销单店、回收站、永久删除分别验收。发布摘要、部分失败和范围准确。 |
| V07 #20 门店生命周期 | I12、R05 | 同批仅 A 换码升 A 版本，B 活跃分配和审核继续。同像素不同文件字节/元数据不升版；同像素打回重发布解锁但不恢复旧审核。打回即禁新分配。重采只产生新执行。跨批次返工保留原归属、历史及未选门店状态。 |
| V08 #22 审核与处置 | I07/I08、R07 | 不合格/待检查清单、原因、人工确认及导出同范围。确认/撤销确认和撤销不合格语义独立，未知绑定先解释。门店多选与批次审核范围解耦，确认窗口显示批次集合；空选不得隐式整批执行。 |
| V09 #22 证据与联动 | I09 | 当前结果、历史、诊断证据三个区；原名/完整原因、失败图、候选码、菜单图能够分别预览，店切换与图片翻页分开。区分无历史、未同步、证据未留存及读取失败。Hub 重采走分配，Worker 从合法本机队列提交。刷新后相关列表和摘要一致。 |
| V10 #24/#23 Windows 原生 | I03/I13–I15、R09 | 实测两种采集模式、暂停恢复/切半自动、F8/F9、终止、模型和窗口诊断、标注坐标。断 Hub 后多店继续、重启后恢复、重连补传；重试幂等、回收迟到留档、配对失效时保留已得结果。关闭页面不停止后台。 |
| V11 #24 发布和安全 | R02/R03/R12 | 锁文件可复现，两个包的 manifest 和构建 ID 一致，断互联网打开 UI。Session/Host/Origin/权限和缓存、资源越界/缺失、二进制文件、重装升级与数据持久化。 |
| V12 #24 清理回滚 | 所有 I 编号 | 全路由和隐含入口逐项对照，常用视口、键盘与缩放通过，旧前端运行时引用归零。Windows 上一 release 与 Hub 上一镜像各回滚一次，验证配对、有效任务、未上传包及历史。 |

### 测试和交接

- Vitest/Testing Library 验证 API 错误、查询取消、稳定 ID 选择、状态标签、图像坐标、可访问性与操作确认。Playwright 用隔离数据和真实 Python API 测基本流程、错误、切页和晚到请求；安装包、双机、Windows 桌面及证据操作用真机补齐。
- 优先复用 `tests/test_shared_workbench.py`、`test_workbench_page.py`、`test_capture_outcomes.py`、`test_run_naming.py`、`test_review_queue.py`、`test_materials_api.py`、`test_annotation.py`、`test_windows_compat.py`、`test_environment.py`。按阶段增静态资源/鉴权和真实业务回归。基线既有失败保留记录并在后续结果单列。
- 每个 PR 按 V 编号回贴需求、相关 SHA/文件、构建和测试命令/实测结果、失败/跳过、双端截图、部署演练版本及回滚方法。Phase 1A 门槛前，#21/#22/#23 只做接口和差异分析。#20 数据模型可独立规划，按契约交付与 #24 联合验证。
