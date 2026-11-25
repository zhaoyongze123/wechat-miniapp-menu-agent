# AGENTS.md

面向在本仓库工作的自动化与人工协作者。详细规范见 [docs/governance/engineering-standards.md](docs/governance/engineering-standards.md)，项目结构见 [docs/architecture/project-map.md](docs/architecture/project-map.md)。

## 协作角色与规范来源

- 产品负责人决定产品需求、业务方向及授权范围；Tech Lead 负责技术调研、架构决策、工程规范、Issue 制定、独立 Code Review 与发布策略；Coding Agent 负责代码实现、测试验证、PR、故障排查及获授权的发布部署。
- 每项任务开始前读取本文件、`docs/governance/engineering-standards.md`、当前 Issue 与相关 PR Review；按范围再读架构、产品、设计体系或更新契约文档。以仓库最新有效规范为准，不用历史聊天替代。

## 执行节奏

- 依据 Issue 读取需求、检查代码和 Git 状态、制定必要步骤、隔离开发、按风险验证、提交 PR 并处理阻断 Review；简单任务直接实施，不写冗长计划。
- 需求明确时直接执行。普通技术细节自主决定；不反复请示、不重复调研已确定的方案。存在假设时说明假设，并继续不受影响的工作。
- 仅在重大业务规则或架构冲突、不可逆数据风险、外部权限缺失或必须由人工完成的现场操作时，暂停受阻部分并说明原因；其他事项继续推进。进度只在实质变化、阻塞或完成时简短汇报。

## 工作边界

- 默认在独立分支或 Worktree 工作；生产切换、发布与合并必须在维护者明确授权范围内执行，既有授权覆盖的连续步骤无需逐项重复请示。
- 不得擅自中断正在运行的生产采集或修改生产数据；获授权的生产操作按批准范围执行并保留恢复方案。
- 开发、测试与清理必须在隔离目录、端口和数据副本上进行。
- 不提交密钥、本机配置、运行数据、截图与日志（见 `.gitignore`）。

## 环境要求

- Windows 10/11，已登录微信桌面版；Python 3.12（`uv`），Node.js 22。
- 控制台默认端口 `8765`，共享后台默认 `8766`；安装目录与数据目录由安装器参数决定。

## 代码与验证

- 最小变更，遵循现有分层与命名；先定位根因再改代码，不在展示层打补丁。
- 状态保持唯一事实源；不引入无依据的缓存、去重或异常吞并。
- 不新增没有实际必要性的测试门禁、缓存、重试、备份或状态机；先复用现有模块、工具、组件及已验收证据。
- 日常开发、Issue 实施、提交、PR 和合并阶段一律禁止主动触发 GitHub Actions，也禁止构建完整 Windows 安装包、Hub 镜像或发布归档。
- 按改动范围与风险执行必要的本地单测、类型检查、Lint 和针对性集成测试。数据库、并发、权限、安装、升级和回滚等高风险改动保留必要验证，优先在隔离环境复用既有产物。已确认且未受影响的验收证据直接引用，避免重复构建、截图、报告和无关测试。
- 全部开发任务完成后，同一交付最多手动运行一次完整集成 CI。失败后读取日志、本地修复和验证，任何再次触发或重跑均须用户明确授权。详见 [发布与云端构建](docs/governance/engineering-standards.md#发布与云端构建)。
- 涉及前端时按影响执行类型检查和相关测试。提交前生成并核对受改动影响的 `web/react` 静态资源。
- 前端新增或修改表格、筛选、分页、批量操作、设置布局、表单、弹窗/抽屉、图片预览与反馈时，先查 [docs/design-system](docs/design-system/README.md) 与 `src/components` 现有契约，优先复用，不重复造同类组件。
- 提交前运行 `npm run lint:design`；确需原生控件或业务专用颜色时，按“文件、规则、原因”登记到 `docs/design-system/acceptance-matrix.md` 与门禁白名单，禁止整目录放行。
- Storybook 仅用于开发与回归：`npm run storybook` / `npm run build-storybook`，不进入生产安装包或 Hub 镜像。
- Python 改动按 `pyproject.toml` 对受影响路径执行 Ruff 检查。

## 提交与交付

- 提交使用 `feat:` / `fix:` / `refactor:` / `test:` / `docs:` / `chore:` 前缀，描述用中文。
- PR 说明实际改动、验证证据与未完成事项；不把“已提交”“CI 通过”当作业务验收。
- Review 中的 P0/P1 阻断问题必须处理；P2 与 Note 不默认纳入本轮，除非维护者明确扩大范围。
- 交付记录引用已有安装、升级、回滚方案及有效验收证据。生产部署和正式发布均须用户明确授权。
- 每次交付集中说明：Issue、PR 与 Commit SHA；实际完成内容；执行的验证及真实结果；未验证事项、风险或阻塞；下一步操作。明确区分代码实现、测试通过、合并、正式发布、生产部署和实际业务验收；引用已有日志和记录，不重复制作报告。

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
