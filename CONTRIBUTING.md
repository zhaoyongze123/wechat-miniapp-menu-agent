# 贡献指南

本仓库交付 Windows 生产采集端（Worker）、共享后台（Hub）与 React 控制台。提交前请先阅读 [工程规范](docs/governance/engineering-standards.md) 与 [项目定位](docs/architecture/PROJECT_MAP.md)。

## 环境要求

- Windows 10/11（采集端），已登录微信桌面版。
- Python 3.12，依赖通过 `uv` 管理。
- Node.js 22（`web-react/`）。
- Docker Desktop（仅 Hub 与容器化验收需要）。

## 本地开发

```powershell
cd app
uv sync --frozen --python 3.12

# 示例：改动共享路由或同步时
uv run --frozen pytest -q tests\test_server.py tests\test_shared_workbench.py

cd web-react
npm ci
npm run typecheck
npm test -- <相关测试文件>
npm run build   # 仅在提交受影响的 React 静态资源前执行
```

## 分支与提交

- 分支：`feat/<issue>-<描述>`、`fix/<issue>-<描述>`、`chore/<issue>-<描述>`、`docs/<issue>-<描述>`。
- 提交信息：`feat: 中文描述`、`fix: …`、`refactor: …`、`test: …`、`docs: …`、`chore: …`。
- 一个提交只做一件事；不提交密钥、本机配置、运行数据、截图或日志。

## 提交前检查

1. 按改动路径执行必要的 `ruff check`、单测、类型检查或集成测试。
2. 涉及前端静态资源时，在提交前生成并核对 `web/react`。
3. 高风险改动在本地隔离验证，并引用仍有效的验收证据。完整 CI 和 Windows 安装包、Hub 镜像、发布归档集中在交付末期。

## 数据边界

- 采集输出默认写入 `输出/`，本地数据库在数据目录，二者均已被 `.gitignore` 排除。
- 如需提交示例数据，只提交脱敏、最小化的测试夹具，放在测试目录内。
- 本地临时脚本写入 `scratch/`（已忽略），不要散落在仓库根目录。

## Pull Request

- 标题与描述使用中文，说明改动动机、实现要点、验证结果与未完成事项。
- 日常提交、PR 与合并均禁止主动触发 Actions。完整 CI 失败后的任何重跑均须用户明确授权。
- 按维护者授权及仓库保护规则合并。生产部署和正式发布分别取得用户明确授权。
- 详见 [SECURITY.md](SECURITY.md) 的漏洞与密钥处理要求。
