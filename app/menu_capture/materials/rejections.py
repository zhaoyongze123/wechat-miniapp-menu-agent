"""从不合格决策与采集任务汇总统一的不合格清单，并处理对应审核动作。

边界：不新增审核状态；事实仍分别保存在 material_decisions 与 tasks.reject_* 字段中。
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from fastapi import HTTPException
from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill
from menu_capture.materials.materials import REJECTION_REASONS, MaterialService
from menu_capture.projects.service import resolve_task_project, resolve_task_projects
from menu_capture.storage.store import TaskStore, now
from menu_capture.capture.capture_results import STORE_STATE_LABELS, capture_outcome, store_state


class RejectionService:
    """提供不合格统一视图、导出和已有业务规则对应的审核处置。"""

    def __init__(self, tasks: TaskStore, materials: MaterialService, *, current_only: bool = False,
                 observed_names=None, task_project_provider=None,
                 observed_names_batch=None, task_project_ids_provider=None):
        self.tasks = tasks
        self.materials = materials
        self.current_only = current_only
        # Hub 已同步的名称落在 shared_results，Worker/单机落在本地表；由装配层注入同一查询入口。
        self.observed_names = observed_names
        self.observed_names_batch = observed_names_batch
        self.task_project_provider = task_project_provider
        self.task_project_ids_provider = task_project_ids_provider

    def _observed_names_map(self, task_ids, project_id: str | None = None) -> dict[str, str]:
        """批量读取任务对应的实际观察门店名称，避免大库导出/预览逐条查库。"""
        unique = [str(tid) for tid in dict.fromkeys(str(t or "") for t in task_ids) if tid]
        if not unique:
            return {}
        effective_project = None if (self.materials is not None and self.materials.role == "standalone") else project_id
        try:
            if self.observed_names_batch is not None:
                return {str(k): str(v or "") for k, v in (self.observed_names_batch(unique, effective_project) or {}).items()}
            if self.observed_names is not None:
                return {tid: self._observed_name(tid, project_id) for tid in unique}
            return self.tasks.observed_store_names_current(unique, project_id=effective_project)
        except Exception:  # noqa: BLE001 - 附带信息异常不得阻断清单与导出
            return {}

    def _task_project_ids(self, task_ids) -> dict[str, str | None]:
        """批量解析任务所属项目，避免全量导出/预览产生 N+1 查询。"""
        unique = [str(tid) for tid in dict.fromkeys(str(t or "") for t in task_ids) if tid]
        if not unique:
            return {}
        if self.task_project_ids_provider is not None:
            return self.task_project_ids_provider(unique)
        if self.task_project_provider is not None:
            return {tid: self.task_project_provider(tid) for tid in unique}
        with self.tasks.connect() as db:
            resolved = resolve_task_projects(db, unique)
        return {tid: (owner["id"] if owner else None) for tid, owner in resolved.items()}

    def _observed_name(self, task_id: str, project_id: str | None = None) -> str:
        """附带信息读取失败只返回空值，不影响不合格清单与导出主流程。

        Hub 走已同步结果归属；Worker/单机按任务当前采集轮次读取，绝不跨轮次回退到历史名称。
        """
        if self.materials is not None and self.materials.role == "standalone":
            # 独立模式只有 legacy 一个工作区；保留没有项目快照的旧轮次名称。
            project_id = None
        try:
            if self.observed_names is not None:
                if project_id is not None:
                    try:
                        return str(self.observed_names(task_id, project_id) or "")
                    except TypeError:
                        pass
                return str(self.observed_names(task_id) or "")
            return str(self.tasks.observed_store_name_current(str(task_id), project_id=project_id) or "")
        except Exception:  # noqa: BLE001 - 附带信息异常不得阻断清单与导出
            return ""

    def _task_project_id(self, task_id: str) -> str | None:
        if self.task_project_provider is not None:
            return self.task_project_provider(task_id)
        with self.tasks.connect() as db:
            project = resolve_task_project(db, task_id)
        return project["id"] if project else None

    def _all(self, project_id: str | None = None) -> list[dict]:
        rows = self.materials.rejections(project_id=project_id) if self.materials is not None else []
        current = ({task["id"] for task in self.materials.effective_capture_tasks(project_id=project_id)}
                   if self.current_only and self.materials is not None else None)
        batch_cache: dict[str, dict | None] = {}
        raw_rejections = [
            task for task in self.tasks.rejections()
            if not ((current is not None and task["id"] not in current) or capture_outcome(task) != "rejected")
        ]
        if project_id is not None and raw_rejections:
            proj_map = self._task_project_ids(task["id"] for task in raw_rejections)
            raw_rejections = [task for task in raw_rejections if proj_map.get(str(task["id"])) == project_id]
        names_map = self._observed_names_map((task["id"] for task in raw_rejections), project_id) if raw_rejections else {}
        for task in raw_rejections:
            code = task.get("reject_reason", "")
            source_path = Path(task["source_path"])
            batch_id = task.get("material_batch_id", "") or ""
            can_return, return_disabled_reason = False, "该任务未关联素材批次，无法打回素材清洗"
            if batch_id:
                if batch_id not in batch_cache:
                    try:
                        batch_cache[batch_id] = self.materials.get_batch(batch_id, project_id=project_id)
                    except ValueError:
                        batch_cache[batch_id] = None
                batch = batch_cache[batch_id]
                if batch is None:
                    return_disabled_reason = "原素材批次已不存在，无法打回素材清洗"
                else:
                    matches = [item for item in (batch.get("stores") or [])
                               if item.get("source_name") == task.get("source_name")]
                    can_return = len(matches) == 1
                    return_disabled_reason = "" if can_return else "原批次中无法唯一匹配该门店，无法安全打回"
            rows.append({
                "source_type": "capture",
                "source_label": "采集任务",
                "record_id": task["id"],
                "store_key": task["source_name"],
                "store_name": task["source_name"],
                "source_folder": source_path.parent.name,
                "source_item": source_path.name,
                "reason_code": code,
                "reason_label": REJECTION_REASONS.get(code, "未记录原因"),
                "detail": task.get("reject_detail", ""),
                "evidence": task.get("reject_evidence", ""),
                "marked_at": task.get("rejected_at", ""),
                "reviewed_at": task.get("reject_reviewed_at", ""),
                "status": task["status"],
                "stage": task["stage"],
                "batch_id": batch_id,
                "batch_name": task.get("material_batch_name", "") or "",
                "task_id": task["id"],
                "run_id": task.get("run_id", ""),
                # 伴随提取的实际门店名称：原始编号仍是主键，名称只作为附带信息展示/导出。
                "observed_store_name": names_map.get(str(task["id"]), ""),
                "can_return_to_materials": can_return,
                "return_to_materials_disabled_reason": return_disabled_reason,
            })
        return self._merge_by_store(rows)

    @staticmethod
    def _merge_by_store(rows: list[dict]) -> list[dict]:
        """按门店合并素材与采集两类不合格记录，保证一家店只出现一次。

        采集记录带任务证据和处置入口，优先保留；素材记录的 store_id 补进来，
        后续仍可从同一条记录打回素材清洗。
        """
        merged: dict[tuple[str, str], dict] = {}
        for row in rows:
            key = (row.get("batch_id") or row.get("batch_name") or "",
                   row.get("source_folder") or row.get("store_key") or "")
            existing = merged.get(key)
            if existing is None:
                merged[key] = row
                continue
            preferred, other = (row, existing) if row["source_type"] == "capture" else (existing, row)
            if preferred["source_type"] == "capture" and not preferred.get("store_id") and other.get("store_id"):
                preferred = dict(preferred) | {"store_id": other["store_id"]}
            merged[key] = preferred
        return sorted(merged.values(), key=lambda row: (row.get("marked_at", ""), row["store_name"]), reverse=True)

    @staticmethod
    def _summary(rows: list[dict]) -> dict:
        # 已按门店合并，stores 与 records 都表示唯一门店数，素材/采集为来源归属。
        return {
            "stores": len(rows),
            "records": len(rows),
            "materials": sum(row["source_type"] == "materials" for row in rows),
            "capture": sum(row["source_type"] == "capture" for row in rows),
            "reviewed": sum(bool(row.get("reviewed_at")) for row in rows),
        }

    def list(self, source: str = "all", reason: str = "all", query: str = "", batch_id: str = "all",
             reviewed: str = "all", *, project_id: str | None = None) -> dict:
        if source not in {"all", "materials", "capture"}:
            raise ValueError("不合格来源筛选无效")
        if reason != "all" and reason not in REJECTION_REASONS:
            raise ValueError("不合格原因筛选无效")
        if reviewed not in {"all", "reviewed", "pending"}:
            raise ValueError("人工确认状态筛选无效")
        if not isinstance(batch_id, str) or len(batch_id) > 200:
            raise ValueError("不合格批次筛选无效")
        all_rows = self._all(project_id)
        normalized_query = query.strip().casefold()
        batch_options = {
            row["batch_id"]: row["batch_name"] or row["batch_id"]
            for row in all_rows if row.get("batch_id")
        }
        batches = [
            {"id": identifier, "name": name}
            for identifier, name in sorted(batch_options.items(), key=lambda item: (item[1].casefold(), item[0]))
        ]
        if any(not row.get("batch_id") and row["source_type"] == "capture" for row in all_rows):
            batches.append({"id": "__unbound__", "name": "未关联素材批次"})
        filtered = [
            row for row in all_rows
            if (source == "all" or row["source_type"] == source)
            and (reason == "all" or row["reason_code"] == reason)
            and (reviewed == "all" or (reviewed == "reviewed") == bool(row.get("reviewed_at")))
            and (batch_id == "all" or (batch_id == "__unbound__" and not row.get("batch_id"))
                 or (batch_id not in {"all", "__unbound__"} and row.get("batch_id") == batch_id))
            and (not normalized_query or normalized_query in " ".join(str(row.get(key, "")) for key in (
                "store_name", "source_folder", "source_item", "reason_label", "detail", "stage", "batch_name",
            )).casefold())
        ]
        return {"summary": self._summary(filtered), "records": filtered, "batches": batches}

    def mark_task(self, task_id: str, reason: str, detail: str = "") -> dict:
        self._validate_reason(reason, detail)
        task = self.tasks.get(task_id)
        if task["status"] not in {"needs_review", "error", "blocked", "captured"}:
            raise ValueError("只有已失败或已完成待复核的采集任务可以标记不合格")
        if task.get("reject_reason"):
            raise ValueError("该任务已经标记不合格")
        self.tasks.update(
            task_id,
            reject_reason=reason,
            reject_detail=detail.strip(),
            rejected_at=now(),
            reject_reviewed_at="",
        )
        return self.tasks.get(task_id)

    def mark_annotation_task(self, task_id: str, reason: str, detail: str = "") -> dict:
        """人工标注时发现二维码过期/无效、门店不存在等情况，允许直接标记不合格。

        自动采集只处理 pending 任务，因此这里同时把任务转为待检查，避免被再次自动采集。
        """
        self._validate_reason(reason, detail)
        task = self.tasks.get(task_id)
        if task.get("reject_reason"):
            raise ValueError("该任务已经标记不合格")
        self.tasks.update(
            task_id,
            status="needs_review",
            stage="annotation_rejected",
            message="人工标注时标记为不合格",
            reject_reason=reason,
            reject_detail=detail.strip(),
            rejected_at=now(),
            reject_reviewed_at="",
        )
        self.tasks.event("人工标注标记为不合格", "warning", task_id)
        return self.tasks.get(task_id)

    def undo_task(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if not task.get("reject_reason"):
            raise ValueError("该任务当前没有不合格标记")
        self.tasks.update(task_id, reject_reason="", reject_detail="", rejected_at="", reject_evidence="",
                          reject_reviewed_at="")
        return self.tasks.get(task_id)

    def mark_reviewed(self, source_type: str, *, task_id: str = "", batch_id: str = "",
                      store_id: str = "", reviewed: bool = True,
                      project_id: str | None = None) -> dict:
        """人工复核标记：只改复核结论，不改变任务状态或不合格原因。"""
        if source_type == "capture":
            task = self.tasks.get(task_id)
            if not task.get("reject_reason"):
                raise ValueError("该任务当前没有不合格记录")
            self.tasks.update(task_id, reject_reviewed_at=now() if reviewed else "")
            return {"ok": True, "reviewed_at": self.tasks.get(task_id)["reject_reviewed_at"]}
        if source_type == "materials":
            if not batch_id or not store_id:
                raise ValueError("素材不合格记录缺少批次或门店编号")
            store = self.materials.mark_rejection_reviewed(batch_id, store_id, reviewed, project_id=project_id)
            return {"ok": True, "reviewed_at": (store.get("rejection") or {}).get("reviewed_at", "")}
        raise ValueError("不合格来源无效")

    def material_return_target(self, task: dict, *, project_id: str | None = None) -> tuple[str, dict | None, str]:
        """只在当前任务唯一映射到原批次门店时开放打回入口。"""
        binding = self.tasks.task_material_binding(task["id"])
        batch_id = binding.get("batch_id", "")
        if not batch_id:
            return "", None, "该任务未关联素材批次，无法打回素材清洗"
        try:
            batch = self.materials.get_batch(batch_id, project_id=project_id)
        except ValueError:
            return "", None, "原素材批次已不存在，无法打回素材清洗"
        matches = [item for item in (batch.get("stores") or [])
                   if item.get("source_name") == task.get("source_name")]
        if len(matches) != 1:
            return batch_id, None, "原批次中无法唯一匹配该门店，无法安全打回"
        return batch_id, matches[0], ""

    def undo_many(self, items: list[dict], protect_task=None, *, project_id: str | None = None) -> dict:
        """逐项执行与单条入口一致的撤销规则，返回每项真实结果而不吞掉失败。"""
        if not isinstance(items, list) or not 1 <= len(items) <= 100:
            raise ValueError("请选择 1 到 100 条不合格记录")
        results, seen = [], set()
        for item in items:
            if not isinstance(item, dict) or item.get("source_type") not in {"capture", "materials"}:
                raise ValueError("批量撤销参数无效")
            source_type = item["source_type"]
            if source_type == "capture":
                if set(item) != {"source_type", "task_id"} or not isinstance(item["task_id"], str) or not item["task_id"].strip():
                    raise ValueError("采集任务撤销参数无效")
                key = (source_type, item["task_id"].strip())
            else:
                if (set(item) != {"source_type", "batch_id", "store_id"}
                        or any(not isinstance(item[field], str) or not item[field].strip()
                               for field in ("batch_id", "store_id"))):
                    raise ValueError("素材门店撤销参数无效")
                key = (source_type, item["batch_id"].strip(), item["store_id"].strip())
            if key in seen:
                raise ValueError("批量撤销列表不能包含重复记录")
            seen.add(key)

        for item in items:
            source_type = item["source_type"]
            identity = ({"source_type": source_type, "task_id": item["task_id"].strip()}
                        if source_type == "capture" else
                        {"source_type": source_type, "batch_id": item["batch_id"].strip(),
                         "store_id": item["store_id"].strip()})
            try:
                if source_type == "capture":
                    task_id = identity["task_id"]
                    task = self.tasks.get(task_id)
                    if protect_task:
                        protect_task(task_id)
                    task = self.undo_task(task_id)
                    store_name = task.get("source_name", "")
                else:
                    batch_id, store_id = identity["batch_id"], identity["store_id"]
                    store = self.materials.get_store(batch_id, store_id, project_id=project_id)
                    if store.get("decision") != "skipped":
                        raise ValueError("该门店当前没有不合格标记")
                    self.materials.decide_store(batch_id, store_id, "pending", project_id=project_id)
                    store_name = store.get("source_name", "")
                results.append({**identity, "ok": True, "store_name": store_name})
            except (ValueError, HTTPException) as exc:
                reason = getattr(exc, "detail", None) or str(exc)
                results.append({**identity, "ok": False, "store_name": "", "error": str(reason)})
        return {"ok": all(result["ok"] for result in results), "results": results}

    def return_blocker(self, task_id: str, current: bool | None = None, *,
                       project_id: str | None = None) -> str:
        """返回打回素材清洗的阻塞原因，空串表示可打回。

        待检查记录（capture_outcome=review）也允许打回：用户确认无需继续采集时，
        可直接退回重选二维码。是否属于当前有效任务由调用方传入 current 复用已有查询。
        """
        task = self.tasks.get(task_id)
        if not (task.get("reject_reason") or task.get("status") == "pending"
                or capture_outcome(task) == "review"):
            return "只有不合格、待处理或待检查任务可以打回素材清洗"
        _, store, reason = self.material_return_target(task, project_id=project_id)
        if store is None:
            return reason
        if current is None:
            current = not self.current_only or task_id in {
                row["id"] for row in self.materials.effective_capture_tasks(project_id=project_id)}
        if not current:
            return "这条采集记录已不是当前素材的有效任务，请刷新后重试"
        return ""

    def return_to_materials(self, task_id: str, *, project_id: str | None = None) -> dict:
        """把采集任务打回素材清洗：清不合格 + 决策回 pending + 任务置 blocked。

        不合格、待处理与待检查任务都允许打回：复核后确认无需继续采集时，退回重选
        二维码即可。能否打回统一由 return_blocker 判定，避免列表与操作口径不一致。
        不新建批次也不新建发布；重新确认二维码后按原批次增量发布即可生成新的采集任务。
        """
        task = self.tasks.get(task_id)
        blocker = self.return_blocker(task_id, project_id=project_id)
        if blocker:
            raise ValueError(blocker)
        batch_id, store, _ = self.material_return_target(task, project_id=project_id)
        self.materials.return_rework_store(batch_id, store["store_id"], project_id=project_id)
        self.tasks.update(
            task_id, status="blocked", stage="returned_to_materials",
            message="已打回素材清洗；请重新确认二维码后增量发布并重跑",
            reject_reason="", reject_detail="", rejected_at="", reject_evidence="", reject_reviewed_at="", error_code="",
        )
        self.tasks.event("已打回素材清洗：等待重新确认二维码", "warning", task_id)
        return {"ok": True, "task": self.tasks.get(task_id), "batch_id": batch_id,
                "batch_name": self.tasks.material_batch_name(batch_id), "store_id": store["store_id"]}

    @staticmethod
    def _validate_reason(reason: str, detail: str) -> None:
        if not isinstance(reason, str) or reason not in REJECTION_REASONS:
            raise ValueError("请选择有效的不合格原因")
        if not isinstance(detail, str) or len(detail.strip()) > 200:
            raise ValueError("补充说明最多 200 字")
        if reason == "other" and not detail.strip():
            raise ValueError("请选择其他时请填写具体原因")

    @staticmethod
    def _task_reason(task: dict) -> str:
        """提取任务关联的判定/导航原因，辅助人工从模型描述中核对门店名称。"""
        message = str(task.get("message") or "").strip()
        detail = str(task.get("reject_detail") or "").strip()
        if message and detail and detail not in message:
            return f"{message}｜{detail}"
        return message or detail

    def export_rejection_preview(
        self, source: str = "all", reason: str = "all", query: str = "", batch_id: str = "all",
        reviewed: str = "all", *, project_id: str | None = None,
    ) -> dict:
        """返回与不合格门店 Excel 导出完全同源的在线预览数据。"""
        records = self.list(source, reason, query, batch_id, reviewed, project_id=project_id)["records"]
        columns = [
            {"key": "store_name", "label": "门店", "width": 34},
            {"key": "source_label", "label": "来源", "width": 14},
            {"key": "batch_name", "label": "批次", "width": 22},
            {"key": "source_item", "label": "来源文件", "width": 28},
            {"key": "stage", "label": "处理阶段", "width": 18},
            {"key": "reason_label", "label": "不合格原因", "width": 24},
            {"key": "detail", "label": "备注", "width": 42},
            {"key": "status", "label": "任务状态", "width": 18},
            {"key": "marked_at", "label": "标记时间", "width": 24},
            {"key": "task_id", "label": "任务编号", "width": 22},
            {"key": "reviewed", "label": "人工确认", "width": 14},
            {"key": "observed_store_name", "label": "实际观察门店名称", "width": 28},
        ]
        rows = [
            {
                "store_name": str(record.get("store_name") or ""),
                "source_label": str(record.get("source_label") or ""),
                "batch_name": str(record.get("batch_name") or ""),
                "source_item": str(record.get("source_item") or ""),
                "stage": str(record.get("stage") or ""),
                "reason_label": str(record.get("reason_label") or ""),
                "detail": str(record.get("detail") or ""),
                "status": str(record.get("status") or ""),
                "marked_at": str(record.get("marked_at") or ""),
                "task_id": str(record.get("task_id") or ""),
                "reviewed": "✓ 已确认" if record.get("reviewed_at") else "",
                "observed_store_name": str(record.get("observed_store_name") or ""),
            }
            for record in records
        ]
        reviewed_count = sum(1 for row in rows if row["reviewed"])
        return {
            "filename": "不合格门店清单.xlsx",
            "sheet_name": "不合格门店",
            "columns": columns,
            "rows": rows,
            "total": len(rows),
            "summary": {
                "total": len(rows),
                "reviewed": reviewed_count,
                "unreviewed": len(rows) - reviewed_count,
            },
        }

    def export_xlsx(self, source: str = "all", reason: str = "all", query: str = "", batch_id: str = "all",
                    reviewed: str = "all", *, project_id: str | None = None) -> bytes:
        preview = self.export_rejection_preview(source, reason, query, batch_id, reviewed, project_id=project_id)
        columns = preview["columns"]
        rows = preview["rows"]
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet(preview["sheet_name"])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:L{max(1, len(rows) + 1)}"
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="167A70")
        header_cells = []
        for col in columns:
            cell = WriteOnlyCell(sheet, value=col["label"])
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center")
            header_cells.append(cell)
        sheet.append(header_cells)
        # 所有外部文本都显式写成字符串，避免以 = 开头的备注被 Excel 当成公式。
        keys = [col["key"] for col in columns]
        for row in rows:
            cells = []
            for key in keys:
                cell = WriteOnlyCell(sheet, value=str(row.get(key) or ""))
                cell.data_type = "s"
                cells.append(cell)
            sheet.append(cells)
        for column_letter, col in zip("ABCDEFGHIJKL", columns):
            sheet.column_dimensions[column_letter].width = col["width"]
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()

    def store_name_rows(self, *, project_id: str | None = None) -> list[dict]:
        """全量门店名称清单：名称提取独立于采集结果，覆盖成功、待检查与不合格。

        以全部未归档任务为范围；原始任务编号与原始门店名称来自任务本身，
        实际观察名称按当前有效结果归属（Hub）或当前采集轮次（Worker/单机）批量读取，
        不跨轮次回退，避免历史名称掩盖本轮撤销或换版；同时附带任务/模型 reason 便于人工核对。
        """
        active = [task for task in self.tasks.tasks() if not task.get("archived_at")]
        if not active:
            return []
        if project_id is not None:
            proj_map = self._task_project_ids(task["id"] for task in active)
            active = [task for task in active if proj_map.get(str(task["id"])) == project_id]
        if not active:
            return []
        names_map = self._observed_names_map((task["id"] for task in active), project_id)
        rows: list[dict] = []
        for task in active:
            task_id = str(task["id"])
            rows.append({
                "task_id": task_id,
                "store_name": str(task.get("source_name") or ""),
                "observed_store_name": names_map.get(task_id, ""),
                "state": STORE_STATE_LABELS.get(store_state(task), ""),
                "reason": self._task_reason(task),
            })
        return rows

    def export_store_names_preview(self, *, project_id: str | None = None) -> dict:
        """返回与全量门店名称 Excel 导出完全同源的在线预览数据。"""
        rows = self.store_name_rows(project_id=project_id)
        columns = [
            {"key": "task_id", "label": "原始任务编号", "width": 40},
            {"key": "store_name", "label": "原始门店名称", "width": 30},
            {"key": "observed_store_name", "label": "实际观察门店名称", "width": 30},
            {"key": "state", "label": "采集状态", "width": 18},
            {"key": "reason", "label": "原因（reason）", "width": 48},
        ]
        extracted = sum(1 for row in rows if str(row.get("observed_store_name") or "").strip())
        total = len(rows)
        states: dict[str, int] = {}
        for row in rows:
            label = str(row.get("state") or "未知")
            states[label] = states.get(label, 0) + 1
        return {
            "filename": "全量门店名称.xlsx",
            "sheet_name": "全量门店名称",
            "columns": columns,
            "rows": rows,
            "total": total,
            "summary": {
                "total": total,
                "extracted": extracted,
                "missing": total - extracted,
                "states": states,
            },
        }

    def export_store_names(self, *, project_id: str | None = None) -> bytes:
        """导出全量门店名称、采集状态与原因，供人工核对名称提取覆盖率。"""
        preview = self.export_store_names_preview(project_id=project_id)
        columns = preview["columns"]
        rows = preview["rows"]
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet(preview["sheet_name"])
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:E{max(1, len(rows) + 1)}"
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="167A70")
        header_cells = []
        for col in columns:
            cell = WriteOnlyCell(sheet, value=col["label"])
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center")
            header_cells.append(cell)
        sheet.append(header_cells)
        # 外部文本统一写成字符串，避免以 = 开头的门店名或 reason 被 Excel 当作公式执行。
        keys = [col["key"] for col in columns]
        for row in rows:
            cells = []
            for key in keys:
                cell = WriteOnlyCell(sheet, value=str(row.get(key) or ""))
                cell.data_type = "s"
                cells.append(cell)
            sheet.append(cells)
        for column_letter, col in zip("ABCDE", columns):
            sheet.column_dimensions[column_letter].width = col["width"]
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()
