# 文档索引

本文件是仓库唯一的文档入口。当前有效规范在前，历史材料在后；历史文档只作追溯，不作为现行指令。

## 当前有效规范

| 主题 | 入口 | 说明 |
| --- | --- | --- |
| 项目结构 | [架构与项目地图](architecture/project-map.md) | 目录职责、模块划分、数据流与角色边界 |
| API 与角色契约 | [接口契约](architecture/api-contracts.md) / [角色矩阵](architecture/role-matrix.md) | HTTP 契约、Hub/Worker/远程控制权限 |
| GUI Agent 边界 | [GUI Agent 开发规范](architecture/gui-agent-development.md) | 决策权、工具边界与验收口径 |
| 工程规范 | [工程规范](governance/engineering-standards.md) | 分层、测试、版本管理、双 Agent 协作、发布与备份、CI 门禁 |
| 工作区治理 | [跨平台工作区与运行时存储治理](workspace-governance.md) | Mac/Windows 职责、主仓与 Worktree 生命周期、交接与清理规则 |
| 前端设计体系 | [设计体系](design-system/README.md) | L1–L4 四层模型、上游索引、组件契约与验收矩阵 |
| 前端设计规格 | [界面设计规格](design-system/ui-design-spec.md) | 现有视觉与交互标准 |
| 产品需求 | [产品规格](product/README.md) | 逐页面产品设计、功能保真与用户情境验收 |
| 软件更新 | [更新与版本生命周期](updates/README.md) | Release 清单、发现/下载/安装/回滚、集中更新与清理 |
| 合格成品目录 | [合格成品目录](delivery/README.md) | 一店一目录、最新整轮覆盖、历史存量初始化与宿主机交付 |
| 采集合规 | [采集回归台账](governance/menu-capture-regression.md) | 按链路记录的问题与复测证据 |
| 部署与发布 | [部署指南](../deploy/README.md) | 安装、升级、回滚与手动发布流程 |

## 发布与验收证据

| 版本/阶段 | 入口 |
| --- | --- |
| React 迁移阶段 | [迁移总结](releases/react-migration/final-migration-report.md)、[同步基线](releases/react-migration/sync-baseline-report.md) |
| 0.2.0-rc.3 生产交付 | [生产交付报告](releases/react-migration/production-delivery-report.md) |
| 浏览器/实机证据 | [verify](releases/react-migration/verify)、[screenshots](releases/react-migration/screenshots) |

## 历史归档

已结束的阶段计划、调研与实施记录见 [归档索引](archive/README.md)：

- [2026-11 采集修复需求](archive/2026-11-capture-fixes/)
- [2026-11 React 迁移过程材料](archive/2026-11-react-migration/)
- [2026-11 仓库治理审计](governance/repository-audit-2026-11.md)（现行治理记录，保留在 governance）

## 维护规则

1. 新文档放入对应主题目录，并在此索引登记；不在仓库根或业务包内散落规格。
2. 阶段结束或被替代的文档移入 `docs/archive/<年月>-<主题>/`，在归档索引说明替代关系。
3. 文档中的路径与命令必须可直接复制执行，不写个人机器绝对路径。
4. 产品、设计、更新等规范变更时同步更新本索引与相关入口。
