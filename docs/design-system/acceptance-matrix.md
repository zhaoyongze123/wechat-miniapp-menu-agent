# 全站交互验收矩阵

状态说明：已完成 / 部分完成（说明剩余项）/ 保留现状（评估后无需改动）/ 未实施（记录理由）。
本矩阵按 Issue #31 的页面清单整理，复用了 #30 已交付的表格、列宽、固定列、门店详情、提示音与接口权限修复。

## 基础设施

| 项 | 状态 | 证据 |
| --- | --- | --- |
| 四层规范索引与冲突优先级 | 已完成 | `docs/design-system/README.md`、`upstream-index.md` |
| 页面模式映射 | 已完成 | `page-patterns.md` |
| 组件契约 | 已完成 | `component-contracts.md` |
| Storybook（开发依赖、不进生产包） | 已完成 | `npm run storybook`、`npm run build-storybook` 构建通过；`.gitignore` 排除产物 |
| 关键组件故事与交互 | 已完成 | 表格长文本/窄容器/加载/空态/键盘进入/行内展开、列表筛选与批量条、信息展示（StatCard/StatusBadge/KeyValueList/KeyValuePair）、弹窗/抽屉/确认/页头均有真实合成数据实例与交互断言 |
| 前端门禁 | 已完成 | `typecheck`、`lint:design`（禁止手写表格/原生选择滑块/页面私有 Metric·Fact·Field/未登记硬编码颜色）、28 个测试文件/136 用例、生产构建 |

## 页面与弹层

| 页面/区域 | 状态 | 本轮改动与说明 |
| --- | --- | --- |
| AppShell / 角色加载 | 已完成 | 侧栏可折叠并记忆；加载中/失败不再显示“本机独立模式”；窄屏单导航；未保存修改覆盖刷新、站内链接与浏览器后退/前进 |
| 设置中心 | 已完成 | 分类导航、Hub/Worker 分类过滤、草稿基线、后台刷新不覆盖、吸底保存栏、放弃修改、音量轨道/百分比/静音、业务化通知文案、关于页真实版本 |
| 共享工作台卡片 | 已完成 | 去除重复标题层级，连接/配对/待补传集中展示 |
| Hub 采集工作台 | 已完成 | 筛选清除、批量操作条复用、表格键盘进入详情、行内交互不触发行点击；门店详情支持当前筛选结果内上一家/下一家与位置显示 |
| SelectedStoresDrawer | 保留现状 | 数量、移除、清空、分配与长名称查看已满足 |
| HubStoreDetailDrawer | 已完成 | #30 入口、二维码、版本、分页与错误态；新增当前页连续查看、位置显示与门店名/任务 ID 复制 |
| AssignDialog | 保留现状 | 处理中防重复、冲突排除与选择保留已有 |
| HubAssignmentsPage | 已完成 | 详情任务/分配 ID 可复制；证据连续浏览；详情支持上一条/下一条；筛选条与批量条复用公共组件，恢复“回收选中”，确认说明条数、涉及设备、解除占用与迟到结果留档 |
| AssignmentDetailDrawer | 已完成 | 结果图片序列连续浏览；ID 复制；忙碌/处理中保持现状 |
| HubDevicesPage / WorkerDetailDrawer | 已完成 | 长名称/ID 沿用 #30 表格约束；设备详情可直接查看该设备分配记录并返回设备列表；未保存备注受导航守卫保护；设备搜索暂不添加（数量少） |
| PairingDialog | 保留现状 | 生成防重、有效期与一次性说明已有 |
| Worker 工作台 | 已完成 | 开始弹窗处理中禁止关闭；运行/暂停/终止/同步语义保持；筛选与页码沿用 URL 参数，刷新保留 |
| StartRunDialog | 已完成 | 提交中锁定 Esc/遮罩/关闭 |
| ManualPanel | 保留现状 | 上下文校验、F8/F9、失败保留已有 |
| WorkerSyncDialog | 保留现状 | 无伪造上传百分比，同步入口按真实状态显示 |
| WorkerTaskDetailDrawer | 已完成 | 证据与已发布截图合并序列；任务 ID 复制；调试信息仍按需展开 |
| RemoteControlPage / PiP | 保留现状 | 复用 #30 声音与接口权限修复，未扩大权限 |
| AnnotationPage | 已完成 | 框选坐标映射与草稿保护已有；标注框颜色按建议/编辑/只读语义集中定义，并经 `lint:design` 白名单登记 |
| MaterialsPage / ReworkPanel | 已完成 | 图片序列、弹窗忙碌保护、失败保留与部分失败反馈沿用；批量分析确认文案说明真实范围 |
| ImportBatchDialog | 已完成 | 上传/创建中锁定关闭；取消上传与关闭界面区分 |
| BatchManagementDialog | 保留现状 | 编辑草稿、删除/恢复确认与禁用原因已有 |
| AnalysisDialog | 保留现状 | 关闭只关 UI，不取消后台任务 |
| StoreDetailDrawer / ManualQrCanvas | 已完成 | 原图序列连续浏览（原有能力确认）；草稿保护保留 |
| PublishDialog | 已完成 | 发布中锁定 Esc/遮罩/关闭，失败保留输入 |
| QualityPage | 已完成 | 待检查门店“标记不合格”行内/批量/详情均可提交；筛选条与批量条复用公共组件，批次通过/撤销通过归回批次范围；不合格清单筛选统一并可一键清除 |
| RejectionDetailDrawer | 已完成 | 两条证据连续浏览；处理中禁止关闭；操作成功后才进入下一条（沿用） |
| PendingReviewDrawer | 已完成 | 当前结果/历史记录/诊断证据三分区；源图/最近画面连续浏览；历史缺失与同步状态如实说明 |
| TaskCenterPage / ActiveTasksPanel | 已完成 | 保留 5 秒轮询、批次摘要、重命名与详情；任务 ID、轮次 ID 与原始来源可复制并省略展示，长文本不再撑开行 |
| TaskDetailDrawer | 已完成 | 已有轮询、分页、保留最近数据警告；逐店详情补充门店画面、失败说明与直达质量审核处置，深链自动打开对应门店 |
| ModelUsageDialog | 已完成 | 统计与调用明细统一为公共表格，保留日期筛选、分组下钻、行内调用详情与 Token 空值说明；未伪造价格/ETA |
| DiagnosticsPage | 保留现状 | 只读检查与复制降级已有；未新增重复入口 |
| 未知路由/旧深链 | 保留现状 | `/overview`、`/environment`、`/review-queue` 重定向保留 |

## 已知例外

1. 表格“筛选/页码记忆”依赖 URL 查询参数，刷新保留；跨设备或跨角色各自独立。未额外引入本地缓存。
2. 设备搜索未新增：当前已配对设备数量少，避免增加低价值控件；设备数增长后再按需补充。
3. 视觉回归以 Storybook 布局断言与真实浏览器关键路径检查为主，不做全站逐像素基线。
4. `AnnotationCanvas` 的标注框颜色为业务语义色（建议/编辑/只读），需覆盖任意门店截图，登记在 `scripts/check-design-system.mjs` 白名单中；新增白名单必须同时说明文件、规则与原因。
5. `UnsavedChangesProvider` 为导航守卫基础设施而非可视组件，由路由守卫相关测试覆盖，不单独提供 Storybook 实例。
