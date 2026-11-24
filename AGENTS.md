# AGENTS.md

面向在本仓库工作的自动化与人工协作者。详细规范见 [docs/engineering-standards.md](docs/engineering-standards.md)，项目结构见 [docs/PROJECT_MAP.md](docs/PROJECT_MAP.md)。

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
- 按风险验证：优先跑相关测试；数据库、并发、升级回滚类改动必须做集成验证。
- 云端 CI 不自动触发；合并前按改动风险在本地执行相关检查，不等待 GitHub Actions 反馈。
- 正式发布仅通过手动 `workflow_dispatch`，复用同一次构建产物；PR、提交和标签推送不重新打包。
- 前端改动需 `npm run typecheck`、`npm test`、`npm run build` 并提交 `web/react` 产物。
- 前端新增或修改表格、筛选、分页、批量操作、设置布局、表单、弹窗/抽屉、图片预览与反馈时，先查 [docs/design-system](docs/design-system/README.md) 与 `src/components` 现有契约，优先复用，不重复造同类组件。
- Storybook 仅用于开发与回归：`npm run storybook` / `npm run build-storybook`，不进入生产安装包或 Hub 镜像。
- Python 改动需 `ruff check .` 通过（门禁规则见 `pyproject.toml`）。

## 提交与交付

- 提交使用 `feat:` / `fix:` / `refactor:` / `test:` / `docs:` / `chore:` 前缀，描述用中文。
- PR 说明实际改动、验证证据与未完成事项；不把“已提交”“CI 通过”当作业务验收。
- 交付需附安装、升级、回滚方案与验收记录；发布产物复用已验收构建，不重复打包。

## 文档

- 当前有效文档放 `docs/`，历史阶段文档按 `docs/archive/<年月>-<主题>/` 归档并在索引说明替代关系。
- 前端设计体系入口为 [docs/design-system/README.md](docs/design-system/README.md)：四层模型、上游索引、页面模式、组件契约与验收矩阵。
- 软件更新与版本生命周期见 [docs/updates/README.md](docs/updates/README.md)：Release 清单契约、发现/校验边界、版本保留与后续执行器计划。
- 新功能（包括 #32 软件更新、备份恢复、健康监控、设备管理）默认先选择现有模式与公共组件，禁止另起一套表格、弹窗、设置布局或反馈机制。
- 产品需求放 `docs/product/`；发布说明记录在 [CHANGELOG.md](CHANGELOG.md)。
