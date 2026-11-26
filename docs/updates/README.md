# 软件更新与版本生命周期（Issue #32）

本目录记录应用内更新的发布契约、安全边界与当前实施阶段。更新发现、清单校验、偏好、审计、受控下载已实现；旧 Inno Windows 执行器曾通过隔离环境验收；新 Velopack 执行器尚未通过真实安装、切换与回滚验收，当前安全门禁关闭自动安装。Hub 镜像更新执行器已实现并通过隔离 Compose 验收（含真实 v0.2.2 跨版本回滚）；历史版本/下载缓存/日志清理与 Hub 镜像保留策略已实现（先预览、确认后删除）；多设备状态汇总与版本元数据分发已实现，批量推广仍待实施。

Issue #32 最新正文已把产品形态收敛为「一次授权完成更新」：用户只需点击一次“立即更新”，系统负责下载、校验、等待采集结束、安装、重启、重连与健康检查；技术细节下沉到高级设置。本目录按该形态记录契约，并明确取消 Hub 远程触发安装与多设备分批安装编排。

## 一次授权更新（当前产品形态）

- 入口：设置中心「软件更新」首屏。`POST /api/updates/update`，请求体 `{ "mode": "immediate" | "idle" }`：
  - `immediate`：立即开始，若下载未完成则先下载并校验，随后等待采集/标注结束再安装。
  - `idle`：只登记授权，后台在采集/标注结束后开始下载与安装。
- 取消：`POST /api/updates/update/cancel`。安装执行器进入启动/安装/切换阶段后拒绝取消（409），其余阶段解除授权并停止在途下载；已下载部分保留，可续传。
- 授权记录复用 `data/update-schedule.json`（含 `version`、`mode`、`attempts`），不新增第二套状态机；进程重启后授权仍有效。
- 下载未完成也可以先授权：后台先等待下载完成，再进入空闲闸门；进程重启导致下载 `interrupted` 时自动续传一次，仍失败才解除授权。
- 安装仍由既有执行器完成最终空闲校验、静默安装、健康检查与回滚；服务端授权不绕过任何安全闸门，也不强行结束采集或人工标注。
- 服务端统一进度：`GET /api/updates/status` 的 `flow` 字段（`active`、`stage`、`version`、`mode`、`message`、`error`、`progress`、`authorized_at`、`finished_at`）。`stage` 为 `idle/downloading/waiting/installing/verifying/restarting/rolling_back/completed/failed/rolled_back`，前端只展示，不自行拼接下载、安排与安装状态。
- 执行器终态只有在其 `target_version` 与本次授权版本一致时才映射到 `flow`，避免历史 `completed`/`failed` 串到新版本。
- 页面关闭或刷新不影响更新：下载、等待、安装均在服务端后台继续；页面重连后继续读取同一 `flow`。
- 更新完成后，前端在确认 `current_version` 已是目标版本后自动刷新一次以加载新版前端（`sessionStorage` 防循环）。
- 界面层级：首屏只保留目标版本、当前版本、更新说明摘要、进度、状态与「立即更新」「空闲后更新」「取消更新」「重试更新」；发布渠道、自动检查/下载、保留数量、下载详情、安装日志、Release 资产与 SHA-256 统一收进「高级设置与诊断」。SHA-256、Docker、执行器、安装路径等不作为普通用户的主流程。
- 范围收敛：不做 Hub 远程触发其他电脑安装、不做多设备分批安装编排；独立发布签名基础设施、Hub 镜像归档定时治理、历史技术方案清理均暂缓。暂缓独立签名不代表取消既有校验——固定仓库正式 Release、清单 SHA-256 与 Windows 执行器签名状态检查仍全部保留。


## 发布源与发现

- 固定仓库：`zhaoyongze123/wechat-miniapp-menu-agent`。
- 只读取 GitHub **正式 Release**；忽略 draft。`stable` 渠道忽略 prerelease，`preview` 渠道才接收预发布版。
- Hub 与 Worker 各自在服务端检查并缓存，默认有效期 1 小时；失败退避 5 分钟并保留最近一次成功信息，标记为过期。
- 版本按语义版本比较（含预发布），不按字符串或发布时间；禁止自动降级。
- 手动检查通过 `POST /api/updates/check`；状态通过 `GET /api/updates/status`；偏好 `PUT /api/updates/preferences`；审计 `GET /api/updates/history`。
- 网络请求不持有状态锁；自动检查在后台线程执行，状态查询只读取缓存快照，慢网络不会阻塞设置页。
- 同一时刻只允许一个检查任务；手动检查遇到在途任务时复用其结果，不重复请求 GitHub。
- 渠道切换会使在途请求失效：请求返回后先比对渠道代号，旧渠道的成功/失败结果一律丢弃，不覆盖新渠道缓存。
- 缓存是否过期按 `checked_at` 与 1 小时有效期计算；检查失败保留上次成功信息，但信息随年龄正确标记为过期。
- 状态接口返回本机 `platform` 与归一化后的 `arch`；只有发布资产的实际架构与本机一致时才标记 `update_available`。

## Release 资产契约

正式 Release 必须附带 `update-manifest.json`（与安装目录内的运行时 `release-manifest.json` 区分）：

```json
{
  "version": "0.3.0",
  "channel": "stable",
  "git_commit": "<40 位 commit>",
  "built_at": "2026-11-24T00:00:00Z",
  "min_supported_version": "0.2.0",
  "rollback": "supported",
  "assets": [
    {
      "kind": "windows_installer",
      "name": "MenuCapture-0.3.0.exe",
      "size": 123456789,
      "sha256": "<64 位小写十六进制>",
      "url": "https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/releases/download/v0.3.0/MenuCapture-0.3.0.exe",
      "platform": "windows",
      "arch": "x64"
    },
    {
      "kind": "hub_image",
      "name": "hub-image-0.3.0.tar.gz",
      "size": 123456789,
      "sha256": "<64 位小写十六进制>",
      "url": "https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/releases/download/v0.3.0/hub-image-0.3.0.tar.gz",
      "platform": "linux",
      "arch": "amd64"
    }
  ]
}
```

服务端校验：

- 清单 `version` 必须与 Release 标签一致；清单渠道必须与版本本身一致（正式版 `stable`、预发布版 `preview`）。
- `stable` 渠道只接收正式版；`preview` 渠道同时接收正式版与预发布版，因此预览用户仍能升级到更新的正式版。
- 资产 `kind` 只能是 `windows_installer` / `hub_image`，不可重复；名称必须存在于同一 Release。
- 资产平台/架构必须与类型一致：`windows_installer` 为 `windows/x64`，`hub_image` 为 `linux/amd64`；架构写法按 `x64/amd64/x86_64`、`arm64/aarch64` 归一化后再与本机真实 CPU 架构比较，当前平台没有匹配资产时不提供更新。
- 大小与 Release 资产一致；SHA-256 为 64 位十六进制；若 Release 提供 `digest: sha256:...` 必须一致。
- 资产 URL 只允许 HTTPS 的 `github.com` / `objects.githubusercontent.com` / `release-assets.githubusercontent.com`，且路径必须包含 `/releases/download/<tag>/`。
- `hub_image` 可带可选 `registry`（固定 `ghcr.io/<org>/<repo>`）与 `digest`（`sha256:<64 位十六进制>`），用于 OCI 分层拉取；任一字段非法只丢弃这两个可选字段，完整归档资产仍然可用，旧客户端完全不受影响。
- 缺少清单或校验失败时仍展示该 Release，但标记不可更新并说明原因，不静默回退到旧版本。

SHA-256 只证明完整性。Windows Velopack 发布还要求 Ed25519 发布者签名，签名绑定版本、渠道、commit、最低支持版本、回滚策略、完整资产清单以及 Velopack full/delta/index 身份；客户端只信任代码内固定公钥，缺签名、未知密钥或外层策略不一致时拒绝更新。正式发布密钥由受控发布环境注入，绝不提交私钥或用 unsigned 产物代替。当前客户端固定公钥表尚未配置，因此正式签名发布和安装均 fail-closed，属于发布启用阻断项。

### Windows 增量更新（Velopack）

Windows 的差分、重建与安装已交给 Velopack，不再由本项目生成 `patches`。

- 构建期用 `vpk pack` 生成 `Setup.exe`、full/delta `.nupkg` 与 `releases.win-x64-<channel>.json`；有历史正式包时由 `vpk download github` 取回并由 vpk 自动生成增量。
- 一次性全量过渡可传 `-FullOnly`（工作流输入 `full_only=true`）：跳过历史包下载，并使用 Velopack 原生 `vpk pack --delta None`；构建、索引与签名清单三层都会校验本次版本不包含 Delta。
- 发布草稿可传 `source_run_id` 复用已验收候选 run 的产物：用原生 `download-artifact` 跨 run 取值，核验源 run 的成功状态、仓库、工作流、版本、commit、`full_only` 与资产摘要；签名清单的 `git_commit` 与 Release 目标取自产物 commit，GHCR 只从归档镜像 `docker load/tag/push`，不重新构建。
- 客户端用 Velopack Python SDK 的 `UpdateManager` 发现、下载与应用更新；SDK 负责版本选择、差分计算、包重建与全量兜底。
- 新发布清单的 `assets` 保留 `hub_image`，顶层 `velopack` 扩展记录 setup/full/deltas/index 的实际名称、大小、SHA-256、URL、pack ID、版本、commit、tag 与渠道，并由完整清单签名绑定。禁止将 Velopack Setup 声明为旧 `windows_installer`。下载前后核对 SDK 资产身份及实际完整包摘要；仅由 Velopack 官方 SDK 选择 delta 或 full 并执行重建，不实现自制补丁应用。
- 渠道固定为 `win-x64-stable` / `win-x64-preview`，平台与架构编码在通道名中，避免跨平台误装。
- 首次迁移到 Velopack 的版本没有历史 Velopack 包，按全量迁移处理；之后才由框架生成真实增量。
- 当前正式 v0.3.4 清单仍是 Inno/文件补丁契约，只有 `windows_installer`，没有 Velopack full 包或签名；它不能成为该执行器的 OTA 恢复基线。首次切换必须经过独立验证的完整安装迁移，随后基于已签名的 Velopack 版本验证增量；不得把旧版改写或伪装成 Velopack 发布。

## 受控下载

- 通过 `POST /api/updates/download` 启动，`POST /api/updates/download/cancel` 取消；进度随 `GET /api/updates/status` 的 `download` 字段返回。
- 只有已通过清单校验、且 `update_available` 为真的版本才允许下载；资产必须与本机平台、归一化架构一致，否则直接拒绝。
- 文件只写入 `data_root/data/updates/<version>/`：先写 `<asset>.part`，完整校验 SHA-256 后才原子替换为正式文件，绝不写入程序目录或安装目录。Windows 的 Velopack 包由 SDK 写入 `%LOCALAPPDATA%\Yipu.MenuCapture\packages`，不经过本目录。
- 断点续传使用 HTTP `Range` 与 `.part` 现有长度；服务器忽略 Range 返回 200 时从零重写，并重新按完整文件大小检查磁盘余量。
- 续传响应必须提供匹配的 `Content-Range`（起点、末尾、总长）与 `Content-Length`；记录 `ETag`/`Last-Modified` 并在续传时发送 `If-Range`。范围不一致时丢弃断点并自动重下一次完整文件，绝不把错误区段拼进产物。
- 下载与清单请求只允许重定向到受信任的 GitHub 域名（`github.com`、`objects.githubusercontent.com`、`release-assets.githubusercontent.com`、`api.github.com`），其他主机或 HTTP 一律拒绝。
- 下载前按剩余字节 + 64 MiB 余量做磁盘空间预检；响应体超过清单声明大小时立即中止。
- 同一时刻只允许一个下载任务；进程重启后把未完成的下载如实标记为“已中断”，不会假装仍在下载。
- 完成后只上报文件路径与校验结果，不执行安装、切换或重启；这些能力由下一节的独立执行器负责。

## Windows 安装执行器（Velopack，当前开发门禁）

- PR #103 已实现独立于 `current` 的恢复看门狗、SDK 更新交接、候选版本隔离数据库验证与恢复门禁；但当前开发机不具备 Windows 原生运行环境，尚未完成真实 SDK/Windows 安装与切换验证，故 `install_supported=false`，`MENU_CAPTURE_ALLOW_SELF_UPDATE=1` 不能绕过门禁。
- 下载及资产身份验证与安装权限分离。正式资产必须具有有效的 Ed25519 签名并匹配客户端固定公钥；`MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE=1` 仅是显式开发/测试豁免，不是生产回退路径。发布清单缺签名、信任根未配置、签名错误或未知签名模式均拒绝。
- `data/update-install.json` 为唯一安装事务事实源，事务带 schema_version、transaction_id、目标发布资产身份、数据库结构基线。跨进程短时锁与原子替换保护状态迁移；陈旧请求不得覆盖新事务。
- Worker 关闭入口仅接受本机 HTTP POST、协调令牌、合法 stopping 事务及匹配目标版本；重新检查采集和标注空闲。旧 update-apply-request、update-apply-state、rollback-request 不再驱动安装。
- 超时只用于诊断，不解除业务闸门；仅版本字符串相等不能完成安装。未知状态和损坏记录保持恢复阻断。
- 恢复看门狗位于 `data/recovery`，必须先复制自当前受信发布并完成 nonce/PID/创建时间 ready 握手。它与 Python 共用同一字节锁（`data/update-install.lock`），在锁内重读事务、确认协调器已退出且官方 `Update.exe` 已停止并超过无进展时限后写入 `recovery_required`，**定位为独立于 `current` 的故障检测器**，不自行解包、切换目录或回滚；新启动器完全无法启动时按下方受控人工恢复 SOP 处置（不宣称已验证无人值守自动恢复）。
- 验证候选版本时，使用 SQLite backup API（包含 WAL 已提交事务）在**与 `data_root`、`current`、`packages` 完全分离的独立目录**创建一致性副本；缺少主库、数据库清单为空、复制失败、结构或核心业务表行数不一致均拒绝成功。真实 Worker 在 `verifying` 阶段启动时，`TaskStore` 初始化与后台业务/同步写入（`recover_publishes`、`store.recover`、`materials`、`shared worker`、`connections`、`resume_schedule`）明确分离，须待事务进入 `completed` 并通过本机 `/api/internal/release-business-gate` 放行后才启动后台业务服务。
- 准备阶段（`prepared`）先按事务所有权规则 `claim` 再做预检，确保预检失败合法落盘为 `failed` 并保留旧 Worker；HTTP shutdown 响应丢失时核验真实进程树与端口，状态不确定一律进入 `recovery_required`，绝不直接 `failed` 解锁。
- 恢复包严格使用 `previous_version` 的实际发布渠道（支持 `stable -> preview`、`preview -> stable` 与同渠道），并在 `recovery_asset.channel` 中持久化，回滚时按旧包实际渠道调用 SDK。
- 回滚前要求候选 Worker 通过受控本机 API 确认关闭，并验证 Job Object 进程树退出且业务端口释放。停止失败或进程状态未知时保持 `recovery_required`，不得启动 updater。
- 直接 `--rollback-to` 已禁用。真实 Windows 验收、固定发布公钥引导、数据库迁移兼容及 Inno 迁移脚本/实机验证完成前，不恢复生产自动安装。历史 Inno 验收不构成新框架通过证据。

## Hub 镜像更新执行器（宿主机，需显式部署）

- 执行器独立于被替换的业务容器运行：容器内不能挂载 Docker Socket，也不该自己替换自己。代码见 `deploy/hub-update/hub_updater.py`，仅用标准库。
- 入口：`GET /status`、`POST /apply`、`POST /rollback`；默认只监听 `127.0.0.1`，要求 `Authorization: Bearer <HUB_UPDATE_TOKEN>`，并按 `HUB_UPDATE_ALLOWED_CLIENTS` 网段二次校验来源。供容器访问时才把监听地址改为可达地址，并把来源收敛到 Docker 网桥网段。请求体格式非法返回 400，事务状态冲突（进行中、禁止降级、无回滚目标）返回 409，未授权返回 401。
- `POST /apply` 接受 `{version, confirm, sha256, size, url|archive}`，并可带可选的 `image`/`digest`（OCI 分层交付，见下节）：版本必须语义化且高于当前运行版本，`confirm` 必须等于版本，归档大小不超过 2 GiB；`url` 只允许受信任 GitHub 域名且路径必须指向 Release 资产，`archive` 只能是归档目录内的文件名，禁止绝对路径与 `..`。`image`/`digest` 必须成对出现，`image` 必须等于配置的固定仓库，`digest` 必须是 `sha256:<64 位小写十六进制>`；`url`/`archive` 仍是必需的归档回退依据。
- 执行顺序：磁盘与数据卷检查 → 下载（或读取归档）→ 大小与 SHA-256 校验 → `docker load` 并确认加载出的是 `<镜像仓库>:<版本>` → 以 `MENU_CAPTURE_IMAGE=<镜像仓库>:<版本>` 执行固定的 `docker compose up -d --no-build hub` → 核对容器健康与 `/api/version` 真实版本。任一步失败都会尝试切回上一版本并核对健康。
- 只对配置的 compose 文件、project 和 service 执行 `up -d`，从不执行 `down -v`、`prune` 或删除卷；更新前要求数据卷已存在，避免因拼错卷名创建空卷。
- 事务状态写入 `HUB_UPDATE_STATE_DIR/hub-update.json`（含执行器 PID），执行器重启或状态查询发现进程已退出时，按实际运行镜像收尾，不会永久停在切换中；每次成功/失败/回滚追加到 `hub-update-audit.jsonl`。
- 必须配置的环境变量：`HUB_UPDATE_TOKEN`（≥16 字符）、`HUB_UPDATE_COMPOSE_FILE`、`HUB_UPDATE_PROJECT`、`HUB_UPDATE_DATA_VOLUME`、`HUB_UPDATE_STATE_DIR`；可选 `HUB_UPDATE_ARCHIVE_DIR`、`HUB_UPDATE_HEALTH_URL`、`HUB_UPDATE_HEALTH_USER`/`HUB_UPDATE_HEALTH_PASSWORD`（Hub 的 `/api/version` 走基本认证）、`HUB_UPDATE_BIND`、`HUB_UPDATE_PORT`、`HUB_UPDATE_ALLOWED_CLIENTS`、`HUB_UPDATE_HEALTH_TIMEOUT`。
- Hub 服务端通过 `HUB_UPDATE_URL` / `HUB_UPDATE_TOKEN` 代理调用执行器（`deploy/hub/compose.yaml` 已声明 `host.docker.internal:host-gateway`）；未配置时设置页只显示原因，不提供安装按钮。容器提交「最新版本 + 清单校验后的 SHA-256/大小/URL」，并在执行器声明 OCI 能力时附带 `image`/`digest`，由执行器再次校验。
- 部署时执行器必须监听容器可达的地址，并把 `HUB_UPDATE_ALLOWED_CLIENTS` 收敛到 Docker 网桥网段；切勿对公网开放。
- 回滚兜底：手动回滚或安装失败自动回滚时，若目标版本切回后健康检查不通过，执行器会立即切回回滚前仍在运行的版本并复核健康，避免把采集服务留在停机状态；恢复失败才标记失败并保留状态与审计。
- 回滚架构边界：切换前用当前应用镜像启动一次隔离只读探测容器，禁用网络和 Linux capabilities，只挂载命名数据卷且为只读，不读取宿主机 Docker volume Mountpoint。探测器保存 `data/tasks.sqlite3` 架构快照。自动回退与显式回滚都比较快照；旧表/列/索引/约束被删除或改写、触发器/视图改变、基线缺失或无法读取时停止回滚并标记 `needs_manual_recovery`，保留当前容器与诊断信息，要求人工恢复。仅新增表、带可供旧代码写入的默认值（或允许为空）的列，以及非唯一索引，按向后兼容扩展处理，允许旧镜像回滚；新增唯一索引、既有表约束或外键都需人工判断。这对应现有初始化迁移以 `CREATE TABLE`、`ALTER TABLE ADD COLUMN` 和 `CREATE INDEX` 为主的机制。无法读取架构时不推断兼容。
- 跨版本回滚验收：CI 以 **v0.2.2 正式发布镜像**为基线（直接 `docker load` 已发布产物，不重新打包），验证「生产 0.2.2 → 新版 → 回滚 0.2.2」全流程及旧版本能在新版本迁移过的数据库上启动；此证据适用于已验证的兼容性扩展，不代表任意破坏性迁移都可回滚。

### OCI 分层交付（可选，默认关闭）

- 能力协商：执行器 `GET /status` 在配置了 `HUB_UPDATE_REGISTRY_REPO` 时返回 `capabilities.oci=true`；Hub 只读取最近一次快照，旧执行器没有该字段时一律按不支持处理，继续使用完整归档。只有明确支持时，`POST /apply` 才会附带 `image`/`digest`。
- 固定仓库：`HUB_UPDATE_REGISTRY_REPO` 必须是 `ghcr.io/<org>/<repo>`，且只能等于发布清单里的镜像仓库；执行器不接受请求方任意指定镜像、tag 或平台。平台固定为 `linux/amd64`，digest 必须是不可变 `sha256:` 摘要。
- 执行顺序（OCI 分支）：`docker pull --platform linux/amd64 <repo>@<digest>` → 用 `RepoDigests` 核对摘要、用 `Os/Architecture` 核对平台 → `docker tag` 为 compose 使用的 `<HUB_UPDATE_IMAGE_REPO>:<版本>` → 复用现有切换、健康核对与回滚。拉取失败、摘要不一致或平台不符时记录 `pull failed` 审计，并在同一事务内改用完整归档，不产生第二套状态机。
- Hub 服务端在 OCI 模式下不下载完整归档（`authorize_update`/`schedule`/`download_status` 整条链路都跳过“等待下载完成”），手动下载请求会被明确拒绝，避免“先下 175 MB 再 docker pull”。
- 发布：仅在**正式发布授权**（`create_release`）下，工作流才用 `GITHUB_TOKEN`（`packages: write`）把本次已验收镜像推送到固定 GHCR 仓库；推送前先确认该版本标签不存在，已存在则保留既有已发布镜像、不覆盖其 digest。推送、登录或摘要获取失败只跳过 OCI 扩展，完整归档照常交付。**镜像保持私有，不得擅自改为公开**；清单缺失或字段非法时只忽略 OCI 扩展。
- 能力变化：授权后若宿主执行器丢失 `capabilities.oci`（降级或回滚），Hub 不会空等一个从未启动的归档下载，而是在同一次授权内回退一次完整归档下载；回退失败则明确解除安排并记录原因，不会永久停在等待状态。
- 宿主机要求：能访问 `ghcr.io`，并使用具备 `read:packages` 的凭证执行 `docker login ghcr.io`；不可达或未登录时拉取失败会自动回退归档，不阻塞更新。
- 首次升级：旧执行器没有 `capabilities`，Hub 继续走归档；运维需一次性替换 `hub_updater.py`、配置 `HUB_UPDATE_REGISTRY_REPO` 并重启执行器。之后 Hub 新版本检测到能力后才会启用分层拉取，旧版本客户端与旧执行器行为不变。

## 本地版本清点（Windows）

- 三类目录严格分离，不允许互相反推：
  - `data_root`：可写数据目录（配置、缓存、审计），Windows 通常由 `MENU_CAPTURE_HOME` 指定；
  - `app_root`：当前运行的程序目录，只读取其中的 `release-manifest.json`；
  - `install_root`：多版本安装根（含 `releases/`），仅由显式参数或 `app_root` 的安装布局推导。
- 安装布局：`<install>/releases/<version>/app/...`，`<install>/current-version` 指定启动版本。
- 默认保护：运行版本、`current-version` 指定版本、最近两个兼容版本；保护规则优先于数量上限。
- 清点本身只读：`GET /api/updates/status` 与 `GET /api/updates/cleanup` 不删除目录、不修改
  `current-version`；符号链接/重解析点跳过。
- 非 Windows 或缺少 `releases` 目录时明确显示“不支持”，不伪造可释放空间。数据目录下即使存在 `releases` 也不会被当作安装根。
- Velopack 安装布局下不再暴露 `releases` 清点与历史版本清理（`install_root` 返回空）：`current`、`packages` 与旧版本保留由 Velopack 管理，设置中心只保留下载缓存与日志治理，避免与框架争抢版本目录。

## 存储治理（先预览，确认后删除）

- `GET /api/updates/cleanup` 返回三类范围的只读预览：`releases`（Windows 历史版本）、
  `downloads`（下载缓存）、`logs`（超过 7 天的 CLI 转录）；预检安装/下载状态，阻止期间返回
  `blocked` 与原因。
- `POST /api/updates/cleanup` 必须提交 `{"confirm": "cleanup", "scopes": [...]}`；服务端在删除前
  重新计算候选，只删除仍属于候选的项，任何一项校验失败只记录到 `failures`，不扩大删除范围。
- 删除边界：
  - 历史版本：只删除 `releases/<version>` 直接子目录，逐级拒绝符号链接/重解析点，确认解析后仍
    位于 `releases` 内；绝不修改 `current-version`，不删除运行中/当前/最近两个版本。
  - 下载缓存：只删除 `data/updates/<version>` 直接子目录；保留当前下载状态指向且产物仍在的版本，
    安装执行器仍需使用该包。
  - 日志：只删除超过 7 天的 `debug-logs/cli/*.log`；调试分段与截图证据由日志服务按固定配额管理，
    避免删除后破坏证据引用。
- 安装或回滚进行中、或下载任务运行时，服务端拒绝清理；清理结果写入 `data/update-history.jsonl`。
- 设置中心“存储治理”展示各范围的可释放空间、保留项与阻止原因，只有用户确认后才提交删除。

## 多设备集中更新状态与元数据分发

- Worker 在同步心跳中上报精简软件状态：当前版本、平台、最新版本、是否可更新、渠道、检查时间、
  下载状态与安装状态。字段为固定白名单，不接受路径、凭证或业务数据；Hub 未确认支持前不发送。
- Hub 持久化每个 Worker 的软件状态；`GET /api/shared/status` 的每个设备带 `software`，未上报为
  `null`，并汇总 `update_summary`（已上报/未上报数量、待更新数量、版本分布、目标版本）。
- Hub 的同步响应携带 `supports_software_status` 与 `update_target`：后者只在 Hub 本地缓存新鲜
  （1 小时内）且清单校验通过时提供，包含完整资产元数据。
- Worker 采纳前重新核对版本、渠道、资产平台/架构、大小、SHA-256 与 URL 归属；过期、渠道不一致或
  结构异常一律忽略并回退本机 GitHub 检查。采纳结果在状态中标为 `source: "hub"`，不伪装成本机检查。
- 设备页展示各采集电脑的当前版本、是否可更新与目标版本；未上报单独计数，不用“已是最新”冒充未知。
- 分批推广：`GET/PUT /api/shared/update-rollout` 保存推送范围，`all` 覆盖全部在用设备，`batch`
  只对选中设备分发目标版本；被移除/解除配对的设备自动退出范围，不会继续收到元数据。
- 分批只改变**分发范围**，不触发远程安装：设备仍需在本地确认，`in_rollout=false` 的设备保持本机
  GitHub 检查；Hub/Worker 权限边界不变。

## 运维 SOP（部署、升级、回滚、清理）

以下命令均在隔离环境先演练，生产解冻前不得直接执行。

1. **首次 Inno → Velopack 迁移**：记录并保护实际 MENU_CAPTURE_HOME、设备身份、配置、数据库和待补传数据；确认业务空闲后停止旧 Worker 与登录入口，保留旧程序和可恢复资料。独立安装经验证的新 Setup，复用原数据目录，核对版本、commit、配对和数据后再清理旧入口。失败恢复旧入口，不先卸载旧版。新 Setup 不进入旧 windows_installer 清单，全局 min_supported_version 不为迁移而抬高。
2. **日常升级**：Hub 保持受控更新；Windows 真实安装和恢复验证完成前仅允许检查/受控下载，不启用自动切换。暂停但未结束的业务轮次也阻止安装。
3. **升级前备份（禁止整卷打包素材目录）**：只对核心 SQLite 数据库执行一致性备份（`sqlite3.backup`）并保留配置文件、设备身份与可信旧版完整包；**严禁**全量复制或 `tar` 打包 `materials`、`outputs`、`screenshots` 或整份 Docker 数据卷（升级与 `docker compose up -d` 不会改动既有媒体素材）。结构一致不等于数据语义一定兼容。
4. **回滚与 `current` 外受控人工恢复 SOP**：Windows 直接切换入口已禁用。当新启动器完全无法启动或事务进入 `recovery_required` 时，运维在 `current` 之外按以下顺序执行受控人工恢复（不得解包或手工覆盖 `current`）：
   1. **确认进程与更新器均已停止**：核对 `MenuCapture.exe`、`python.exe`（业务 Worker）与 `<install_root>\Update.exe` 均已退出，且业务端口（默认 `8765`）未被占用；若 `Update.exe` 仍在运行则等待其结束，禁止并发干预。
   2. **审计事务与数据库基线**：读取 `<data_root>\data\update-install.json`（确认 `transaction_id`、`previous_version`、`previous_commit`、`recovery_asset` 与 `schema_baseline.snapshot.sha256`）及 `<data_root>\recovery\watchdog.log`；对当前 `<data_root>\data\tasks.sqlite3` 做只读 SQLite backup 后比对 SHA-256。若数据库已发生前向变更或存在待补传/新增业务数据，必须先备份整份 `<data_root>\data` 并评估兼容性，禁止盲目覆盖。
   3. **核验可信恢复包**：定位 `recovery_asset.path`（位于 `<data_root>\data\recovery\<version>-<sha12>\*.nupkg`），用 `Get-FileHash -Algorithm SHA256` 核对与 `recovery_asset.sha256` 一致，并确认包内 `release-manifest.json` 的 `version`/`git_commit` 与 `previous_version`/`previous_commit` 一致；将已核验的 `.nupkg` 复制到 `<install_root>\packages\` 并再次核对 SHA-256。
   4. **通过官方更新器恢复并复核**：仅在确认恢复包与数据库兼容后，调用官方 `<install_root>\Update.exe`（或对应旧版经验证的官方 `Setup.exe`）完成受控安装；启动后核对 `/healthz`、`/api/version`（版本与 commit 回到 `previous_version`/`previous_commit`）与 `tasks.sqlite3` 完整性，最后留存人工恢复审计记录后再解除 `update-install.json` 恢复闸门。
   - Hub 回滚使用宿主机执行器 `POST /rollback`。
5. **存储清理**：先 `GET /api/updates/cleanup` 预览，再以 `confirm: "cleanup"` 确认；安装/下载
   期间拒绝清理。Hub 镜像与归档执行 `python3 hub_retention.py --keep 3` 预览，确认后加
   `--apply --confirm prune`。绝不删除数据卷、Docker 容器或业务素材。
6. **自动检查/下载**：自动检查默认开启；自动下载默认关闭，开启后只下载并校验、不安装。
   取消下载按 `POST /api/updates/download/cancel`。
7. **撤销分发**：将推送范围改回 `batch` 并清空选择不可保存；正确做法是切回 `all` 或缩小到具体
   设备。已下载到设备的安装包仍由该设备本地决定是否安装。
8. **故障恢复**：安装任务卡住时查看 `GET /api/updates/install` 与安装日志；进程已退出会按实际
   运行版本收尾。Hub 执行器状态见 `HUB_UPDATE_STATE_DIR/hub-update.json` 与
   `hub-update-audit.jsonl`，必要时手动切回上一版本镜像。

## Hub 镜像与归档保留（宿主机脚本）

- `deploy/hub-update/hub_retention.py` 只处理 `<HUB_UPDATE_IMAGE_REPO>:<semver>` 标签与
  `HUB_UPDATE_ARCHIVE_DIR` 下的 `hub-image-<version>.tar.gz`，默认 **dry-run 只预览**。
- 保留规则：最近 `--keep` 个版本（默认 3）、当前容器镜像、执行器 journal 中的目标/上一版本，
  以及任何容器（含已停止）仍在引用的镜像。
- 实际删除必须同时提供 `--apply --confirm prune`；`docker rmi` 不使用 `-f`，镜像仍被占用时如实
  记录失败；绝不删除数据卷、容器或 compose 栈。
- 每次实际删除追加到 `HUB_UPDATE_STATE_DIR/hub-retention-audit.jsonl`。
- 示例：`python3 hub_retention.py --keep 3` 预览；确认后 `python3 hub_retention.py --keep 3 --apply --confirm prune`。

## 当前阶段与后续

已实现：

- 固定仓库正式 Release 发现、stable/preview 过滤、语义版本比较、禁用降级。
- `update-manifest.json` 校验、资产完整性信息展示、1 小时缓存、失败退避与持久化状态。
- 单飞检查与在途结果失效控制；本机 CPU 架构与资产架构匹配校验。
- `data_root` / `app_root` / `install_root` 分离，Windows 版本清点不受数据目录影响。
- 设置中心“软件更新”：当前/最新版本、检查时间、渠道、自动检查、自动下载、已安装程序保留数量、发布说明、资产 SHA-256、版本清点与更新记录。
- 一次授权更新：`POST /api/updates/update` 按 `immediate`/`idle` 授权，后台串联下载、等待空闲、安装、重启与健康检查；`flow` 统一进度、取消与失败重试，页面关闭后仍继续。
- 自动下载：开关打开后，检查发现新版本时复用受控下载链路在后台下载并校验；不自动安装。
- 空闲后更新：`POST /api/updates/install/schedule` 保存安排，后台在采集/标注结束后尝试安装；
  `POST /api/updates/install/schedule/cancel` 取消。安排跨刷新与服务重启保留，执行器仍会二次校验安全条件。
- 保留数量可配置（2–5，默认 3，含当前版本）：清点与清理始终优先保护运行中、当前、待安装与回滚目标。
- 受控下载：资产匹配、磁盘预检、断点续传、进度展示、取消、SHA-256 校验与下载审计。
- Windows 安装执行器：安装前重校验、路径与签名校验、空闲闸门、独立进程静默安装（显式 `/DIR`）、目标版本文件/清单/前端哈希与角色核对、事务异常恢复、迁移感知回滚；需显式启用。
- 旧 Inno OTA 隔离验收已覆盖应用内安装及回滚；该证据不适用于新 Velopack 框架。当前 Windows 自动安装能力关闭。 [隔离验收记录](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/actions/runs/45438329640)
- Windows 增量更新：`vpk` 在构建期基于历史正式 full 包生成 delta；客户端由 Velopack SDK 完成差分下载、重建与全量兜底。首次迁移版本按全量处理。
- 存储治理：`GET/POST /api/updates/cleanup` 提供只读预览与确认后删除；按配置保留当前/运行中/最近多个
  版本并保护待安装下载包，安装/下载期间拒绝清理；设置中心提供存储治理面板。
- Hub 镜像/归档保留脚本 `deploy/hub-update/hub_retention.py`：默认 dry-run，确认后按版本保留策略
  删除旧标签与归档，不触碰数据卷与容器。
- Hub 更新与显式回滚在切换前保存数据库架构快照；只允许已知的兼容性扩展继续自动/显式回滚，破坏性或无法判定的架构变化停止回滚并转人工恢复。
- 多设备集中更新状态：Worker 上报版本/更新状态，Hub 汇总展示并分发新鲜版本元数据，Worker 二次
  校验后采纳，异常自动回退本机 GitHub 检查。
- 分批推广：`GET/PUT /api/shared/update-rollout` 控制分发范围，设备页展示本批设备与“本批不推送”；
  不包含远程安装，安装仍由设备本地确认。
- 检查、偏好变更、下载结果与清理操作写入 `data/update-history.jsonl`。

## 原需求能力对照（Issue #32）

| 能力 | 状态 | 实现与边界 |
| --- | --- | --- |
| 版本发现与清单校验 | 已实现 | 固定仓库 Release、stable/preview、语义版本、SHA-256 与清单校验、1 小时缓存与退避 |
| 自动检查 | 已实现 | 默认开启，可按设备关闭；关闭后仍可手动检查 |
| 自动下载 | 已实现 | 默认关闭；开启后只下载并校验，不自动安装 |
| 手动下载/取消/续传 | 已实现 | 临时文件、If-Range 校验、磁盘预检、取消保留断点 |
| 一次授权完成更新 | 已实现 | `POST /api/updates/update` 一次授权串联下载、等待空闲、安装、重启与健康检查；`flow` 统一进度；页面关闭后服务端继续；可取消/重试 |
| 立即安装 | 已实现 | 需显式启用执行器；下载产物与清单、签名状态、空闲闸门均重校验 |
| 空闲后更新（安排/取消） | 已实现 | 持久化安排，后台等待采集/标注结束；执行器二次校验；可随时取消，不强行结束任务 |
| 回滚 | 部分完成 | Hub 执行器回滚；Windows Velopack 独立恢复协调器及可信旧包恢复待验证 |
| 更新记录 | 已实现 | `data/update-history.jsonl`，含检查/偏好/下载/安装/安排/清理结果 |
| 多设备状态汇总 | 已实现 | Worker 上报版本与更新状态，Hub 汇总区分未上报/离线/已下载/等待空闲/安装成功 |
| 集中分批推广范围 | 已实现 | `GET/PUT /api/shared/update-rollout` 控制目标版本分发范围；不含远程安装 |
| 远程触发安装 | 未实现（明确不在范围） | 安装由各设备本地确认；Hub 不传输任意命令，避免扩大权限 |
| 单台/分批远程安装编排（P2 后半） | 未实现 | 需要设备授权模型、失败停止/重试与并发上限设计，未在本期落地 |
| 代码签名/发布者证明 | 受限 | 尚未配置代码签名证书；执行器强制校验签名状态，未签名需显式放行；SHA-256 不能替代签名 |
| Hub 镜像定时保留 | 部分 | 脚本支持 `--keep/--apply --confirm prune` 与 dry-run；未接入定时任务或管理界面 |
| 保留数量可配置 | 已实现 | 2–5（默认 3，含当前版本）；保护规则优先于数量 |
| 存储治理 | 已实现 | 程序版本/下载缓存/CLI 日志先预览后确认删除；安装或下载期间拒绝清理 |

尚未实现或仍有明确限制（不得在 UI 中伪造）：

- 发布流水线尚未配置代码签名证书，正式包当前为未签名；执行器强制校验签名状态，未签名需显式放行。
  受信任发布证明只有 SHA-256 与仓库来源约束，不能替代代码签名。
- Hub 保留策略尚未接入定时任务或管理界面，当前由运维在宿主机手动执行（按最新范围暂缓接入）。
- 多设备批量安装（远程触发）不在范围内；分批推广只控制目标版本的分发范围，安装仍由设备本地确认。
- Windows 真实设备（隔离机）安装、增量与回滚仍需维护者安排；自动化安装布局、启动与数据保留由 `deploy/windows/smoke.ps1` 覆盖，增量需两个正式包在专用环境验证。本轮“一次授权”只完成单测与组件测试，真实浏览器 + 隔离环境的 OTA 全流程证据需在解冻后按 Issue #32 验收条件单独产出，不能以单测通过代替。
- Windows 增量由 Velopack 生成与校验，不再维护自研补丁链、补丁清单或补丁执行器。

## 运维建议

- 采集期间不执行升级；等待当前任务结束或安全暂停后再进行。
- 升级前备份配置、数据库与必要发布记录；保留当前及最近两个可回滚版本。
- 不删除正式 GitHub Releases/Tags、业务数据、设备身份或待上传结果。
- 自动检查与自动下载可分别关闭；关闭后仍可手动检查/下载。渠道切到 preview 前需明确验证范围。
- 空闲后更新只安排在安全时机安装，不会结束正在执行的采集或人工标注。
