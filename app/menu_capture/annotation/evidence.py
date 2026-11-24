"""人工标注样本的归档与清单格式。"""
from __future__ import annotations

import csv
import json
import re
import shutil
from datetime import datetime
from pathlib import Path

from .schema import CATEGORIES


MANIFEST_FIELDS = (
    "file", "category", "layout", "sub_type", "store_code", "page_state", "configured_people",
    "expected_target", "expected_x", "expected_y", "after_click", "notes",
    # 建议链路与人工最终位置的对比：用于判断错在本地规则、模型还是坐标换算。
    "suggestion_source", "suggestion_label", "suggestion_method",
    "adopted", "deviation_px", "attribution",
)


def _safe_store_code(task: dict) -> str:
    name = Path(str(task.get("source_name") or task.get("id") or "store")).stem
    cleaned = re.sub(r"[^\w\u4e00-\u9fff.-]+", "_", name).strip(" ._-")
    return (cleaned or str(task.get("id", "store")))[:48]


def archive_samples(sample_root: Path, task: dict, settings, steps: list[dict],
                    session_id: str) -> list[dict]:
    """把本次门店每次确认点击的前后截图归档到规则样本目录，并追加 manifest。"""
    root = Path(sample_root).expanduser().resolve()
    store_steps = [step for step in steps if step.get("task_id") == task.get("id")
                   and step.get("category") in CATEGORIES and step.get("before") and step.get("after")]
    if not store_steps:
        return []
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / "manifest.csv"
    if manifest.exists() and manifest.stat().st_size > 0:
        with manifest.open("r", encoding="utf-8", newline="") as stream:
            reader = csv.DictReader(stream)
            header = list(reader.fieldnames or [])
            existing = list(reader)
        if header != list(MANIFEST_FIELDS):
            # 列结构升级时原地补齐缺失列，只维护一份线上数据，不留历史副本。
            with manifest.open("w", encoding="utf-8", newline="") as stream:
                writer = csv.DictWriter(stream, fieldnames=MANIFEST_FIELDS, extrasaction="ignore")
                writer.writeheader()
                for row in existing:
                    writer.writerow({key: row.get(key, "") for key in MANIFEST_FIELDS})
    write_header = not manifest.exists() or manifest.stat().st_size == 0
    records: list[dict] = []
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    store_code = _safe_store_code(task)
    with manifest.open("a", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        if write_header:
            writer.writerow(MANIFEST_FIELDS)
        for step in store_steps:
            category = step["category"]
            category_dir = root / category
            category_dir.mkdir(parents=True, exist_ok=True)
            base = f"{store_code}-{stamp}-{int(step.get('index', 0)):02d}"
            before_name = base + "-before.png"
            after_name = base + "-after.png"
            shutil.copy2(step["before"]["path"], category_dir / before_name)
            shutil.copy2(step["after"]["path"], category_dir / after_name)
            relative_before = f"{category}/{before_name}"
            relative_after = f"{category}/{after_name}"
            notes = json.dumps({
                "session": session_id,
                "box_px": step.get("box_px"),
                "effect": step.get("effect"),
                "hard_negative": bool(step.get("hard_negative")),
                "note": step.get("note", ""),
                "ocr_text": step.get("matched_text", ""),
                "suggestion": step.get("suggestion"),
                "attribution": step.get("attribution"),
            }, ensure_ascii=False, separators=(",", ":"))
            points = step.get("center_points") or {}
            suggestion = step.get("suggestion") or {}
            attribution = step.get("attribution") or {}
            writer.writerow([
                relative_before, category, step.get("layout", ""),
                "hard_negative" if step.get("hard_negative") else "positive",
                store_code, step.get("page_state", "unknown"), settings.people_count,
                step.get("target", ""), points.get("x", ""), points.get("y", ""),
                relative_after, notes,
                suggestion.get("source", ""), suggestion.get("target_label", ""),
                suggestion.get("method", ""), attribution.get("adopted", ""),
                attribution.get("deviation_px", ""), attribution.get("attribution", ""),
            ])
            records.append({"category": category, "before": relative_before, "after": relative_after})
    return records
