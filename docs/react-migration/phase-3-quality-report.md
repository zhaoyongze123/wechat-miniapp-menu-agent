# 阶段报告：质量审核 React 迁移

- 分支：`feat/24-react-migration`
- 工作区：`D:\menu-agent-react`
- 范围：`features/quality`（不合格清单、待检查清单、两套详情、批次通过/撤销）
- 状态：等待验收；未合并 main、未部署生产、未删除旧前端
- 后端：本阶段零改动（`git diff --stat 67597bc..HEAD -- "*.py"` 为空）

## 1. 交付范围

| 能力 | 实现 |
| --- | --- |
| 视图切换 | 页内分段控件「不合格门店 / 待检查门店」，URL 持久化 `?view=review`；旧路由 `#/review-queue` 自动重定向 |
| 不合格清单 | 批次/来源/原因/确认状态四类服务端筛选、300ms 防抖搜索、5 列统计、客户端分页 |
| 人工确认 | 行内与抽屉复选框，真实调用 `/api/rejections/review`，统计即时刷新 |
| 不合格详情 | 来源/批次/阶段/原因/时间/确认状态、证据与任务源图、上一家/下一家 |
| 来源分流处置 | 素材：继续处理（跳门店）、撤销不合格；采集：撤销不合格、打回素材清洗（确认窗含门店与批次）、重新采集（按状态启用） |
| 导出 Excel | 导出当前服务端筛选（批次/来源/原因/搜索），真实请求 `/api/rejections/export.xlsx` |
| 待检查清单 | 4 项统计、批次范围多选、分类筛选与计数、服务端搜索、分页、当前页全选 |
| 待检查详情 | 状态/失败标签/分类/批次阶段/错误码/源路径/输出目录、源图与最近画面、上一家/下一家 |
| 重跑 | Hub 走采集分配弹窗（选中批量或单行），未提交时不改动数据；分配成功后刷新 |
| 批次通过 | 「标记批次通过 / 撤销通过」确认窗显示批次数量与范围，真实写入批次工作流阶段 |
| 跳转素材 | 「继续处理」携带 `batch` 与 `store` 进入素材清洗，并自动打开该门店详情抽屉 |

## 2. 文件清单

新增：

| 文件 | 行数 | 说明 |
| --- | --- | --- |
| `src/features/quality/types.ts` | 99 | 不合格/待检查 DTO |
| `src/features/quality/api.ts` | 160 | 列表、确认、撤销、打回、重试、导出、待检查、批次通过与撤销 |
| `src/features/quality/format.ts` | 74 | 来源/确认/处置桶/重试资格/记录键 |
| `src/features/quality/format.test.ts` | 59 | 5 项口径单测 |
| `src/features/quality/hooks.ts` | 78 | 查询键、列表查询、运行状态轮询、动作 mutations |
| `src/features/quality/QualityPage.tsx` | 941 | 页面装配、筛选、两套表格、确认窗、分配弹窗 |
| `src/features/quality/RejectionDetailDrawer.tsx` | 250 | 不合格详情与来源分流处置 |
| `src/features/quality/PendingReviewDrawer.tsx` | 167 | 待检查详情与重跑入口 |

修改：

- `src/app/App.tsx`：新增 `/rejections` 与 `/review-queue` 重定向。
- `src/app/AppShell.tsx`：质量审核导航标记已迁移。
- `src/features/materials/MaterialsPage.tsx`（8 行）：读取 `?store=`，跳转后只消费一次并自动打开详情。
- `UFS_Menu_Scraper/macos_runner/web/react/`：重建产物 `index-CqCYDvtw.js` / `index-DIYRTIdd.css`。

## 3. 口径与行为对齐

- 人工确认与不合格原因是两条独立链路：确认只切换 `reviewed_at`，不改变任务状态。
- 撤销不合格：素材来源走 `/api/rejections/materials/{batch}/{store}/undo`，采集来源走 `/api/tasks/{id}/rejection/undo`。
- 打回素材清洗：清除不合格、素材决策回待处理、采集任务置为已阻塞；确认窗展示门店与批次。
- 重新采集：仅 `needs_review/needs_retry/error/blocked/captured` 可点，运行中禁用；成功后跳转工作台并携带任务编号。
- 待检查分类优先使用后端 `bucket/bucket_label`；缺失或未知值统一归入「其他·未分类」，不在前端复制失败码表。
- 批次通过/撤销只记录审核结论（`set_approved`），不改变门店任务状态；撤销后回到自动阶段判断。
- 重跑在 Hub 走分配流程，不在前端直接调用 `/api/reviews/pending/rerun`。
- 状态徽标复用任务中心既有 `statusOf`，与其余 React 页面口径一致。

## 4. 验证证据

前端门禁（2026-11-24 00:45 于 `web-react`）：

- `npm run typecheck`：0 错误。
- `npm test`：7 个文件、35 个用例全部通过（质量审核 5 项）。
- `npm run build`：通过，产物 `index-CqCYDvtw.js`，服务端已加载新包。

真实 API 浏览器实测（`http://127.0.0.1:8783/?ui=react&build=v2`，无 Mock）：

| 场景 | 证据 |
| --- | --- |
| 不合格清单加载 | 真实 3 条（1 素材 + 2 采集），统计 3/3，批次/来源/原因/确认筛选可用 |
| 来源筛选 | 选「素材清洗」后仅剩 1 行；「采集任务」两行 |
| 人工确认 | 抽屉内取消勾选后提示「已取消人工确认」，统计由 1 变 0 |
| 素材来源详情 | 抽屉展示来源、原因、批次、时间、候选二维码区域与处置按钮 |
| 撤销不合格 | 素材记录撤销后统计 3→2、素材/采集 1/2→0/2，行消失 |
| 继续处理 | 跳转 `#/materials?batch=…&store=…`，素材页自动打开「演示门店001」抽屉 |
| 采集来源详情 | 来源「采集任务」、源图卡片、打回/重采/撤销三按钮；打回确认窗显示门店与批次，取消后统计不变 |
| 导出 Excel | 点击后请求 `GET /api/rejections/export.xlsx?...` 返回 200，提示「已导出当前筛选的不合格门店」 |
| 待检查清单 | 2 家门店、1 个批次、需人工确认 2、可重跑 2；批次范围显示演示批次；「其他·未分类（2）」 |
| 待检查详情 | 状态、失败标签、批次阶段、任务编号、源路径、源图与最近画面 |
| 勾选与全选 | 勾选 1 行后「已选 1 家门店」「重跑选中（1）」可用，表头呈混合态 |
| 重跑分配 | 打开「分配 1 家门店」弹窗，含所选门店；取消后统计与勾选不变 |
| 批次通过 | 确认窗显示 1 个批次（当前列表全部批次）；写入后行阶段变「人工审核通过（可建档）」；撤销后恢复「已重跑」 |
| 分类筛选 | 「素材问题（0）」显示空状态，「全部（2）」恢复 2 行 |
| 旧路由兼容 | `#/review-queue` 自动变为 `#/rejections?view=review`，内容正常 |

截图目录 `docs/react-migration/screenshots/`：

`quality-rejections.jpg`、`quality-rejection-detail.jpg`、`quality-continue-materials.jpg`、`quality-review-queue.jpg`、`quality-review-detail.jpg`、`quality-review-approved.jpg`。

数据状态（验收后已还原）：

- 临时验证批次（3 家门店）已软删并彻底清除，回收站 `deleted_count=0`。
- 演示批次保持 `128 家 / 已确认 128 / 待确认 0 / 不合格 0`。
- 人工确认标记、批次通过标记均已往返还原；不合格清单回到 2 条采集来源。

## 5. 已知遗留

1. 采集来源「撤销不合格」未实际提交（素材来源已实测），避免改写演示任务状态且缺少便捷还原路径。
2. 「打回素材清洗」只验证到确认窗与取消，未真实提交。
3. 「重新采集」在真实记录上因状态不可重采而禁用，跳转工作台的提交路径未实测。
4. 分配弹窗未提交（当前无在线采集电脑，确认按钮禁用）。
5. Windows 采集端视角的角色守卫未做浏览器切换验证（导航对 worker 隐藏审核入口）。
6. 待检查列表的状态徽标沿用任务中心英文状态码（如 `needs_review`），与现有 React 页面一致。

## 6. 回滚方式

1. `git revert 4b5325e 758c55f 03204da 093b879`。
2. 或恢复 `web/react/` 到 `67597bc` 版本后重启 Hub；也可用 `?ui=vue` 回旧前端（旧前端未删除）。
3. 本阶段未改后端、数据库结构与 Hub↔Windows 协议，无数据迁移需要回滚。

## 7. 提交记录

| 提交 | 内容 |
| --- | --- |
| `093b879` | 质量审核数据层、处置口径与查询动作 |
| `03204da` | 质量审核页面（不合格清单、待检查、详情与批次通过） |
| `758c55f` | 不合格继续处理跳转素材门店 |
| `4b5325e` | 重建 React 生产产物 |
| 待提交 | 本报告与验收截图 |
