# Phase -1：Windows 成果同步与 React 基线报告

- 报告生成时间：2026-11-23
- 关联计划：Issue #24《React 全量迁移》前置闸门 Phase -1
- 仓库：`zhaoyongze123/wechat-miniapp-menu-agent`
- 工作区：`D:\menu-agent`（Windows 原工作区，分支 `main`）

## 一、结论

1. Windows 工作区的 17 个已跟踪改动全部属于**应纳入 main 的业务修复与功能**，没有夹带本机配置、数据库、密钥或构建产物。
2. 这 17 个改动已经通过 **PR #19** 进入 `origin/main`（3 个提交、18 个文件，含新增测试）。本地工作区只是缺少最新提交，属于“落后”而非“分叉”。
3. 本次通过 **ff-only** 把本地 `main` 从 `d1f4b48` 快进到 `c5e9c34`，未执行 `reset --hard`、`clean -fd` 或强制推送。
4. 本地未提交成果已保护性存档到独立分支 `fix/24-windows-baseline-sync`（提交 `623c85e`），可随时恢复。
5. 当前工作区无待提交的项目源码改动；本报告提交后，React 工作区从**新的 `origin/main`** 创建，不再使用旧 SHA `d1f4b48` / `c5e9c34` 作为正式基线。

## 二、同步前后 SHA

| 项目 | 同步前 | 同步后 |
| --- | --- | --- |
| 本地分支 | `main` | `main` |
| 本地 HEAD | `d1f4b48` | `c5e9c34`（代码同步基线） |
| `origin/main` | `c5e9c34` | 本报告提交后的最新 SHA |
| 本地相对远端 | 落后 4 个提交、无本地独有提交 | 一致 |

Phase -1 最终文档提交 SHA：

```powershell
git log -1 --format=%H -- docs/react-migration/sync-baseline-report.md
```

## 三、17 个改动文件审查与去向

| # | 文件 | 分类 | 去向 |
| --- | --- | --- | --- |
| 1 | `macos_runner/menu_capture/capture_results.py` | 门店状态口径统一 | `fc4f7bc` |
| 2 | `macos_runner/menu_capture/config.py` | Windows 微信窗口校准配置 | `624d47d` |
| 3 | `macos_runner/menu_capture/failures.py` | 失败原因筛选与文案 | `fc4f7bc` |
| 4 | `macos_runner/menu_capture/materials.py` | 当前快照映射 | `fc4f7bc` |
| 5 | `macos_runner/menu_capture/rejections.py` | 素材返工按来源路径解析批次 | `fc4f7bc` |
| 6 | `macos_runner/menu_capture/server.py` | 审核/待检查接口口径 | `fc4f7bc` |
| 7 | `macos_runner/menu_capture/shared.py` | 共享端快照与状态下发 | `fc4f7bc` |
| 8 | `macos_runner/menu_capture/shared_worker.py` | 采集端快照过滤/状态跟随；缓存二维码登记 | `fc4f7bc` + `6a8a309` |
| 9 | `macos_runner/menu_capture/store.py` | 任务/批次关联与恢复 | `fc4f7bc` |
| 10 | `macos_runner/menu_capture/uipath_runner.py` | Windows 原生采集容错与窗口恢复 | `624d47d` |
| 11 | `macos_runner/menu_capture/windows_desktop.py` | 微信分栏/残留小程序隔离 | `624d47d` |
| 12 | `macos_runner/menu_capture/workbench_page.py` | 工作台统计与筛选口径 | `fc4f7bc` |
| 13 | `macos_runner/tests/test_capture_outcomes.py` | 状态口径测试 | `fc4f7bc` |
| 14 | `macos_runner/tests/test_failure_codes.py` | 失败原因测试 | `fc4f7bc` |
| 15 | `macos_runner/tests/test_workbench_page.py` | 工作台测试 | `fc4f7bc` |
| 16 | `macos_runner/ui_tars/adapter.mjs` | 工具调用格式错误重试 | `624d47d` |
| 17 | `macos_runner/web/index.html` | 与后端协议耦合的工作台 UI | `fc4f7bc` |

### 分类结论

- **应纳入 main**：上述 17 个文件全部属于此类。
- **独立执行器清理分支的工作**：本次 17 个文件中没有；`feat/agent-target-history` 等独立分支保持独立。
- **仅本机配置或暂存数据**：无已跟踪文件；未跟踪目录 `.build-tmp/`、`.cache/`、`.pytest-tmp/`、`.tmp-build/`、`.tmp-verify-7/` 已加入仓库本地 `.git/info/exclude`，不入库。
- **日志/产物/机密/不可提交内容**：无。

### 本地与远端的唯一实质差异

本地 `shared_worker.py` 是较早实现，远端 `6a8a309` 提供了更强的“缓存二维码与分配一致登记”逻辑并新增 68 行隔离测试。快进后采用远端版本，本地旧实现已包含在保护性存档 `623c85e` 中，可追溯、无需重复提交。

## 四、整合与冲突处理

1. 保护性存档：在 `fix/24-windows-baseline-sync` 分支提交 `623c85e`（17 files, +600/-91），只暂存已跟踪文件，未提交临时目录。
2. 切换回 `main`，执行 `git merge --ff-only origin/main`，`d1f4b48..c5e9c34` 快进成功。
3. 冲突：无。未使用 ours/theirs 全量覆盖。
4. 未触碰其他独立分支/工作树：`fix/12-windows-worktree-sync`、`fix/windows-qr-handoff`、`feat/agent-target-history`、`docs/1-source-evidence` 等保持原状。

## 五、测试与 CI 证据

本机在快进后的 `c5e9c34` 上执行：

```powershell
.\.venv\Scripts\python.exe -m pytest tests/test_shared_workbench.py tests/test_workbench_page.py tests/test_capture_outcomes.py tests/test_failure_codes.py tests/test_review_queue.py -q --basetemp=D:\menu-agent\.tmp-verify-7\pytest-main
```

结果：**31 passed，1 failed（132.76s）**。

- 失败项：`tests/test_review_queue.py::test_pending_reviews_include_batch_and_approval`
- 原因：测试只创建本地批次并扫描任务，没有发布快照；而 `review_queue` 只读取“未删除批次的最新发布快照”，因此返回 0。
- 定性：**改动前既有问题**。`d1f4b48` 上 `review_queue` 已是相同的 effective-only 实现，该测试文件未被同步提交更新；与本次 Phase -1 整合无关，记录为 React 迁移前已知基线缺陷。

PR #19 已记录的历史验证（远端合并前）：

- Python：107 项通过、2 项跳过；6 项旧 UiPath 测试在干净 `d1f4b48` 上同样失败，已排除（Windows 正式执行链路不依赖旧 UiPath 执行器）。
- Node：`ui_tars` 适配器 59 项测试通过，工作台内联 JavaScript 语法检查通过。
- GitHub Actions：Windows 安装包与共享后台容器校验通过。

PR：<https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/pull/19>（已合并）。

## 六、React 工作区

- 分支：`feat/24-react-migration`
- 工作树：`D:\menu-agent-react`
- 创建来源：本报告提交后的最新 `origin/main`
- 基线 SHA：`git -C D:\menu-agent-react rev-parse HEAD`
- 原 Windows 工作树保持在 `main`，不继续承载 React 混合改动。

## 七、保留分支与本机配置

- 保护性存档：`fix/24-windows-baseline-sync` @ `623c85e`（仅本地，不推送）。
- 其余独立工作树保持原样，未合并、未删除。
- 本机排除规则：`.git/info/exclude` 中的 5 个临时目录；不影响远端与 CI。

## 八、Phase 0 进入条件核对

| 条件 | 状态 |
| --- | --- |
| 远端 main 含经审查确认的 Windows 有效成果 | 是（PR #19，`fc4f7bc`/`624d47d`/`6a8a309`） |
| 本地 main 与远端 SHA 一致 | 是（本报告提交后再次核对） |
| 工作区无待提交项目源码改动 | 是 |
| 17 个改动均有审查去向 | 是（见第三节） |
| 测试结果与失败原因明确 | 是（31 通过 / 1 既有失败） |
| React 工作区创建自新 SHA | 是（`feat/24-react-migration`） |
