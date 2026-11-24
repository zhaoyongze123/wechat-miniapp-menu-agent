# 仓库治理审计（2026-11，Issue #28）

范围：清理历史运行产物、规范化 `.gitignore`、整理文档、完善协作文档与 CI 门禁、评估旧工程依赖与大模块边界。不改变业务功能，不重构采集执行链路。

## 1. 清理前后统计

| 指标 | 清理前 | 清理后 |
| --- | --- | --- |
| 跟踪文件数 | 601 | 429（含新增治理文档 7 个） |
| 跟踪体积 | 62.8 MB | 11.33 MB |
| 最大目录 | `输出/` 153 个文件 / 44.22 MB | `docs/` 136 个文件（含验收证据） |
| 历史运行截图 | `UFS_Menu_Scraper/macos_runner/output/` 26 个文件 / 3.49 MB | 已退出跟踪 |

处理方式：

- `输出/` 与 `macos_runner/output/` 使用 `git rm -r --cached` 退出跟踪，**本地文件保留**，不删除任何采集结果、截图或数据库。
- 新增 `.gitignore` 目录约定（`/输出/`、`data/`、`output/`、`*.sqlite3*`、日志、`scratch/`），移除历史日期白名单与一次性脚本特例。
- 有意提交的构建产物 `web/react/` 保留跟踪，CI 继续校验其与源码一致。

## 2. 敏感信息检查

- 对全部跟踪文本文件扫描 `password|secret|token|api key|sk-*|wxid_*|手机号` 等模式：命中均为代码标识符与测试变量名，未发现硬编码真实凭据。
- `输出/` 内含真实门店截图、运行记录与分配数据，属于业务敏感数据，已退出跟踪；本地副本保留。
- 本机密钥文件（`config.local.json`、`secrets.local.json`、`deploy/.env`）均已忽略，未进入仓库。
- 审计当时记录的可见性已失效。当前仓库公开，旧提交仍可能访问已退出跟踪的数据。现行风险和处置状态见 [公开仓库资产核查](public-repository-review.md)。

## 3. 文档整理

| 处理 | 内容 |
| --- | --- |
| 移动到 `docs/product/` | 微信小程序菜单导航 Agent PRD |
| 归档到 `docs/archive/2026-11-capture-fixes/` | 根目录 `spec.md`/`tasks.md`、`macos_runner/spec.md`/`tasks.md` |
| 新增索引 | `docs/archive/README.md`（时间、原位置、替代文档） |
| 保持现状 | `docs/react-migration/`（当前版本的迁移与验收证据） |
| 新增 | `AGENTS.md`（精简规则）、`docs/governance/engineering-standards.md`、`CONTRIBUTING.md`、`SECURITY.md`、`CHANGELOG.md`、`.editorconfig` |

`AGENTS.md` 已移除个人机器绝对路径，改为环境要求与约束说明；细节规范集中在工程规范文档，避免两份规则漂移。

## 4. 静态检查与 CI 门禁

- Python 采用 Ruff（`pyproject.toml`）：
  - 门禁 `E9,F63,F7,F82`：当前 0 项，已加入 CI `python-lint` 作业。
  - `E4,E7,E9,F` 存量基线 65 项：`E701` 35、`F401` 11、`F841` 8、`E702` 7、`E731` 2、`E402` 1、`F811` 1，按模块逐步治理，不做一次性全量格式化。
- 前端继续以 `typecheck` + Vitest + 构建一致性作为门禁。
- ESLint 暂不引入：仓库当前无 ESLint 依赖与基线，直接接入会在数百个既有文件上产生大量噪声，且需要改动依赖锁文件与 CI 时长；按“最少必要配置”原则推迟，接入前置条件为先在独立分支固化基线并只拦截新增问题。
- 既有 `react-web` / `windows` / `hub` 三项门禁保持不变，覆盖安装、升级回滚与 Hub 前台。

## 5. 旧工程依赖矩阵

| 组件 | 使用方 | 生产是否必需 | 结论 |
| --- | --- | --- | --- |
| `UFS_Menu_Capture_CrossPlatform/`（UiPath xaml/json，21 个文件） | `server.py:89` 作为 `project_dir`；`tests/test_uipath_window_contract.py` | 否（Windows 原生执行为默认；安装包只打包 `menu_capture`、`web`、`ui_tars`） | 保留为 UiPath CLI 启动方式的兼容资产，标注 legacy |
| `menu_capture/macos.py` | `environment.py`、`image_tools.py` 内惰性导入 | 否（Windows 分支不加载） | 保留，供跨平台调试 |
| 目录名 `macos_runner` | 安装器、路径与文档 | 是（路径兼容） | 不改名，README 已说明历史命名 |
| `ui_tars/` 适配层 | `build.ps1` 打包；`ui_tars_trial.py` | 是（模型试跑） | 保持现状 |

移除条件：UiPath CLI 启动方式正式下线、对应契约测试迁移完成、安装器不再引用 `project_dir`。在此之前删除会破坏可选启动方式与测试。

## 6. 大型 Python 模块评估

| 模块 | 行数 | 结构 | 被依赖 | 拆分边界建议 | 风险 |
| --- | --- | --- | --- | --- | --- |
| `uipath_runner.py` | 7150 | 11 类 / 186 方法 | `annotation`、`environment`、`server` | 1) 传输与进程（`FileRobotTransport`、管道、超时）2) 协议错误类型 3) 窗口流程编排 4) 模型/OCR 结果处理 | 高，需先补执行链路契约测试 |
| `materials.py` | 2629 | `MaterialService`（100 方法） | 5 个模块 | 已拆出 `material_jobs` / `material_versions` / `store_material_versions`；剩余按“上传与去重”“版本发布”“审核联动”拆分 | 中 |
| `server.py` | 2026 | 3 个注册函数 | 无 | 采集路由可迁至 `capture_routes.py`，与 `shared_routes.py` 对称 | 低 |
| `annotation.py` | 1926 | 3 类 / 78 方法 | `server` | 会话生命周期、执行器适配、坐标几何三部分 | 中 |
| `ui_tars_trial.py` | 931 | 4 类 | `uipath_runner` | 协议、模型、管道读取已基本分离，维持现状 | 低 |
| `shared.py` / `store.py` / `rejections.py` | 685 / 648 / 342 | 单服务类 | 多模块 | 规模可接受，不拆分 | 低 |

本轮只给出边界与前置条件，不做拆分实现；后续按独立 Issue 逐个进行，每次只动一个模块并跑对应契约测试。

## 7. 遗留事项

1. ESLint 接入（含依赖与基线固化的独立 PR）。
2. 仓库公开前评估历史 blob 重写与凭据轮换。
3. `docs/react-migration/` 证据在下一版本稳定后可归档。
4. `uipath_runner.py` 与 `materials.py` 的拆分按独立 Issue 推进。
5. Ruff 存量 65 项按模块随改动顺带清理，不单独全量格式化。

## 8. 回滚

本分支只涉及文档、忽略规则、跟踪清单与 CI 配置，不涉及运行时数据与安装器逻辑。回滚方式为 `git revert` 相关提交；本地运行数据未受影响。
