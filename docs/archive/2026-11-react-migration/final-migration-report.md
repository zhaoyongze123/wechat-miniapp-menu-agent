# React 迁移最终报告

日期：2026-11-24
分支：`feat/24-react-migration`（关联 Issue #24）
范围：Phase 0–4C 收尾三项——全功能覆盖核查、React 默认唯一前端与发布链路、旧前端彻底清理。

## 1. 结论

- React 已成为唯一业务前端：`GET /` 直接返回 `web/react/index.html`，`GET /assets/{name}` 只返回 Vite 清单内的内容哈希资源。
- 旧版单文件前端、Vue 试点、旧静态资源路由与 `?ui=` 迁移开关全部移除；保留旧版业务深链（`#overview`、`#environment` 等）到新路由的兼容跳转。
- 发布链路的安装包、Hub 镜像与 CI 均以 React 产物为准，运行时不依赖 Node、npm 或外部 CDN。
- 数据、配置、设备身份与历史记录仍存放在安装目录之外，升级与回滚不改动业务数据。

## 2. 最终架构

| 层 | 位置 | 说明 |
| --- | --- | --- |
| 前端源码 | `macos_runner/web-react/` | React 19 + TS strict + Vite 7 + Tailwind 4 + TanStack Query。 |
| 前端产物 | `macos_runner/web/react/` | 提交入库：`index.html`、`assets/*`、`.vite/manifest.json`、`favicon.svg`。 |
| 静态服务 | `menu_capture/server.py` | `/` 返回入口（`no-store`）；`/assets/{name}` 按清单白名单返回（一年 immutable）；`/favicon.svg`。 |
| 版本信息 | `GET /api/version` | 返回 `version`（启动器/镜像注入）与 `frontend`（入口哈希文件名）。 |
| 构建 | `deploy/windows/build.ps1` | 打包前校验 React 入口与清单，生成 `release-manifest.json`。 |
| 镜像 | `deploy/Dockerfile.hub` | 构建参数写入版本，镜像内校验前端产物存在，运行时不装 Node。 |
| 安装器 | `deploy/windows/setup.iss` | 按版本目录并存，`current-version` 决定运行版本，随包提供回滚脚本。 |

页面与角色：

| 路由 | 页面 | Hub | Windows 采集端 |
| --- | --- | --- | --- |
| `/workbench` | 采集工作台（Hub 调度 / 本机执行） | ✓ | ✓ |
| `/annotation` | 人工标注 | — | ✓ |
| `/devices` | 采集电脑 | ✓ | — |
| `/assignments` | 分配记录 | ✓ | — |
| `/materials` | 素材清洗 | ✓ | — |
| `/rejections` | 质量审核（不合格 / 待检查） | ✓ | — |
| `/task-center` | 任务中心 | ✓ | ✓ |
| `/settings` | 设置（含模型用量） | ✓ | ✓ |
| `/diagnostics` | 环境诊断 | — | ✓ |

## 3. 覆盖核查与修复

对照 `docs/archive/2026-11-react-migration/inventory.md`（I01–I15）、API 契约与角色矩阵逐项核对，本轮补齐：

1. **Windows 采集端同步状态**：旧版只有弹窗能查看待上传与最近确认列表，React 原仅显示待上传徽标。新增 `features/worker/WorkerSyncDialog.tsx`，按后端 `assignments` 展示待上传、最近确认、连接摘要与最近错误，并在工作台提供「同步状态」入口。
2. **全站活动任务入口**：旧版各页都有运行面板浮层。壳层新增 `ActiveTaskIndicator`，任一页面可见“运行中 N 个任务”并跳转任务中心；任务中心仍保留完整面板。
3. **旧版深链兼容**：`#overview` → `/workbench`、`#environment` → `/diagnostics` 增加别名路由；`#materials`、`#rejections`、`#task-center`、`#settings`、`#annotation` 原样可用；未知地址改为明确的“页面不存在”并给出有效入口。
4. **Hub 调试视图**：补回 `最近活动`（`/api/status?include_events=true` 的 events），默认关闭，仅排障时开启。
5. **首页默认页**：`/` 由任务中心改为采集工作台，与旧版默认入口 `#overview` 一致。
6. **移除过期文案与回退**：删除导航“返回旧版主页/旧版控制台”、分配记录与任务中心的“请使用旧版”提示、导航组件中的旧 hash 回退逻辑。
7. **自包含产物**：构建时剥离 SDK 样式里指向 CDN 的 KaTeX 字体声明，新增本地 `favicon.svg`，消除远程字体请求与 404。

已确认等价或无需修复：素材三栏同步/缩放/候选切换/手动框选/补图/重分析、发布与返工、不合格 Excel 导出、待检查批次、门店抽屉上一家/下一家、标注全流程、诊断与环境检查、设置与模型用量下钻。

本轮未迁移（调试用途，非业务能力，已记录为已知限制）：旧版门店抽屉的“动作时间线/调试包导出”、Hub 运行区折叠偏好记忆。

## 4. 删除清单

已删除并停止引用（23 个跟踪文件）：

- 旧版业务前端：`web/index.html`、`web/workspace.css`、`web/shared-workbench.js`、`web/materials.{js,css}`、`web/rejections`（随 index.html）、`web/annotation.{js,css}`、`web/model-usage.{js,css}`、`web/task-center.{js,css}`、`web/task-center-vue-loader.js`。
- Vue 试点源码：`web-ui/package.json`、`web-ui/package-lock.json`、`web-ui/vite.config.js`、`web-ui/src/**`（TaskCenter.vue、TaskDetailDialog.vue、lib、styles、main.js）。
- Vue 试点产物：`web/vue/task-center.{js,css}`。
- 旧静态路由：`/vue/{name}`、`/assets/{name}` 旧白名单、`/react/assets/{name}` 迁移前缀。
- 迁移开关：`?ui=react`、`?ui=vue`、`build=v2`（服务端不再解析，旧参数无副作用）。

保留：`web/react/**`（SPA 入口与资源）、`web-react/**`（源码与构建）、`ui_tars/**` 及其 `node_modules`（GUI Agent 运行时依赖）、`menu_capture/**` 全部后端模块。

## 5. 升级与回滚

Windows 采集端：

- 升级：运行新版安装包；新版本写入 `releases/<新版本>`，旧版本目录保留，数据在 `%LOCALAPPDATA%\MenuCapture`。
- 回滚：`<安装目录>\rollback.ps1 -Version <旧版本>`。脚本校验目标版本文件完整，写入 `current-version` 并等待守护进程在采集/标注结束后切换，最后通过 `/api/version` 确认。
- 核对：`release-manifest.json` 记录版本、源码提交、前端入口与 SHA256；`/api/version` 返回当前版本与入口哈希。

Hub 镜像：

- 升级：`MENU_CAPTURE_VERSION=<版本> MENU_CAPTURE_IMAGE=menu-capture-hub:<版本> docker compose -f deploy/compose.yaml up -d --build`。
- 回滚：把 `MENU_CAPTURE_IMAGE` 指回上一标签执行 `up -d`；数据卷不变。

## 6. 验证结果

| 检查 | 结果 |
| --- | --- |
| `npm run typecheck` | 通过，0 错误 |
| `npm run test -- --run` | 12 个文件 / 66 用例通过 |
| `npm run build` | 通过，产物 `web/react`（Vite manifest 一致） |
| `pytest` 受影响模块回归 | 7 个文件 / 55 用例通过（server、shared_workbench、windows_compat、environment、workbench_page、model_usage、model_usage_integration，216s） |
| Python 全量套件 | 本机存在既有环境类失败（采集 UI 自动化 / OCR / 桌面依赖相关），与本次改动模块无关；CI 以受影响的 7 个契约文件作为阻断项 |
| 服务端契约测试 | `/` 始终 React、`/assets` 白名单、旧 `/vue` 与旧资源 404、`/api/version`、503 无构建、favicon |
| Hub 镜像构建与运行 | `menu-capture-hub:verify`（`VERSION=0.1.0`，176MB）构建成功并实跑：`/healthz` 200；未鉴权 `/` 401、鉴权 `/` 200；`/api/version` 返回 `{"version":"0.1.0","frontend":"index-BRVN5sxk.js"}`；`/assets/index-BRVN5sxk.js` 与 CSS 200 且 `cache-control: public, max-age=31536000, immutable`；`/favicon.svg` 200；旧 `/web/index.html`、`/vue/task-center.js`、`/assets/materials.js` 与越界路径均 404；入口 HTML/JS/CSS 无外部 URL、无 `cdn.openai.com` |
| 浏览器验收 | 见 `docs/archive/2026-11-react-migration/verify/final/`：默认入口、导航、调试视图、同步弹窗、标注页、深链跳转，控制台 0 错误 |
| 发布包前置校验 | React 入口 `web/react/index.html` 与 `.vite/manifest.json` 存在；旧 `web/index.html` 已删除；清单入口 `assets/index-BRVN5sxk.js` 与产物 SHA256 一致（`68c2be52…b8b0`，739703 字节）；入口 HTML 无外部 URL、无 `__CAPTURE_TOKEN__` 占位符 |
| 发布脚本 | `build.ps1`/`Launch.ps1`/`smoke.ps1`/`rollback.ps1` 语法解析 0 错误 |

## 7. 已知限制

- 本机缺少 7-Zip、Inno Setup 6 与 uv，Windows 安装包未在本机实际编译；发布包前置校验已在本地执行，安装包由 CI `windows-release` 作业编译并执行 smoke（安装→启动→升级→重启→回滚→数据保留）。
- 未做真实微信环境下的现场采集/标注操作验收；相关链路以接口契约与界面状态验证为准。
- 调试用途的“动作时间线/调试包导出”和折叠偏好未迁移。

## 8. 代码回滚

- 整体回退：`git revert <本轮提交>`，React 源码、服务端路由、发布脚本同时恢复。
- 前端单点回退：重新检出上一版 `web/react/**` 并提交，服务端无需改动。
- 发布版本回退：见第 5 节，按版本目录或镜像标签切换，不影响数据。
