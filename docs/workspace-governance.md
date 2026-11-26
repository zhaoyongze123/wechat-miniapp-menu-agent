# 跨平台开发工作区与运行时存储治理

本文件是 [AGENTS.md](../../AGENTS.md) 的展开版，只约束“开发主机上的文件从哪来、放哪、何时回收”，不改变业务模型、API 与 OTA 协议。适用于 macOS 与 Windows 两台设备以及其上运行的所有 Agent。

## 1. 原则

1. GitHub 仓库是唯一共享代码事实源；代码交接走 commit、push、fetch、PR，不复制完整源码目录。
2. 每台设备只维护一个 Git 主仓，主仓只负责同步 `main`、管理 Git 引用与 Worktree，不承担功能开发。
3. 开发工作区按任务临时存在，Issue 结束、PR 合并且确认无独有内容后即回收，不长期堆积完整副本。
4. 独有提交、未提交修改、业务数据、测试证据必须先保护再回收，保护方式可为远端备份分支、Git Bundle 或可恢复补丁。
5. 同一 Issue 分支同一时间只允许一个 Agent 持有写入权，其他设备只读审查。
6. 开发跨平台，验收按目标平台：macOS 测试通过不能替代 Windows 原生功能验收。
7. 不新增工作区管理平台、分布式锁服务、管理数据库或常驻进程，依靠 Git、Issue、PR 与系统工具完成。

## 2. 职责划分

| 工作内容 | macOS | Windows |
| --- | --- | --- |
| Python 后端开发、单元与集成测试 | 支持 | 支持 |
| React 前端开发与构建产物核对 | 支持 | 支持 |
| 架构设计、源码审查、Docker Hub 开发 | 支持 | 支持 |
| Windows 桌面自动化、微信小程序采集 | 仅静态审查 | 主要环境 |
| Velopack 安装、升级、回滚验收 | 不可替代 | 必须 |
| 正式生产运行（Worker / Hub） | 不承担 | 承担 |

## 3. 目录规范

两台设备使用同一套逻辑结构，绝对路径不要求一致；由官方框架管理的目录（Docker 数据卷、Velopack 安装根、包管理器缓存）保持官方布局，不为整齐而搬移。

macOS 受管理根目录：`~/项目/意谱科技/ma-workspace`

Windows 受管理根目录：`D:\ma-workspace`

```text
ma-workspace/
├── development/worktrees/   # issue-<编号>-<简名>，唯一允许存放 Worktree 的位置
├── testing/
│   ├── fixtures/            # 公共测试素材
│   └── environments/        # 按 Issue 隔离的测试运行环境
├── release/
│   ├── packages/            # 当前版本与必要回滚包
│   └── manifests/           # 版本与摘要清单
├── cache/build/             # 可重建构建缓存
├── backups/
│   ├── database/            # 一致性数据库备份
│   └── snapshots/           # 迁移恢复点
└── archive/
    ├── legacy/              # 不可重建的历史文件
    └── development/         # 独有开发记录与测试证据
```

约束：

- 禁止在 `/tmp`、`/private/tmp`、`/var/folders`、盘根目录或源码目录旁创建需要持续保留的 Worktree。
- 新 Worktree 一律 `git worktree add <ma-workspace>/development/worktrees/issue-<编号>-<简名>`。
- 主仓目录保持原地，不迁移；运行时数据不写入主仓源码目录。

## 4. Git 主仓与 Worktree 生命周期

1. 创建 Issue，明确范围与验收平台。
2. 从最新 `origin/main` 建分支与 Worktree，命名 `issue-<编号>-<简名>`。
3. 在 Worktree 内开发并做定向测试（Mac 或 Windows 均可）。
4. 提交并推送到 GitHub，冲突先 rebase 到最新 `main`。
5. 需要另一台设备接力时，该设备 fetch 同一分支到自己的 Worktree。
6. 提 PR，按目标平台验收，Review 通过后合并并关闭 Issue。
7. 核实成果已全部进入远端后，清理本地 Worktree。

第 7 步是 Agent 的强制职责，不依赖人工提醒。回收前必须逐项核实：

- `git fetch --prune` 后的真实合并状态（包含 squash / rebase 合并）；
- 独有提交（`git log --all --not --remotes`）；
- 未提交修改、未跟踪与 ignored 文件；
- 独有业务数据、配置、测试证据；
- 是否被运行中的进程、计划任务、启动项引用。

上述项目可先用只读脚本盘点：

```bash
python3 deploy/tools/workspace_audit.py --repo <主仓路径>        # 表格
python3 deploy/tools/workspace_audit.py --repo <主仓路径> --json # 机器可读
```

`状态` 列区分 `missing`（目录已删）、`locked`、`prunable`（Git 判定失效）与 `inaccessible`（目录存在但 Git 读取失败）；`merged` 表示是否已进 `origin/main`，`pushed` 表示 HEAD 是否已存在于任一远端分支，`unique` 为不在任何远端引用中的提交数。Git 命令失败时输出 `-`/`?` 并把原因写入“盘点异常”，不会显示为干净。

脚本只读，结果只是判断依据：`ignored` 内容、运行进程与计划任务引用仍需人工核实，盘点通过不等于可以删除。

回收使用 `git worktree remove`；不以 `--force`、`reset --hard`、`clean -fdx` 绕过保护。需要保留但暂不活跃的 Worktree，用 `git worktree move` 迁入 `development/worktrees`。

## 5. 跨设备交接与单分支写入

- 交接以 commit 为准：接手方先 `git fetch`，确认 `origin/<branch>` 与本地一致后再动手。
- 一次只有一个设备持有写权；另一端只读审查，或在 Issue / PR 中留言交接。
- 交接记录写明：Issue、分支、最新 commit、已完成项、待验证项、目标验收平台。
- 禁止把整份工作区复制到另一端继续开发；这会绕开 Git 产生分叉事实源。

## 6. 目标平台验收

- macOS 可完成通用开发、单测、集成测试与前端构建核对。
- 涉及 Windows 桌面自动化、微信客户端、Velopack 安装/升级/回滚、Hub 容器镜像的功能，必须在 Windows 实机验收。
- 验收证据（日志、截图、安装包摘要）随 PR 记录，不再依赖本机 Worktree 存活。

## 7. 运行时数据、日志与忽略规则

- 运行数据、日志、截图、数据库不入库；仓库 `.gitignore` 负责通用规则。
- 仅本机存在的旧布局目录（如布局迁移后的历史源码目录）写入 `.git/info/exclude`，不污染共享 `.gitignore`。
- 已确认 `main` 不跟踪运行日志；若后续分支重新跟踪日志，应在该分支取消跟踪并补齐忽略规则，磁盘上的本地日志文件保留。

## 8. 历史工作区清理与备份保护

保护优先级：远端备份分支 > Git Bundle > 补丁与文件副本。

- 独有提交：推送到 `backup/105-*` 类远端分支，并 `git ls-remote` 核验可达。
- 未提交修改：`git diff HEAD` 导出补丁，连同原文件副本归档到 `archive/development/`。
- 独有文档与业务数据：分类归档，记录来源、时间、对应 commit、恢复方法。
- 可重建内容（`.venv`、`node_modules`、构建缓存）：不归档，直接回收。

固定保留策略：

| 类型 | 保留周期 |
| --- | --- |
| 日常数据库备份 | 最近 7 天 |
| 每周备份 | 最近 4 周 |
| 每月关键备份 | 最近 3 个月 |
| 迁移前恢复点 | 验收完成后 30 天 |
| 正式安装包 | 当前版本及必要回滚版本 |
| 本地临时构建产物 | 任务完成后 7 天 |
| 测试环境 | Issue 结束后回收 |
| 独有业务数据 | 不自动删除 |
| 活跃事务恢复包 | 事务安全结束前不得删除 |

## 9. Agent 任务结束报告

每轮任务结束时必须报告：

1. Issue、PR、分支与最新 commit；
2. 未提交修改是否已保护及保护位置；
3. 测试平台与验证结果（区分“代码实现”“测试通过”“业务验收”）；
4. Worktree 当前状态（保留 / 回收 / 迁移）；
5. 保留原因或清理结果，以及实际释放的空间。

## 10. 2026-11-26 盘点与治理结果

### macOS

| 指标 | 治理前 | 治理后 |
| --- | --- | --- |
| 主仓 | `1ef91cc`，落后 `origin/main` 235 个提交，2 个已跟踪文件与 1 个日志脏改 | `9e2b88b`，领先 0 / 落后 0；无靠 `.git/info/exclude` 掩盖的目录 |
| 注册 Worktree | 16 | 3（主仓 + #101 + #102，均属未关闭 Issue） |
| 旧克隆 | `/private/tmp/menu-agent-pr25.*` 及 22 个同源 Worktree，实测 4.16 GB | 已回收，`df` 释放 2.78 GB |
| 旧运行环境 | launchd `com.yipu.menu-capture`（PID 1320），数据 5.2 GB | 服务停止、启动项移除；数据归档，删除可重建部分 0.68 GB |
| 保护 | 3 个未推送分支、2 个 stash、4 个工作区补丁 | 5 个远端 `backup/105-mac-*` 分支、`archive/development/issue105-mac/`、`archive/development/issue105-mac-clones/`、`archive/legacy/mac-legacy-runner/` |

旧克隆归档：`menu-agent-pr25.bundle`（56 MB，`git bundle verify` 报告完整历史）、31 个独有提交（23 个分支头）逐个验证可读取、3 个未提交补丁（反向应用校验通过）、2 个构建候选包与浏览器测试证据，合计 58 MB；未推送任何内容到远端，未保留完整仓库副本。

旧运行环境：任务库 1380 条、最后写入 2026-11-09，停止前无连接、无请求日志，判定为淘汰的开发服务。`data/`（含 4.2 GB 素材、3807 个文件，`integrity_check=ok`）与凭证配置整体移入 `archive/legacy/mac-legacy-runner/`，权限 600；素材未与生产 Hub 逐哈希比对，按“价值未证明可丢弃”保留。

### Windows

| 指标 | 治理前 | 治理后 |
| --- | --- | --- |
| 主仓 | `D:\menu-agent`，`bf03388`，落后 145 个提交 | `9e2b88b`，领先 0 / 落后 0（`fetch` + `merge --ff-only`） |
| 注册 Worktree | 3（主仓 + #105 + #103） | 2（主仓 + #103 活跃区） |
| 旧治理分支 | `chore/105-storage-governance @ a165f1a`，PR #107 | 本地与远端分支已删除，PR #107 关闭；补丁归档 |
| 保护 | `work/` 未跟踪 | `work/` 全量备份至 `archive/development/issue105-workdir/`（234,200,210 字节一致） |

主仓同步不触及生产：Worker 0.3.4 从 `runtime/MenuCapture/releases/0.3.4` 运行，数据在 `data/MenuCapture`，两者均未被 Git 跟踪；同步后 `/healthz` 返回 200。

### 剩余风险与未完成项

1. `D:\menu-agent\UFS_Menu_Scraper`（13.76 GB，含素材）与 `UFS_Menu_Capture_CrossPlatform`（5.8 MB）仍在主仓内，被计划任务的 `.venv` 入口引用；已按规范加入本机 `.git/info/exclude` 过渡防误提交，待 #101 生产迁移窗口移出并修正计划任务引用。
2. `archive/legacy/mac-legacy-runner/data/materials`（4.2 GB）未与生产 Hub 做逐哈希比对，暂按独有业务数据保留，不自动删除。
3. `D:\menu-agent-backups\issue88-*`、`D:\handoff\menu-hub-migration-*` 与 `D:\menu-agent\hub-data` 按 30 天保留期继续观察，尚未证明可回收。
4. Windows Worker `/api/version` 响应缓慢为既有问题；另有 0.3.3 残留 updater 进程（PID 23880），均未在本次重启或清理。
5. `/private/tmp/menu-agent-issue101` 与 `issue102` 属未关闭 Issue，Issue 结束后按第 4 节回收。
