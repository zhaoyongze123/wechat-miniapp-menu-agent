# v0.3.1 发布与 OTA 验收报告

日期：2026-11-25（Asia/Shanghai）
范围：A 机 DESKTOP-3UPCJQU（100.68.79.1）真实应用内 OTA，0.3.0 → 0.3.1。B 机未操作。

## 结论先行

- **v0.3.1 已发布并在 A 机通过真实 OTA 升级成功**，采集端当前运行 0.3.1（`/api/version` 实测），健康检查 `role=worker`。
- **原 218.5 秒的状态接口阻塞问题已实际解决**：升级后 `/api/updates/status` 实测 **0.75s（冷启动首次）/ 0.007s（热）**，稳定在 0.007–0.054s。
- **体积统计的同一份工作量已转入后台**：升级后约 4 分 45 秒完成 5 个版本目录扫描并落盘 `update-release-sizes.json`，期间接口始终快速返回（`sizes_pending=true` → `false`）。
- **升级过程中暴露并修复了一个独立的 0.3.0 锁反序死锁**（非磁盘、非 Defender）；该缺陷在 0.3.1 仍存在，已列为必须跟进的遗留项。

## 一、发布信息

| 项 | 值 |
| --- | --- |
| 提交 | `f75b969` fix: 版本目录体积改为缓存与后台统计，避免更新状态接口阻塞（PR #82） |
| 构建 | workflow_dispatch run 45567611215（Windows 安装包、Hub 镜像、OTA smoke、Release 草稿全绿） |
| Release | `v0.3.1`（非 draft、非 prerelease、latest） |
| 安装包 | `MenuCapture-Setup-0.3.1-win-x64.exe`，238,203,603 字节，sha256 `6da13663…bf8e` |
| 清单 | `min_supported_version=0.2.0`、`channel=stable`、资产 sha256 与 `.sha256` 一致 |

本轮未新增功能、未重复构建、未重新触发 CI。

## 二、OTA 时间线（A 机）

| 时间 | 事件 |
| --- | --- |
| 15:49:50–15:50:02 | 下载 0.3.1 安装包完成（238,203,603 字节） |
| 15:51:13–15:51:46 | 首次安装：产物校验通过，因“无有效代码签名”被拒（当时未放开未签名） |
| 15:56:22 | 设 `MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE=1` 后重启采集端（PID 2752，0.3.0） |
| 15:57:41 | `POST /api/updates/update` 授权（mode=immediate，日程 armed） |
| 15:57:41–16:36:26 | **卡死约 39 分钟**：已知死锁，journal 停在 15:51:46 failed，无安装子进程 |
| 16:28–16:33 | 只读取证：线程栈、I/O、句柄、Defender |
| 16:35–16:36:26 | 部署锁顺序热修（仅调整持锁范围，3 处），备份 `updates.py.bak-deadlock` |
| 16:36:26 / 16:36:32 | 结束死锁进程 2752；守护进程拉起新 0.3.0（PID 28952） |
| 16:38–16:39:53 | 0.3.0 持锁调用 `status_no_check()` 触发同步目录遍历（约 100s，即被修掉的原缺陷） |
| 16:39:53 / 16:39:55 | 写 `update-install-job.json`、journal=starting；安装子进程 29636 启动 |
| 16:40:11 | journal=installing，“安装包校验通过（签名状态：unsigned）” |
| 16:42:20 | journal=restarting，`current-version` 已是 0.3.1 |
| 16:43:52 | journal=completed“已切换到 0.3.1”；16:43:56 healthz ok、version 0.3.1 |
| 16:43:56–16:48:41 | 后台体积扫描 4m45s，写入 `update-release-sizes.json`（5 个版本） |
| 16:51:28 | `sizes_pending=false`，各版本体积 707 MB 级 |

## 三、卡死取证（用户要求五项）

| # | 问题 | 结论与证据 |
| --- | --- | --- |
| 1 | SHA-256 校验进程是否仍在读安装包 | **否**。`update-schedule` 线程停在 `artifact_provider()` 取锁，未进入 `_file_sha256`；Restart Manager 查该 exe 句柄持有者 `needed=0`（无任何进程打开，含 Defender） |
| 2 | 累计读取是否增长 | 有增长（411.9→414.4 MB/30s、再 +1.7 MB/20s，≈85 KB/s），但同段 CPU 增量 0.00s 且无安装包句柄 → 属进程内原生旁路 I/O，**不是安装包校验** |
| 3 | Defender 是否在扫描该包 | **无证据**。实时保护开启；`MsMpEng` 20s 读 0 KB；近 2 小时仅 1150/1151 健康事件，无扫描/查杀事件；安装包目录不在排除项内 |
| 4 | 是否存在线程锁阻塞 | **是，ABBA 死锁**。见下节 |
| 5 | 是否有超时与失败恢复 | 子进程内 900s/240s 超时 + 自动回滚；**父进程侧取锁、产物校验、空闲检查均无超时**；`recover()/_refresh()` 仅在启动或 journal 为活动态时收尾，`failed` 状态不会自愈 |

### 死锁链路

- `update-schedule`：持有 `InstallController._lock`（RLock）→ 调 `updates.download_status()` 等 `updates._lock`
- `shared-worker`：在 `UpdateService.update_flow()` 的 `with self._lock` 内调 `_install_snapshot()` → 等 `InstallController._lock`
- `MainThread`：`local_only` 中间件在**事件循环线程**同步调 `install.active()` → 同样等 `InstallController._lock`，导致所有 HTTP 接口不可用（curl 000/超时）
- 触发概率高：共享线程每 3 秒轮询一次并走 `update_flow`，而 `start()` 持锁期间要跑整包 SHA-256（数秒级）

## 四、处置（已获用户授权）

- 仅调整持锁顺序，不改任何校验逻辑：`authorize_update()`、`cancel_update()`、`update_flow()` 三处把执行器快照移到 `updates._lock` 之外。
- 未跳过 SHA-256（0.3.1 安装子进程仍重新校验 238 MB）、未关闭 Defender、未中断任何安装事务（当时不存在安装事务：journal=failed、无安装子进程）。
- 热修仅存在于 A 机 0.3.0 运行目录，**未进入仓库、未重新构建**；已随升级目录替换不再生效（备份保留在该目录 `updates.py.bak-deadlock`）。

## 五、修复效果实测

| 指标 | 升级前（0.3.0） | 升级后（0.3.1） |
| --- | --- | --- |
| `GET /api/updates/status` | 218.5s（另测 116s、156s） | 0.7547s（冷）/ 0.0072s / 0.0075s |
| 体积字段 | 同步遍历，阻塞请求 | 首次 `sizes_pending=true`，4m45s 后 `false`，各版本 707 MB 级 |
| 第三次复测（16:51） | — | 0.031s / 0.007s / 0.054s |

## 六、数据保留核对（三层证据，不可互相替代）

**结论：只能确认“规模未缩减、数据库文件未被替换”，不能据此声称数据内容完全一致。**

### 6.1 文件存在与大小检查（辅助证据）

| 项 | 基线（升级前） | 升级后 | 说明 |
| --- | --- | --- | --- |
| `...\MenuCapture\data` 文件数 | 4,690 | 4,696 | +6：体积缓存、安装 job、日志等 |
| `...\MenuCapture\data` 字节数 | 2,368,784,767 | 2,617,786,519 | +249 MB，主要是安装包 238,203,603 B |
| `tasks.sqlite3` 文件大小 | 72,372,224 | 72,372,224 | 大小一致，仅能排除“被重建为小库” |

限制：文件大小一致不代表内容一致；数据库大小相同既可能内容不变，也可能是等量增删。

### 6.2 数据库完整性检查（未执行）

计划在 A 机以只读方式 (`mode=ro`) 执行 `PRAGMA integrity_check`、`PRAGMA foreign_key_check` 并导出各业务表行数，实际执行被中止，**本轮未取得结果**。因此“数据库结构完整、无页损坏”属未验证项。

### 6.3 业务记录核对（未完成）

升级前未采集业务表基线（批次、门店任务、截图、上传队列、标注记录等行数与关键 ID），升级后也未逐条比对。因此**无法证明业务记录在升级前后完全一致**，只能说明安装器未重建数据库文件、服务启动后读写正常。

## 六之二、附加观察：工作台加载与公网入口延迟

验收后用户反馈“数据加载仍然慢”，实测定位如下（均为只读测量）：

| 链路 | 结果 |
| --- | --- |
| worker `GET /api/status?include_events=true` | 200，27,895 字节，0.061–0.087s |
| Caddy(127.0.0.1:8080，浏览器实际入口) 同路径 | 200，0.085s |
| 公网 `https://desktop-3upcjqu.tail00ac63.ts.net/api/status?...` | 200，TTFB 3.50s，总 3.94s |
| 同域首页 `/` | 200，1.63s |

结论：数据查询与序列化不慢，延迟主要来自 Tailscale Funnel 公网转发；升级前遗留的未完成请求会让 `refetchInterval` 轮询停在 loading，刷新页面即恢复。建议远程访问改用直连内网地址，Funnel 仅作备用。

## 七、遗留风险（必须跟进）

1. **0.3.1 仍含同一处锁反序**：`update_flow()`/`cancel_update()` 仍在 `updates._lock` 内取执行器快照。下次安装仍可能复现死锁，需在下个版本按根因修复并补并发单测。
2. **`local_only` 中间件在事件循环线程同步取锁**：任何安装锁竞争都会让整个 HTTP 服务不可用，应改为非阻塞判断或放到线程池。
3. **父进程侧无超时/看门狗**：取锁与产物校验需加超时与失败恢复，避免再次出现“页面全挂且不会自愈”。
4. **未签名放行依赖 `MENU_CAPTURE_ALLOW_UNSIGNED_UPDATE=1`**：属运维开关，发布流程做代码签名前不宜长期依赖。
5. **Hub 容器无法直连 GitHub API**（SSL UNEXPECTED_EOF）：Hub 分发元数据只能用 Worker 已缓存的检查结果，本次靠 Worker 回退本机 GitHub 才完成升级。

## 八、未验证项

- B 机（100.68.79.x）未做任何操作，未验证。
- 真实微信采集/OCR 全流程未在本轮执行；仅验证服务健康、接口性能与安装链路。
- Hub 侧 worker 在线状态未复核（本轮只做采集端验收）。
- 数据库完整性检查（`PRAGMA integrity_check` / `foreign_key_check`）未执行，见 6.2。
- 业务记录逐条核对未完成，见 6.3。
- 公网 Funnel 链路延迟只做了单次采样，未做持续压测。
- 本轮死锁修复仅以诊断性热修落在 A 机 0.3.0 运行目录，未入库、未构建、未随发布验证。

## 九、后续动作

- 本轮不发布 v0.3.2：A 机继续运行 0.3.1。
- 死锁根因作为独立缺陷跟踪并关联 Issue #32，在 `fix/ota-lock-order` 分支统一锁契约、修复事件循环阻塞与失败恢复，补并发回归测试；完成本地测试与 PR 后，随后续集中发布统一做真实 OTA 回归。
