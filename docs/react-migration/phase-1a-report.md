# React 迁移 Phase 1A 交付报告

- 分支：`feat/24-react-migration`（基线 `e6b7ea8`，独立工作树 `D:\menu-agent-react`）
- 范围：React 19 底座 + 任务中心真实试点 + FastAPI 静态装配
- 状态：已完成并通过本地前后端验证

## 一、交付内容

### 1. React 底座（`UFS_Menu_Scraper/macos_runner/web-react/`）

| 能力 | 实现 |
| --- | --- |
| 技术栈 | React 19 + TypeScript strict + Vite 7 + Tailwind CSS 4 + `@openai/apps-sdk-ui` |
| 应用入口 | 单一 `App`，`HashRouter` 路由；兼容旧式 `#task-center` 与规范 `#/task-center` |
| 导航与权限 | 角色由 `GET /api/shared/status` 决定；Hub 隐藏标注/诊断，Worker 隐藏素材/审核；未迁移入口跳回旧版同源 hash |
| 设计 Token | `src/styles/index.css` 统一深色工作台表层、文字、线条与青绿强调色 |
| 基础控件 | Apps SDK UI 的 Button / Badge / Input / Select / Alert 为主，复杂后台组件封装在 `src/components/` |
| 数据表格 | `AppDataTable`（语义表格、表头吸顶、空状态、加载态、窄容器最小宽度） |
| 分页 | `AppPagination`（页码窗口、首末页边界、20/50/100 每页） |
| 弹窗 / 抽屉 | `AppDialog`、`AppDrawer`、`ConfirmActionDialog`（Radix Dialog，ESC/遮罩关闭、焦点回退） |
| 图片预览 | `ImagePreview`（适应窗口 / 原始尺寸切换，同源受控图片） |
| 状态反馈 | `StatePanel`（加载/空/错误/提示）+ `ToastProvider`（`aria-live`） |
| API 访问 | `apiFetch` 内存会话令牌、非 GET 附带 `X-Capture-Token`、令牌失效仅重试一次、`ApiError` 统一错误语义 |

### 2. 任务中心试点（`src/features/task-center/`）

- 活动任务面板：`GET /api/tasks/active` 每 5 秒轮询，页面隐藏自动停表。
- 历史列表：`GET /api/tasks/history`，类型/状态/关键词筛选，20/50/100 分页。
- 筛选与页码保存在地址栏，刷新、浏览器前进后退后保持条件。
- 详情抽屉：`GET /api/tasks/history/{kind}/{id}`，采集运行展示逐店结果，素材任务展示阶段事件。
- 任务改名：`PATCH /api/tasks/history/capture/{run_id}`，成功后同步抽屉与列表，不重置分页与滚动。
- 空状态、错误重试、统计卡、角色说明与旧版任务中心入口齐备。

### 3. 服务端装配

- `GET /?ui=react` 返回 React 入口；未构建时返回 503 且提示构建命令。
- `GET /react/assets/{name}` 只返回 Vite manifest 白名单内的内容哈希资源，带 `immutable` 缓存；其余路径/后缀一律 404。
- 全局 `Cache-Control: no-store` 对 React 资源放行，且不改变旧版页面与 API 行为。
- 产物已装配到 `web/react/`，发布包不依赖 Node 运行或外部 CDN。

### 4. 修复：素材任务详情进度口径

- 问题：`GET /api/tasks/history/materials/{id}` 返回 `counts: {}`，而列表返回真实进度，导致抽屉显示 `0 / 0`。
- 修复：详情改为与列表相同的口径（`total_stores` / `done_stores` / `failed_stores`），并补充 `batch_id`、`batch_name`。
- 回归：`tests/test_server.py::test_materials_history_detail_counts_match_list`。

## 二、改动清单

- `UFS_Menu_Scraper/macos_runner/web-react/`：React 工程源码、配置、单测与 `package-lock.json`
- `UFS_Menu_Scraper/macos_runner/web/react/`：构建产物（`index.html`、`assets/*`、`.vite/manifest.json`）
- `UFS_Menu_Scraper/macos_runner/menu_capture/server.py`：React 入口/资源路由；素材详情计数修复
- `UFS_Menu_Scraper/macos_runner/tests/test_server.py`：React 路由、未构建降级、素材详情口径回归
- `.github/workflows/windows-release.yml`：新增 `react-web` 校验（typecheck / 单测 / 构建产物一致性），Windows 打包依赖该任务
- `.gitignore`：忽略 `*.tsbuildinfo`
- `docs/react-migration/`：Phase 0 五份规划文档、本报告与验证截图

## 三、验证结果

### 前端（`web-react`）

- `npm run typecheck`：通过
- `npm test`：3 个文件 / 11 项通过（API 令牌与 403 重试、分页窗口、状态口径与时间格式化）
- `npm run build`：通过，产物 1006 模块，JS 443 kB / CSS 159 kB

### 后端（`macos_runner`）

- `pytest tests/test_server.py`：9 项通过（含 React 入口、资源白名单、未构建 503、素材详情计数）
- 完整套件：见提交说明；沿用迁移前已知基线 `tests/test_review_queue.py::test_pending_reviews_include_batch_and_approval` 失败，本次未新增失败。

### 真实服务与界面验证

以临时目录 + 演示任务库启动 `MENU_CAPTURE_ROLE=worker`，未读写原工作区数据：

- 列表展示 5 条真实任务与状态徽章；类型筛选后保留 2 条素材任务，刷新后筛选仍在地址栏。
- 采集详情展示 4 家门店逐店结果；改名为“早间试采-已改名”后抽屉与列表同步。
- 素材详情修复后显示“已分析 18 / 32、失败 1”。

截图：

- [任务历史列表](screenshots/phase1a-1-list.jpg)
- [采集运行详情](screenshots/phase1a-2-detail.jpg)
- [改名后状态](screenshots/phase1a-3-renamed.jpg)
- [素材筛选结果](screenshots/phase1a-4-filter.jpg)
- [素材详情 18/32（修复后）](screenshots/phase1a-5-materials-detail-fixed.jpg)
- [带边框筛选控件](screenshots/phase1a-6-filtered-list.jpg)

## 四、回滚方案

1. 代码回滚：`git revert <Phase 1A 提交>`，或删除 `web/react/` 后 `/?ui=react` 自动返回 503，旧版控制台不受影响。
2. 数据回滚：本阶段无数据库迁移、无表结构变更、无生产数据写入。
3. 发布回滚：Windows 安装包与 Hub 镜像不依赖 React 产物；移除 `web/react` 即可回到纯旧版界面。
4. 素材详情计数修复为纯响应字段补全，回滚只影响新前端抽屉显示。

## 五、未纳入本阶段

- 其余模块（Hub 调度与设备、素材清洗、质量审核、Windows 原生采集工作台、设置与辅助页）按 `phase-plan.md` 后续阶段迁移。
- 旧 Vue / Element Plus / 原生 HTML 业务脚本尚未删除，待全部模块验收后统一清理。
- Standalone 模式在 React 中明确提示并引导回旧版，不在本次支持范围。
