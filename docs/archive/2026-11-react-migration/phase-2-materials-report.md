# 阶段报告：素材清洗 React 迁移

- 分支：`feat/24-react-migration`
- 工作区：`D:\menu-agent-react`
- 范围：`features/materials`（素材清洗）全部业务界面，不含质量审核（下一阶段）
- 完成后状态：等待验收，未合并 main、未部署生产、未删除旧前端

## 1. 交付范围

| 能力 | 实现 |
| --- | --- |
| 批次工作台 | 批次选择（服务端 options）、状态/工作流/版本徽标、5 项统计、门店筛选/搜索/分页、URL 持久化 `?batch=` |
| 单店选码 | 详情抽屉：原图、候选缩略图、放大切换、推荐理由、结论卡；确认/恢复待审核/标记不合格/重新分析 |
| 手动框选 | 原图 pointer 拖拽生成归一化 Box（最小 2%），提交人工框选结果 |
| 不合格处置 | 14 类原因，默认 `blur`，`other` 必填说明，确认前展示门店与范围 |
| 补图 | 追加/替换两种模式、单店 100 张/500MB 预检、逐文件 PUT 与进度、可取消 |
| 导入批次 | 本地图片/文件夹（`webkitdirectory`）与服务器目录两种来源、命中预检、进度与取消 |
| 分析作业 | 启动分析、进度/ETA/当前门店/事件、暂停/继续/取消（二次确认）、历史作业展开 |
| 自动确认推荐 | 批量采纳推荐、失败项提示、作业状态轮询 |
| 发布 | 批次名/版本/描述、建议版本 `v1.0.1`、draft 变更摘要、历史版本与“设为默认”二次确认 |
| 返工 | 4 项统计、分类筛选、300ms 防抖服务端 `q`、跨页选择（`Set` 键）、批量打回、批量发布、错误汇总 |
| 批次管理 | 回收站开关、行内阶段选择、编辑、清除选用、软删/恢复/彻底删除（阻断原因+二次确认） |

## 2. 文件清单

新增：

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `src/features/materials/types.ts` | 310 | 素材域完整 DTO |
| `src/features/materials/api.ts` | 215 | 全部素材接口与图片 URL 构造 |
| `src/features/materials/format.ts` | 176 | 决策/状态/用途/推荐/拒绝原因/上传预检/门店键 |
| `src/features/materials/format.test.ts` | 65 | 7 项状态与格式单测 |
| `src/features/materials/hooks.ts` | 320 | 查询键、轮询、批次/单店/上传/返工动作 |
| `src/features/materials/MaterialsPage.tsx` | 635 | 页面装配、筛选、分页、统计、弹窗编排 |
| `src/features/materials/StoreDetailDrawer.tsx` | 528 | 单店详情与选码/处置 |
| `src/features/materials/ManualQrCanvas.tsx` | 106 | 归一化坐标框选 |
| `src/features/materials/ImportBatchDialog.tsx` | 224 | 导入批次 |
| `src/features/materials/AnalysisDialog.tsx` | 191 | 分析作业进度与历史 |
| `src/features/materials/PublishDialog.tsx` | 199 | 发布与版本激活 |
| `src/features/materials/ReworkPanel.tsx` | 394 | 返工列表与批量操作 |
| `src/features/materials/BatchManagementDialog.tsx` | 337 | 批次管理/回收站 |

修改：

- `src/app/App.tsx`：新增 `/materials` 路由。
- `src/app/AppShell.tsx`：素材清洗导航标记已迁移。
- `UFS_Menu_Scraper/macos_runner/web/react/`：重建产物 `index-Ddf8CmZh.js` / `index-CQvDm6Yl.css`。

后端、数据库与 Hub↔Windows 协议零改动（`git diff --stat 80a0e89..HEAD` 仅前端与文档）。

## 3. 口径对齐（实测中发现并修正）

- 返工门店键必须为 `batch_id:store_id`（单冒号），修正前使用 `::` 会被后端拒绝。
- 工作流阶段与后端 `_WORKFLOW_LABELS` 对齐：`''`（自动）、`not_started`、`first_run`、`rerun`、`approved`。
- `rework` 接口补充 `q` 参数并保持翻页数据（`keepPreviousData`）。
- 小程序校验状态补充 `unverified`。

## 4. 验证证据

前端门禁（2026-11-24 00:00 于 `web-react`）：

- `npm run typecheck`：0 错误。
- `npm test`：6 个测试文件、30 个用例全部通过（含素材 7 项）。
- `npm run build`：通过，产物 `index-Ddf8CmZh.js`，FastAPI `?ui=react&build=v2` 已加载新包。

真实 API 浏览器实测（`http://127.0.0.1:8783/?ui=react&build=v2`，无 Mock）：

- 批次加载与统计：门店 128 / 原图 128 / 待审核 0 / 已确认 128 / 不合格 0。
- 待审核筛选、详情抽屉、候选确认（127→128）、标记不合格（`blur`，127/1）、恢复待审核并重新确认（回到 128/0）。
- 发布弹窗：建议版本 `v1.0.1`、当前默认 `v1.0.0`、无切换按钮时按预期隐藏。
- 批次管理：当前批次徽标、阶段自动判断、删除阻断原因、编辑弹窗取消。
- 服务器目录导入 3 店并完成分析 3/3，进度、当前门店、历史作业均正确。
- 手动框选 UI：选框与“确认人工框选”启用状态正确（见遗留 2）。
- 回收站：软删、恢复、彻底删除；演示批次已还原，`deleted_count=0`。
- 构建后冒烟：任务中心与素材清洗均正常，侧栏选中态为官方 `bg-secondary-soft`。

截图目录 `docs/archive/2026-11-react-migration/screenshots/`：

`materials-overview.jpg`、`materials-overview-final.jpg`、`materials-detail.jpg`、`materials-confirmed.jpg`、`materials-rejected.jpg`、`materials-rejected-stats.jpg`、`materials-undo.jpg`、`materials-rework.jpg`、`materials-publish.jpg`、`materials-management.jpg`、`materials-management-deleted.jpg`、`materials-import.jpg`、`materials-analysis.jpg`、`materials-manual.jpg`。

Python 回归（后端零改动，作为环境基线）：

- 全量 `97 passed, 1 skipped, 3 failed`（338.52s）。
  - `test_skip_pending_and_adopted_recommendation_are_not_implicit_confirmations`：全量下 `FileNotFoundError`（manifest），单独重跑通过，属测试间共享状态。
  - `test_purge_journal_recovers_interruption_without_touching_originals[False/True]`：Windows `PermissionError [WinError 5]`，`path.rename` 目录句柄竞态，单独跑 `test_batch_management.py` 仍失败，既有环境问题。

## 5. 已知遗留

1. 返工面板只验证了真实空状态，未用真实返工数据实测“批量打回/批量发布”。
2. 手动框选仅验证 UI 状态，未做真实物理拖拽并提交到后端。
3. 批次管理“恢复”按钮未在浏览器实测（软删/彻底删除已测）。
4. 上述 Python 两个 Windows 权限失败用例未处理（本阶段不改后端）。

## 6. 回滚方式

1. 前端回退：`git revert 7e846b0 f54eb48 15abd2f 9e3f1b8`，或直接 checkout 上一构建产物并重建。
2. 产物回退：将 `UFS_Menu_Scraper/macos_runner/web/react/` 恢复到 `9fadf44` 版本后重启 Hub。
3. 运行时回退：访问 `/?ui=vue`（或去掉 `ui=react`）继续使用旧前端，旧前端本阶段未删除。
4. 素材数据：本阶段未改数据模型；演示批次已恢复为 128/128，回收站为空。

## 7. 提交记录

| 提交 | 内容 |
| --- | --- |
| `9e3f1b8` | 素材清洗 DTO、接口适配与状态口径 |
| `15abd2f` | 校正门店键格式、工作流阶段枚举与返工搜索参数 |
| `f54eb48` | 素材清洗工作台 UI（9 个组件 + 路由） |
| `7e846b0` | 重新构建 React 生产产物 |
| 待提交 | 本报告与验收截图 |
