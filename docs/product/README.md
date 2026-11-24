# 产品体验设计规格（L5）

状态：目标设计，待维护者审阅；不是已实现或已通过现场验收的报告。
源码核查基线：[ce2856b](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/tree/ce2856ba2c56d3da46adf2dde3c183606b126133)，历史前端基线 [e6b7ea8](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/tree/e6b7ea82079b45f1720ea6dec6411bc29ed5276b)，结合 #3/#20/#21/#22/#23/#24/#26/#31/#32 与历史迁移 inventory。实施时按最新路径定位，不因 #57 改目录重做规格。基线为远端静态源码，未操作线上环境。

**目标：操作者用最短的合理路径完成真实工作，不需要理解内部代码、重复搜索门店或跨页面拼凑结果。** 本规格先于 #58 的页面适配：定义页面应该提供什么、布局如何组织、信息和操作如何呈现。#58 负责 L1–L4 的成熟组件/模式复用与实际交付，不再自行决定产品布局或删减功能。

## 页面索引

| 界面 | 主要任务 | 规格 |
| --- | --- | --- |
| 全局壳层 | 找到当前工作、随时进入任务 | [全局与导航](pages/global.md) |
| Hub 采集工作台 | 选店、分配、查看进度 | [共享调度](pages/hub-workbench.md) |
| Worker 采集工作台 | 启动/控制本机采集、同步 | [本机采集](pages/worker-workbench.md) |
| 设备、分配 | 配对、占用、回收与再分配 | [设备与分配](pages/devices-assignments.md) |
| 任务中心 | 查轮次、定位逐店结果并处置 | [任务中心及详情](pages/task-center.md) |
| 素材 | 导入、分析、选码、发布、返工 | [素材清洗](pages/materials.md) |
| 质量 | 查证据、判断、处置并连续处理 | [质量审核](pages/quality.md) |
| 人工标注 | 标注画面和真实动作 | [人工标注](pages/annotation.md) |
| 远程控制 | 半自动等待与快速操作 | [远程控制](pages/remote.md) |
| 设置 | 配置实例、浏览器偏好、模型 | [设置与模型用量](pages/settings.md) |
| 软件更新 | 安全完成版本更新与清理 | [软件更新](pages/updates.md) |
| 环境诊断 | 找到阻塞和恢复方法 | [诊断](pages/diagnostics.md) |

共用约束：[业务对象、信息架构与文案](experience-contract.md)；[功能保真及用户任务验收](acceptance.md)。页面规格中的方框/顺序是目标布局而非像素稿；必须按 #58 的 OpenAI 视觉与 Ant/Cloudscape/Carbon 公共组件实现，允许响应式合理重排，不允许改变信息层级和功能可达性。

## 设计依据（只采用对当前产品有价值的模式）

| 来源（2026-11-25 核对公开说明） | 采纳的具体模式 | 应用 |
| --- | --- | --- |
| [GitLab UX Scorecards](https://handbook.gitlab.com/handbook/product/ux/ux-scorecards/)、[UX heuristics](https://handbook.gitlab.com/handbook/product/ux/heuristics/) | 以用户任务为单位检查入口、任务完成、理解与恢复，不把视觉评分当业务通过 | acceptance 中逐条情境验收 |
| [Prefect workers](https://github.com/PrefectHQ/prefect/blob/main/docs/v3/concepts/workers.mdx)、[产品流程](https://www.prefect.io/how-it-works) | 调度与执行分离、设备轮询状态、运行历史；不照搬其自动调度语义 | Hub/Worker/设备 |
| [Airflow UI](https://airflow.apache.org/docs/apache-airflow/stable/ui.html) | 运行概览、单次执行下钻、日志及元信息分区 | 任务中心、诊断 |
| [Label Studio Data Manager](https://labelstud.io/guide/manage_data)、[标注流程](https://labelstud.io/guide/labeling) | 筛选排序决定处理范围、数据列表到单项工作区 | 素材、连续处理 |
| [CVAT Manual QA](https://docs.cvat.ai/docs/qa-analytics/manual-qa/)、[编辑器导航](https://docs.cvat.ai/docs/annotation/annotation-editor/navbar/) | 证据与操作同屏、审核与标注角色清晰、问题处置 | 审核、标注 |
| [Carbon 内容规范](https://www.carbondesignsystem.com/building-blocks/foundations/content)、[通知规范](https://www.carbondesignsystem.com/building-blocks/core/components/notification/guidelines) | 简洁可执行的按钮和错误信息，行内反馈优先 | 全站文案 |
| [现有四层设计体系](../design-system/README.md) | 统一视觉、基础控件、公共复合组件、布局模式 | 所有实现 |

上游仅参考公开已核对的产品行为，不宣称已运行其每个页面，不引入外部业务模型、企业版专属能力或新 UI 库。实际业务规则以项目既有确认的规格和后端事实为准。

## 维护规则

1. 每个新页面/操作先更新对应页面规格与功能矩阵，再按四层规范实现。业务功能不得因“更美观”“组件不支持”被删除。
2. 规格中的【目标】为本次产品设计决定；涉及改变资格、权限、数据和生命周期的【待确认】不能由实现者自行决定。可独立实现已明确部分，不以一个待确认项阻塞其他工作。
3. 以 #58 的实际浏览器验收共用证据完成产品验收，避免另做一轮同样的 UI 重构或重复截图。无实际体验数据不得编造评分、操作时间或通过结论。
4. 不改生产、不发布；仅在隔离环境实施和验证。
