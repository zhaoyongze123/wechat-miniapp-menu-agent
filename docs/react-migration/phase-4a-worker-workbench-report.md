# 阶段报告：Phase 4A Windows 原生采集工作台

- 分支：`feat/24-react-migration`
- 工作区：`D:\menu-agent-react`
- 范围：`features/worker`（本机任务队列、全自动/半自动开跑、运行控制、半自动人工面板、连接与补传提示、角色路由）
- 状态：阶段交付，等待验收；未合并 main、未部署生产、未删除旧前端
- 后端：本阶段零改动（改动仅 `web-react/src`、`web/react` 构建产物与 `docs/`）

## 1. 交付范围与能力矩阵

| 要求能力 | 实现位置 | 行为 | 验证方式 |
| --- | --- | --- | --- |
| 全自动开跑 | `StartRunDialog` → `POST /api/capture/start` | 勾选本机门店后选择「全自动」，提交 `mode=auto` 与批次名 | 浏览器打开弹窗断言两种模式；接口契约据 `uipath_runner` 既有实现 |
| 半自动开跑 | 同上 + `ManualPanel` | `mode=manual`；面板显示当前门店、进度、阶段，F8 触发截图、F9 打开不合格表单、可跳过 | 组件与接口对齐 `/api/manual/*`；面板 1.5s 独立轮询 |
| 运行控制 | 当前批次卡 | 暂停（当前门店完成后生效）、继续、切换半自动并继续（仅全自动暂停批次）、终止（确认窗说明结果保留与剩余门店） | 动作 mutation 直连 `/api/stop`、`/api/resume`、`/api/terminate` |
| 本机任务队列 | 「本机任务队列」 | 服务端全量任务在页面内完成批次/状态筛选、排序、250ms 防抖搜索、分页；7 张统计卡点击即筛选 | UI 断言：6 行渲染、搜索 SH006 得 1 行、待检查筛选 1 行 |
| 勾选与上限 | 表格首列 | 仅 `runnable===true` 可选；本页全选带 indeterminate；上限 300 家并提示 | UI 断言：勾选 2 家后按钮显示「开跑采集 2 家」 |
| 离线继续执行 | 连接提示 + 后端持久化 | 未配对/断线时提示「本机队列可继续执行」，开跑不依赖 Hub 在线；结果由后端落本机 | 复用既有 SQLite 队列事实，前端不拦截开跑 |
| 重启恢复 | 当前批次卡 | 页面按 `/api/status` 读取 `run`（`paused`/`blocked`/`interrupted`/`resuming`）并给出继续入口；服务端负责批次事实与发布事务恢复 | 状态口径单测 + 真实状态渲染 |
| 重连补传 | 连接提示 + 事件区 | `pending_uploads` 常驻徽标，断线提示携带待补传数量，恢复后自动续传由后端线程完成 | 隔离 Worker 真实 `/api/shared/status` 字段 |
| 连接/凭据异常 | 顶部 Alert | 未配对（info）、已配对但离线（warning，含待补传数）、凭据失效（danger，指引重新配对） | 角色 Provider 真实数据 |
| 角色路由 | `WorkbenchRoute` | 按 `useRole()` 分流 Hub/Worker/standalone；Hub 走 Hub 工作台，Worker 走本页 | UI 断言：Worker 侧栏仅工作台/任务中心/设置；Hub `/workbench` 正常 |

本子阶段只交付“本机队列 + 运行控制”；设置/诊断（配对与重连入口）、标注与图像、模型用量属于 Phase 4 后续子阶段，尚未迁移，旧前端对应入口保持可用。

## 2. 文件清单

新增：

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `src/features/worker/types.ts` | 139 | `WorkerTask/WorkerRun/ManualState/WorkerStatus`、筛选与排序类型、`START_RUN_LIMIT=300` |
| `src/features/worker/api.ts` | 81 | 状态、开跑、恢复、暂停、终止、重新排队、`/api/manual/*` 适配 |
| `src/features/worker/format.ts` | 169 | 运行状态文案、采集模式、14 项不合格原因、状态口径、过滤/排序/分页、进度与可恢复判断 |
| `src/features/worker/format.test.ts` | 81 | 7 项口径单测 |
| `src/features/worker/hooks.ts` | 23 | 3 秒轮询查询键与刷新 |
| `src/features/worker/StartRunDialog.tsx` | 143 | 批次名、全自动/半自动、门店预览、上限与错误提示 |
| `src/features/worker/ManualPanel.tsx` | 133 | 半自动人工面板（截图/不合格/跳过、快捷键提示） |
| `src/features/worker/WorkerWorkbenchPage.tsx` | 676 | 统计、当前批次、连接提示、队列表格、事件与输出目录、确认窗 |
| `src/features/worker/WorkbenchRoute.tsx` | 22 | 角色分流 |

修改：

- `src/app/App.tsx`：`/workbench` 指向 `WorkbenchRoute`。
- `src/app/AppShell.tsx`：工作台迁移标记扩展为 `['hub','worker']`。
- `UFS_Menu_Scraper/macos_runner/web/react/`：重建产物 `index-D8I_EqZz.js` / `index-BEJ4a2ed.css`，删除旧产物。

## 3. 测试与证据

| 项 | 命令/方式 | 结果 |
| --- | --- | --- |
| 类型检查 | `npm run typecheck` | 通过，0 错误 |
| 单元测试 | `npm test` | 8 个文件、42 个用例全部通过（worker 7 个） |
| 生产构建 | `npm run build` | 成功，仅既有 chunk 体积警告 |
| 构建引用 | `index.html` 与 `assets/` 比对 | 引用 `index-D8I_EqZz.js`、`index-BEJ4a2ed.css`，旧产物已移除 |
| 浏览器验收 | Playwright（`channel="msedge"`） | 8/8 通过，见 `verify/worker-workbench/ui-check.json` |

UI 断言明细：

| 断言 | 结果 |
| --- | --- |
| Worker 队列渲染 6 行 | 通过（`rows=6`） |
| 侧栏含「采集工作台」 | 通过 |
| 侧栏无「质量审核」等 Hub 入口 | 通过 |
| 勾选 2 家后按钮计数 | 通过 |
| 开跑弹窗显示全自动/半自动 | 通过 |
| 搜索 SH006 | 通过（`rows=1`） |
| 待检查筛选 | 通过（`rows=1`） |
| Hub `/workbench` 回归 | 通过 |

浏览器 console 仅剩两类既有噪音：CSP 拦截 KaTeX 字体、一个静态资源 404；不是本页脚本错误。

截图：

- `docs/react-migration/screenshots/verify-worker-workbench.jpg`（Worker 工作台整页）
- `docs/react-migration/screenshots/verify-worker-start-dialog.jpg`（开跑弹窗）
- `docs/react-migration/screenshots/verify-worker-nav.jpg`（Worker 角色侧栏）

## 4. 隔离验证环境

| 项 | 值 |
| --- | --- |
| Hub | `8786`，home `D:\menu-agent\.tmp-quality-test\hub`，`MENU_CAPTURE_ROLE=hub` |
| Basic 代理 | `8788 → 8786`（仅本机浏览器） |
| Worker | `8787`，home `D:\menu-agent\.tmp-quality-test\worker`，迁移仓库产物 |
| 配对 | worker id `7b92f4fdb09227857c0e911663b325fd`，已分配 6 家（SH005/006/011/012/014/015） |
| 前端入口 | `/?ui=react&build=v2#/workbench` |
| 未触碰 | 运行实例 `8765`、生产数据、旧前端产物 |

## 5. 已知限制与风险

1. R15（沿用质量审核阶段）：撤销“采集不合格”后任务 `status` 仍为 `rejected`，`/retry` 返回 400；前端 `RETRYABLE_STATUSES` 不含 `rejected`，重新采集入口保持禁用，需在 #22/#20 确认规则后再改，本阶段未改门店版本模型。
2. 设置/诊断、标注与图像、模型用量尚未迁移；Worker 断线后的“重新配对”当前指引用户到旧设置页。
3. 离线继续、重启恢复、重连补传的持久化由既有后端负责；本阶段验证的是界面状态与入口闭环，未做跨重启的端到端演练，需在后续设置/诊断子阶段补齐。

## 6. 回滚说明

- 代码回滚：`git revert <本阶段提交>`，仅影响 `web-react/src` 与 `web/react` 产物，无数据库迁移、无后端改动。
- 运行回滚：前端入口使用 `?ui=react` 显式加载；去掉该参数即回到旧前端，不影响旧路由与数据。
- 产物回滚：恢复上一版 `assets/index-*.js|css` 与 `index.html` 引用即可；旧前端目录未删除。
- 数据回滚：本阶段无写入结构变更；隔离环境可整目录删除 `D:\menu-agent\.tmp-quality-test`。
