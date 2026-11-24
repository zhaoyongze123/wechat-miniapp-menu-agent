# 阶段报告：Phase 4B 设置与环境诊断

- 分支：`feat/24-react-migration`
- 工作区：`D:\menu-agent-react`
- 范围：`features/settings`（目录、模型服务、截图与循环参数、调试日志、共享工作台配对）、`features/diagnostics`（连接总览、模块动作、预检阻塞、诊断事件与底层检查）
- 状态：阶段交付，等待验收；未合并 main、未部署生产、未删除旧前端
- 后端：本阶段零改动（改动仅 `web-react/src`、`web/react` 构建产物与 `docs/`）

## 1. 交付范围与能力矩阵

| 要求能力 | 实现位置 | 行为 | 验证 |
| --- | --- | --- | --- |
| 目录配置 | 设置页「目录」 | Worker 显示「任务来源：由共享工作台分配」，只配置输出目录；Hub 只读展示且不渲染选择按钮；Standalone 输入/输出均可选 | Worker 输出目录真实值；Hub 页面无选择按钮 |
| 目录选择 | `POST /api/select-directory` | 系统目录对话框；取消不改表单；失败 toast 提示 | 接口契约与旧页一致；Hub 被中间件拦截时按钮不存在 |
| 模型服务 | 设置页「模型服务」 | 供应商（OpenAI 兼容/Anthropic）、模型名、接口地址、超时 10–300、思考模式三态、API 密钥留空保留、显式清除密钥 | 三态转换与载荷单测；真实保存往返成功 |
| 模型连接测试 | 右栏「模型连接」 | 仅使用已保存配置；未配置时按钮禁用；成功/失败展示 message 与时间 | 隔离 Worker 未配置模型，按钮为禁用态 |
| 采集参数 | 「截图与循环参数」 | 窗口宽高/重叠比例沿用旧页“预留”只读；最大页数、Agent 步数、单任务上限、稳定等待、素材并发可编辑并按服务端范围校验 | 5 项范围单测；页面字段完整 |
| 采集人数与授权建议 | 「模型服务」下方 | 就餐人数 1–12；「人工标注时提供授权操作建议」说明仅影响标注建议 | 单测覆盖人数范围 |
| 调试日志 | 「开发调试日志」 | 读取占用（`/api/debug/summary`）、手动刷新、容量上限 64–4096、清理前二次确认并说明素材/结果保留 | 隔离环境显示「已用 2.7 MB / 上限 512 MB · 2 个文件」；确认窗打开/取消验证通过 |
| 共享工作台（Hub） | 顶部「共享工作台」卡 | 展示已配对/在线台数，跳转采集电脑管理生成配对码 | 真实 Hub 显示「已配对 4 台，当前在线 1 台」 |
| 共享工作台（Worker） | 同上 | 已配对展示连接摘要；未配对/凭证失效时展示地址、电脑名、配对码表单并调用 `POST /api/shared/connect`；离线显示待补传数量 | Worker 显示「已连接共享后台 · 前台采集电脑（补测）」；表单分支单测级渲染路径 |
| 环境总览 | 诊断页 | `/api/connections` 快照、总状态、预检阻塞、最近检查时间；不自动轮询 | 真实 Worker 显示三模块与阻塞提示 |
| 模块动作 | 诊断页模块卡 | 重连（`/api/connections/connect` 强制）、配置模型跳设置、重新连接、按 target 执行系统设置或单项测试；批量动作时禁用 | 「配置模型」真实跳转 `/settings`；按钮集合断言 |
| 高级诊断 | 折叠区 | 阶段、事件流（倒序）、底层检查项与各自动作；动作目标白名单校验，未知目标不误调 | 事件/检查渲染；目标解析单测 |
| 角色权限 | 页面与路由 | Hub 无采集参数、目录选择与环境诊断入口，直接访问 `/diagnostics` 显示仅采集端可用 | Hub 截图与 DOM 断言；服务端中间件未改动 |
| 旧入口保留 | 右栏「高级工具」 | 人工标注、模型用量指向旧版 hash（模型用量为旧设置页内弹窗），4C 迁移后再替换 | 链接指向 `/#annotation`、`/#settings` |

## 2. 文件清单

新增：

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `src/features/settings/types.ts` | 78 | 配置、保存载荷、目录选择、调试占用、共享配对 DTO |
| `src/features/settings/api.ts` | 39 | 配置读写、目录选择、模型测试、调试占用/清理、共享配对 |
| `src/features/settings/hooks.ts` | 28 | 配置与调试占用查询键 |
| `src/features/settings/format.ts` | 130 | 表单/载荷转换、范围校验、思考模式三态、占用文案 |
| `src/features/settings/format.test.ts` | 83 | 5 项单测（密钥留空/清除、隐藏参数保留、范围与 HTTPS、占用文案） |
| `src/features/settings/SharedWorkbenchCard.tsx` | 180 | Hub 概况与设备入口；Worker 配对表单与凭证失效提示 |
| `src/features/settings/SettingsPage.tsx` | 581 | 设置表单、模型测试、调试日志、危险操作确认、旧版入口 |
| `src/features/diagnostics/types.ts` | 56 | 连接快照、模块、动作、检查项、事件 DTO |
| `src/features/diagnostics/api.ts` | 23 | 快照、强制检查、单项测试、系统设置 |
| `src/features/diagnostics/format.ts` | 40 | 状态口径、测试目标与系统设置面板白名单 |
| `src/features/diagnostics/format.test.ts` | 22 | 3 项单测（未知状态、目标白名单、面板白名单） |
| `src/features/diagnostics/DiagnosticsPage.tsx` | 254 | 总览、模块卡、高级诊断、动作分发与角色拦截 |

修改：

- `src/app/App.tsx`：新增 `/settings`、`/diagnostics` 路由。
- `src/app/AppShell.tsx`：设置项标记为已迁移。
- `UFS_Menu_Scraper/macos_runner/web/react/`：重建产物 `index-grBdMn-Y.js` / `index-D15Pe7pd.css`，旧产物已移除。

## 3. 测试与证据

| 项 | 命令/方式 | 结果 |
| --- | --- | --- |
| 类型检查 | `npm run typecheck` | 通过，0 错误 |
| 单元测试 | `npm run test -- --run` | 10 个文件、50 个用例全部通过（设置 5 + 诊断 3） |
| 生产构建 | `npm run build` | 成功，仅既有 chunk 体积警告 |
| 构建引用 | `index.html` 与 `assets/` 比对 | 引用 `index-grBdMn-Y.js`、`index-D15Pe7pd.css`，旧产物已删除 |
| 浏览器验收 | Playwright（`channel="msedge"`，1440×1000） | 见 `verify/phase-4b/ui-check.json`，无页面脚本错误 |

UI 断言明细：

| 断言 | 结果 |
| --- | --- |
| Worker 设置页 8 个分区齐全 | 通过 |
| Worker 输出目录读取真实值 | 通过（`D:\menu-agent\.tmp-quality-test\worker\output`） |
| 保存配置往返 `POST /api/config` | 通过（toast「配置已保存」） |
| 调试占用展示与刷新 | 通过（`已用 2.7 MB / 上限 512 MB · 2 个文件`） |
| 清理日志确认窗文案/取消关闭 | 通过 |
| 诊断页总览 + 3 模块 + 动作 | 通过（`重新检测`/`重新连接`/`配置模型`） |
| 诊断「配置模型」跳转设置 | 通过 |
| Hub 设置无采集参数、无目录选择按钮 | 通过 |
| Hub 共享工作台概况与设备入口 | 通过（4 台/在线 1 台） |
| Hub 保存配置往返 | 通过 |
| Hub 直接访问诊断页被角色拦截 | 通过 |
| 页面脚本异常 | 0 |

截图：

- `docs/archive/2026-11-react-migration/screenshots/phase-4b-worker-settings.jpg`（Worker 设置页顶部）
- `docs/archive/2026-11-react-migration/screenshots/phase-4b-worker-settings-debug.jpg`（截图参数与调试日志）
- `docs/archive/2026-11-react-migration/screenshots/phase-4b-clear-confirm.jpg`（清理日志二次确认）
- `docs/archive/2026-11-react-migration/screenshots/phase-4b-worker-diagnostics.jpg`（Worker 环境诊断）
- `docs/archive/2026-11-react-migration/screenshots/phase-4b-hub-settings.jpg`（Hub 设置与角色差异）
- `docs/archive/2026-11-react-migration/screenshots/phase-4b-hub-diagnostics-role.jpg`（Hub 诊断角色拦截）

浏览器 console 仅剩既有噪音：CSP 拦截 KaTeX 远程字体；本阶段新增页面无脚本错误。

## 4. 隔离验证环境

| 项 | 值 |
| --- | --- |
| Hub | `8786`，home `D:\menu-agent\.tmp-quality-test\hub` |
| Basic 代理 | `8788 → 8786`（仅本机浏览器） |
| Worker | `8787`，home `D:\menu-agent\.tmp-quality-test\worker`，已配对 6 店 |
| 前端入口 | `/?ui=react#/settings`、`/#/diagnostics` |
| 未触碰 | 运行实例 `8765`、生产数据、旧前端产物 |

## 5. 已知限制与风险

1. 人工标注与模型用量仍为旧版入口（Phase 4C 迁移）；本页不复制旧弹窗逻辑，链接回旧设置页/标注页。
2. 消息中的“检查连接/重新连接”、模型连接测试和调试日志清理在隔离环境只验证了入口、禁用态与确认流程，未执行真实桌面探测与删除动作，避免打断隔离 Worker 的微信状态；真实探测在 V10 真机验收补充。
3. Hub 保存模型配置时按当前值回传未展示的采集参数，避免服务端默认值覆盖；已在单测固定该行为。
4. R15（沿用）：撤销“采集不合格”后任务仍为 `rejected`、`/retry` 返回 400，前端未放宽重采入口，等待 #22/#20 规则确认。
5. “重新配对”只提供 Worker 表单，不包含 Hub 侧解除/移除；后者已在采集电脑页实现。

## 6. 回滚说明

- 代码回滚：`git revert <本阶段提交>`，仅影响 `web-react/src`、`web/react` 产物与 `docs/`，无数据库迁移、无后端改动。
- 运行回滚：入口使用 `?ui=react` 显式加载；移除该参数即回到旧前端。旧设置页与标注页未删除，hash 入口仍可用。
- 产物回滚：恢复上一版 `assets/index-*.js|css` 与 `index.html` 引用。
- 数据回滚：本阶段只读写既有配置/调试日志；隔离环境可整目录删除 `D:\menu-agent\.tmp-quality-test`。
