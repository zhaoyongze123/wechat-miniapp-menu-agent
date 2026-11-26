# #24 Phase 0 API 契约与 React 接入

基于 `e6b7ea8` 的 `menu_capture/server.py`、`shared_routes.py`、对应 service 与页面调用点。生产角色见 [角色矩阵](role-matrix.md)。当前 FastAPI 禁用 OpenAPI 文档页，以下为源码契约与应补测试。未来新增契约明确标为缺口。所有路径均为同源相对路径。

## 传输、鉴权与响应

| 层 | 已有契约 | 迁移约束 |
| --- | --- | --- |
| HTTP 本地服务 | 校验 Host，默认 `127.0.0.1` / `localhost`，Hub 另有配置允许列表。Hub UI、普通 API 与其资源使用 Basic `admin`。健康检查和机器协议路径例外。Worker 浏览器访问本机服务。 | React 角色从 `GET /api/shared/status?include_history=false` 确定，加载失败显式显示。角色控制由服务端继续执行，浏览器导航守卫用于提示。 |
| 浏览器写操作 | 除机器协议外，非 GET/HEAD 检查 `X-Capture-Token` 与 Origin。进程启动生成令牌。旧 HTML 注入，`GET /api/session` 返回 `{token}`。Hub Origin 可 http/https，Worker 仅 http。 | React 入口使用运行时 session，内存持有令牌，所有 POST/PUT/PATCH/DELETE 一致。空 body、JSON、原始二进制使用各自传输方法。仅服务端明确拒绝令牌且同源换得新值时补一次请求。超时、网络断开、业务 403/409 先读回事实。 |
| 机器协议 | 配对前使用一次性码，之后 Hub 机器路由使用 Bearer 对应设备。浏览器不接触机器 token。 | 机器协议保留旧版本可用性。禁止将机器鉴权移到前端或让浏览器直接轮询 Worker 服务。 |
| 错误 | `ValueError → 400 {detail}`，参数校验 `422 {detail}`，冲突 `409 {detail,conflicts?}`，设备/Hub 鉴权 `401`，Host/Origin/令牌 `403`，资源缺失 `404`，过大 `413`，ZIP 类型 `415`，不支持的平台操作可为 `501`。文件异常可能映射为通用 400。 | `ApiError` 保持 HTTP status、业务 detail、conflicts 和请求范围，区分缺少证据、无可见数据和服务失败。批量成功以业务结果数组为准，不能只看 2xx。React 不回显令牌、上传原文、客户端文件真实路径或服务堆栈。 |
| 读取、证据、缓存 | 中间件当前对通过的响应统一设置 `Cache-Control: no-store`、nosniff、Referrer-Policy 与 CSP。文件路由返回原生 Image/FileResponse 或 ZIP/XLSX。 | 查询适配用 `AbortSignal` 及响应归属键。附件独立路径编码、可取消、错误占位，关闭撤销对象 URL。React 哈希产物的缓存策略需明确绕过通用 no-store，仅对受控构建文件生效。API、HTML、会话和业务图片保留不缓存。 |

## 浏览器接口与请求响应映射

路径记号 `B=/api/materials/batches/{batch_id}`，`S=B/stores/{store_id}`，`T=/api/tasks/{task_id}`，`A=/api/annotation`。表内未列 body 的动作按当前实现发送空对象或无 body，行为按 handler 校验。分页索引从 1 开始。

| 页面 / 接口组 | 请求与约束 | 响应、失败与调用方 |
| --- | --- | --- |
| I01/I02/I03 角色与工作台 | `GET /api/shared/status?include_history=false`，`GET /api/status` 根据 Hub 队列传 `queue_page/queue_size/queue_scope/queue_batch/queue_q/queue_filter/queue_sort/selected/include_events`，`GET /api/capture/queue?batch_ids&q` 用于已发布候选/旧跨批次入口。 | shared status 区分 `workers/assignments` 与 `paired/online/credential_invalid/pending_uploads`。status 返回 `run/current_task/manual/tasks/counts/queue/material_release` 等，`queue` 带页码、总数、已选 ID 回查。React 用分角色 DTO。搜索和排序是否改变总览统计依当前 API 明示，目标 #23 如需同口径则改服务层。 |
| I03 正式采集 | `POST /api/capture/start {mode:"auto"|"manual",task_ids,name?}`，1–300 个非空任务 ID，名称最多 80。暂停 `POST /api/stop`、恢复 `/api/resume`、终止 `/api/terminate`。恢复可用 `{mode:"manual"}` 受限于 Windows 已暂停全自动。 | 开始受配对、当前活动轮次、下载、素材版本、后端窗口/模型检查保护。返回启动结果与 `status`，刷新该轮、队列、历史。收到开始成功仍需展示后续真实状态。半自动的 `GET /api/manual/state` 和 POST `/api/manual/{capture,reject,skip,reason}`，判退与原因操作使用 `{reason,detail?}` 或 `{reason}`，单动作忙碌受服务端状态约束。 |
| I04 设备 | Hub `POST /api/shared/pairing`，`PATCH /api/shared/workers/{id} {name,description}`，`POST …/{revoke,archive}`。Worker `POST /api/shared/connect {url,code,name}`。 | 配对码和到期时间，设备 ID、名称、备注、上报、配对与撤销状态。码一次性约 10 分钟。解除配对要求无活动分配，移除要求先解除配对，授权错误保留已存历史。 |
| I02/I04 分配记录 | `GET /api/shared/assignments?worker_id&phase&q&page&size`，phase 为 assigned/ready/completed/reclaimed/history 或空，size 1–100；`POST /api/shared/assignments {worker_id,task_ids}`；`POST …/{assignment_id}/reclaim` 或 `POST …/reclaim {assignment_ids}`。 | 返回 `records,total,page,pages` 或分配 `{assignments}`。单次上限 300。同批次同店已占用、素材返工、版本过期、不可运行、源文件问题返回 409 及冲突明细。当前分配整组提交，批量回收校验全部仍活动。回收返回可重分配任务。使用 `GET /api/shared/results/{assignment_id}/files` 及其文件子路由浏览结果。 |
| I05/I06 任务中心 | `GET /api/tasks/active`、`GET /api/tasks/history?kind&status&q&page&page_size`（每页 5–100，默认 20），`GET /api/tasks/history/{kind}/{item_id}`，`PATCH /api/tasks/history/capture/{run_id} {name}`（trim 后 1–64）。 | active 统一作业与运行摘要，history `records,summary,page,page_size,page_count`，采集详情有逐店结果，素材详情有事件。名称按原 run ID 保持关联。旧结果缺失显示未记录。基线 history 搜索服务端按类型最多先取 500 条再筛，不支持按设备全局筛选。 |
| I07 不合格 | `GET /api/rejections` 和 `/api/rejections/export.xlsx` 接收 `source,reason,q,batch_id`。确认人工复核 `POST /api/rejections/review {source_type,task_id?,batch_id?,store_id?,reviewed}`，区分素材/采集 ID。 | 列表 `records,summary` 等，导出 XLSX；人工确认只改复核事实。当前人工确认筛选发生在页面侧，导出没有等效参数。#22 用独立 API 变更使承诺范围一致。 |
| I07/I08 质量处置 | `POST T/reject {reason,detail?}`、`T/rejection/undo`、`T/retry`、`T/mark-review`；`POST /api/rejections/tasks/{task_id}/return-to-materials` 或 `/api/rejections/materials/{batch_id}/{store_id}/undo`；`GET …/tasks/{task_id}/evidence`。 | 返回新任务/决策或明确异常。Hub 对活动分配和不明批次保留保护。当前无通用的批量撤销不合格单接口。若 #3 延续多选，按已确认 ID 串行调用既有单项 API，汇总成功/失败后刷新，服务端事务型批量另审。 |
| I08 待检查与批次审核 | `GET /api/reviews/pending?batch_ids&q`，`batch_ids=all` 或逗号串。已有 `POST /api/reviews/pending/rerun {task_ids,name?}`（1–300、去重要求、名称上限 80，两个支持角色的浏览器均被此路由限制），Hub 使用共享分配。 `POST /api/reviews/approve {batch_ids}`（1–100）、`B/approve`、`B/approve/undo`、`B/stage {stage}`。 | 待检查 `records,batches,summary`，含 `workflow,bucket,bucket_label`，当前原因与页码在页面侧过滤。批次审核按明确批次 ID，Hub 会校验无占用。Windows 补充重采入口遵循本机队列资格与 `/api/capture/start`。确认窗口只使用明确选定的批次。 |
| I09 文件、历史与诊断 | `GET T/source`、`T/last-frame`、`T/actions?before`、`/api/action-evidence/{name}`、`/api/artifacts/{task_id}`、`/api/artifact/{task_id}/{relative_path}`、`/api/source/{task_id}`。`GET /api/debug/summary`、`/api/debug/runs/{run_id}/export`，`POST /api/debug/clear`。 | 原始二维码、失败帧、菜单页和操作帧分开。动作按游标读取，ZIP 和附件各自下载。部分已过期、未保存或未同步，需要单独语义。来源路径/输出绝对路径仅作诊断展示。 |
| I10/I11 批次及上传 | `GET /api/materials/batches?view=full|options`、`GET /api/materials/batch-management?deleted`、`GET B`。本地导入 `POST /api/materials/batches {input_dir,name,description?}`；浏览器 `POST /api/materials/uploads {name,description?}` → `PUT /api/materials/uploads/{batch_id}/file?path=...` 原始 bytes → `POST …/{finish,abort}`。 | 每张最大 50 MiB。保留目录内相对层级与失败/取消恢复。Hub 通过浏览器上传，后端路径由管理员按现有部署条件使用；系统目录选择在 Hub 被阻止。 |
| I10 单店资料、选码和补图 | `GET S`、`S/images/{image_index}`、`…/candidates/{candidate_index}`、`S/selected`。 `POST S/decision {decision,image_index?,candidate_index?,box?,reject_reason?,reject_detail?}`，skip 要求原因。补图 `POST S/uploads` → `PUT …/{upload_id}/file?filename` → `POST …/{upload_id}/commit {mode,files}` 或 `DELETE …/{upload_id}`；`POST S/reanalyze`。 | 原图、候选、当前选中返回图像，尚未选码的 selected 返回 404。确认/手动框/跳过及补图回读门店、批次与审核数据。提交捕获原图和门店身份，保留失败时编辑状态。 |
| I11 分析任务 | `POST B/analyze`、`B/reanalyze {store_ids}`（最多 2000）、`B/retry-failed`；`GET /api/materials/jobs?active&batch_id&limit`（1–100）、`GET /api/materials/jobs/{job_id}?events`、`POST …/{pause,resume,cancel}`。 | 队列、活跃状态、进度、失败和事件以服务端为准。点击暂停以状态回读确认边界。已开始任务离开页面仍在后台运行。 |
| I12 发布、管理与返工 | `PATCH B {name,description?}`，`DELETE B`、`POST B/restore`、`DELETE B/permanent`，`POST /api/materials/selection/clear {batch_id?}`。 `POST B/accept-recommendations`、`B/publish {name,version,description?}`、`B/activate {version}`。 `GET /api/materials/rework?batch_ids&q`、`POST …/return {batch_id,store_id}`、`POST …/publish {store_ids:["batch_id:store_id"],description?}`。 | 发布及当前版本含 `snapshot_id`、`release`、重复发布标记。跨批次返回逐原批次 `results/errors`，可能部分成功。删除与物理清除各有约束，选择清除的作用域和语义单独确认。#20 前明确显示批次快照版本。 |
| I13 标注 | `GET A/state`、`A/history?task_id`、`A/frame?session&name`。 `POST A/start {task_ids}`、`A/click {box,category?,layout?,page_state?,target?,hard_negative?,note?}`、`A/step {step_index?,category?,layout?,page_state?,target?,hard_negative?,note?}`。还有 POST `pause/resume/decide/complete/skip/reject/retry/refresh/stop`，reject 带原因和说明。 | 会话状态、当前店、截图坐标元数据、建议、历史样本和受控执行。开始受自动采集/设备分配保护。帧与坐标属于当前会话，禁止自动重发点击。 |
| I14/I15 设置和诊断 | `GET/POST /api/config`，`GET/POST /api/robot/profile`，`GET /api/environment`、`POST …/{check,test,settings}`，test `{id}`，settings `{pane}`。 `GET /api/connections`、`POST …/connect {force}`、`POST /api/test-model`、`/api/select-directory {kind}`、`/api/reveal {path}`。 | 保存后读取公开配置，空密码沿用既有语义，测试使用服务端已保存配置。区分主动检查与读取，默认读取不得拉起微信或模型。Windows 原生显示 Win32 诊断。保留必要兼容字段，旧系统设置权限入口在退役项中审查。 |
| I15 用量 | `GET /api/model-usage?date_from&date_to&group_by&source&run_id&task_id&provider&model&page&page_size`。 | 本地计量查询，不触发模型。处理分组结果、请求量、缺失计费、关联任务过滤。 |

## 机器协议与经审查的缺口

- `POST /api/shared/worker/pair {code,name}` 返回设备 ID、名称和 Bearer。 `POST …/sync {ready_ids,pending_uploads,run_summary?}` 接受最多 1000 个已下载 ID，响应当前分配、快照映射、门店状态、机器名与能力标记。可选摘要为 `status,mode,processed,total`，Hub 存上报时间。Worker 先处理本地待补传，再心跳取最新任务。
- `GET …/assignments/{assignment_id}/source` 与 `PUT …/result` 按设备和分配身份校验。上传是 `application/zip` 原始 bytes，当前总共最多 1 GiB、单产物和来源文件 32 MiB。相同 assignment ID 加相同摘要幂等，异内容冲突。响应 `{disposition:"accepted"|"history",status}`，过期或回收后的有效旧结果进历史。
- **#21** 现有 `run_summary` 是每台设备最新快照，无稳定本机 run ID 及完整逐店结果。先另审增量协议与后端按 `worker_id + run_id` 索引、游标/分页、结果成熟度、回补幂等及旧 Worker 兼容。建议 Worker 作为轮次名称权威，Hub 先只读并显示上报时效。任何 Hub 改名写入需另定冲突规则。
- **#20** 现有公开字段中的 `version/revision/snapshot_id` 表示批次发布层，`source_hash` 用于文件内容校验和下载缓存。店级有效版本、像素等同和分配资格由后端增量契约输出，React 以可选能力识别逐步接入。
- **旧接口** `/api/start`、`/api/scan`、`/api/ui-tars/trial` 和 `/api/doctor` 在基线仍注册，其中所述独立执行写入口受角色限制。按真实支持角色验证后退场。当前唯一浏览器生产采集入口按 `/api/capture/start` 设计。旧 API 删改另审依赖、脚本与历史读取。

## 路由逐条索引

下表从基线两个路由模块的 FastAPI 装饰器静态读取。它列出公开注册声明，具体角色、身份和条件以中间件及 handler 为准。

| 声明位置 | 方法与路径 | Handler |
| --- | --- | --- |
| `server.py:320` | `GET /` | `index` |
| `server.py:325` | `GET /assets/{name}` | `material_assets` |
| `server.py:333` | `GET /vue/{name}` | `vue_assets` |
| `server.py:342` | `GET /api/model-usage` | `model_usage_report` |
| `server.py:352` | `GET /api/config` | `configuration` |
| `server.py:356` | `POST /api/select-directory` | `select_directory` |
| `server.py:403` | `GET /api/session` | `local_session` |
| `server.py:408` | `POST /api/config` | `configure` |
| `server.py:445` | `GET /api/status` | `status` |
| `server.py:572` | `GET /api/capture/queue` | `capture_queue` |
| `server.py:600` | `GET /api/materials/batches` | `material_batches` |
| `server.py:604` | `GET /api/materials/batch-management` | `manage_material_batches` |
| `server.py:608` | `POST /api/materials/batches` | `import_local_materials` |
| `server.py:619` | `POST /api/materials/uploads` | `begin_material_upload` |
| `server.py:627` | `PATCH /api/materials/batches/{batch_id}` | `edit_material_batch` |
| `server.py:634` | `DELETE /api/materials/batches/{batch_id}` | `delete_material_batch` |
| `server.py:644` | `POST /api/materials/batches/{batch_id}/restore` | `restore_material_batch` |
| `server.py:649` | `DELETE /api/materials/batches/{batch_id}/permanent` | `purge_material_batch` |
| `server.py:659` | `POST /api/materials/selection/clear` | `clear_material_selection` |
| `server.py:672` | `PUT /api/materials/uploads/{batch_id}/file` | `upload_material_file` |
| `server.py:681` | `POST /api/materials/uploads/{batch_id}/finish` | `finish_material_upload` |
| `server.py:685` | `POST /api/materials/uploads/{batch_id}/abort` | `abort_material_upload` |
| `server.py:690` | `POST /api/materials/batches/{batch_id}/analyze` | `analyze_materials` |
| `server.py:697` | `POST /api/materials/batches/{batch_id}/reanalyze` | `reanalyze_materials` |
| `server.py:709` | `POST /api/materials/batches/{batch_id}/retry-failed` | `retry_failed_materials` |
| `server.py:719` | `GET /api/materials/jobs` | `material_jobs` |
| `server.py:725` | `GET /api/materials/jobs/{job_id}` | `material_job` |
| `server.py:732` | `POST /api/materials/jobs/{job_id}/pause` | `pause_material_job` |
| `server.py:739` | `POST /api/materials/jobs/{job_id}/resume` | `resume_material_job` |
| `server.py:746` | `POST /api/materials/jobs/{job_id}/cancel` | `cancel_material_job` |
| `server.py:753` | `GET /api/tasks/active` | `active_tasks` |
| `server.py:877` | `GET /api/tasks/history` | `task_history` |
| `server.py:941` | `GET /api/tasks/history/{kind}/{item_id}` | `task_history_detail` |
| `server.py:959` | `PATCH /api/tasks/history/capture/{run_id}` | `rename_capture_run` |
| `server.py:974` | `GET /api/materials/batches/{batch_id}` | `material_batch` |
| `server.py:978` | `GET /api/materials/batches/{batch_id}/stores/{store_id}` | `material_store` |
| `server.py:982` | `POST /api/materials/batches/{batch_id}/stores/{store_id}/uploads` | `begin_material_store_upload` |
| `server.py:988` | `PUT /api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}/file` | `upload_material_store_file` |
| `server.py:999` | `POST /api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}/commit` | `commit_material_store_upload` |
| `server.py:1007` | `DELETE /api/materials/batches/{batch_id}/stores/{store_id}/uploads/{upload_id}` | `abort_material_store_upload` |
| `server.py:1014` | `POST /api/materials/batches/{batch_id}/stores/{store_id}/reanalyze` | `reanalyze_material_store` |
| `server.py:1020` | `GET /api/materials/batches/{batch_id}/stores/{store_id}/images/{image_index}` | `material_image` |
| `server.py:1025` | `GET /api/materials/batches/{batch_id}/stores/{store_id}/images/{image_index}/candidates/{candidate_index}` | `material_candidate` |
| `server.py:1030` | `GET /api/materials/batches/{batch_id}/stores/{store_id}/selected` | `material_selected_candidate` |
| `server.py:1042` | `POST /api/materials/batches/{batch_id}/stores/{store_id}/decision` | `decide_material_store` |
| `server.py:1063` | `POST /api/materials/batches/{batch_id}/accept-recommendations` | `accept_material_recommendations` |
| `server.py:1067` | `POST /api/materials/batches/{batch_id}/publish` | `publish_materials` |
| `server.py:1084` | `GET /api/materials/rework` | `material_rework` |
| `server.py:1104` | `POST /api/materials/rework/return` | `return_material_rework` |
| `server.py:1113` | `POST /api/materials/rework/publish` | `publish_material_rework` |
| `server.py:1152` | `POST /api/materials/batches/{batch_id}/activate` | `activate_material_version` |
| `server.py:1163` | `GET /api/doctor` | `doctor` |
| `server.py:1167` | `GET /api/environment` | `environment_status` |
| `server.py:1171` | `GET /api/connections` | `connection_status` |
| `server.py:1175` | `POST /api/connections/connect` | `connection_start` |
| `server.py:1184` | `POST /api/environment/check` | `environment_check` |
| `server.py:1188` | `POST /api/environment/test` | `environment_test` |
| `server.py:1200` | `POST /api/environment/settings` | `environment_open_settings` |
| `server.py:1212` | `POST /api/test-model` | `test_model` |
| `server.py:1220` | `POST /api/scan` | `scan` |
| `server.py:1242` | `POST /api/start` | `start` |
| `server.py:1252` | `POST /api/resume` | `resume` |
| `server.py:1282` | `POST /api/ui-tars/trial` | `start_ui_tars_trial` |
| `server.py:1303` | `POST /api/capture/start` | `start_capture` |
| `server.py:1351` | `GET /api/manual/state` | `manual_state` |
| `server.py:1355` | `POST /api/manual/capture` | `manual_capture` |
| `server.py:1360` | `POST /api/manual/reject` | `manual_reject` |
| `server.py:1368` | `POST /api/manual/skip` | `manual_skip` |
| `server.py:1373` | `POST /api/manual/reason` | `manual_reason` |
| `server.py:1379` | `GET /api/robot/profile` | `robot_profile` |
| `server.py:1383` | `POST /api/robot/profile` | `save_robot_profile` |
| `server.py:1397` | `POST /api/stop` | `stop` |
| `server.py:1406` | `POST /api/terminate` | `terminate` |
| `server.py:1412` | `GET /api/tasks/{task_id}/source` | `task_source_image` |
| `server.py:1421` | `GET /api/tasks/{task_id}/last-frame` | `task_last_frame` |
| `server.py:1466` | `GET /api/tasks/{task_id}/actions` | `task_actions` |
| `server.py:1482` | `GET /api/debug/summary` | `debug_summary` |
| `server.py:1487` | `GET /api/debug/runs/{run_id}/export` | `export_debug_run` |
| `server.py:1504` | `POST /api/debug/clear` | `clear_debug_logs` |
| `server.py:1510` | `GET /api/rejections` | `list_rejections` |
| `server.py:1514` | `GET /api/rejections/export.xlsx` | `export_rejections` |
| `server.py:1524` | `POST /api/rejections/review` | `review_rejection` |
| `server.py:1538` | `POST /api/tasks/{task_id}/reject` | `reject_task` |
| `server.py:1548` | `POST /api/tasks/{task_id}/rejection/undo` | `undo_task_rejection` |
| `server.py:1556` | `POST /api/rejections/tasks/{task_id}/return-to-materials` | `return_rejection_to_materials` |
| `server.py:1565` | `GET /api/rejections/tasks/{task_id}/evidence` | `rejection_evidence` |
| `server.py:1581` | `GET /api/reviews/pending` | `pending_reviews` |
| `server.py:1585` | `POST /api/reviews/pending/rerun` | `rerun_pending_reviews` |
| `server.py:1605` | `POST /api/reviews/approve` | `approve_reviews` |
| `server.py:1622` | `POST /api/materials/batches/{batch_id}/stage` | `set_material_batch_stage` |
| `server.py:1627` | `POST /api/materials/batches/{batch_id}/approve` | `approve_material_batch` |
| `server.py:1634` | `POST /api/materials/batches/{batch_id}/approve/undo` | `unapprove_material_batch` |
| `server.py:1639` | `POST /api/rejections/materials/{batch_id}/{store_id}/undo` | `undo_material_rejection` |
| `server.py:1648` | `GET /api/action-evidence/{name}` | `action_evidence` |
| `server.py:1657` | `POST /api/tasks/{task_id}/retry` | `retry` |
| `server.py:1672` | `POST /api/tasks/{task_id}/mark-review` | `mark_task_review` |
| `server.py:1689` | `POST /api/reveal` | `reveal` |
| `server.py:1707` | `GET /api/artifacts/{task_id}` | `artifacts` |
| `server.py:1717` | `GET /api/source/{task_id}` | `source_image` |
| `server.py:1731` | `GET /api/artifact/{task_id}/{relative_path:path}` | `artifact` |
| `server.py:1744` | `GET /api/annotation/state` | `annotation_state` |
| `server.py:1748` | `GET /api/annotation/history` | `annotation_history` |
| `server.py:1755` | `POST /api/annotation/start` | `annotation_start` |
| `server.py:1769` | `POST /api/annotation/pause` | `annotation_pause` |
| `server.py:1776` | `POST /api/annotation/resume` | `annotation_resume` |
| `server.py:1783` | `POST /api/annotation/decide` | `annotation_decide` |
| `server.py:1790` | `POST /api/annotation/click` | `annotation_click` |
| `server.py:1802` | `POST /api/annotation/step` | `annotation_step` |
| `server.py:1814` | `POST /api/annotation/complete` | `annotation_complete` |
| `server.py:1821` | `POST /api/annotation/skip` | `annotation_skip` |
| `server.py:1831` | `POST /api/annotation/reject` | `annotation_reject` |
| `server.py:1842` | `POST /api/annotation/retry` | `annotation_retry` |
| `server.py:1849` | `POST /api/annotation/refresh` | `annotation_refresh` |
| `server.py:1856` | `POST /api/annotation/stop` | `annotation_stop` |
| `server.py:1863` | `GET /api/annotation/frame` | `annotation_frame` |
| `shared_routes.py:19` | `GET /healthz` | `health` |
| `shared_routes.py:23` | `GET /api/shared/status` | `shared_status` |
| `shared_routes.py:32` | `POST /api/shared/connect` | `connect` |
| `shared_routes.py:48` | `POST /api/shared/pairing` | `pairing` |
| `shared_routes.py:52` | `PATCH /api/shared/workers/{worker_id}` | `edit_worker` |
| `shared_routes.py:59` | `POST /api/shared/workers/{worker_id}/revoke` | `revoke_worker` |
| `shared_routes.py:64` | `POST /api/shared/workers/{worker_id}/archive` | `archive_worker` |
| `shared_routes.py:69` | `GET /api/shared/assignments` | `assignment_records` |
| `shared_routes.py:77` | `POST /api/shared/assignments/reclaim` | `reclaim_many` |
| `shared_routes.py:84` | `POST /api/shared/assignments` | `assign` |
| `shared_routes.py:91` | `POST /api/shared/assignments/{assignment_id}/reclaim` | `reclaim` |
| `shared_routes.py:104` | `GET /api/shared/results/{assignment_id}/files` | `result_files` |
| `shared_routes.py:110` | `GET /api/shared/results/{assignment_id}/files/{relative:path}` | `result_file` |
| `shared_routes.py:118` | `POST /api/shared/worker/pair` | `pair` |
| `shared_routes.py:124` | `POST /api/shared/worker/sync` | `sync` |
| `shared_routes.py:132` | `GET /api/shared/worker/assignments/{assignment_id}/source` | `source` |
| `shared_routes.py:136` | `PUT /api/shared/worker/assignments/{assignment_id}/result` | `receive` |

## Issue #102 项目范围与兼容协议

Hub 项目管理接口：`GET /api/projects[?include_archived=true]`、`GET /api/projects/{project_id}`、`POST /api/projects`、`PATCH /api/projects/{project_id}`、`POST /api/projects/{project_id}/archive`、`POST /api/projects/{project_id}/restore`。Worker 的列表和详情仅返回本机已有项目目录；独立端使用固定历史项目。

Hub 业务列表读取 `project_id` 查询参数；写请求以 payload 中的 `project_id` 或批次/任务 ID 解析可信归属，并拒绝项目与资源不一致、未知或归档项目的写操作。项目 ID 是范围键，名称不参与身份判定。设备、连接和活动运行摘要仍按实例级状态管理，任务自身携带项目 ID。项目 API 不能代替当前 HTTP 鉴权机制。

Worker 项目字段通过独立布尔能力 `supports_project_scope` 协商。双方均确认后，分配、批次目录、任务和轮次/历史摘要才携带项目字段；未协商或旧端连接时保持既有 wire shape，并限定到历史单项目兼容语义。接收端对已协商 DTO 严格拒绝未知字段和不一致归属，不凭版本号猜能力。离线 outbox 和旧分配按现有任务身份及 ACK 规则继续处理；未知历史归属不得映射到同名项目。

数据库前向迁移把既有批次固定归入历史项目、保留原业务 ID，并将原素材选择迁入项目范围选择表；不按批次名、门店名或文件路径跨域匹配。若旧 Hub 不具备新 schema/协议理解能力，回滚由兼容性保护拒绝；具体旧版本二进制回滚、升级中断恢复及真实数据迁移仍需单独集成验证。
