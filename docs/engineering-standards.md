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
| 纯文档 / 配置 | 无（CI 路径无关时不触发构建） |
| 服务端路由、共享同步 | 对应 `tests/test_server.py`、`test_shared_workbench.py` 等契约测试 |
| 采集执行、窗口契约 | `test_windows_compat.py`、`test_environment.py` 与相关回归 |
| 前端 | `typecheck` + `vitest` + `build`，并确认 `web/react` 产物一致 |
| 安装器 / Hub / 升级回滚 | `smoke.ps1` 或 CI 的升级-回滚链路，附日志 |

- 优先跑相关测试，避免无依据的全量重复执行。
- 区分“代码实现”“测试通过”“真实业务验收”，汇报时分别说明。

## 静态检查与格式

- Python：Ruff，配置在 `UFS_Menu_Scraper/macos_runner/pyproject.toml`。
  - 门禁规则：`E9`、`F63`、`F7`、`F82`（语法错误、未定义名、无效比较、无效 f-string），当前 0 项。
  - 存量基线：`E4,E7,E9,F` 共 65 项（多语句单行、未使用导入/变量等），按模块逐步治理，不做一次性全量格式化。
  - 本地命令：`uvx ruff check .`；批量修复仅在对应模块的独立提交中进行。
- 前端：`tsc -b --noEmit`（`npm run typecheck`）与 Vitest 作为门禁；ESLint 暂不引入，原因见 [治理报告](governance/repository-audit-2026-11.md)，如需引入须先固化基线并只拦截新增问题。
- 编码统一 UTF-8（无 BOM），换行 LF，规则见 `.editorconfig`。

## CI 门禁

`.github/workflows/windows-release.yml` 包含：

- `react-web`：类型检查、单测、构建，校验 `web/react` 与源码一致，确认旧前端已清理。
- `windows`：Python 契约测试、安装包构建、安装与进程恢复 smoke。
- `hub`：容器启动、版本与前端资源校验、数据卷、升级回滚链路。
- `python-lint`：Ruff 门禁。

标签触发的 Release 草稿仅用于人工审阅，发布需维护者确认。

## 文档规范

- 当前有效文档位于 `docs/`；历史阶段文档归档到 `docs/archive/<年月>-<主题>/`，索引在 `docs/archive/README.md`。
- 产品需求放 `docs/product/`；发布变更记录在 `CHANGELOG.md`。
- 文档中的命令与路径使用相对路径或环境变量，不写个人机器绝对路径。

## 评审清单

- 是否在正确的层修复，是否存在跨层直连或状态重复。
- 是否覆盖失败路径与回滚路径，错误是否可诊断。
- 是否新增未忽略的运行产物、密钥或大文件。
- 验证证据是否与改动风险匹配，是否说明了未完成事项。
