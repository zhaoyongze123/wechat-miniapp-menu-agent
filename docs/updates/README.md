# 软件更新与版本生命周期（Issue #32）

本目录记录应用内更新的发布契约、安全边界与当前实施阶段。更新发现、清单校验、偏好、审计与受控下载已实现；安装、切换与回滚执行器尚未启用，前端不会显示不存在的操作。

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
- 完成后只上报文件路径与校验结果，不执行安装、切换或重启；这些能力仍属于后续阶段。

## 本地版本清点（Windows）

- 三类目录严格分离，不允许互相反推：
  - `data_root`：可写数据目录（配置、缓存、审计），Windows 通常由 `MENU_CAPTURE_HOME` 指定；
  - `app_root`：当前运行的程序目录，只读取其中的 `release-manifest.json`；
  - `install_root`：多版本安装根（含 `releases/`），仅由显式参数或 `app_root` 的安装布局推导。
- 安装布局：`<install>/releases/<version>/app/...`，`<install>/current-version` 指定启动版本。
- 默认保护：运行版本、`current-version` 指定版本、最近两个兼容版本；保护规则优先于数量上限。
- 只做只读清点与清理候选预览；不删除目录、不修改 `current-version`。符号链接/重解析点跳过。
- 非 Windows 或缺少 `releases` 目录时明确显示“不支持”，不伪造可释放空间。数据目录下即使存在 `releases` 也不会被当作安装根。
- Hub 容器不使用 Docker Socket；镜像清点与切换必须由宿主机受限执行器完成，不属于当前阶段。

## 当前阶段与后续

已实现：

- 固定仓库正式 Release 发现、stable/preview 过滤、语义版本比较、禁用降级。
- `update-manifest.json` 校验、资产完整性信息展示、1 小时缓存、失败退避与持久化状态。
- 单飞检查与在途结果失效控制；本机 CPU 架构与资产架构匹配校验。
- `data_root` / `app_root` / `install_root` 分离，Windows 版本清点不受数据目录影响。
- 设置中心“软件更新”：当前/最新版本、检查时间、渠道、自动检查、发布说明、资产 SHA-256、版本清点与更新记录。
- 受控下载：资产匹配、磁盘预检、断点续传、进度展示、取消、SHA-256 校验与下载审计；安装仍不启用。
- 检查、偏好变更与下载结果写入 `data/update-history.jsonl`。

尚未实现（不得在 UI 中伪造）：

- Hub 集中检查并向 Worker 分发版本元数据；当前 Hub 与 Worker 各自检查并缓存（设备量少时可接受，后续再收敛）。
- Windows 独立更新执行器、事务锁、安全空闲检查、安装后健康验证与自动回滚。
- Hub 宿主机受限更新执行器与镜像保留。
- 多设备集中更新与分批推广。
- 自动清理执行器与 dry-run 删除。

## 运维建议

- 采集期间不执行升级；等待当前任务结束或安全暂停后再进行。
- 升级前备份配置、数据库与必要发布记录；保留当前及最近两个可回滚版本。
- 不删除正式 GitHub Releases/Tags、业务数据、设备身份或待上传结果。
- 自动检查可在设置中关闭；关闭后仍可手动检查。渠道切到 preview 前需明确验证范围。
