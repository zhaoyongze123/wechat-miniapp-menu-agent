"""从现有清洗决策与采集任务中汇总仍有效的不合格记录。"""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from openpyxl import Workbook
from openpyxl.cell import WriteOnlyCell
from openpyxl.styles import Alignment, Font, PatternFill

from .materials import REJECTION_REASONS, MaterialService
from .store import TaskStore, now


class RejectionService:
    """只提供统一视图和导出，不创建另一套工单或审核状态。"""

    def __init__(self, tasks: TaskStore, materials: MaterialService):
        self.tasks = tasks
        self.materials = materials

    def _all(self) -> list[dict]:
        rows = self.materials.rejections()
        for task in self.tasks.rejections():
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
                "marked_at": task.get("rejected_at", ""),
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
        )
        self.tasks.event("人工标注标记为不合格", "warning", task_id)
        return self.tasks.get(task_id)

    def undo_task(self, task_id: str) -> dict:
        task = self.tasks.get(task_id)
        if not task.get("reject_reason"):
            raise ValueError("该任务当前没有不合格标记")
        self.tasks.update(task_id, reject_reason="", reject_detail="", rejected_at="")
        return self.tasks.get(task_id)

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
        sheet.auto_filter.ref = f"A1:J{max(1, len(records) + 1)}"
        headers = ["门店", "来源", "批次", "来源文件", "处理阶段", "不合格原因", "备注", "任务状态", "标记时间", "任务编号"]
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
            ]
            cells = []
            for value in values:
                cell = WriteOnlyCell(sheet, value=str(value or ""))
                cell.data_type = "s"
                cells.append(cell)
            sheet.append(cells)
        for column, width in zip("ABCDEFGHIJ", (34, 14, 22, 28, 18, 24, 42, 18, 24, 22)):
            sheet.column_dimensions[column].width = width
        output = BytesIO()
        workbook.save(output)
        return output.getvalue()
