# Phase 4C 人工标注与模型用量迁移报告

日期：2026-11-24
分支：`feat/24-react-migration`
入口：`?ui=react&build=v2`（旧前端与旧 hash 入口保持不变）

## 1. 范围与结论

本阶段完成 Phase 4 的两个剩余能力：Windows 采集端「人工标注」与设置内「模型用量」。

- 新增独立页面 `/#/annotation`，仅 Worker 显示导航；Hub 访问时展示角色说明，不发起任何本机标注请求。
- 设置页的旧版用量入口替换为 React 弹窗，Hub 与 Worker 均可查看。
- 未修改后端接口、数据库结构与采集流程；旧 `web/` 前端未删除。
- 沿用《标题与辅助文案规范》：页面只有 H1，无自动副标题；说明仅出现在操作前提、数据范围、风险与状态区域。

## 2. 能力矩阵

### 人工标注

| 能力 | 实现 |
| --- | --- |
| 门店排队 | 读取本机任务列表，按全部 / 待处理 / 待复核 / 已采集 / 已标注 / 不合格分组并计数 |
| 选择门店 | 逐家勾选、当前分组全选、默认不自动勾选 |
| 开始会话 | 确认弹窗提示“微信停留在文件传输助手、屏幕解锁”，提交选中门店 |
| 暂停 / 恢复 | 调用暂停与恢复接口；暂停时停止画面轮询 |
| 截图与坐标 | Canvas 按截图原始像素绘制，拖动框选，提交截图坐标；支持触摸 |
| 系统建议 | 展示建议品类、置信度、匹配文本、错误；可一键应用品类/目标/位置 |
| 系统判断 | 展示来源、目标、命中阶段、方法、原因、候选目标；可一键采用位置 |
| 确认点击 | 二次确认坐标后提交，记录前后截图与画面变化 |
| 刷新截图 | 重新抓取当前画面；新画面自动请求一次系统判断 |
| 步骤记录 | 上一步/下一步切换，展示变化判定、品类、目标、备注与前后截图预览 |
| 完成 / 跳过 / 停止 | 完成归档并进入下一家；跳过不归档；停止结束会话并保留已完成记录 |
| 标记不合格 | 原因码与服务端 `REJECTION_REASONS` 完全一致；可选择“其他”并填写说明 |
| 异常重试 | 打开失败、收尾异常、标注异常时提供重试 |
| 回看 | 已标注门店可只读回看全部步骤；会话进行中沿用旧版提示，禁止回看 |

### 模型用量

| 能力 | 实现 |
| --- | --- |
| 筛选 | 日期范围（默认近 7 天）、用途、采集队列、统计维度（天/队列/门店/模型） |
| 汇总 | 调用/成功/失败/进行中/中断、输入/输出/总 Token、缓存读/推理/图片 Token、累计耗时 |
| 用量口径 | 已知/部分/未返回分别展示；没有任何已知值时显示 `—`，不把缺失当成 0 |
| 分组统计 | 分组表展示调用、成功、失败、进行中、中断、Token 与耗时 |
| 下钻 | 天 → 队列 → 门店 → 该门店调用明细；任务筛选可单独清除 |
| 调用明细 | 状态、用途/归属、模型、Token、耗时；展开查看调用标识、请求、HTTP、错误类型、阶段/步/尝试、Token 明细 |
| 分页 | 调用明细分页与每页条数切换 |
| 空态/错误 | 无记录、加载失败（含重试）均有明确提示 |

## 3. 关键实现决策

1. **默认不勾选门店**：与旧版一致，避免误触“开始标注”一次打开全部门店。
2. **操作期间暂停轮询**：忙碌或生成建议时暂停 2 秒轮询，避免服务端返回的中间态覆盖本地动作结果。
3. **回看与实时互斥**：会话一旦开始自动退出回看；进行中点击“查看效果”提示先结束会话。
4. **新画面自动判断**：仅在 `ready/clicked` 且无判断时自动请求一次，失败后由“生成建议”手动重试，避免循环模型调用。
5. **积分口径**：Token 汇总按“有返回用量的调用数”判断，未知调用不计入合计并明确标注。
6. **角色边界**：Hub 不渲染标注导航，路由内直接展示说明；用量对 Hub 与 Worker 均开放。

## 4. 接口与文件

使用接口（未新增后端契约）：

- `GET /api/annotation/state`、`GET /api/annotation/history?task_id=`
- `POST /api/annotation/{start,pause,resume,decide,click,step,complete,skip,reject,retry,refresh,stop}`
- `GET /api/status?include_events=false`（仅取排队所需任务字段）
- `GET /api/model-usage?date_from&date_to&group_by&source&page&page_size&run_id&task_id`

新增文件：

- `web-react/src/features/annotation/{types,api,hooks,format,format.test}.ts`
- `web-react/src/features/annotation/{AnnotationPage,AnnotationCanvas,RejectDialog}.tsx`
- `web-react/src/features/model-usage/{types,api,format,format.test}.ts`
- `web-react/src/features/model-usage/ModelUsageDialog.tsx`

修改文件：

- `app/App.tsx`：新增 `/annotation` 路由
- `app/AppShell.tsx`：Worker 导航新增「人工标注」
- `features/settings/SettingsPage.tsx`：用量入口改为弹窗；高级工具区仅 Worker 显示标注/诊断

## 5. 验证结果

### 静态与单测

- `npm run typecheck`：退出码 0
- `npm run test -- --run`：12 个文件、66 个用例全部通过（新增标注 9 例、用量 7 例）
- `npm run build`：成功，产物 `assets/index-jGZPGj4u.js`（760.02 kB）、`assets/index-DAouqdZC.css`；chunk 体积警告为既有提示

### 浏览器验收（隔离 Worker 8787 / Hub 8788）

| 检查项 | 结果 |
| --- | --- |
| Worker 标注页 H1「人工标注」、导航项存在 | 通过 |
| 门店队列 6 家、状态徽标、默认 0 选中 | 通过 |
| 全选联动 0 → 6 → 0 | 通过 |
| 用量弹窗标题「模型用量」、5 项筛选、汇总 | 通过 |
| 按天统计 2 行；点击后下钻为「按采集队列」 | 通过 |
| 调用明细表、展开详情（调用标识/Token 明细） | 通过 |
| Hub 标注页无 H1、显示角色说明、导航无标注 | 通过 |
| 页面脚本错误 | 0 |

证据：`docs/react-migration/verify/phase-4c/check.json`；截图：

- `docs/react-migration/screenshots/phase-4c-worker-annotation.jpg`
- `docs/react-migration/screenshots/phase-4c-worker-usage-stats.jpg`
- `docs/react-migration/screenshots/phase-4c-worker-usage-calls.jpg`
- `docs/react-migration/screenshots/phase-4c-hub-annotation.jpg`

说明：隔离 Worker 原账本为空，验收前临时写入 6 条样例调用以覆盖统计/明细/下钻，证据采集后已删除；未触碰 8765 运行实例与生产数据。

### 交互边界

- 未在隔离环境点击“开始标注”，避免真实控制本机微信窗口；开始、点击、不合格等写操作沿用既有后端接口与单测，UI 侧已按状态机完成禁用/确认。
- 人工标注的完整端到端验收需要一台解锁的 Windows + 微信环境，由使用者现场确认。

## 6. UI 规范符合性

- 页面与弹窗无自动副标题；标题使用真实 `h1/h2`。
- “开始前请把微信停留在文件传输助手”等操作前提放在开始按钮附近。
- “硬负样本”“不合格原因”“未返回用量”等风险/口径说明放在对应控件或汇总区域。
- 删除与状态重复的描述；状态、错误与进度均显示在相关数据或操作区域。

## 7. 回滚

- 代码回滚：`git revert <本阶段提交>`，重新执行 `npm run build` 即可恢复旧构建产物。
- 无数据库迁移、无后端契约变更，旧前端与旧 hash 入口仍可直接使用。
- 标注样本与用量账本均为既有数据，本阶段不写入或迁移。

## 8. 已知限制与后续

- 用量弹窗未提供导出与按模型筛选；模型维度仅用于统计，与旧版能力一致。
- 标注页一次最多渲染 300 家门店，超出时提示用筛选缩小范围。
- 人工标注的真实点击链路需在现场 Windows 微信环境验收；建议作为 Phase 4 收尾的人工验证项。
