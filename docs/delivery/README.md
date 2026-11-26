# 合格成品目录

Hub 将当前有效的合格门店截图投影到宿主机可见的 Bind Mount。项目业务状态是唯一事实源，目录和索引都是可重建的派生视图。Issue #73 的逐店整轮替换、待检查计入合格、PNG 文件筛选及 SHA-256 校验语义继续适用；Issue #102 增加项目隔离。

## 项目目录布局

配置的 `MENU_CAPTURE_DELIVERY_MOUNT` 是项目目录的父根：

```text
<bind mount root>/
└── 项目/
    ├── <project.code>/
    │   └── 合格/
    │       └── <stable store directory>/page-0001.png
    └── ...
```

项目身份由权威 `project_id` 确定，路径使用不可变且经校验的 `project.code`；门店目录名只负责展示，同名门店通过稳定身份分配独立目录。旧版扁平 `<root>/合格/<门店>/` 已一次性并入历史项目：成品经 SHA-256 核验后迁入 `项目/legacy/合格/`，服务不再写入或重建该结构，也不维护两套交付目录。无项目身份且无法核验归属的索引条目仍只保留待核验，不按名称猜测。

## 收录和同步

- 当前候选来自 Hub `effective_capture_tasks()` 的权威批次/任务，并解析其可信项目身份。缺少项目 ID、code 或项目查询失败的候选不能投影，归入待核验或失败状态。
- 资格仍按 `capture_results.store_state(task) == "captured"` 判定，待检查也计入合格；仅收录当前有效轮次直接结果中的 `page-*.png`，不从旧批次/门店目录拼图。
- 门店身份沿用批次 ID + store ID；缺少 store ID 时只在该批次内使用门店名。每家门店整轮 staging 后替换；发布前复核当前任务，轮次变化则放弃并重排。逐字节复制并校验 SHA-256，不按文件大小复用旧图。
- 失败和重试、索引、暂存、回收及内容校验都按项目/门店记录隔离。一个项目的变化不应改写另一项目目录。未知历史归属不会被名称相似性自动纳入。
- 同步服务仍为一个全局服务，周期 reconcile 所有项目；可选 `project_id` 只用于请求触发/响应状态范围，不会把后台任务缩成该项目。归档项目允许只读状态、列表、预览/打开及导出；同步修复仅作为派生视图操作，不能触发业务写入或改动旧投影。

## 部署与接口

挂载必须可写且位于配置的 Bind Mount 内。未配置时不写文件；挂载不可用时暂停写入。暂存/回收路径必须位于挂载根，不能落入只读容器文件系统。`MENU_CAPTURE_DELIVERY_MOUNT` 是挂载根（宿主机 `D:/MenuCapture`），`MENU_CAPTURE_DELIVERY_ROOT` 指向容器内同一挂载（`/var/lib/menu-delivery`），`MENU_CAPTURE_DELIVERY_HOST_PATH` 用于界面复制宿主路径（`D:/MenuCapture`）；服务再按 `项目/<code>/合格` 拼出项目交付目录。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/delivery/status?project_id=` | 项目级状态、统计、摘要与打开能力 |
| GET | `/api/delivery/stores?project_id=&page=&size=&q=&batch_id=` | 当前项目内的合格门店分页与批次选项 |
| POST | `/api/delivery/sync?project_id=` | 触发全局 reconcile，响应可按项目范围返回 |
| POST | `/api/delivery/reveal?project_id=` | 打开当前项目的宿主机交付目录；不支持时返回冲突 |

仅 Hub 装配交付服务；Worker/standalone 不提供该业务接口。容器模式无法直接打开宿主机资源管理器时，前端应禁用打开并提供复制路径。

## 页面和验证边界

质量审核的「合格清单」以当前项目显示统计、目录路径和门店列表。上述代码契约与定向单测不代表真实 Hub Bind Mount、Windows 资源管理器或生产业务验收通过。历史扁平目录并入 `项目/legacy/合格/` 时按权威索引逐店核验文件清单与 SHA-256，核验不一致的目录不移动、不删除。
