# Phase 3B 质量审核高风险场景补测报告

本报告对应 Issue #24 阶段计划中“进入 Windows 原生采集工作台迁移前”的前置核验，覆盖质量审核（#22）遗留高风险场景。结论取自隔离环境中的真实接口调用与界面截图，不改后端、不改门店版本模型。

## 1. 环境与隔离方式

| 项 | 值 |
| --- | --- |
| Hub 数据根 | `D:\menu-agent\.tmp-quality-test\hub`（复制自既有隔离演示 home，含 `raw` 副本） |
| Hub 端口 / 角色 | `8786` / `hub`（`MENU_CAPTURE_ADMIN_PASSWORD=demo-pass`，仅本机） |
| Worker 端口 / 角色 | `8781` / `worker`（原有隔离 worker）；`8787` / `worker`（迁移仓库产物，用于角色界面截图） |
| 截图代理 | `8788 → 8786` 注入 Basic 认证，仅供本机浏览器访问 |
| 前端产物 | 迁移仓库 `web/react`，入口 `?ui=react&build=v2` |
| 数据清理 | 补测完成后仅保留证据文件；隔离副本可整目录删除恢复，未触碰 8765 运行实例与生产数据 |

复制 home 后演示数据仍保存旧绝对路径，Hub 发布快照校验会判为来源缺失（分配冲突 `reason=source`）。本轮按“测试夹具归一化”把副本数据库与 `config.local.json` 的路径前缀改到副本目录，属于环境准备，不是产品缺陷。

## 2. 覆盖结果

自动化脚本 46 项断言全部通过；其中 1 项因演示数据没有 `runs` 运行历史而标记 SKIP，历史保留改以任务行与任务事件为证据。

| 场景 | 关键步骤 | 实际结果 |
| --- | --- | --- |
| 打回素材清洗 | `POST /api/rejections/tasks/{id}/return-to-materials` | 200，任务 `status=blocked`、`stage=returned_to_materials`，不合格字段清空；素材门店回到 `pending` |
| 打回后单店分配阻塞 | `POST /api/shared/assignments` | 409，冲突 `{"task_id":"7d24…","reason":"materials"}` |
| 打回后禁用重采 | `POST /api/tasks/{id}/retry` | 409 `{"detail":"该门店待重新选码并发布"}` |
| 历史保留 | `/api/status?include_events=true`、`/api/capture/queue` | 任务行仍存在（`returned_to_materials`），事件保留“已打回素材清洗：等待重新确认二维码” |
| Hub 权限 | 无认证访问 `/api/rejections` | 401 |
| Hub 写令牌 | 无 `x-capture-token` 调用 `/api/rejections/review` | 403 |
| Worker 只读 | `GET /api/rejections` | 200（允许只读核查） |
| Worker 写操作 | review / retry / 建批次 / 撤销素材不合格 / 分配 | 409（前四项）与 403（分配，先被本机令牌拦截） |
| Worker 角色界面 | 侧栏文本 | 仅“采集工作台 / 任务中心 / 设置”，无采集电脑、分配记录、素材清洗、质量审核 |
| 采集来源撤销不合格 | `POST /api/tasks/{id}/rejection/undo` | 200，`ok=true`，不合格字段清空，清单与导出同步归零 |
| 重新采集 | `POST /api/tasks/{id}/retry`（待检查任务） | 200，任务回到 `status=pending`、`stage=queued` |
| 真实分配提交 | `POST /api/shared/assignments` | 200，生成 `assigned` 分配（SH011），`/api/shared/status` 可见；重复分配占用中门店返回 409 `reason=assigned` |
| 收回分配 | `POST /api/shared/assignments/{id}/reclaim` | 200，记录转“已收回”，不再计入活动分配 |
| Excel 导出 | `/api/rejections/export.xlsx` 共 17 个筛选组合 | 表头与行集合跟列表接口完全一致；非法原因返回 400 |

关键响应摘录：

```json
{"task":{"status":"blocked","stage":"returned_to_materials","reject_reason":"",
 "message":"已打回素材清洗；请重新确认二维码后增量发布并重跑"}}
{"conflicts":[{"task_id":"7d243b8b464b6025c0d8","reason":"materials"}]}
{"detail":"此任务还没有可重试或重新采集的结果"}
{"conflicts":[{"task_id":"1d3277c67e2df0ec1a98","reason":"assigned","worker_name":"前台采集电脑"}]}
```

## 3. Excel 导出筛选核对

导出表头固定为：`门店 / 来源 / 批次 / 来源文件 / 处理阶段 / 不合格原因 / 备注 / 任务状态 / 标记时间 / 任务编号 / 人工确认`。

逐个筛选组合先取 `/api/rejections` 的记录集合，再解析导出的 xlsx 行，两者按集合比对一致：

| 筛选 | 导出数据行 |
| --- | --- |
| 全部 | 2（SH009、SH010） |
| `source=capture` | 2 |
| `source=materials`（无素材不合格时） | 0（仅表头） |
| `reason=blur`（无该原因时） | 0 |
| `q=SH010` / `q=不存在的门店` | 1 / 0 |
| `batch_id=命中` / `batch_id=不存在` | 2 / 0 |
| 组合筛选无命中 | 0 |
| 临时素材不合格后 `source=materials`、`reason=blur`、`q=SH009` | 各 1 |
| 撤销临时素材不合格后 `source=materials` | 0 |
| 撤销采集不合格后 `source=capture` | 0 |
| 非法原因 `reason=demo_blur` | 400 拒绝 |

注意：导出顺序随“标记时间倒序”变化，因此核对采用集合比对；筛选域与列表接口共用同一服务端实现，未发现前端二次过滤。

## 4. 补测前后状态

| 指标 | 补测前 | 补测后 |
| --- | --- | --- |
| 不合格门店/记录 | 2 / 2（素材 0、采集 2） | 0 / 0 |
| 已确认/跳过/待处理 | 128 / 0 / 0 | 127 / 0 / 1（SH009 打回待重新选码） |
| 待检查门店 | 2 | 1（SH012 重采后移出，SH011 保留） |
| 活动分配 | 5 | 5（补测分配已收回） |

“后”状态保留了打回与撤销的真实结果，符合预期；隔离副本可直接删除重建，不影响既有演示环境与生产。

## 5. 截图

- `screenshots/verify-quality-before.jpg`：补测前不合格清单 2 条
- `screenshots/verify-quality-after.jpg`：撤销/打回后清单为 0
- `screenshots/verify-materials-returned.jpg`：打回后素材门店回到待审核（128 家中 1 家待审核）
- `screenshots/verify-assignments.jpg`：真实分配提交后已收回，分配记录保留痕迹
- `screenshots/verify-worker-nav.jpg`：Worker 侧栏无 Hub 专属入口

原始证据在 `verify/quality-high-risk/`：`results.json`（46 条断言与完整响应）、`snapshot-before.json`、`snapshot-after.json`、`events-sh009.json`、`queue-*.json`、17 个导出文件、`report.md`、`nav-labels.json`。

## 6. 与 #20 门店版本模型相关的观察（只记录，不改代码）

1. **撤销采集不合格后任务状态仍为 `rejected`**：`POST /api/tasks/{id}/rejection/undo` 只清空不合格字段，不改变 `status`；随后 `/api/tasks/{id}/retry` 返回 400“此任务还没有可重试或重新采集的结果”，React 与旧前端都按 `needs_review / needs_retry / error / blocked / captured` 判定可重采，因此该门店只能走“打回素材清洗”或重新发布产生新任务。
   该行为是共享采集端 `rejected` 处置结果的既有语义，新旧前端口径一致，未发现本阶段迁移引入的回归；但“撤销后是否应允许直接重采”需要在 #22 完整业务验收或 #20 店级版本方案中确认，已登记为风险 R15。

2. **打回素材清洗会清空该门店已选二维码**：`decision=pending` 时 `selected_json` 置空，只有任务行与任务事件保留历史。这与“必须重新确认二维码后再发布”的提示一致；若 #20 要求保留历史选码快照，需要在店级版本方案中补充，不在本次迁移内改动。

## 7. 回滚说明

- 本次只新增 `docs/archive/2026-11-react-migration/screenshots/verify-*.jpg` 与 `docs/archive/2026-11-react-migration/verify/quality-high-risk/` 证据，以及本报告和风险登记中的 R15；无源码、构建产物、数据库 schema 或生产配置变更。
- 隔离环境回滚：停止 8786/8787/8788 进程，删除 `D:\menu-agent\.tmp-quality-test` 即可；原演示环境（8782/8783）与运行实例（8765）未受影响。
- 如需恢复补测前数据，重新从 `.tmp-verify-hub\hub` 复制副本并按本报告第 1 节修正路径即可。
