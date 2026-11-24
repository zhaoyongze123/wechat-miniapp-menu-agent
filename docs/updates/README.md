# 软件更新与版本生命周期（Issue #32）

本目录记录应用内更新的发布契约、安全边界与当前实施阶段。更新发现、清单校验、偏好、审计、受控下载已实现；Windows 安装、切换与回滚执行器已通过隔离环境验收，前端按服务端能力显示安装入口（默认未启用自动安装，见下节）。Hub 镜像更新执行器已实现并通过隔离 Compose 验收（含真实 v0.2.2 跨版本回滚）；历史版本/下载缓存/日志清理与 Hub 镜像保留策略已实现（先预览、确认后删除）；多设备状态汇总与版本元数据分发已实现，批量推广仍待实施。

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
- 跨版本回滚验收：CI 以 **v0.2.2 正式发布镜像**为基线（直接 `docker load` 已发布产物，不重新打包），验证「生产 0.2.2 → 新版 → 回滚 0.2.2」全流程及旧版本能在新版本迁移过的数据库上启动；结构指纹拦截仍只在 Windows 执行器实现，Hub 侧依赖上述恢复兜底与真实跨版本验收。

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
- 当前仅集中展示与分发元数据，安装仍由各设备在本地确认（见 Windows 安装执行器）；批量安装/分批
  推广尚未实现。

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
- 设置中心“软件更新”：当前/最新版本、检查时间、渠道、自动检查、发布说明、资产 SHA-256、版本清点与更新记录。
- 受控下载：资产匹配、磁盘预检、断点续传、进度展示、取消、SHA-256 校验与下载审计。
- Windows 安装执行器：安装前重校验、路径与签名校验、空闲闸门、独立进程静默安装（显式 `/DIR`）、目标版本文件/清单/前端哈希与角色核对、事务异常恢复、迁移感知回滚；需显式启用。
- Windows OTA 隔离验收已覆盖应用内安装及回滚。前端安装入口须由服务端显式启用。 [隔离验收记录](https://github.com/zhaoyongze123/wechat-miniapp-menu-agent/actions/runs/45438329640)
- 存储治理：`GET/POST /api/updates/cleanup` 提供只读预览与确认后删除；保护当前/运行中/最近两个
  版本与待安装下载包，安装/下载期间拒绝清理；设置中心提供存储治理面板。
- Hub 镜像/归档保留脚本 `deploy/hub-update/hub_retention.py`：默认 dry-run，确认后按版本保留策略
  删除旧标签与归档，不触碰数据卷与容器。
- 多设备集中更新状态：Worker 上报版本/更新状态，Hub 汇总展示并分发新鲜版本元数据，Worker 二次
  校验后采纳，异常自动回退本机 GitHub 检查。
- 检查、偏好变更、下载结果与清理操作写入 `data/update-history.jsonl`。

尚未实现（不得在 UI 中伪造）：

- 发布流水线尚未配置代码签名证书，正式包当前为未签名；执行器已强制校验签名状态，未签名需显式放行。
- Hub 保留策略尚未接入定时任务或管理界面，当前由运维在宿主机手动执行。
- 多设备批量安装与分批推广；当前只做状态汇总与元数据分发，安装仍需各设备本地确认。

## 运维建议

- 采集期间不执行升级；等待当前任务结束或安全暂停后再进行。
- 升级前备份配置、数据库与必要发布记录；保留当前及最近两个可回滚版本。
- 不删除正式 GitHub Releases/Tags、业务数据、设备身份或待上传结果。
- 自动检查可在设置中关闭；关闭后仍可手动检查。渠道切到 preview 前需明确验证范围。
