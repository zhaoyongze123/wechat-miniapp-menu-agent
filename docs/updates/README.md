# 软件更新与版本生命周期（Issue #32）

本目录记录应用内更新的发布契约、安全边界与当前实施阶段。更新发现、清单校验、偏好、审计、受控下载已实现；Windows 安装、切换与回滚执行器已通过隔离环境验收，前端按服务端能力显示安装入口（默认未启用自动安装，见下节）。Hub 镜像更新执行器已实现并通过隔离 Compose 验收（含真实 v0.2.2 跨版本回滚）；历史版本/下载缓存/日志清理与 Hub 镜像保留策略已实现（先预览、确认后删除）；多设备状态汇总与版本元数据分发已实现，批量推广仍待实施。

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
- 缺少清单或校验失败时仍展示该 Release，但标记不可更新并说明原因，不静默回退到旧版本。

SHA-256 只证明完整性，发布者真实性依赖固定仓库的正式 Release 语义；后续如引入签名/发布证明，必须在此文档与实现中显式升级，不能把摘要当作唯一真实性依据。

### 可选增量补丁（`patches`）

发布清单可带可选顶层 `patches` 数组；旧客户端忽略该字段，只使用 `assets` 中的完整资产。

- 每条补丁字段：`from`、`name`、`size`、`sha256`、`url`、`platform`（固定 `windows`）、`arch`、`format`（固定 `zip-sha256-v1`），可选 `catalog`（同一 Release 内的签名目录资产）。
- 服务端逐条尽力校验：`from` 必须低于目标版本、格式/平台/架构匹配、URL 归属正确、名称/大小/SHA-256 与同一 Release 资产一致；任一不合法只丢弃该条，**不影响**完整安装包仍可更新。
- 补丁本体为 ZIP，内含 `patch-manifest.json` 与 `files/<路径>`；清单携带基线/目标的完整受管文件清单、版本、`from_commit`/`to_commit`、平台、架构与受限区域指纹。
- 安全开关：只有 `MENU_CAPTURE_ALLOW_INCREMENTAL_UPDATE=1` 时才考虑增量（默认关闭）。没有签名 `catalog` 时仍必须沿用既有 `MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE=1`，不新增任何放宽；有 `catalog` 时执行器用 `Test-FileCatalog` + Authenticode 双重校验，任一不通过即拒绝增量。
- 只允许应用层变化：运行环境（`python/`、`node/`、`tesseract/`、`vcruntime/`）、依赖锁（`requirements*.txt`、`package-lock.json` 等）、安装器与根级 `Launch.ps1`/`rollback.ps1`/`setup.iss` 指纹变化时，该版本不生成补丁，客户端也只走全量。
- 不引入补丁链：跨多个版本升级只有在存在 `from == 当前版本` 的补丁时才使用，否则直接全量。
- 生成工具：`deploy/windows/make_patch.py`（纯标准库）比较两棵完整构建树后输出 ZIP 与同名 `.json`；`build.ps1 -BundleOutput <dir>` 可另存同一次正式构建的 staging bundle，避免为生成补丁重复完整构建。条件不满足时输出 `status: skipped`，不产出补丁，也不阻断完整安装包发布。

## 受控下载

- 通过 `POST /api/updates/download` 启动，`POST /api/updates/download/cancel` 取消；进度随 `GET /api/updates/status` 的 `download` 字段返回。
- 只有已通过清单校验、且 `update_available` 为真的版本才允许下载；资产必须与本机平台、归一化架构一致，否则直接拒绝。
- 文件只写入 `data_root/data/updates/<version>/`：先写 `<asset>.part`，完整校验 SHA-256 后才原子替换为正式文件，绝不写入程序目录或安装目录。
- 断点续传使用 HTTP `Range` 与 `.part` 现有长度；服务器忽略 Range 返回 200 时从零重写，并重新按完整文件大小检查磁盘余量。
- 续传响应必须提供匹配的 `Content-Range`（起点、末尾、总长）与 `Content-Length`；记录 `ETag`/`Last-Modified` 并在续传时发送 `If-Range`。范围不一致时丢弃断点并自动重下一次完整文件，绝不把错误区段拼进产物。
- 下载与清单请求只允许重定向到受信任的 GitHub 域名（`github.com`、`objects.githubusercontent.com`、`release-assets.githubusercontent.com`、`api.github.com`），其他主机或 HTTP 一律拒绝。
- 下载前按剩余字节 + 64 MiB 余量做磁盘空间预检；响应体超过清单声明大小时立即中止。
- 同一时刻只允许一个下载任务；进程重启后把未完成的下载如实标记为“已中断”，不会假装仍在下载。
- 完成后只上报文件路径与校验结果，不执行安装、切换或重启；这些能力由下一节的独立执行器负责。

## Windows 安装执行器（需显式启用）

- 仅 Windows Worker、能定位安装根，且运维设置 `MENU_CAPTURE_ALLOW_SELF_UPDATE=1` 时，能力接口才把 `install`/`rollback` 上报为可用；未完成真实 Windows 升级/回滚验收前不默认开启。
- `POST /api/updates/install` 启动安装，必须提交与目标版本一致的 `confirm`；`GET /api/updates/install` 返回安装日志与当前状态。
- 控制器与执行器共用 `build_job()` 生成的安装任务契约（`schema`、`artifact_path`、`asset_name`、`sha256`、`size`、`download_dir`、`data_dir`、`install_root`、目标/上一版本、端口），字段只定义一次；契约不匹配直接拒绝，不再出现"写入 `path`、读取 `artifact_path`"这类跨模块断裂。
- 增量任务使用 schema 2，并在同一任务里携带补丁与同版本完整安装包身份（名称/大小/SHA-256/URL）；执行器校验补丁大小与 SHA-256、`from` 与当前版本一致、基线 `release-manifest.json` 版本与 commit、平台/架构，以及可选签名目录。
- 补丁应用先按基线清单复制未变化文件，再写入修改文件，最终受管文件集合与哈希必须与补丁目标清单完全一致；只写 `releases/.<版本>.patch-tmp`，完成后原子改名为目标目录，绝不原地修改当前版本。
- 回退（同一次用户授权内最多一次）：补丁下载失败、切换版本前的基线校验失败、补丁应用失败都会改用同版本完整安装包并重新走 Authenticode 校验；用户取消不触发兜底；切换后的健康失败仍按既有回滚规则处理。
- 失败或回滚后清理未被 `current-version` 指向的目标目录，避免 `setup.iss` 的 `onlyifdoesntexist` 让残缺目录挡住下一次完整安装。
- 安装前重新校验：版本语义化、平台/架构、资产名、大小、SHA-256、`download_dir/<version>/<asset>` 路径归属，且与当前发布清单的资产哈希/名称/大小一致；不信任历史 `completed` 状态。
- 路径安全：先校验路径片段、逐级拒绝符号链接/重解析点，再解析并确认仍在下载目录内；安装根同样拒绝链接、文件系统根与非 `releases` 目录。
- 可信发布证明：固定仓库正式 Release + 清单 SHA-256 + 安装包 Authenticode 签名状态。签名有效才通过；未签名需显式设置 `MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE=1`；签名无效一律拒绝并记录到安装日志。
- 采集或人工标注未结束时拒绝安装；采集/标注启动与安装启动共用同一把锁，闭合"空闲检查 → 写任务"之间的竞态窗口；安装进行中服务端拒绝新的采集/标注请求。
- 独立进程执行（守护进程会终止并重启采集端，工作线程无法观察重启后的健康状态）：静默运行安装器并显式传入 `/DIR=<安装根>` → 校验 `releases/<version>`、发布清单、前端入口哈希与 `current-version` → 轮询 `/api/version` 与 `/healthz`，要求版本、角色 `worker`、健康状态、前端入口和清单 `git_commit` 全部一致。
- 事务异常恢复：任务记录执行器 PID；服务启动或状态查询发现进程已退出时，按 `current-version` 与实际运行版本收尾为 `completed`/`failed`，不会长期停在 `installing`。
- 安装器失败且已改动 `current-version`、或健康检查超时，自动调用 `rollback.ps1 -Version <上一版本>` 回滚并核对上一版本；回滚前比对数据库结构指纹，若新版本已执行迁移则阻止自动回滚并提示人工确认，避免旧版本读不懂新结构。
- 单进程内串行化安装启动，同一时刻只允许一个安装任务。
- 设置中心按 `GET /api/updates/install` 的真实能力显示安装入口：下载完成且能力可用时提供"安装并重启"，确认窗口写明重启与回滚影响；能力未启用时只说明原因，不显示按钮。
- 隔离验收使用 `deploy/windows/ota-smoke.ps1`，覆盖安装 API、独立执行器、静默安装、版本切换和回滚。现有产物可用于本地验证。候选包需要复验时，在本轮唯一完整 CI 中使用适用的 OTA 基线参数。 [集成交付规则](../governance/engineering-standards.md#发布与云端构建)

## Hub 镜像更新执行器（宿主机，需显式部署）

- 执行器独立于被替换的业务容器运行：容器内不能挂载 Docker Socket，也不该自己替换自己。代码见 `deploy/hub-update/hub_updater.py`，仅用标准库。
- 入口：`GET /status`、`POST /apply`、`POST /rollback`；默认只监听 `127.0.0.1`，要求 `Authorization: Bearer <HUB_UPDATE_TOKEN>`，并按 `HUB_UPDATE_ALLOWED_CLIENTS` 网段二次校验来源。供容器访问时才把监听地址改为可达地址，并把来源收敛到 Docker 网桥网段。请求体格式非法返回 400，事务状态冲突（进行中、禁止降级、无回滚目标）返回 409，未授权返回 401。
- `POST /apply` 只接受 `{version, confirm, sha256, size, url|archive}`：版本必须语义化且高于当前运行版本，`confirm` 必须等于版本，归档大小不超过 2 GiB；`url` 只允许受信任 GitHub 域名且路径必须指向 Release 资产，`archive` 只能是归档目录内的文件名，禁止绝对路径与 `..`。
- 执行顺序：磁盘与数据卷检查 → 下载（或读取归档）→ 大小与 SHA-256 校验 → `docker load` 并确认加载出的是 `<镜像仓库>:<版本>` → 以 `MENU_CAPTURE_IMAGE=<镜像仓库>:<版本>` 执行固定的 `docker compose up -d --no-build hub` → 核对容器健康与 `/api/version` 真实版本。任一步失败都会尝试切回上一版本并核对健康。
- 只对配置的 compose 文件、project 和 service 执行 `up -d`，从不执行 `down -v`、`prune` 或删除卷；更新前要求数据卷已存在，避免因拼错卷名创建空卷。
- 事务状态写入 `HUB_UPDATE_STATE_DIR/hub-update.json`（含执行器 PID），执行器重启或状态查询发现进程已退出时，按实际运行镜像收尾，不会永久停在切换中；每次成功/失败/回滚追加到 `hub-update-audit.jsonl`。
- 必须配置的环境变量：`HUB_UPDATE_TOKEN`（≥16 字符）、`HUB_UPDATE_COMPOSE_FILE`、`HUB_UPDATE_PROJECT`、`HUB_UPDATE_DATA_VOLUME`、`HUB_UPDATE_STATE_DIR`；可选 `HUB_UPDATE_ARCHIVE_DIR`、`HUB_UPDATE_HEALTH_URL`、`HUB_UPDATE_HEALTH_USER`/`HUB_UPDATE_HEALTH_PASSWORD`（Hub 的 `/api/version` 走基本认证）、`HUB_UPDATE_BIND`、`HUB_UPDATE_PORT`、`HUB_UPDATE_ALLOWED_CLIENTS`、`HUB_UPDATE_HEALTH_TIMEOUT`。
- Hub 服务端通过 `HUB_UPDATE_URL` / `HUB_UPDATE_TOKEN` 代理调用执行器（`deploy/hub/compose.yaml` 已声明 `host.docker.internal:host-gateway`）；未配置时设置页只显示原因，不提供安装按钮。容器只提交「最新版本 + 清单校验后的 SHA-256/大小/URL」，由执行器再次校验。
- 部署时执行器必须监听容器可达的地址，并把 `HUB_UPDATE_ALLOWED_CLIENTS` 收敛到 Docker 网桥网段；切勿对公网开放。
- 回滚兜底：手动回滚或安装失败自动回滚时，若目标版本切回后健康检查不通过，执行器会立即切回回滚前仍在运行的版本并复核健康，避免把采集服务留在停机状态；恢复失败才标记失败并保留状态与审计。
- 回滚架构边界：切换前用当前应用镜像启动一次隔离只读探测容器，禁用网络和 Linux capabilities，只挂载命名数据卷且为只读，不读取宿主机 Docker volume Mountpoint。探测器保存 `data/tasks.sqlite3` 架构快照。自动回退与显式回滚都比较快照；旧表/列/索引/约束被删除或改写、触发器/视图改变、基线缺失或无法读取时停止回滚并标记 `needs_manual_recovery`，保留当前容器与诊断信息，要求人工恢复。仅新增表、带可供旧代码写入的默认值（或允许为空）的列，以及非唯一索引，按向后兼容扩展处理，允许旧镜像回滚；新增唯一索引、既有表约束或外键都需人工判断。这对应现有初始化迁移以 `CREATE TABLE`、`ALTER TABLE ADD COLUMN` 和 `CREATE INDEX` 为主的机制。无法读取架构时不推断兼容。
- 跨版本回滚验收：CI 以 **v0.2.2 正式发布镜像**为基线（直接 `docker load` 已发布产物，不重新打包），验证「生产 0.2.2 → 新版 → 回滚 0.2.2」全流程及旧版本能在新版本迁移过的数据库上启动；此证据适用于已验证的兼容性扩展，不代表任意破坏性迁移都可回滚。

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

1. **首次接入更新执行器（旧版 → 支持 OTA 的版本）**：旧版没有安装入口，仍需按传统方式部署一次；
   升级后确认 `release-manifest.json`、`current-version` 与 `/api/version` 一致，再启用
   `MENU_CAPTURE_ALLOW_SELF_UPDATE=1`。Hub 侧额外部署 `deploy/hub-update/hub_updater.py` 并配置
   `HUB_UPDATE_*` 变量，未配置时设置页只显示原因。
2. **日常升级**：Hub 在设置中心检查 → 下载 → 确认安装；Worker 由 Hub 分批推送目标版本后在本机
   确认安装。采集中/标注中会被安全闸门拒绝，等待任务结束或安全暂停后重试。
3. **升级前备份**：保留当前及最近两个可回滚版本、数据目录、设备凭据与待上传结果；不修改生产
   数据库结构。
4. **回滚**：Windows 使用版本管理中的回滚入口或 `deploy/windows/rollback.ps1 -Version <版本>`；
   若新版本已执行不可逆迁移，执行器按数据库结构指纹阻止自动回滚并提示人工确认。Hub 使用执行器
   `POST /rollback`，回滚目标不健康时会自动切回回滚前版本。
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
- Windows OTA 隔离验收已覆盖应用内安装及回滚。前端安装入口须由服务端显式启用。 [隔离验收记录](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/actions/runs/45438329640)
- Windows 文件级增量更新（Phase 1）：`deploy/windows/make_patch.py` 在正式构建 bundle 上生成补丁，发布清单可选 `patches`；执行器 schema 2 重建目标版本，失败在同一授权内回退一次完整安装包；默认关闭。
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
| 回滚 | 已实现 | Windows 版本目录回滚 + Hub 执行器回滚；数据库结构指纹不兼容时转人工恢复 |
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
- Windows 真实设备（隔离机）升级/回滚仍待维护者安排；自动化验收已由 `ota-smoke.ps1` 覆盖。本轮“一次授权”只完成单测与组件测试，真实浏览器 + 隔离环境的 OTA 全流程证据需在解冻后按 Issue #32 验收条件单独产出，不能以单测通过代替。
- 增量补丁默认关闭；发布流水线尚未配置签名目录（`catalog`）证书，生产启用前需要等价可信的签名载体，否则只能沿用既有 unsigned 放行口径。
- 补丁生成复用 `ota_base_version` 的源构建作为基线；跨正式 Release 的补丁需要保留上一版本的正式构建 bundle，当前未纳入发布资产，未匹配时以完整安装包兜底。
- 补丁链、二进制差分、文件块级分发与 P2P 下载不在范围；不新增第二套更新状态机。

## 运维建议

- 采集期间不执行升级；等待当前任务结束或安全暂停后再进行。
- 升级前备份配置、数据库与必要发布记录；保留当前及最近两个可回滚版本。
- 不删除正式 GitHub Releases/Tags、业务数据、设备身份或待上传结果。
- 自动检查与自动下载可分别关闭；关闭后仍可手动检查/下载。渠道切到 preview 前需明确验证范围。
- 空闲后更新只安排在安全时机安装，不会结束正在执行的采集或人工标注。
