"""从不合格决策与采集任务汇总统一的不合格清单，并导出 Excel。

边界：只做只读汇总、筛选与导出，不新增审核状态；事实仍分别保存在
material_decisions 与 tasks.reject_* 字段中。
"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill

from .materials import REJECTION_REASONS, MaterialService
from .store import TaskStore, now
from .capture_results import capture_outcome


class RejectionService:
    """只提供统一视图和导出，不创建另一套工单或审核状态。"""

    def __init__(self, tasks: TaskStore, materials: MaterialService, *, current_only: bool = False):
        self.tasks = tasks
        self.materials = materials
        self.current_only = current_only

    def _all(self) -> list[dict]:
        rows = self.materials.rejections()
        current = {task["id"] for task in self.materials.effective_capture_tasks()} if self.current_only else None
        for task in self.tasks.rejections():
            if (current is not None and task["id"] not in current) or capture_outcome(task) != "rejected":
                continue
            code = task.get("reject_reason", "")
            source_path = Path(task["source_path"])
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
                "batch_id": task.get("material_batch_id", "") or "",
                "batch_name": task.get("material_batch_name", "") or "",
                "task_id": task["id"],
                "run_id": task.get("run_id", ""),
            })
        return sorted(rows, key=lambda row: (row.get("marked_at", ""), row["store_name"]), reverse=True)

    @staticmethod
    def _summary(rows: list[dict]) -> dict:
        # 两个阶段没有稳定的跨阶段门店主键；直接按当前有效不合格记录统计，
        # 避免同名门店被误合并，也避免素材目录名与采集任务名口径不一致。
        return {
            "stores": len(rows),
            "records": len(rows),
            "materials": sum(row["source_type"] == "materials" for row in rows),
            "capture": sum(row["source_type"] == "capture" for row in rows),
            "reviewed": sum(bool(row.get("reviewed_at")) for row in rows),
        }

    def list(self, source: str = "all", reason: str = "all", query: str = "", batch_id: str = "all") -> dict:
        if source not in {"all", "materials", "capture"}:
            raise ValueError("不合格来源筛选无效")
        if reason != "all" and reason not in REJECTION_REASONS:
            raise ValueError("不合格原因筛选无效")
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

    def return_to_materials(self, task_id: str) -> dict:
        """把采集不合格打回素材清洗：清不合格 + 决策回 pending + 任务置 blocked。

        不新建批次也不新建发布；重新确认二维码后按原批次增量发布即可生成新的采集任务。
        """
        task = self.tasks.get(task_id)
        if not task.get("reject_reason"):
            raise ValueError("该任务当前没有不合格标记")
        binding = self.tasks.task_material_binding(task_id)
        batch_id = binding.get("batch_id", "")
        if not batch_id:
            raise ValueError("该任务未关联素材批次，无法打回素材清洗")
        batch = self.materials.get_batch(batch_id)
        stores = batch.get("stores") or []
        store = next((item for item in stores if item.get("source_name") == task["source_name"]), None)
        if store is None:
            # 兜底：发布快照中的父目录名与素材目录名一致，可按目录后缀匹配。
            folder = Path(task["source_path"]).parent.name
            store = next((item for item in stores
                          if str(item.get("source_folder") or "").endswith(folder)), None)
        if store is None:
            raise ValueError("未在素材批次中找到对应门店，无法打回素材清洗")
        self.materials.decide_store(batch_id, store["store_id"], "pending")
        self.tasks.update(
            task_id, status="blocked", stage="returned_to_materials",
            message="已打回素材清洗；请重新确认二维码后增量发布并重跑",
            reject_reason="", reject_detail="", rejected_at="", reject_evidence="", reject_reviewed_at="", error_code="",
        )
        self.tasks.event("已打回素材清洗：等待重新确认二维码", "warning", task_id)
        return {"ok": True, "task": self.tasks.get(task_id), "batch_id": batch_id,
                "batch_name": binding.get("batch_name", ""), "store_id": store["store_id"]}

    @staticmethod
    def _validate_reason(reason: str, detail: str) -> None:
        if not isinstance(reason, str) or reason not in REJECTION_REASONS:
            raise ValueError("请选择有效的不合格原因")
        if not isinstance(detail, str) or len(detail.strip()) > 200:
            raise ValueError("补充说明最多 200 字")
        if reason == "other" and not detail.strip():
            raise ValueError("请选择其他时请填写具体原因")

    def export_xlsx(self, source: str = "all", reason: str = "all", query: str = "", batch_id: str = "all") -> bytes:
        records = self.list(source, reason, query, batch_id)["records"]
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
