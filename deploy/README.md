# 双机部署和验收

A 机运行共享后台。A、B 都安装 Windows 采集端，用各自已登录的微信操作。后台当前使用一个持久化卷。采集端的缓存、待上传结果和本机模型设置位于 `%LOCALAPPDATA%\MenuCapture`。

## 安装

1. 在 A 机安装并启动 Docker Desktop。在仓库根目录把 `deploy/.env.example` 复制为 `deploy/.env`，设置管理员口令，并把 A 机的 Tailscale IPv4 填到 `MENU_CAPTURE_ALLOWED_HOSTS`。管理员账号是 `admin`。执行 `docker compose --env-file deploy/.env -f deploy/compose.yaml up -d --build --wait`，然后执行 `tailscale serve --bg --tcp=8766 tcp://127.0.0.1:8766`。同一 Tailscale 内网的浏览器和采集端使用 `http://<A 机 Tailscale IPv4>:8766`，机器之间的连接由 Tailscale 加密。
2. 在两台 Windows 上运行 Release 的 `MenuCapture-Setup-<版本>-win-x64.exe`，用开始菜单的「菜单采集工作台」打开本机端。新机器保留 `8765` 给本机采集端。安装包配置当前 Windows 用户登录后启动。把微信登录并打开文件传输助手，进「环境」查看截图、OCR 和微信检查结果。
3. 在共享工作台打开「采集电脑」，生成配对码。每台采集端输入共享工作台地址、电脑名称和自己的一次性配对码。需要全自动时，在各电脑本机设置 GUI-Plus 模型和密钥。配对后在共享工作台勾选门店，分给一台电脑。采集端显示「可离线采集」才表示二维码已校验并保存在本机。

服务端环境变量与容器端口写在 `compose.yaml` 和 `Dockerfile.hub`。单机开发可继续运行 `python -m menu_capture.server`。若已在旧计划任务中使用 `8765`，先借备用端口演练共享后台，再在获准切换时停旧进程并调整旧任务及反向代理。

## 快速验收

- 在没有 Python、Node、OCR 和 UiPath 的 Windows x64 上安装。退出后重新登录，确认本机工作台启动。用真实小程序完成全自动、半自动和独立标注各一次。升级后确认本机配置和待上传结果还在。
- A、B 分别配对并获领不同门店，同时开始采集。暂时断开 B 到 A 的链路，在 B 连续采集两家并重启采集端。恢复链路后确认截图和状态都已补齐。云模型不可达时验证全自动暂停，并能由操作员切到半自动。
- 让一台电脑离线，在 A 收回它的一家店并另行分配。旧结果补传后，应能在分配记录打开历史附件。再用两个原批次验证打回、重选、统一发布和重跑。同一门店仍被占用时，发布会提示等待同步或先收回。

## 升级与回滚

首次升级到 React 的验证需要旧版安装包。在 GitHub Actions 手动运行 Windows workflow 时，可通过 `previous_installer_tag` 选择旧 Release，或通过 `previous_installer_run` 选择已通过的旧 CI 产物。验收依次安装旧版、升级 React、再回滚到旧版。验收生产现用安装包时，应使用其原始安装文件和 SHA-256。旧包在没有生产采集服务和启动项的独立 Windows 环境执行，避免安装器覆盖同一账号的启动项。

前后端随发布版本一起打包：React 构建产物在 `web/react/`，运行时不依赖 Node、npm 或外部 CDN。安装包与镜像都会写入版本信息，可用 `GET /api/version` 核对实际运行的后端版本与前端入口哈希。

Windows 采集端按版本目录并存（`releases\<版本>`），`current-version` 决定守护进程启动哪个版本；数据、配置、设备身份和待上传结果都在 `%LOCALAPPDATA%\MenuCapture`，升级与回滚都不触碰。升级：运行新版安装包。回滚：执行安装目录下的 `rollback.ps1 -Version <旧版本>`，守护进程会在当前采集或标注结束后切回旧版本；脚本校验目标版本文件完整，并通过健康检查与 Worker 进程路径等待切换完成，兼容没有 `/api/version` 与 React 清单的迁移前版本。新版版本目录内的 `release-manifest.json` 记录版本、源码提交与前端入口哈希，可用于发布核对。

共享后台按镜像标签发布与回滚，数据卷保持不变：

```bash
MENU_CAPTURE_VERSION=1.2.3 MENU_CAPTURE_IMAGE=menu-capture-hub:1.2.3 docker compose -f deploy/compose.yaml up -d --build
# 回滚到上一镜像
MENU_CAPTURE_IMAGE=menu-capture-hub:1.2.2 docker compose -f deploy/compose.yaml up -d
```

## 迁移旧 Windows 备份

先备份旧采集端的 `data`、截图、配置，以及独立存放的原素材和不合格归档。SQLite 使用备份 API 导出一致副本，或者在暂停旧工作台写入后复制完整数据目录。演练使用备份和新的空数据卷。

```powershell
python deploy/migrate_legacy.py --backup-home 'D:\backup\runner' --legacy-home 'D:\menu-agent\UFS_Menu_Scraper\macos_runner' --backup-output 'D:\backup\screenshots' --destination 'D:\backup\hub-ready'
```

旧配置缺少输出绝对路径时加 `--legacy-output <旧路径>`。转换时报缺少旧素材或不合格根目录映射时，对应每个目录加 `--extra <原路径> <该目录备份>`。脚本保留源备份，校验 SQLite、主要记录数、发布二维码和已采集截图，通过后才创建目标目录。检查 `migration-report.json`，把结果写进一个**空的演练卷**，并把卷内权限交给容器用户 10001。

在演练后台核对门店、批次、不合格原因和截图，再决定切换日期。正式切换先确认没有正在采集的门店或未完成的输出发布事务，停旧入口写入，做最终备份和导入，最后把内网地址切到新后台。首次正式写入前做健康检查并打开样本截图。旧计划任务、目录和备份保留供回退。

开发者可在独立 Windows 构建机执行 `deploy/windows/build.ps1`。安装、自动启动和崩溃恢复由 `deploy/windows/smoke.ps1` 在隔离构建机验收。GitHub workflow 仅手动触发，一次运行完成构建与验收，并把同一份产物上传为待审阅的 Release 草稿；PR、提交和标签推送不会自动构建。


## Windows 半自动远程控制

远程控制为可选功能，Worker 仍监听本机回环地址。正式开启前，在独立 Windows 验收机上配置可信内网 HTTPS 反向代理，保留请求 Host 并传递可信的 HTTPS 协议信息。Tailscale 环境可使用仅对 tailnet 开放的 [Tailscale Serve](https://tailscale.com/docs/reference/tailscale-cli/serve)。

在运行 Worker 的用户或服务环境设置：

- `MENU_CAPTURE_REMOTE_CONTROL_HOST` 为代理的 HTTPS 主机名，填写域名。
- `MENU_CAPTURE_REMOTE_CONTROL_PASSWORD` 为密码管理器生成的独立操作员密码，至少 16 字符。使用 `operator` 账号登入。

操作员在自己的浏览器访问该域名下的 `/remote-control`。此入口只提供当前半自动状态、开始截图和确认不合格。两种动作都校验设备、轮次、门店和本次等待标识。轮询中断期间保留最近状态并停止远程操作。浏览器支持时可在用户点击后打开 [文档画中画](https://developer.chrome.com/docs/web-platform/document-picture-in-picture)，其可用性应在操作员的实际浏览器验证。
