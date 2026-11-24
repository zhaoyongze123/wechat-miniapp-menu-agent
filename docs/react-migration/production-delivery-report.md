# 生产版本交付报告（React 新版本替换 0.1.2）

日期：2026-11-24
交付分支：`feat/26-production-delivery`（HEAD `e90a8ed`）
目标版本：`0.2.0-rc.3`
关联 Issue：#3、#8、#20、#21、#22、#23、#24、#26

## 1. 结论

React 新版本已完成开发、自动化回归、浏览器验收与单台真实微信采集验收，发布产物已由 CI 构建并校验。
代码与产物满足替换现有 0.1.2 的条件；**唯一未闭环项是本机 0.1.2 → 0.2.0-rc.3 安装 smoke 需要生产停机窗口**（安装器固定 AppId，升级会覆盖现有 0.1.2 并在 8765 端口重启服务）。该项需业务确认后执行，不在未授权情况下操作生产。

生产服务当前状态保持原样：PID 15512，`127.0.0.1:8765`，`running=false`，批次 `25de5ef5b8fe49b7aa8e0168bda9699b` 仍为 `paused`、`current_index=75/88`，本轮全部操作未触碰该批次。

## 2. 交付产物

| 产物 | 大小（字节） | SHA256 |
| --- | --- | --- |
| `windows-x64.zip`（含安装包与校验文件） | 237,511,682 | `A7B7367636BE4293A2E0C9A93A4848A130BBDEA8A2E2456EFE9452259FFE3C54` |
| `MenuCapture-Setup-0.2.0-rc.3-win-x64.exe` | 237,998,320 | `117cc195d78cca14df0c56b4959a440c9657f21e3e3170a3ac7a441f24d4c78f` |
| `hub-linux-amd64.zip` | 175,234,768 | `E925ECFAE64623DA8C4A57D673055AF0E546CC446ECEEBB9F563B98BE2D9B85B` |

- CI：`windows-release` 运行 [45328280453](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/actions/runs/45328280453)（commit `e90a8ed`），全部作业通过。
- 产物本地落盘：`D:\menu-agent-delivery-artifacts\`（离线副本，可直接用于发布）。
- 现有生产安装包（回滚基线）：`D:\menu-agent\deploy\artifacts\MenuCapture-Setup-0.1.2-win-x64.exe`，SHA256 `E21BF7B5805E6E499060B71246B7283F75B909A01172C8189A9E64F3F90583F3`。

## 3. 功能范围验收

| Issue | 内容 | 验收依据 |
| --- | --- | --- |
| #24 | React 全量迁移、移除 Vue 与旧前端 | `final-migration-report.md`：`/` 只返回 React、`/assets` 清单白名单、旧路由 404、23 个旧文件删除 |
| #23 | 采集工作台产品化与共享调度交互 | 浏览器验收 `hub-workbench.jpg`、`verify/worker-workbench/`；工作台默认入口、运行面板、同步状态弹窗 |
| #22 | 质量审核产品化与交互规范 | `hub-quality.jpg`、`hub-quality-review.jpg`、`verify/quality-high-risk/results.json`（批量撤销、打回素材、Excel 导出） |
| #21 | 任务中心精修与全局任务汇总 | `hub-task-center.jpg`；壳层 `ActiveTaskIndicator` 全局可见 |
| #20 | 门店级素材版本与采集状态解耦 | 候选端素材/质量页与后端契约测试；`verify/materials-*` |
| #8 | 任务详情、逐店筛选与快捷浮层 | `hub-assignments.jpg`、`hub-materials.jpg` 行内「详情」抽屉、筛选与快捷操作 |
| #3 | 打回素材批次关联、批量撤销不合格 | `verify/quality-high-risk/`（快照前后、撤销后队列、导出文件） |
| #26 | 远程置顶控制窗、通知提醒、连续处理、交互规范 | `worker-remote-control.jpg`、`hub-settings.jpg`、`hub-materials-continuity.jpg`；`documentPictureInPicture` 可用、通知权限已授权、两处连续处理开关可切换且刷新保持 |

## 4. 自动化验证

| 检查 | 结果 |
| --- | --- |
| `npm run typecheck` | 通过，0 错误 |
| `npm run test -- --run`（Vitest） | 17 个文件 / 83 用例通过 |
| `npm run build` | 通过；`web/react` 产物与入库版本一致 |
| Python 回归（11 个契约文件） | 129 用例通过：test_server、test_shared_workbench、test_windows_compat、test_environment、test_workbench_page、test_model_usage、test_model_usage_integration、test_materials、test_rejections_batch、test_review_queue、test_capture_outcomes |
| CI 发布作业 | 通过；含安装、升级、重启恢复、回滚与数据保留 smoke |

## 5. 浏览器验收（隔离环境：Hub 8781 / Worker 8782）

- 覆盖页面：工作台、分配记录、任务中心、质量审核、素材清洗、设置、环境诊断（Worker 角色）、远程半自动控制页。
- 结果：页面渲染与接口数据正常，控制台与页面错误 0 条。
- 远程置顶：`documentPictureInPicture` 可用，按钮「打开置顶控制窗」存在；不支持时给出明确提示。
- 通知：`Notification.permission === granted`，设置页「通知与提醒」面板可用。
- 连续处理：素材页「提交本店决策后自动进入下一家」、质量页「确认不合格后自动进入下一条」均可切换，刷新后偏好保持（localStorage）。
- 截图：`docs/react-migration/screenshots/release/`（12 张）。

## 6. 真实微信采集验收（生产机单台）

- 运行：`58a1ea00d8f04f7ea644866c5bfb2806`，模式 `auto`，1 家门店，13:30:09 → 13:33:06，总耗时 177s。
- 链路：微信窗口准备 → 发送门店名 → 发送二维码 → 打开图片并识别 → 打开小程序 → GUI Agent 导航（12 次模型调用、12 张截图、10 次导航动作）→ 返回微信。
- 结果：`needs_review`，`error_code=agent_needs_review`；原因为该门店小程序停留在促销横幅页，点餐入口点击后无菜品列表，属门店页面结构问题，非执行故障。
- 复核：React 待处理队列正确显示该店为「技术重试 / GUI Agent 待人工检查」，证据截图完整。
- 证据：`docs/react-migration/verify/real-capture/`（batch-result、navigation-result、result）；原始运行目录已从生产 `.uipath-runs` 移出至 `D:\menu-agent-delivery-artifacts\real-capture\`，未向生产 `output` 写入任何数据。
- 生产批次 `25de5ef5…` 在采集前后均为 `paused`、index 75，未被中断或改写。

## 7. 数据兼容与设备身份

- 以生产库副本分别用候选版与 0.1.2 打开：设备身份、26 轮历史、2,196 条分配记录一致。
- 候选版未修改 `tasks.sqlite3` 结构；升级只切换 `releases/<版本>` 与 `current-version`，业务数据在安装目录之外。
- 离线与断线补传：双设备隔离契约测试覆盖（同 run id、离线改名、重启补传、旧请求重放），本轮未在真机重复。

## 8. 安装、升级与回滚

已具备：
- 版本并存安装（`releases/<版本>`）、`current-version` 切换、随包 `rollback.ps1`。
- `smoke.ps1` 按**实际监听 8765 的进程**核对版本（修复了旧守护进程仍监听导致误判的问题），并校验 `current-version`、`release-manifest.json`、前端入口 SHA256、`/api/version`。
- CI smoke 证据：`0.1.0 → 0.2.0-rc.3 → 0.1.0`，日志显示 `Install #1/#2 exit code 0`、`Rollback switched worker 6924 to 1804 on version 0.1.0`、`Previous release install, candidate upgrade, restart recovery, rollback and data preservation passed.`

待执行：
- 本机 `0.1.2 → 0.2.0-rc.3 → 0.1.2` smoke。`smoke.ps1` 在 8765 被占用或存在 MenuCapture 计划任务时主动拒绝；安装器固定 AppId，执行即等于短暂停产升级。需确认停机窗口后运行。

## 9. 上线方案

1. 备份：停止工作台继续采集（保持当前 paused），复制 `D:\menu-agent\data\MenuCapture`（含 `data\tasks.sqlite3`、`config.local.json`、`secrets.local.json`、`shared-worker.local.json`）到备份目录。
2. 安装：运行 `MenuCapture-Setup-0.2.0-rc.3-win-x64.exe`（用户级安装，写入新版本目录），确认 `current-version` 指向 `0.2.0-rc.3`。
3. 校验：`GET /healthz` 为 worker；`GET /api/version` 返回 `0.2.0-rc.3` 与 React 入口；打开工作台确认 paused 批次仍在、数据完整。
4. 恢复：在原工作台继续该批次，观察首店采集与结果落库。
5. 回填发布版本：将 `0.2.0-rc.3` 定版为 `0.2.0` 后按同一流程重打包，或直接以 rc.3 作为生产版本记录。

Hub 侧：`MENU_CAPTURE_VERSION=0.2.0-rc.3 MENU_CAPTURE_IMAGE=menu-capture-hub:0.2.0-rc.3 docker compose -f deploy/compose.yaml up -d --build`，数据卷不变。

## 10. 回滚方案

- 采集端：`<安装目录>\rollback.ps1 -Version 0.1.2`，脚本校验目标版本文件后写入 `current-version`，等待空闲切换并以 `/api/version` 复核；旧版本目录在升级时保留。
- 兜底：重新运行 `MenuCapture-Setup-0.1.2-win-x64.exe`（SHA256 `E21BF7B5…`）。
- Hub：将 `MENU_CAPTURE_IMAGE` 指回上一标签后 `docker compose up -d`。
- 数据：升级/回滚均不改写业务数据；如需还原数据库，使用第 9 步备份副本。

## 11. 已知限制与风险

1. 本机 0.1.2 升级 smoke 未执行（需停机窗口），当前升级证据来自 CI 的 0.1.0 路径与版本目录并存机制。
2. 真机验收单店结果为 `needs_review`（门店小程序页面结构），本轮未取得 `captured` 成功样本；该分支由生产历史 524 条已采集记录与契约测试覆盖。
3. 现场仅单台生产机可用，双设备离线补传复用既有隔离测试证据。
4. GUI Agent 依赖本机已保存的 GUI-Plus 配置与 `ui_tars/node_modules`；安装包已内置运行依赖，生产机配置沿用现有安装。
