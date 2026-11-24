# AGENTS.md

面向在本仓库工作的自动化与人工协作者。详细规范见 [docs/governance/engineering-standards.md](docs/governance/engineering-standards.md)，项目结构见 [docs/architecture/project-map.md](docs/architecture/project-map.md)。

## 执行节奏

- 先复述需求与关键约束，确认理解无偏差后再动手；有歧义先列出假设。
- 长任务先给出 3–5 步计划，每完成一步汇报结果；阻塞、风险或需授权时立即停下说明。
- 常规技术决策自主推进；不反复请示，也不在同一失败路径上盲试超过两次。

## 工作边界

- 默认在独立分支或 Worktree 工作；生产切换、发布与合并需要维护者确认。
- 严禁中断正在运行的生产采集，禁止改动生产数据库、素材与截图目录。
- 开发、测试与清理必须在隔离目录、端口和数据副本上进行。
- 不提交密钥、本机配置、运行数据、截图与日志（见 `.gitignore`）。

## 环境要求

- Windows 10/11，已登录微信桌面版；Python 3.12（`uv`），Node.js 22。
- 控制台默认端口 `8765`，共享后台默认 `8766`；安装目录与数据目录由安装器参数决定。

## 代码与验证

- 最小变更，遵循现有分层与命名；先定位根因再改代码，不在展示层打补丁。
- 状态保持唯一事实源；不引入无依据的缓存、去重或异常吞并。
- 日常开发、Issue 实施、提交、PR 和合并阶段一律禁止主动触发 GitHub Actions，也禁止构建完整 Windows 安装包、Hub 镜像或发布归档。
- 按改动范围与风险执行必要的本地单测、类型检查、Lint 和针对性集成测试。数据库、并发、权限、安装、升级和回滚等高风险改动保留必要验证，优先在隔离环境复用既有产物。已确认且未受影响的验收证据直接引用，避免重复构建、截图、报告和无关测试。
- 全部开发任务完成后，同一交付最多手动运行一次完整集成 CI。失败后读取日志、本地修复和验证，任何再次触发或重跑均须用户明确授权。详见 [发布与云端构建](docs/governance/engineering-standards.md#发布与云端构建)。
- 涉及前端时按影响执行类型检查和相关测试。提交前生成并核对受改动影响的 `web/react` 静态资源。
- 前端新增或修改表格、筛选、分页、批量操作、设置布局、表单、弹窗/抽屉、图片预览与反馈时，先查 [docs/design-system](docs/design-system/README.md) 与 `src/components` 现有契约，优先复用，不重复造同类组件。
- Storybook 仅用于开发与回归：`npm run storybook` / `npm run build-storybook`，不进入生产安装包或 Hub 镜像。
- Python 改动按 `pyproject.toml` 对受影响路径执行 Ruff 检查。

## 提交与交付

- 提交使用 `feat:` / `fix:` / `refactor:` / `test:` / `docs:` / `chore:` 前缀，描述用中文。
- PR 说明实际改动、验证证据与未完成事项；不把“已提交”“CI 通过”当作业务验收。
- 交付记录引用已有安装、升级、回滚方案及有效验收证据。生产部署和正式发布均须用户明确授权。

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
