# 贡献指南

本仓库交付 Windows 生产采集端（Worker）、共享后台（Hub）与 React 控制台。提交前请先阅读 [工程规范](docs/engineering-standards.md) 与 [项目定位](docs/PROJECT_MAP.md)。

## 环境要求

- Windows 10/11（采集端），已登录微信桌面版。
- Python 3.12，依赖通过 `uv` 管理。
- Node.js 22（`web-react/`）。
- Docker Desktop（仅 Hub 与容器化验收需要）。

## 本地开发

```powershell
cd UFS_Menu_Scraper\macos_runner
uv sync --frozen --python 3.12

# 只跑与改动相关的测试，避免全量等待
uv run --frozen pytest -q tests\test_server.py tests\test_shared_workbench.py

cd web-react
npm ci
npm run typecheck
npm test
npm run build   # 产物提交到 ../web/react，CI 会校验一致性
```

## 分支与提交

- 分支：`feat/<issue>-<描述>`、`fix/<issue>-<描述>`、`chore/<issue>-<描述>`、`docs/<issue>-<描述>`。
- 提交信息：`feat: 中文描述`、`fix: …`、`refactor: …`、`test: …`、`docs: …`、`chore: …`。
- 一个提交只做一件事；不提交密钥、本机配置、运行数据、截图或日志。

## 提交前检查

1. `ruff check .`（在 `UFS_Menu_Scraper/macos_runner` 下，门禁规则见 `pyproject.toml`）。
2. 相关 Python 测试与前端 `typecheck` / `test`。
3. 涉及前端时重新构建并提交 `web/react` 产物。
4. 涉及安装器、Hub 镜像或升级回滚时，说明验证方式与证据位置。

## 数据边界

- 采集输出默认写入 `输出/`，本地数据库在数据目录，二者均已被 `.gitignore` 排除。
- 如需提交示例数据，只提交脱敏、最小化的测试夹具，放在测试目录内。
- 本地临时脚本写入 `scratch/`（已忽略），不要散落在仓库根目录。

## Pull Request

- 标题与描述使用中文，说明改动动机、实现要点、验证结果与未完成事项。
- 生产切换、发布与合并需要维护者确认；PR 合并不等于发布。
- 详见 [SECURITY.md](SECURITY.md) 的漏洞与密钥处理要求。
