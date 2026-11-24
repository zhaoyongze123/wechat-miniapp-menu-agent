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
from menu_capture.storage.store import TaskStore, now
from menu_capture.capture.capture_results import capture_outcome


class RejectionService:
    """提供不合格统一视图、导出和已有业务规则对应的审核处置。"""

    def __init__(self, tasks: TaskStore, materials: MaterialService, *, current_only: bool = False):
        self.tasks = tasks
        self.materials = materials
        self.current_only = current_only

    def _all(self) -> list[dict]:
        rows = self.materials.rejections()
        current = {task["id"] for task in self.materials.effective_capture_tasks()} if self.current_only else None
        batch_cache: dict[str, dict | None] = {}
        for task in self.tasks.rejections():
            if (current is not None and task["id"] not in current) or capture_outcome(task) != "rejected":
                continue
            code = task.get("reject_reason", "")
            source_path = Path(task["source_path"])
            batch_id = task.get("material_batch_id", "") or ""
            can_return, return_disabled_reason = False, "该任务未关联素材批次，无法打回素材清洗"
            if batch_id:
                if batch_id not in batch_cache:
                    try:
                        batch_cache[batch_id] = self.materials.get_batch(batch_id)
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
             reviewed: str = "all") -> dict:
        if source not in {"all", "materials", "capture"}:
            raise ValueError("不合格来源筛选无效")
        if reason != "all" and reason not in REJECTION_REASONS:
            raise ValueError("不合格原因筛选无效")
        if reviewed not in {"all", "reviewed", "pending"}:
            raise ValueError("人工确认状态筛选无效")
        if not isinstance(batch_id, str) or len(batch_id) > 200:
            raise ValueError("不合格批次筛选无效")
        all_rows = self._all()
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
                      store_id: str = "", reviewed: bool = True) -> dict:
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
            store = self.materials.mark_rejection_reviewed(batch_id, store_id, reviewed)
            return {"ok": True, "reviewed_at": (store.get("rejection") or {}).get("reviewed_at", "")}
        raise ValueError("不合格来源无效")

    def material_return_target(self, task: dict) -> tuple[str, dict | None, str]:
        """只在当前任务唯一映射到原批次门店时开放打回入口。"""
        binding = self.tasks.task_material_binding(task["id"])
        batch_id = binding.get("batch_id", "")
        if not batch_id:
            return "", None, "该任务未关联素材批次，无法打回素材清洗"
        try:
            batch = self.materials.get_batch(batch_id)
        except ValueError:
            return "", None, "原素材批次已不存在，无法打回素材清洗"
        matches = [item for item in (batch.get("stores") or [])
                   if item.get("source_name") == task.get("source_name")]
        if len(matches) != 1:
            return batch_id, None, "原批次中无法唯一匹配该门店，无法安全打回"
        return batch_id, matches[0], ""

    def undo_many(self, items: list[dict], protect_task=None) -> dict:
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
                    store = self.materials.get_store(batch_id, store_id)
                    if store.get("decision") != "skipped":
                        raise ValueError("该门店当前没有不合格标记")
                    self.materials.decide_store(batch_id, store_id, "pending")
                    store_name = store.get("source_name", "")
                results.append({**identity, "ok": True, "store_name": store_name})
            except (ValueError, HTTPException) as exc:
                reason = getattr(exc, "detail", None) or str(exc)
                results.append({**identity, "ok": False, "store_name": "", "error": str(reason)})
        return {"ok": all(result["ok"] for result in results), "results": results}

    def return_to_materials(self, task_id: str) -> dict:
        """把采集任务打回素材清洗：清不合格 + 决策回 pending + 任务置 blocked。

        不合格任务与待处理任务都允许打回：前者是复核后的处置，后者用于把尚未采集的
        门店退回重选二维码；能否打回以素材批次和门店能否解析为准。
        不新建批次也不新建发布；重新确认二维码后按原批次增量发布即可生成新的采集任务。
        """
        task = self.tasks.get(task_id)
        if not task.get("reject_reason") and task.get("status") != "pending":
            raise ValueError("只有不合格或待处理任务可以打回素材清洗")
        batch_id, store, reason = self.material_return_target(task)
        if store is None:
            raise ValueError(reason)
        if self.current_only and task_id not in {row["id"] for row in self.materials.effective_capture_tasks()}:
            raise ValueError("这条采集记录已不是当前素材的有效任务，请刷新后重试")
        self.materials.return_rework_store(batch_id, store["store_id"])
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

    def export_xlsx(self, source: str = "all", reason: str = "all", query: str = "", batch_id: str = "all",
                    reviewed: str = "all") -> bytes:
        records = self.list(source, reason, query, batch_id, reviewed)["records"]
        workbook = Workbook(write_only=True)
        sheet = workbook.create_sheet("不合格门店")
        sheet.freeze_panes = "A2"
        sheet.auto_filter.ref = f"A1:K{max(1, len(records) + 1)}"
        headers = ["门店", "来源", "批次", "来源文件", "处理阶段", "不合格原因", "备注", "任务状态", "标记时间", "任务编号", "人工确认"]
        header_font = Font(bold=True, color="FFFFFF")
        header_fill = PatternFill("solid", fgColor="167A70")
        header_cells = []
        for value in headers:
            cell = WriteOnlyCell(sheet, value=value)
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = Alignment(vertical="center")
            header_cells.append(cell)
        sheet.append(header_cells)
        # 所有外部文本都显式写成字符串，避免以 = 开头的备注被 Excel 当成公式。
        for record in records:
            values = [
                record["store_name"], record["source_label"], record["batch_name"],
                record["source_item"], record["stage"], record["reason_label"],
                record["detail"], record["status"], record["marked_at"], record["task_id"],
                "✓ 已确认" if record.get("reviewed_at") else "",
            ]
            cells = []
            for value in values:
                cell = WriteOnlyCell(sheet, value=str(value or ""))
                cell.data_type = "s"
                cells.append(cell)
            sheet.append(cells)
        for column, width in zip("ABCDEFGHIJK", (34, 14, 22, 28, 18, 24, 42, 18, 24, 22, 14)):
            sheet.column_dimensions[column].width = width
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()
