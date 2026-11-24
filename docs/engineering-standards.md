# 工程规范

本文件是 [AGENTS.md](../AGENTS.md) 的展开版，面向服务端、采集执行、前端与发布链路。规范以“正确性优先、最小变更、按风险验证”为前提，不为了流程增加流程。

## 分层与职责

| 层 | 位置 | 职责 | 约束 |
| --- | --- | --- | --- |
| 传输 | `menu_capture/server.py`、`shared_routes.py` | HTTP 路由、鉴权、参数校验、错误映射 | 不写业务规则，不直接操作执行器内部状态 |
| 业务 | `batch_management.py`、`rejections.py`、`materials.py`、`annotation.py` | 批次、素材版本、审核与标注流程 | 跨模块走显式接口，不共享可变全局状态 |
| 执行 | `uipath_runner.py`、`ui_tars_trial.py`、`windows_desktop.py` | 机器人与桌面执行、协议与超时 | 失败要可归因，不吞异常；平台分支显式判断 |
| 状态与存储 | `store.py`、`shared*.py` | SQLite 事实源、共享分配与同步 | 唯一事实源；迁移只做前向兼容 |
| 展示 | `web-react/` → `web/react/` | React 控制台 | 不在前端复制服务端业务状态；构建产物随代码提交 |

- 新能力优先落在既有层内；跨层需求先加契约，再改实现。
- 拒绝无依据的本地缓存、去重与重试；确需缓存时说明失效条件。

## 数据与迁移

- SQLite 数据库、素材与截图属于运行数据，不进入版本库。
- 结构变更必须前向迁移：旧版本可读、新版本可回滚到旧数据快照。
- 升级前备份 `config.local.json`、`secrets.local.json` 与数据库；回滚脚本需恢复旧版本目录与数据。
- 不得修改或清空生产数据；需要变更时先做副本验证。

## 测试与验证

| 改动类型 | 最低验证 |
| --- | --- |
| 纯文档 / 配置 | 无（云端不自动构建，合并前按需本地检查） |
| 服务端路由、共享同步 | 对应 `tests/test_server.py`、`test_shared_workbench.py` 等契约测试 |
| 采集执行、窗口契约 | `test_windows_compat.py`、`test_environment.py` 与相关回归 |
| 前端 | `typecheck` + `vitest` + `build`，并确认 `web/react` 产物一致；公共组件改动附 Storybook 证据 |
| 安装器 / Hub / 升级回滚 | `smoke.ps1` 或手动发布工作流的升级-回滚链路，附日志 |

- 优先跑相关测试，避免无依据的全量重复执行。
- 区分“代码实现”“测试通过”“真实业务验收”，汇报时分别说明。

## 静态检查与格式

- Python：Ruff，配置在 `UFS_Menu_Scraper/macos_runner/pyproject.toml`。
  - 门禁规则：`E9`、`F63`、`F7`、`F82`（语法错误、未定义名、无效比较、无效 f-string），当前 0 项。
  - 存量基线：`E4,E7,E9,F` 共 65 项（多语句单行、未使用导入/变量等），按模块逐步治理，不做一次性全量格式化。
  - 本地命令：`uvx ruff check .`；批量修复仅在对应模块的独立提交中进行。
- 前端：`tsc -b --noEmit`（`npm run typecheck`）与 Vitest 作为门禁；ESLint 暂不引入，原因见 [治理报告](governance/repository-audit-2026-11.md)，如需引入须先固化基线并只拦截新增问题。
- 编码统一 UTF-8（无 BOM），换行 LF，规则见 `.editorconfig`。

## 前端设计体系

- 入口：[docs/design-system/README.md](design-system/README.md)。四层模型为 L1/L2 OpenAI Apps SDK UI、L3 自有复合组件、L4 Cloudscape/Carbon 产品模式参考。
- 新页面先复用 `src/components` 契约；表格、筛选、分页、批量操作、设置布局、表单、弹窗/抽屉、图片预览与反馈不得在页面内重复实现。
- 视觉只使用官方语义 Token；不引入 Ant Design/Carbon/Cloudscape 运行时依赖或额外主题。
- Storybook 为开发依赖：`npm run storybook`、`npm run build-storybook`；产物 `storybook-static/` 已忽略，不进入安装包或镜像。
- 组件状态至少覆盖适用的正常、加载、空态、错误、禁用、长文本与窄容器；交互测试优先稳定 DOM/布局断言。

## 发布与云端构建

当前处于快速开发阶段，采用 **Manual Release + Local Validation**：

- 正式 Release 必须附 `update-manifest.json`（契约见 [docs/updates/README.md](updates/README.md)）；缺清单或校验失败的版本不得作为可更新版本提供。
- 更新发现只允许固定仓库正式 Release；禁止接受前端传入的任意 URL、版本路径、镜像或命令。
- 下载/安装/镜像切换必须有独立执行器、事务锁与健康检查；不得把 Docker Socket、宿主机 shell 或任意路径暴露给业务容器/浏览器。
- CI 临时 artifact 默认保留 14 天；正式 Release 资产独立永久保留。

- PR、普通提交、标签推送都不自动触发云端构建；合并前由开发者或 Coding Agent 按改动风险执行本地检查。
- 正式发布仅通过手动运行 `.github/workflows/windows-release.yml`（`workflow_dispatch`）。
- 一次手动运行完成构建与验收：`react-web`（类型检查、单测、构建一致性）、`python-lint`、`windows`（契约测试、安装包、安装与重启恢复）、`hub`（容器、版本、数据卷、升级回滚）。
- 验收通过后，`draft` 作业把**本次运行同一份产物**上传到 `v<version>` Release 草稿；不重新构建。
- 正式发布流程：本地验证 → 手动触发构建 → 部署并验证同一产物 → 人工发布 Release 草稿。
- 标签推送不再触发构建；已发布版本拒绝覆盖，需要新版本号。
- 将来恢复自动 CI 时，应先评估构建成本、缓存与分支保护要求，不直接恢复全量打包。

## 文档规范

- 当前有效文档位于 `docs/`；历史阶段文档归档到 `docs/archive/<年月>-<主题>/`，索引在 `docs/archive/README.md`。
- 产品需求放 `docs/product/`；发布变更记录在 `CHANGELOG.md`。
- 文档中的命令与路径使用相对路径或环境变量，不写个人机器绝对路径。

## 评审清单

- 是否在正确的层修复，是否存在跨层直连或状态重复。
- 是否覆盖失败路径与回滚路径，错误是否可诊断。
- 是否新增未忽略的运行产物、密钥或大文件。
- 验证证据是否与改动风险匹配，是否说明了未完成事项。
