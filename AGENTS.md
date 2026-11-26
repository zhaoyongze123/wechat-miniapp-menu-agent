# AGENTS.md

面向在本仓库工作的自动化与人工协作者。详细规范见 [docs/governance/engineering-standards.md](docs/governance/engineering-standards.md)，项目结构见 [docs/architecture/project-map.md](docs/architecture/project-map.md)。

## 角色与决策边界

- 维护者决定需求及授权；规划/Review Agent 制定方案、Issue、独立审查与交付建议；Coding Agent 按已确认 Issue 实现、验证、提交 PR 并执行获授权的交付。
- 先读取当前 Issue 与工程规范；不重复调研已确认方案，不擅自扩展范围、增加框架、门禁、备份或重构。具体实现自主决定；业务语义、安全、兼容性或范围冲突回报规划方。Review 的 P0/P1 阻断项按证据修复，P2/Note 不阻塞。
- 汇报 Issue/PR、commit SHA、改动、真实验证结果、未验证项及下一步；等待独立 Review，不把自测当作批准。具体工作流及版本规则以工程规范为准。

## 执行节奏

- 简单任务直接实施，不强制冗长计划；复杂任务只给完成工作所需的步骤。
- 仅在实质进展、阻塞或完成时简短汇报；常规技术决策自主推进，不反复请示。
- 重大业务、安全或兼容性问题需要人工决策时，说明影响并暂停受阻部分；其他不受影响的工作继续推进。

## 工作边界

- 默认在独立分支或 Worktree 工作；生产切换、发布与合并需要维护者确认。
- 严禁中断正在运行的生产采集，禁止改动生产数据库、素材与截图目录。
- **备份策略（强制）**：日常软件升级默认不创建业务数据备份；禁止全量复制 Hub Docker 数据卷、Worker 生产数据目录，禁止备份素材、二维码候选、截图及交付目录，禁止为升级备份完整源码、运行环境与安装目录。仅当存在不可逆数据库迁移或明确的数据修改风险时，才允许创建最小必要的 SQLite 一致性恢复点（`sqlite3.backup`）；超出上述范围的备份必须获得维护者明确授权，不得以安全、谨慎、验收或回滚为由擅自扩大备份范围。清理误建备份前必须确认复制进程已停止，并核验目标确为本次新增的冗余副本，禁止误删原始数据。普通版本升级直接复用 Velopack、Docker Compose、持久化数据卷及兼容回滚机制。
- 开发、测试与清理必须在隔离目录、端口和数据副本上进行。
- 不提交密钥、本机配置、运行数据、截图与日志（见 `.gitignore`）。
- 每台设备只保留一个主仓，主仓只同步 `main` 和管理 Git 引用；Worktree 统一放在 `ma-workspace/development/worktrees/issue-<编号>-<简名>`，禁止建在 `/tmp`、盘根或源码目录旁。Issue 结束、PR 合并且无独有内容后必须回收。完整规则见 [跨平台工作区治理](docs/workspace-governance.md)。
- **开发文件生命周期**：谁创建的临时脚本、测试数据、构建产物与日志，谁在任务结束前回收，不留给后续 Agent。开发前先复用已有 Worktree、测试环境、依赖与缓存，禁止重复 Clone、整仓复制和无意义全量备份。临时文件只放 `ma-workspace/cache/build`、`ma-workspace/testing/` 或系统临时目录，不写入源码主仓。Issue 关闭、PR 合并且独有成果已保护后，必须回收对应 Worktree、临时环境与构建产物，交付报告写明清理结果。
- **业务资产保护**：产品前端可查看、查询、预览或用于业务处理的素材候选、历史截图、审核记录与采集结果均为有效资产，禁止按开发垃圾删除；升级与迁移按备份策略仅在不可逆数据风险时创建最小 SQLite 恢复点，不复制素材、截图或完整运行环境。清理前完成引用检查、独有数据保护与恢复能力核验，任一失败立即终止；盘点统一使用 `deploy/tools/workspace_audit.py`，不新增管理平台、脚本、数据库或常驻服务。详见[开发文件生命周期](docs/workspace-governance.md#11-开发文件生命周期)。

## 环境要求

- 开发支持 macOS 与 Windows：通用后端、前端与测试两台设备均可执行；Windows 桌面自动化、微信客户端与 Velopack 安装/升级/回滚验收必须使用 Windows 实机。
- Windows 10/11（验收与生产），已登录微信桌面版；Python 3.12（`uv`），Node.js 22。
- 控制台默认端口 `8765`，共享后台默认 `8766`；安装目录与数据目录由安装器参数决定。

## 代码与验证

- 最小变更，遵循现有分层与命名；先定位根因再改代码，不在展示层打补丁。
- 新增通用基础设施、引入/替换核心依赖或重大架构设计前，必须先完成[技术选型与复用评估](docs/governance/engineering-standards.md#技术选型与复用优先)：优先复用平台原生能力、项目已有组件和成熟框架/SDK；先比较方案与验证关键兼容性，再确定最小自研边界。未完成评估不得直接自研已有成熟方案的通用能力；已批准方案复用有效证据，不重复调研。
- 状态保持唯一事实源；不引入无依据的缓存、去重或异常吞并。
- 日常开发、Issue 实施、提交、PR 和合并阶段一律禁止主动触发 GitHub Actions，也禁止构建完整 Windows 安装包、Hub 镜像或发布归档。
- 按改动范围与风险执行必要的本地单测、类型检查、Lint 和针对性集成测试。数据库、并发、权限、安装、升级和回滚等高风险改动保留必要验证，优先在隔离环境复用既有产物。已确认且未受影响的验收证据直接引用，避免重复构建、截图、报告和无关测试。
- 全部开发任务完成后，默认同一交付手动运行一次完整集成 CI。失败后读取日志、本地修复和验证，再次触发或重跑须在维护者明确授权范围内；可一次授权本轮有界重试。详见 [发布与云端构建](docs/governance/engineering-standards.md#发布与云端构建)。
- 涉及前端时按影响执行类型检查和相关测试。提交前生成并核对受改动影响的 `web/react` 静态资源。
- 前端新增或修改表格、筛选、分页、批量操作、设置布局、表单、弹窗/抽屉、图片预览与反馈时，先查 [docs/design-system](docs/design-system/README.md) 与 `src/components` 现有契约，优先复用，不重复造同类组件。
- 提交前运行 `npm run lint:design`；确需原生控件或业务专用颜色时，按“文件、规则、原因”登记到 `docs/design-system/acceptance-matrix.md` 与门禁白名单，禁止整目录放行。
- Storybook 仅用于开发与回归：`npm run storybook` / `npm run build-storybook`，不进入生产安装包或 Hub 镜像。
- Python 改动按 `pyproject.toml` 对受影响路径执行 Ruff 检查。

## 提交与交付

- 提交使用 `feat:` / `fix:` / `refactor:` / `test:` / `docs:` / `chore:` 前缀，描述用中文。
- PR 说明实际改动、验证证据与未完成事项；不把“已提交”“CI 通过”当作业务验收。
- 交付记录引用已有安装、升级、回滚方案及有效验收证据。生产部署和正式发布均须在用户明确授权范围内，可一次授权本轮交付。普通升级不默认全量复制历史业务文件，按工程规范保护数据库、配置及实际风险数据。
- 任务结束报告 Issue、PR、分支、commit、未提交修改的保护位置、测试平台与验证结果、Worktree 保留或回收结果，以及临时脚本、测试数据、构建产物与日志的清理结果和实际释放空间。同一 Issue 分支同一时间只允许一个 Agent 写入，跨设备接力先 `fetch` 核对最新远端提交。

## 代码结构

- 唯一源码根为 `app/`；`compat/uipath/` 只保留历史 UiPath 兼容工程，不进入安装包。
- 后端 `app/menu_capture/` 按域分包：`api/`、`capture/`、`desktop/`、`vision/`、`agent/`、`materials/`、`annotation/`、`quality/`、`shared/`、`storage/`、`updates/`、`core/`；跨域依赖只走显式接口。
- 测试在 `app/tests/`，按 `unit/`、`integration/`、`contract/`、`platform/` 与 `fixtures/` 组织；测试不得依赖个人机器路径或未提交的运行数据。
- 部署脚本在 `deploy/hub/`、`deploy/hub-update/`、`deploy/migration/`、`deploy/windows/`；文档入口为 [docs/README.md](docs/README.md)。

## 文档

- 当前有效文档放 `docs/`，历史阶段文档按 `docs/archive/<年月>-<主题>/` 归档并在索引说明替代关系；唯一入口是 [docs/README.md](docs/README.md)。
- 前端设计体系入口为 [docs/design-system/README.md](docs/design-system/README.md)：四层模型、上游索引、页面模式、组件契约与验收矩阵。
- 软件更新与版本生命周期见 [docs/updates/README.md](docs/updates/README.md)：Release 清单契约、发现/校验边界、版本保留与后续执行器计划。
- 新功能（包括 #32 软件更新、备份恢复、健康监控、设备管理）默认先选择现有模式与公共组件，禁止另起一套表格、弹窗、设置布局或反馈机制。
- 产品需求放 `docs/product/`；发布说明记录在 [CHANGELOG.md](CHANGELOG.md)。
