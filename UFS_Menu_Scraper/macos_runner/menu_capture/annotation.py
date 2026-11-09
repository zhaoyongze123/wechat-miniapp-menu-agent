"""人工标注模式。

复用已有的微信扫码打开流程与 UiPath 截图/点击能力，但在小程序打开后：
不调用视觉模型，也不执行任何本地规则判定；系统只给出可修改的候选类别与框选，
点击必须由使用者确认，点击前后截图会作为规则样本归档。
"""
from __future__ import annotations

import csv
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from PIL import Image, ImageChops, ImageStat

from .config import WORKSPACE_ROOT, ConfigStore, atomic_json
from .model import VisionModel
from .model_usage import usage_context
from .scene_agent import SceneAgent, SceneError
from .uipath_runner import production_window_geometry

CATEGORIES = ("miniapp_home", "store_select", "people_grid", "consent_checkbox",
              "close_icon", "system_notification", "resting_menu", "other")
PAGE_STATES = ("miniapp_home", "store_select", "people_select", "consent", "ad_or_overlay",
               "system_permission", "menu", "loading", "unknown")
CATEGORY_PAGE_STATE = {
    "miniapp_home": "miniapp_home",
    "store_select": "store_select",
    "people_grid": "people_select",
    "consent_checkbox": "consent",
    "close_icon": "ad_or_overlay",
    "system_notification": "system_permission",
    "resting_menu": "menu",
    "other": "unknown",
}
MANIFEST_FIELDS = (
    "file", "category", "layout", "sub_type", "store_code", "page_state", "configured_people",
    "expected_target", "expected_x", "expected_y", "after_click", "notes",
    # 建议链路与人工最终位置的对比：用于判断错在本地规则、模型还是坐标换算。
    "suggestion_source", "suggestion_label", "suggestion_method",
    "adopted", "deviation_px", "attribution",
)
_MANIFEST_SCHEMA_VERSION = "3"
# 第二层：同一场景内的界面样式（例如主页中部大入口与底部标签栏都有“扫码点餐”，
# 只按 OCR 文字会点到无变化的那个）。变体必须从真实运行/标注截图归纳，当前留空：
# 只标场景，归纳后再把确认过的变体填进来，避免用推测词条污染标注数据。
LAYOUTS: dict[str, tuple[tuple[str, str], ...]] = {
    "miniapp_home": (),
    "store_select": (),
    "people_grid": (),
    "consent_checkbox": (),
    "close_icon": (),
    "system_notification": (),
    "resting_menu": (),
    "other": (),
}
LAYOUT_LABELS = {key: label for options in LAYOUTS.values() for key, label in options}
# 判定“人工采纳建议”的容差；超过说明建议位置落错了目标。
SUGGESTION_ADOPT_TOLERANCE_PX = 24.0

# 只用于给出“建议”，不参与任何自动点击决策。命中越多只代表越像该类别。
CATEGORY_KEYWORDS = {
    "close_icon": {"跳过": 6, "关闭": 5, "广告": 5, "×": 5, "领券": 4, "优惠券": 4, "暂不": 5, "我知道了": 4},
    "system_notification": {"通知": 6, "推送": 5, "定位": 4, "位置": 3, "蓝牙": 3, "无线数据": 3, "允许": 1},
    "people_grid": {"用餐人数": 6, "就餐人数": 6, "选择人数": 6, "请选择": 2, "人数": 4, "几位": 4},
    "consent_checkbox": {"隐私": 5, "协议": 5, "同意": 4, "授权": 4, "允许": 1},
    "resting_menu": {"菜单": 5, "点餐": 4, "菜品": 4, "购物车": 4, "价格": 3, "分类": 3, "¥": 3, "￥": 3},
    # 门店入口与门店选择：后续门店列表、定位授权都会走到这两个场景。
    "miniapp_home": {"开始点餐": 6, "开始点单": 6, "堂食": 4, "自取": 4, "外带": 4, "门店首页": 5},
    "store_select": {"选择门店": 6, "切换门店": 6, "附近门店": 5, "门店列表": 5, "当前门店": 4, "距您": 4},
}


# 直接复用当前画面作为“点击前”截图的上限；超时后重新截图，避免把过期画面当作对照。
FRAME_REUSE_SECONDS = 90.0
# 点击后等待界面稳定的时间。窗口截图本身要数秒，固定长等待只会让每一步更笨重；
# 先截一张，若判定无变化再补一张短等待截图。
CLICK_SETTLE_SECONDS = 0.2
RETRY_SETTLE_SECONDS = 0.45
WECHAT_BUNDLE_ID = "com.tencent.xinWeChat"
# 每次标注会话的落盘目录：output/诊断/annotation/<session-id>。
# 已标注门店的回看从这个目录读取，不依赖当前进程是否仍有会话。
ANNOTATION_SESSIONS_DIR = Path("诊断") / "annotation"


class AnnotationError(ValueError):
    pass


def _resolve_page_state(category: str, page_state: Any) -> str:
    """类别与页面状态一一对应；旧样本或旧前端传了值也以类别推导为准。"""
    derived = CATEGORY_PAGE_STATE.get(category)
    if derived:
        return derived
    return page_state if page_state in PAGE_STATES else "unknown"


def _resolve_layout(category: str, layout: Any) -> str:
    """界面样式必须属于当前场景，否则按未标注处理，避免两层数据对不上。"""
    allowed = {key for key, _ in LAYOUTS.get(category, ())}
    return layout if isinstance(layout, str) and layout in allowed else ""


def _normalize_text(value: str) -> str:
    return re.sub(r"\s+", "", str(value or "")).casefold()


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{name} 必须是数字")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{name} 必须是有限数字")
    return result


def map_box_to_window(box: Any, image_size: tuple[float, float],
                      window_size: tuple[float, float]) -> dict:
    """把截图像素框换算成 UiPath 窗口 points；点击坐标取框中心。"""
    if not isinstance(box, dict):
        raise ValueError("框选区域无效")
    x = _number(box.get("x"), "x")
    y = _number(box.get("y"), "y")
    width = _number(box.get("width"), "width")
    height = _number(box.get("height"), "height")
    image_width, image_height = float(image_size[0]), float(image_size[1])
    window_width, window_height = float(window_size[0]), float(window_size[1])
    if min(image_width, image_height, window_width, window_height) <= 0:
        raise ValueError("截图或窗口尺寸无效")
    if width < 4 or height < 4:
        raise ValueError("框选区域太小，请重新拖动")
    if x < -1 or y < -1 or x + width > image_width + 1 or y + height > image_height + 1:
        raise ValueError("框选区域超出截图")
    x = max(0.0, x)
    y = max(0.0, y)
    width = min(width, image_width - x)
    height = min(height, image_height - y)
    center_x, center_y = x + width / 2, y + height / 2
    scale_x, scale_y = window_width / image_width, window_height / image_height
    return {
        "box_px": {"x": round(x, 1), "y": round(y, 1), "width": round(width, 1), "height": round(height, 1)},
        "center_px": {"x": round(center_x, 1), "y": round(center_y, 1)},
        "center_points": {"x": round(center_x * scale_x, 2), "y": round(center_y * scale_y, 2)},
        "scale": {"x": round(scale_x, 6), "y": round(scale_y, 6)},
        "image_size": {"width": int(image_width), "height": int(image_height)},
        "window_size": {"width": round(window_width, 2), "height": round(window_height, 2)},
    }


def _row_box(row: dict) -> dict | None:
    try:
        box = {"x": float(row["x"]), "y": float(row["y"]),
               "width": float(row["width"]), "height": float(row["height"])}
    except (KeyError, TypeError, ValueError):
        return None
    if box["width"] <= 0 or box["height"] <= 0:
        return None
    return box


def _pad_box(box: dict, pad: float, image_width: float, image_height: float) -> dict:
    x = max(0.0, box["x"] - pad)
    y = max(0.0, box["y"] - pad)
    right = min(image_width, box["x"] + box["width"] + pad)
    bottom = min(image_height, box["y"] + box["height"] + pad)
    return {"x": round(x, 1), "y": round(y, 1),
            "width": round(max(0.1, right - x), 1), "height": round(max(0.1, bottom - y), 1)}


def suggest_target(rows: Any, image_width: float, image_height: float) -> dict | None:
    """根据本地 OCR 给一个可修改的建议；没有把握时返回 None。"""
    if not isinstance(rows, list):
        return None
    scores = {category: 0 for category in CATEGORIES}
    hits: dict[str, list[dict]] = {category: [] for category in CATEGORIES}
    for row in rows:
        if not isinstance(row, dict):
            continue
        text = _normalize_text(row.get("text", ""))
        if not text:
            continue
        for category, keywords in CATEGORY_KEYWORDS.items():
            for keyword, weight in keywords.items():
                if keyword.casefold() in text:
                    scores[category] += weight
                    hits[category].append({"row": row, "weight": weight, "text": text})
                    break
        # “1人 / 12位”这类人数格子没有类别关键词，单独补分。
        if re.search(r"\d{1,2}\s*[人位]", text):
            scores["people_grid"] += 3
            hits["people_grid"].append({"row": row, "weight": 3, "text": text})
    category = max(CATEGORIES, key=lambda item: scores[item])
    if scores[category] < 3 or not hits[category]:
        return None
    best = max(hits[category], key=lambda item: (item["weight"], len(item["text"])))
    box = _row_box(best["row"])
    if box is None:
        return None
    padded = _pad_box(box, max(6.0, min(image_width, image_height) * 0.008), image_width, image_height)
    return {
        "category": category,
        "page_state": CATEGORY_PAGE_STATE[category],
        "target": best["text"][:80],
        "box_px": padded,
        "matched_text": best["text"][:120],
        "score": scores[category],
        "confidence": round(min(0.95, 0.3 + scores[category] / 16), 2),
    }


# 选人数、勾选协议这类控件只占几十像素，全图平均差常低于 1%，
# 单看全图会把“点成功”误判成“没反应”；必须同时看点击目标附近的局部差异。
_EFFECT_GLOBAL_THRESHOLD = 1.5
_EFFECT_ROI_RATIO = 0.02
_EFFECT_ROI_MIN_PIXELS = 24
_EFFECT_ROI_PIXEL_DIFF = 12


def _effect_roi(size: tuple[int, int], box: dict | None, center: dict | None) -> tuple[int, int, int, int]:
    """以实际点击位置为中心的局部窗口；有目标框时按框高收紧，避免小变化被稀释。"""
    width, height = size
    if not center:
        return (0, 0, width, height)
    try:
        center_x = float(center.get("x") or 0)
        center_y = float(center.get("y") or 0)
    except (TypeError, ValueError):
        return (0, 0, width, height)
    pad = 28.0
    if box:
        try:
            pad = max(16.0, min(36.0, float(box.get("height") or 0) * 1.5))
        except (TypeError, ValueError):
            pad = 28.0
    left = max(0, int(center_x - pad))
    top = max(0, int(center_y - pad))
    right = min(width, int(center_x + pad))
    bottom = min(height, int(center_y + pad))
    if right - left < 8 or bottom - top < 8:
        return (0, 0, width, height)
    return (left, top, right, bottom)


def _roi_change(before: "Image.Image", after: "Image.Image", roi: tuple[int, int, int, int]) -> tuple[float, int]:
    difference = ImageChops.difference(before.convert("L").crop(roi), after.convert("L").crop(roi))
    total = max(1, difference.width * difference.height)
    # 直方图统计，避免逐像素遍历整图。
    histogram = difference.histogram()
    changed = sum(histogram[_EFFECT_ROI_PIXEL_DIFF:])
    return round(changed / total, 4), changed


def image_effect(before_path: Path, after_path: Path, *, box: dict | None = None,
                 center: dict | None = None) -> dict:
    """用点击前后截图的差异判断这次点击是否可能生效；只做记录，不自动判定对错。"""
    try:
        with Image.open(before_path) as before, Image.open(after_path) as after:
            if before.size != after.size:
                return {"changed": True, "diff_score": None, "reason": "screenshot_size_changed"}
            difference = ImageChops.difference(before.convert("RGB"), after.convert("RGB"))
            score = round(float(ImageStat.Stat(difference.convert("L")).mean[0]), 2)
            size = before.size
            roi = _effect_roi(size, box, center)
            roi_ratio, roi_pixels = _roi_change(before, after, roi)
    except Exception as exc:  # 图片读取失败不能阻断标注流程
        return {"changed": None, "diff_score": None, "reason": type(exc).__name__}
    # 只有真正缩到点击目标附近时才按局部判据，否则几十个像素的噪声也会误判成“有变化”。
    localized = roi != (0, 0, size[0], size[1])
    changed = score >= _EFFECT_GLOBAL_THRESHOLD or (
        localized and (roi_ratio >= _EFFECT_ROI_RATIO or roi_pixels >= _EFFECT_ROI_MIN_PIXELS))
    return {"changed": changed, "diff_score": score, "threshold": _EFFECT_GLOBAL_THRESHOLD,
            "roi_ratio": roi_ratio, "roi_pixels": roi_pixels, "roi_threshold": _EFFECT_ROI_RATIO,
            "roi": list(roi), "roi_localized": localized}


def _compact_rows(rows: Any, limit: int = 300) -> list[dict]:
    output: list[dict] = []
    if not isinstance(rows, list):
        return output
    for row in rows[:limit]:
        if not isinstance(row, dict):
            continue
        item = {"text": str(row.get("text", ""))[:200]}
        for key in ("x", "y", "width", "height", "confidence"):
            try:
                item[key] = round(float(row[key]), 2)
            except (KeyError, TypeError, ValueError):
                continue
        output.append(item)
    return output


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


class RunnerExecutor:
    """把 UiPathRunner 的现有能力包成标注会话可替换的执行器。"""

    def __init__(self, runner):
        self.runner = runner
        self.work_dir: Path | None = None

    def running(self) -> bool:
        return bool(getattr(self.runner, "running", False))

    def attach(self, run_directory: Path, output_root: Path, work_dir: Path, profile: dict) -> None:
        runner = self.runner
        self.work_dir = Path(work_dir)
        runner.run_id = None
        runner.run_directory = Path(run_directory)
        runner.output_directory = self.work_dir
        runner.transport = runner.transport_factory(Path(run_directory), Path(output_root))
        if hasattr(runner.transport, "retain_command_history"):
            runner.transport.retain_command_history = runner.debug_log is None
        runner._cli_process = None
        runner._cli_metadata_path = runner._cli_log_path = runner._cli_args_path = None
        runner._cli_info = None
        runner._cli_reader_thread = None
        runner._trace_attempt = None
        runner._trace_step = None
        runner._trace_click = {}
        runner._debug_frame_refs.clear()
        runner._annotation_mode = True
        runner.stop_event.clear()
        runner.pause_event.clear()
        runner._deadline = 0
        runner.current_task = None

    def launch(self, run_directory: Path, settings, profile: dict, task_count: int) -> None:
        # 人工标注不限制单店时长，给 CLI 留出充足的人工操作时间。
        timeout_seconds = max(3600, task_count * 1800 + 1800)
        self.runner._launch_cli(Path(run_directory), settings, profile,
                                task_count=task_count, timeout_seconds=timeout_seconds)

    def await_startup(self) -> None:
        self.runner._await_cli_startup()

    def ensure_safe_start(self, profile: dict, settle_seconds: float) -> None:
        self.runner._ensure_file_assistant(profile, self.work_dir, settle_seconds)

    def open_store(self, task: dict, profile: dict, work_dir: Path, settle_seconds: float) -> dict:
        return self.runner._open_source_automatic(task, profile, Path(work_dir), settle_seconds)

    def screenshot(self, selector: str, name: str) -> dict:
        return self.runner._screenshot(self.work_dir, selector, name)

    def screenshot_after_click(self, selector: str, name: str) -> dict:
        """刚点击过，窗口矩形已被 UiPath 校验；优先按矩形直取，异常再走完整截图。"""
        frame = self.runner._fast_window_capture(self.work_dir, selector, name)
        return frame if frame is not None else self.runner._screenshot(self.work_dir, selector, name)

    def resize(self, selector: str, width: int, height: int, x: int, y: int) -> dict:
        return self.runner._send({"action": "resize", "selector": selector, "width": width,
                                  "height": height, "x": x, "y": y})

    def click(self, selector: str, x: float, y: float) -> dict:
        return self.runner._send({"action": "click", "selector": selector, "x": x, "y": y})

    def wait(self, seconds: float) -> dict:
        return self.runner._send({"action": "noop", "seconds": seconds})

    def close(self, profile: dict, settle_seconds: float) -> None:
        self.runner._close_to_file_assistant(profile, self.work_dir, settle_seconds)

    def request_stop(self) -> None:
        if getattr(self.runner, "transport", None) is not None:
            self.runner.transport.request_stop()

    def reap(self) -> None:
        self.runner._reap_cli()

    def detach(self) -> None:
        runner = self.runner
        runner._annotation_mode = False
        runner.transport = None
        runner.run_directory = None
        runner.output_directory = None
        runner._cli_process = None
        runner._cli_info = None
        runner.current_task = None


class AnnotationSession:
    def __init__(self, configs: ConfigStore, store, profiles, executor: RunnerExecutor, *,
                 root: Path = WORKSPACE_ROOT, ocr_reader: Callable[[Path], list[dict]] | None = None,
                 sample_root: Path | None = None, activator: Callable[[], None] | None = None,
                 rejector: Callable[[str, str, str], dict] | None = None,
                 usage_ledger=None):
        self.configs = configs
        self.store = store
        self.profiles = profiles
        self.executor = executor
        # 标注里的模型调用同样要进入“模型用量”，否则统计里只剩批处理。
        self.usage_ledger = usage_ledger
        self.ocr_reader = ocr_reader
        self.activator = activator
        self.rejector = rejector
        self.sample_root = Path(sample_root) if sample_root else Path(root) / "素材" / "规则样本"
        self._lock = threading.RLock()
        self.session_id: str | None = None
        self.status = "idle"
        self.message = ""
        self.task_ids: list[str] = []
        self.tasks: dict[str, dict] = {}
        self.index = 0
        self.profile: dict = {}
        self.steps: list[dict] = []
        self.completed: list[dict] = []
        self.archived: list[dict] = []
        self.skipped: list[dict] = []
        self.rejected: list[dict] = []
        self._resume_status = "ready"
        self.frame: dict | None = None
        self.suggestion: dict | None = None
        # 与批处理同源的决策建议（本地规则或模型），只展示不执行。
        self.decision: dict | None = None
        # 与批处理一致的进度上下文：本店是否已选人数、是否已勾选隐私协议。
        self.people_selected = False
        self.consent_checked = False
        # 当前画面捕获时间与 OCR 结果：点击时可复用，省掉一次截图和一次 OCR。
        self._frame_at = 0.0
        self._frame_rows: list[dict] | None = None
        self._suggest_generation = 0
        self._suggestion_thread: threading.Thread | None = None
        self._advance_thread: threading.Thread | None = None
        self._advance_cancel = threading.Event()
        # 切换微信前的前台应用，点击结束后恢复，方便继续在网页上框选。
        self._restore_bundle: str | None = None
        self.settings = None
        self.work_dir: Path | None = None
        self.run_dir: Path | None = None
        self.started_at: str | None = None
        self.finished_at: str | None = None

    @property
    def active(self) -> bool:
        return self.session_id is not None and self.status not in {"idle", "finished", "stopped"}

    @property
    def current_task(self) -> dict | None:
        if not self.task_ids or self.index >= len(self.task_ids):
            return None
        return self.tasks.get(self.task_ids[self.index])

    def state(self) -> dict:
        with self._lock:
            task = self.current_task
            task_id = task["id"] if task else ""
            store_steps = [step for step in self.steps if step.get("task_id") == task_id]
            return {
                "session_id": self.session_id,
                "active": self.active,
                "paused": self.status == "paused",
                "status": self.status,
                "message": self.message,
                "task_ids": list(self.task_ids),
                "index": self.index,
                "total": len(self.task_ids),
                "task": {"id": task["id"], "source_name": task["source_name"]} if task else None,
                "frame": dict(self.frame) if self.frame else None,
                "suggestion": dict(self.suggestion) if self.suggestion else None,
                "decision": dict(self.decision) if self.decision else None,
                "categories": list(CATEGORIES),
                "page_states": list(PAGE_STATES),
                "layouts": {category: [key for key, _ in options]
                            for category, options in LAYOUTS.items()},
                "layout_labels": dict(LAYOUT_LABELS),
                "steps": [self._public_step(step) for step in self.steps],
                "current_steps": [self._public_step(step) for step in store_steps],
                "last_click": self._public_step(store_steps[-1]) if store_steps else None,
                "completed": list(self.completed),
                "archived": list(self.archived),
                "skipped": list(self.skipped),
                "rejected": list(self.rejected),
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "samples_root": str(self.sample_root),
            }

    @staticmethod
    def _public_step(step: dict) -> dict:
        public = {key: value for key, value in step.items()
                  if key not in {"before_path", "after_path", "ocr_before", "ocr_after"}}
        for key in ("before", "after"):
            item = public.get(key)
            if isinstance(item, dict):
                public[key] = {k: v for k, v in item.items() if k != "path"}
        return public

    def _sessions_root(self) -> Path:
        settings = self.configs.load()
        return Path(settings.output_dir).expanduser().resolve() / ANNOTATION_SESSIONS_DIR

    def history(self, task_id: str) -> dict:
        """回看某店已确认点击的实际效果，当前会话和已落盘的历史会话都可用。"""
        task = self.store.get(task_id)
        with self._lock:
            current = [dict(step) for step in self.steps if step.get("task_id") == task_id]
            active_work_dir = Path(self.work_dir).resolve() if self.work_dir else None
            session_id = self.session_id
            started_at, finished_at = self.started_at, self.finished_at
        steps = [self._public_step(step) for step in current]
        if steps:
            return {
                "task": {"id": task["id"], "source_name": task["source_name"]},
                "session_id": session_id,
                "steps": steps,
                "started_at": started_at,
                "finished_at": finished_at,
            }

        root = self._sessions_root()
        if not root.is_dir():
            return self._empty_history(task)
        directories = sorted((item for item in root.iterdir() if item.is_dir()),
                             key=lambda item: item.name, reverse=True)
        for directory in directories:
            if active_work_dir is not None and directory.resolve() == active_work_dir:
                continue
            steps_dir = directory / "steps"
            if not steps_dir.is_dir():
                continue
            found: list[dict] = []
            for step_path in sorted(steps_dir.glob("*.json")):
                try:
                    step = json.loads(step_path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                if not isinstance(step, dict) or step.get("task_id") != task_id:
                    continue
                before, after = step.get("before"), step.get("after")
                if not isinstance(before, dict) or not isinstance(after, dict):
                    continue
                # 截图已清理时跳过该步，避免前端拿到 404 的图片。
                if not (directory / str(before.get("name", ""))).is_file():
                    continue
                for role, item in (("before", before), ("after", after)):
                    if item.get("name") and not item.get("url"):
                        item["url"] = f"/api/annotation/frame?session={directory.name}&name={item['name']}"
                found.append(step)
            if not found:
                continue
            found.sort(key=lambda item: int(item.get("index") or 0))
            snapshot: dict = {}
            session_file = directory / "session.json"
            if session_file.is_file():
                try:
                    snapshot = json.loads(session_file.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    snapshot = {}
            return {
                "task": {"id": task["id"], "source_name": task["source_name"]},
                "session_id": directory.name,
                "steps": [self._public_step(step) for step in found],
                "started_at": snapshot.get("started_at"),
                "finished_at": snapshot.get("finished_at"),
            }
        return self._empty_history(task)

    @staticmethod
    def _empty_history(task: dict) -> dict:
        return {"task": {"id": task["id"], "source_name": task["source_name"]},
                "session_id": None, "steps": [], "started_at": None, "finished_at": None}

    def start(self, task_ids: list[str]) -> dict:
        # 上一轮“完成本店”的后台推进可能仍在收尾，先等它结束再复用执行器。
        self._advance_cancel.set()
        self._wait_advance()
        with self._lock:
            if self.active:
                raise AnnotationError("当前已有进行中的人工标注，请先结束")
            if self.executor.running():
                raise AnnotationError("自动采集正在运行，请先暂停或终止")
            ordered: list[str] = []
            for task_id in task_ids:
                if task_id not in ordered:
                    ordered.append(task_id)
            if not ordered:
                raise AnnotationError("请至少选择一家门店")
            tasks = {task_id: self.store.get(task_id) for task_id in ordered}
            settings = self.configs.load()
            profile = dict(self.profiles.load())
            if profile.get("launch_mode", "cli") != "cli":
                raise AnnotationError("人工标注需要通过 UiPath CLI 启动；请在设置中把启动方式切换为“通过 UiPath CLI 启动”后重试")
            # 强制重新观察当前微信窗口，避免沿用上一轮的小程序标题。
            profile["miniapp_selector"] = ""
            output_root = Path(settings.output_dir).expanduser().resolve()
            output_root.mkdir(parents=True, exist_ok=True)
            # 服务重启会丢失内存中的会话，但 UiPath CLI/执行器可能仍在运行并占住任务槽。
            self._cleanup_orphaned_runs(output_root)
            session_id = datetime.now().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
            work_dir = output_root / ANNOTATION_SESSIONS_DIR / session_id
            work_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            work_dir.chmod(0o700)
            run_dir = output_root / ".annotation-runs" / session_id
            run_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
            run_dir.chmod(0o700)
            self.session_id = session_id
            self.status = "starting"
            self.message = "正在启动 UiPath 并确认微信安全起点"
            self.task_ids = ordered
            self.tasks = tasks
            self.index = 0
            self.profile = profile
            self.settings = settings
            self.work_dir = work_dir
            self.run_dir = run_dir
            self.steps = []
            self.completed = []
            self.archived = []
            self.skipped = []
            self.rejected = []
            self._resume_status = "ready"
            self.frame = None
            self.suggestion = None
            self.decision = None
            self.people_selected = False
            self.consent_checked = False
            self._frame_at = 0.0
            self._frame_rows = None
            self._suggest_generation += 1
            self._restore_bundle = None
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.finished_at = None
            self._save()
            try:
                self.executor.attach(run_dir, output_root, work_dir, profile)
                if profile.get("launch_mode", "cli") == "cli":
                    self.executor.launch(run_dir, settings, profile, len(ordered))
                self.executor.await_startup()
                # 不在首次打开前额外截图：open_store 自带“文件传输助手”校验，
                # 少一条窗口命令可避免 macOS 置前尚未完成时的焦点竞态。
            except Exception as exc:
                self.status = "error"
                self.message = self._describe(exc, "启动人工标注失败")
                self._save()
                self._shutdown_executor()
                return self.state()
            self._open_current()
            return self.state()

    def decide(self) -> dict:
        """按批处理顺序给出只读建议：本地就绪 → 本地动作 → 模型。绝不点击。"""
        self._ensure_not_advancing()
        with self._lock:
            self._reject_when_paused()
            if not self.frame:
                raise AnnotationError("尚未取得小程序截图")
            frame = dict(self.frame)
            task = dict(self.current_task or {})
            settings = self.settings
            work_dir = self.work_dir
        if settings is None or work_dir is None:
            raise AnnotationError("标注会话尚未就绪")
        path = Path(work_dir) / frame["name"]
        if not path.is_file():
            raise AnnotationError("当前截图已不存在，请重新截图")
        runner = self.executor.runner
        previous_reader = getattr(runner, "ocr_reader", None)
        if previous_reader is None:
            runner.ocr_reader = self.ocr_reader
        try:
            ocr_result = runner._read_scaled_ocr(path)
            runner._active_ocr_path = str(path.resolve())
            runner._active_ocr_result = ocr_result
            resolved = self._decide_with_chain(runner, path, task, settings, ocr_result)
        finally:
            runner.ocr_reader = previous_reader
        payload = self._decision_payload(
            resolved["decision"], resolved["source"], runner, resolved["ocr_result"],
            frame, resolved["error"], meta=resolved)
        with self._lock:
            if (self.frame or {}).get("name") != frame["name"]:
                raise AnnotationError("画面已切换，请对当前画面重新生成建议")
            self.decision = payload
            self.message = {
                "local_ready": "本地规则判断：菜单已就绪",
                "local_prepare": "本地规则给出了可点击目标；请核对区域",
                "local_fused_ready": "本地规则（多尺度 OCR 补充后）判断：菜单已就绪",
                "local_fused_prepare": "本地规则（多尺度 OCR 补充后）给出了可点击目标；请核对区域",
                "model": "本地规则未命中，已由模型给出建议；请核对区域",
                "model_failed": f"模型未给出可用建议：{resolved['error'] or '未知原因'}",
                "unavailable": resolved["error"] or "模型不可用",
            }.get(resolved["source"], "已生成建议")
            self._save()
            return self.state()

    def _decide_with_chain(self, runner, path: Path, task: dict, settings, ocr_result) -> dict:
        """人工标注的单帧建议（只读，不执行动作）。"""
        source_name = str(task.get("source_name", ""))
        result = {
            "decision": None, "source": "unavailable", "error": None,
            "ocr_result": ocr_result, "chain": ["本地就绪", "本地动作"],
            "hit_stage": "none", "coordinate_source": None, "region_source": None,
        }
        try:
            ready = runner._local_ready_preflight(path, source_name, ocr_result)
        except Exception:
            ready = None
        if ready is not None:
            result.update(decision=ready, source="local_ready", hit_stage="local_ready")
            return result
        try:
            local = runner._local_prepare_action(
                ocr_result, settings.people_count, settings.auto_authorize, self.people_selected,
                image_path=path, consent_checked=self.consent_checked,
            )
        except Exception:
            local = None
        if local is not None:
            result.update(decision=local, source="local_prepare", hit_stage="local_prepare")
            return result
        # 与批处理一致：本地规则都未命中时，用 2x/4x 融合 OCR 再召回一次。
        result["chain"].append("多尺度OCR")
        if runner.ocr_reader is not None and ocr_result[0] is not None:
            try:
                fused = runner._read_fused_scaled_ocr(path, ocr_result)
            except Exception:
                fused = (None, [])
            if fused[0] is not None and len(fused[1]) > len(ocr_result[1]):
                try:
                    retry_ready = runner._local_ready_preflight(path, source_name, fused)
                except Exception:
                    retry_ready = None
                if retry_ready is not None:
                    result.update(decision=retry_ready, source="local_fused_ready",
                                  ocr_result=fused, hit_stage="fused_ocr")
                    return result
                try:
                    runner._active_ocr_result = fused
                    retry_local = runner._local_prepare_action(
                        fused, settings.people_count, settings.auto_authorize, self.people_selected,
                        image_path=path, consent_checked=self.consent_checked,
                    )
                except Exception:
                    retry_local = None
                if retry_local is not None:
                    result.update(decision=retry_local, source="local_fused_prepare",
                                  ocr_result=fused, hit_stage="fused_ocr")
                    return result
        result["chain"].append("模型")
        try:
            vision = VisionModel(settings.model, self.configs.key(), usage_ledger=self.usage_ledger)
        except Exception as exc:
            result.update(source="unavailable", error=f"模型未配置：{type(exc).__name__}")
            return result
        agent = SceneAgent(
            vision,
            people_count=settings.people_count,
            auto_authorize=settings.auto_authorize,
            coordinate_resolver=lambda image, decision: runner._resolve_click_coordinates(
                image, decision, settings.people_count),
            region_resolver=lambda image, decision: runner._resolve_ready_region(
                image, decision, source_name, result["ocr_result"]),
        )
        candidates = runner._target_candidates(result["ocr_result"])
        try:
            with usage_context(source="annotation", task_id=str(task.get("id", "")),
                               source_name=source_name, stage="annotation"):
                decision = agent.decide(path, "prepare", [], candidates)
        except SceneError as exc:
            result.update(source="model_failed", error=str(exc)[:300], hit_stage="model")
            return result
        except Exception as exc:
            result.update(source="model_failed", error=f"{type(exc).__name__}: {str(exc)[:200]}",
                          hit_stage="model")
            return result
        result.update(
            decision=decision, source="model", hit_stage="model",
            coordinate_source=getattr(agent, "last_coordinate_source", None),
            region_source=getattr(agent, "last_region_source", None),
        )
        return result

    @staticmethod
    def _decision_payload(decision, source: str, runner, ocr_result, frame: dict,
                          error: str | None, meta: dict | None = None) -> dict:
        meta = meta or {}
        width = int(frame.get("image_width") or 0)
        height = int(frame.get("image_height") or 0)
        x = y = None
        box = None
        if decision is not None:
            x, y = decision.x, decision.y
            if decision.target_box is not None:
                box = {
                    "x": round(float(decision.target_box.x), 1),
                    "y": round(float(decision.target_box.y), 1),
                    "width": round(float(decision.target_box.width), 1),
                    "height": round(float(decision.target_box.height), 1),
                }
            elif x is not None and y is not None:
                half = 12.0
                left, top = max(0.0, float(x) - half), max(0.0, float(y) - half)
                box = {"x": round(left, 1), "y": round(top, 1),
                       "width": round(min(2 * half, width - left), 1),
                       "height": round(min(2 * half, height - top), 1)}
        resolution = dict(getattr(runner, "_last_target_resolution", {}) or {})
        candidates = runner._target_candidates(ocr_result) if ocr_result and ocr_result[0] else []
        method = resolution.get("method") or resolution.get("strategy")
        return {
            "at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "source": source,
            "source_detail": AnnotationSession._describe_source(
                source, method, meta.get("coordinate_source"), meta.get("region_source")),
            "chain": list(meta.get("chain") or []),
            "hit_stage": meta.get("hit_stage"),
            "action": decision.action if decision is not None else None,
            "target_label": decision.target_label if decision is not None else "",
            "reason": decision.reason if decision is not None else "",
            "x": x,
            "y": y,
            "box_px": box,
            "method": method,
            "resolution_reason": resolution.get("reason"),
            "candidates": [
                {"id": item.get("id"), "text": str(item.get("text", ""))[:60],
                 "kind": item.get("kind"), "position": item.get("position")}
                for item in candidates[:30]
            ],
            "flags": ({
                "overlay_visible": decision.overlay_visible,
                "login_required": decision.login_required,
                "phone_required": decision.phone_required,
                "consent_required": decision.consent_required,
                "transaction_visible": decision.transaction_visible,
            } if decision is not None else {}),
            "image_size": {"width": width, "height": height},
            "error": error,
        }

    @staticmethod
    def _describe_source(source: str, method: str | None,
                         coordinate_source: str | None, region_source: str | None) -> str:
        """把建议来源说清楚：本地规则 / 素材匹配 / 模型判断，以及各自细分类别。"""
        if source in {"local_ready", "local_fused_ready"}:
            return "本地规则 · 菜单就绪（OCR 文字与结构校验）"
        if source in {"local_prepare", "local_fused_prepare"}:
            detail = AnnotationSession._describe_method(method)
            text = detail if detail.startswith("素材匹配") else "本地规则 · " + detail
            if source.startswith("local_fused"):
                text += " · 多尺度 OCR 补充"
            return text
        if source == "model":
            parts = ["模型判断"]
            if coordinate_source:
                parts.append("坐标来自" + ("本地解析" if coordinate_source == "local" else "模型"))
            if region_source:
                parts.append("区域来自" + ("本地解析" if region_source == "local" else "模型"))
            return " · ".join(parts)
        if source == "model_failed":
            return "模型调用失败"
        return "模型不可用"

    _METHOD_LABELS = {
        "local_exact_button": "OCR 文字按钮定位",
        "local_exact_button_ranked": "OCR 同名候选排序定位",
        "local_people_grid_ocr": "OCR 人数网格定位",
        "local_people_grid_geometry": "人数网格几何推断",
        "local_consent_checkbox_first": "隐私协议勾选定位",
        "local_consent_pair_geometry": "协议勾选与同意配对定位",
        "local_authorization_single_label": "授权单行文案定位",
    }

    @staticmethod
    def _describe_method(method: str | None) -> str:
        if not method:
            return "OCR 文字定位"
        if "template" in str(method):
            return "素材匹配 · 模板定位"
        return AnnotationSession._METHOD_LABELS.get(str(method), f"OCR 文字定位（{method}）")

    @staticmethod
    def _attribute(decision: dict | None, center_px: dict, image_size: tuple[float, float],
                   effect: dict) -> dict:
        """对比建议位置与人工最终位置，标出错在本地规则、模型还是坐标。"""
        if not decision:
            return {"adopted": None, "deviation_px": None, "attribution": "no_suggestion"}
        suggested_size = decision.get("image_size") or {}
        try:
            same_frame = (int(suggested_size.get("width") or 0), int(suggested_size.get("height") or 0)) == (
                int(image_size[0]), int(image_size[1]))
        except (TypeError, ValueError):
            same_frame = False
        if not same_frame:
            return {"adopted": None, "deviation_px": None, "attribution": "suggestion_from_other_frame",
                    "suggested_source": decision.get("source"),
                    "suggested_label": decision.get("target_label"),
                    "suggested_method": decision.get("method")}
        sx, sy = decision.get("x"), decision.get("y")
        if not isinstance(sx, (int, float)) or not isinstance(sy, (int, float)) or isinstance(sx, bool):
            return {"adopted": None, "deviation_px": None, "attribution": "no_coordinate_suggestion",
                    "suggested_source": decision.get("source"),
                    "suggested_label": decision.get("target_label"),
                    "suggested_method": decision.get("method")}
        deviation = round(math.hypot(float(center_px["x"]) - float(sx), float(center_px["y"]) - float(sy)), 2)
        adopted = deviation <= SUGGESTION_ADOPT_TOLERANCE_PX
        if not adopted:
            attribution = "model_wrong" if decision.get("source") == "model" else "local_rule_wrong"
        elif effect.get("changed") is False:
            attribution = "coordinate_or_target_wrong"
        else:
            attribution = "none"
        return {"adopted": adopted, "deviation_px": deviation, "attribution": attribution,
                "suggested_source": decision.get("source"),
                "suggested_label": decision.get("target_label"),
                "suggested_method": decision.get("method")}

    def click(self, box: Any, category: str, page_state: str, target: str,
              hard_negative: bool = False, note: str = "", layout: str = "") -> dict:
        self._ensure_not_advancing()
        with self._lock:
            self._reject_when_paused()
            if self.status not in {"ready", "clicked"}:
                raise AnnotationError("当前没有可点击的小程序画面")
            if category not in CATEGORIES:
                raise AnnotationError("类别无效")
            page_state = _resolve_page_state(category, page_state)
            layout = _resolve_layout(category, layout)
            if not isinstance(hard_negative, bool):
                raise AnnotationError("硬负样本标记无效")
            target = str(target or "").strip()[:80]
            note = str(note or "").strip()[:200]
            selector = self.profile.get("miniapp_selector") or ""
            if not selector:
                raise AnnotationError("尚未确定小程序窗口，请重试打开本店")
            frame = self.frame
            if not frame:
                raise AnnotationError("尚未取得小程序截图")
            # 记录本次点击针对的建议快照；后置截图会清空当前建议。
            decision_snapshot = dict(self.decision) if self.decision else None
            # 先校验一次，避免明显的非法框仍然驱动桌面动作。
            map_box_to_window(box, (frame["image_width"], frame["image_height"]),
                              (frame["window_width"], frame["window_height"]))
            index = len(self.steps) + 1
            click_sent = False
            # 点击按钮在浏览器里，先把微信重新置前，否则窗口截图和点击会被焦点竞态拒绝。
            self._activate_wechat()
            try:
                before, before_rows = self._before_frame(index, selector, frame)
                if before is None:
                    return self.state()
                mapping = map_box_to_window(box, (before["width"], before["height"]),
                                            (before["window_width"], before["window_height"]))
                self.message = "正在点击并采集点击前后截图"
                self._save()
                self.executor.click(selector, mapping["center_points"]["x"], mapping["center_points"]["y"])
                click_sent = True
                self.executor.wait(CLICK_SETTLE_SECONDS)
                after = self.executor.screenshot_after_click(selector, f"step-{index:02d}-after.png")
                after_rows = self._ocr(Path(after["path"]))
                effect = image_effect(Path(before["path"]), Path(after["path"]),
                                      box=mapping["box_px"], center=mapping["center_px"])
                if effect.get("changed") is False and not self._text_changed(before_rows, after_rows):
                    # 截图本身有几秒延迟；偶尔仍拍到过渡帧，补一张再判定，不靠固定长等待。
                    self.executor.wait(RETRY_SETTLE_SECONDS)
                    retried = self.executor.screenshot_after_click(selector, f"step-{index:02d}-after-retry.png")
                    retried_rows = self._ocr(Path(retried["path"]))
                    retried_effect = image_effect(Path(before["path"]), Path(retried["path"]),
                                                  box=mapping["box_px"], center=mapping["center_px"])
                    if retried_effect.get("changed") or self._text_changed(before_rows, retried_rows):
                        after, after_rows, effect = retried, retried_rows, retried_effect
                text_changed = self._text_changed(before_rows, after_rows)
                effect["text_changed"] = text_changed
                if effect.get("changed") is not None:
                    effect["changed"] = bool(effect["changed"] or text_changed)
                attribution = self._attribute(
                    decision_snapshot, mapping["center_px"],
                    (before["width"], before["height"]), effect,
                )
                step = {
                    "index": index,
                    "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "task_id": (self.current_task or {}).get("id", ""),
                    "source_name": (self.current_task or {}).get("source_name", ""),
                    "category": category,
                    "layout": layout,
                    "page_state": page_state,
                    "target": target,
                    "hard_negative": hard_negative,
                    "note": note,
                    "box_px": mapping["box_px"],
                    "center_px": mapping["center_px"],
                    "center_points": mapping["center_points"],
                    "scale": mapping["scale"],
                    "image_size": mapping["image_size"],
                    "window_size": mapping["window_size"],
                    "matched_text": (self.suggestion or {}).get("matched_text", ""),
                    "before": self._file_info(before, "before"),
                    "after": self._file_info(after, "after"),
                    "effect": effect,
                    "suggestion": decision_snapshot,
                    "attribution": attribution,
                    "ocr_before": _compact_rows(before_rows),
                    "ocr_after": _compact_rows(after_rows),
                }
                step["before_path"] = step["before"]["path"]
                step["after_path"] = step["after"]["path"]
                self.steps.append(step)
                self._save_step(step)
                progressed = self._update_progress_context(decision_snapshot, target, category)
                # 后置截图直接成为下一轮点击的 before，并复用刚算出的 OCR。
                self._set_frame(after, after_rows)
                self.suggestion = self._suggestion_from_rows(after, after_rows)
                self.status = "clicked"
                if progressed == "people_selected":
                    self.message = "已记录人数选择；下一张画面的建议目标会切到“开始点餐”"
                elif progressed == "consent_checked":
                    self.message = "已记录隐私协议勾选；下一张画面的建议目标会切到“同意/允许”"
                elif effect.get("changed") is False and not hard_negative:
                    self.message = ("点击已发出，但画面几乎无变化，可能是无效点击；"
                                    "请检查是否点中，必要时重试或勾选“硬负样本”。本次不会被归档为有效样本")
                else:
                    self.message = "点击已完成；请确认效果后继续框选，或完成标注进入下一家"
                self._save()
                self._restore_frontmost()
                return self.state()
            except Exception as exc:
                # 点击可能已经发出；不能把“没拿到后置截图”误报为未点击。
                self.status = "clicked" if click_sent else ("ready" if self.frame else "open_failed")
                self.message = (f"点击已发出，但未能取得点击后截图（{type(exc).__name__}）；"
                                "请检查当前画面后重试或跳过"
                                if click_sent else
                                f"点击未完成或结果未知（{type(exc).__name__}）；请检查当前画面后重试")
                self._save()
                return self.state()

    def _update_progress_context(self, decision: dict | None, target: str, category: str) -> str | None:
        """点击后同步批处理同款进度：本店人数已选、隐私协议已勾选。"""
        decision = decision or {}
        method = str(decision.get("method") or "")
        label = str(target or decision.get("target_label") or "").strip()
        if (method.startswith("local_people_grid") or category == "people_grid") and \
                label == str((self.settings.people_count if self.settings else 1)):
            if not self.people_selected:
                self.people_selected = True
                return "people_selected"
        if method in {"local_consent_checkbox_first", "local_consent_pair_geometry"}:
            if not self.consent_checked:
                self.consent_checked = True
                return "consent_checked"
        return None

    def mark_step(self, step_index: int | None, category: str | None = None,
                  page_state: str | None = None, target: str | None = None,
                  hard_negative: bool | None = None, note: str | None = None,
                  layout: str | None = None) -> dict:
        self._ensure_not_advancing()
        with self._lock:
            if not self.steps:
                raise AnnotationError("尚未记录任何点击")
            index = -1 if step_index in (None, -1) else int(step_index)
            if index < 0 or index >= len(self.steps):
                index = len(self.steps) - 1
            step = self.steps[index]
            if category is not None:
                if category not in CATEGORIES:
                    raise AnnotationError("类别无效")
                step["category"] = category
                # 页面状态由类别推导，避免两个字段各说各话。
                step["page_state"] = _resolve_page_state(category, page_state)
                step["layout"] = _resolve_layout(category, layout if layout is not None else step.get("layout"))
            elif page_state is not None:
                step["page_state"] = _resolve_page_state(step.get("category", ""), page_state)
            if layout is not None and category is None:
                step["layout"] = _resolve_layout(step.get("category", ""), layout)
            if target is not None:
                step["target"] = str(target).strip()[:80]
            if hard_negative is not None:
                if not isinstance(hard_negative, bool):
                    raise AnnotationError("硬负样本标记无效")
                step["hard_negative"] = hard_negative
            if note is not None:
                step["note"] = str(note).strip()[:200]
            self._save_step(step)
            self._save()
            return self.state()

    def complete(self) -> dict:
        self._ensure_not_advancing()
        with self._lock:
            self._reject_when_paused()
            if not self.active:
                raise AnnotationError("当前没有进行中的人工标注")
            task = self.current_task
            if not task:
                raise AnnotationError("当前没有待完成的门店")
            store_steps = [step for step in self.steps if step.get("task_id") == task["id"]]
            # 没有产生画面变化的有效点击会污染规则样本；除非人工明确标记为硬负样本。
            invalid_steps = [step for step in store_steps
                            if not step.get("hard_negative")
                            and (step.get("effect") or {}).get("changed") is False]
            valid_steps = [step for step in store_steps if step not in invalid_steps]
            message = "已进入菜单，正在归档样本"
            archived_count = 0
            if valid_steps:
                try:
                    records = archive_samples(self.sample_root, task, self.settings, valid_steps, self.session_id)
                    self.archived.extend(records)
                    archived_count = len(records)
                    message = f"已归档 {archived_count} 条样本，正在关闭小程序"
                except Exception as exc:
                    message = f"样本归档失败（{type(exc).__name__}），仍会关闭小程序"
            else:
                message = "本店没有可归档的有效点击样本，正在关闭小程序"
            if invalid_steps:
                message = message.replace("，正在关闭小程序", "")
                message += f"；已跳过 {len(invalid_steps)} 条画面无变化的无效点击，正在关闭小程序"
            if archived_count:
                # 持久标记本店已产出人工样本，避免下一轮又被当作“待处理”重复标注。
                try:
                    self.store.update(task["id"], stage="annotation_complete",
                                      message=f"人工标注完成，已归档 {archived_count} 条样本")
                except Exception:
                    pass
            self.completed.append({
                "task_id": task["id"],
                "source_name": task["source_name"],
                "steps": len(store_steps),
                "archived": archived_count,
                "invalid": len(invalid_steps),
            })
            # 归档在内存里立即完成，关闭小程序和打开下一家交给后台线程，
            # 让前端“完成本店”几乎立刻拿到响应。
            self.status = "closing"
            self.message = message
            self._save()
            self._start_advance(message)
            return self.state()

    def pause(self) -> dict:
        """暂停会话但不释放执行器，方便用户临时离开；继续时从当前门店接着标注。"""
        with self._lock:
            if not self.active:
                raise AnnotationError("当前没有进行中的人工标注")
            if self.status == "paused":
                return self.state()
            if self.status in {"starting", "opening", "closing"}:
                raise AnnotationError("当前正在执行窗口操作，请稍后再暂停")
            self._resume_status = self.status if self.status in {
                "ready", "clicked", "open_failed", "close_failed"} else "ready"
            self.status = "paused"
            self.message = "已暂停；当前门店保持打开，点击“继续标注”后接着框选"
            self._save()
            return self.state()

    def resume(self) -> dict:
        with self._lock:
            if self.status != "paused":
                raise AnnotationError("当前不在暂停状态")
            self.status = self._resume_status
            self.message = "已继续标注；请框选要点击的位置并确认"
            self._save()
            return self.state()

    def _reject_when_paused(self) -> None:
        if self.status == "paused":
            raise AnnotationError("标注已暂停，请先点击“继续标注”")

    def skip(self, reason: str = "") -> dict:
        self._ensure_not_advancing()
        with self._lock:
            self._reject_when_paused()
            if not self.active:
                raise AnnotationError("当前没有进行中的人工标注")
            task = self.current_task
            if task:
                self.skipped.append({"task_id": task["id"], "source_name": task["source_name"],
                                     "reason": str(reason or "")[:200]})
            message = "已跳过本店（未标记不合格，该店仍保留在待处理）"
            if not self.frame:
                # 打开失败时也可能残留一个小程序窗口，先恢复到安全起点再跳过。
                detail = self._ensure_safe_start()
                if detail:
                    self.status = "close_failed"
                    self.message = f"跳过前未能确认微信安全起点（{detail}）；请手动回到文件传输助手后重试"
                    self._save()
                    return self.state()
                self.profile["miniapp_selector"] = ""
            # 打不开的小程序可能仍留在桌面，跳过后同样先回到文件传输助手。
            if not self._close_current(message):
                return self.state()
            return self._advance()

    def reject(self, reason: str, detail: str = "") -> dict:
        """把当前门店标记为不合格，并继续下一家。

        二维码过期、无效或门店不存在都属于正常输入问题，不应卡住整个标注流程。
        """
        self._ensure_not_advancing()
        with self._lock:
            self._reject_when_paused()
            if not self.active:
                raise AnnotationError("当前没有进行中的人工标注")
            task = self.current_task
            if not task:
                raise AnnotationError("当前没有待处理的门店")
            if not self.rejector:
                raise AnnotationError("不合格标记接口尚未接入")
            reason = str(reason or "").strip()
            detail = str(detail or "").strip()
            try:
                self.rejector(task["id"], reason, detail)
            except ValueError as exc:
                raise AnnotationError(str(exc)) from None
            self.rejected.append({"task_id": task["id"], "source_name": task["source_name"],
                                  "reason": reason, "detail": detail})
            message = "已标记不合格"
            if not self.frame:
                detail_safe = self._ensure_safe_start()
                if detail_safe:
                    self.status = "close_failed"
                    self.message = f"已标记不合格，但未能确认微信安全起点（{detail_safe}）；请手动回到文件传输助手后重试"
                    self._save()
                    return self.state()
                self.profile["miniapp_selector"] = ""
            if not self._close_current(message):
                return self.state()
            return self._advance()

    def retry(self) -> dict:
        self._ensure_not_advancing()
        with self._lock:
            self._reject_when_paused()
            if not self.active:
                raise AnnotationError("当前没有进行中的人工标注")
            if self.status == "close_failed":
                self.message = "正在重试关闭小程序"
                self._save()
                if not self._close_current(self.message):
                    return self.state()
                return self._advance()
            if self.status in {"open_failed", "error"}:
                self.message = "正在重试打开本店"
                self._save()
                if self.status == "error":
                    # 启动阶段失败时执行器已释放；先结束旧会话，再从当前门店重新启动。
                    self._shutdown_executor()
                    self.status = "stopped"
                    self.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
                    return self.start(self.task_ids[self.index:])
                try:
                    detail = self._ensure_safe_start()
                except Exception as exc:
                    detail = type(exc).__name__
                if detail:
                    self.status = "close_failed"
                    self.message = f"重试前未能确认微信安全起点（{detail}）；请手动回到文件传输助手后重试"
                    self._save()
                    return self.state()
                self.profile["miniapp_selector"] = ""
                self._open_current()
                return self.state()
            self.message = "当前状态不需要重试"
            self._save()
            return self.state()

    def refresh(self) -> dict:
        """页面还在加载或空白时重新截图；不点击、不推进步骤。"""
        self._ensure_not_advancing()
        with self._lock:
            self._reject_when_paused()
            if not self.active:
                raise AnnotationError("当前没有进行中的人工标注")
            selector = self.profile.get("miniapp_selector") or ""
            if not selector:
                raise AnnotationError("尚未确定小程序窗口，请先重试打开本店")
            self.message = "正在重新截取当前画面"
            self._save()
            self._activate_wechat()
            try:
                reply = self.executor.screenshot(selector, f"refresh-{len(self.steps) + 1:02d}.png")
            except Exception as exc:
                self.message = f"重新截图失败（{type(exc).__name__}）；请稍后重试"
                self._save()
                return self.state()
            rows = self._ocr(Path(reply["path"]))
            previous = self.status
            self._set_frame(reply, rows)
            self.suggestion = self._suggestion_from_rows(reply, rows)
            self.status = previous if previous in {"ready", "clicked"} else "ready"
            self.message = "画面已刷新；等待加载完成后再看初始判断"
            self._save()
            self._restore_frontmost()
            return self.state()

    def stop(self) -> dict:
        # 先取消并等待“完成本店”的后台推进，避免结束后又打开下一家。
        self._advance_cancel.set()
        self._wait_advance(20)
        with self._lock:
            if self.session_id is None:
                return self.state()
            self._suggest_generation += 1
            message = "标注已结束"
            try:
                if self.frame and self.profile.get("miniapp_selector"):
                    self.executor.close(self.profile, float(self.settings.capture.settle_seconds))
            except Exception as exc:
                message = f"标注已结束，但未能确认关闭小程序（{type(exc).__name__}）"
            self._shutdown_executor()
            self.status = "stopped"
            self.message = message
            self.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.frame = None
            self.suggestion = None
            self.decision = None
            self._resume_status = "ready"
            self._save()
            return self.state()

    def frame_path(self, session_id: str, name: str) -> Path:
        with self._lock:
            if not isinstance(name, str) or not name or "\x00" in name:
                raise FileNotFoundError("截图不存在")
            if self.session_id and session_id == self.session_id and self.work_dir:
                work_dir = Path(self.work_dir).resolve()
            else:
                # 历史会话也可以回看：只允许读取标注会话根目录下的一级子目录。
                root = self._sessions_root().resolve()
                work_dir = (root / str(session_id)).resolve()
                if work_dir.parent != root or not work_dir.is_dir():
                    raise FileNotFoundError("标注会话不存在")
            candidate = (work_dir / name).resolve()
            try:
                candidate.relative_to(work_dir)
            except ValueError:
                raise FileNotFoundError("截图不存在") from None
            if candidate.suffix.lower() != ".png" or not candidate.is_file():
                raise FileNotFoundError("截图不存在")
            return candidate

    # 内部流程 -----------------------------------------------------------------
    @staticmethod
    def _pid_alive(pid: int) -> bool:
        try:
            os.kill(int(pid), 0)
            return True
        except (OSError, ValueError, TypeError):
            return False

    @staticmethod
    def _pid_command(pid: int) -> str:
        try:
            result = subprocess.run(["/bin/ps", "-p", str(int(pid)), "-o", "command="],
                                    capture_output=True, text=True, timeout=5)
        except Exception:
            return ""
        return result.stdout.strip()

    def _cleanup_orphaned_runs(self, output_root: Path) -> None:
        """服务重启后清理仍占着 UiPath 任务槽的旧标注进程。"""
        runs = Path(output_root) / ".annotation-runs"
        cleaned = False
        stale = False
        if runs.is_dir():
            for directory in sorted(runs.iterdir()):
                if not directory.is_dir():
                    continue
                try:
                    info = json.loads((directory / "cli-process.json").read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                pid = info.get("pid")
                running = info.get("status") == "running" or info.get("exit_code") is None
                if not running:
                    continue
                if not isinstance(pid, int) or not self._pid_alive(pid):
                    stale = True
                    continue
                if "uip" not in self._pid_command(pid):
                    stale = True
                    continue
                try:
                    atomic_json(directory / "command.json", {"id": str(uuid.uuid4()), "action": "stop"})
                except Exception:
                    pass
                deadline = time.monotonic() + 6
                while time.monotonic() < deadline and self._pid_alive(pid):
                    time.sleep(0.25)
                if self._pid_alive(pid):
                    try:
                        os.kill(pid, signal.SIGTERM)
                    except OSError:
                        pass
                    deadline = time.monotonic() + 4
                    while time.monotonic() < deadline and self._pid_alive(pid):
                        time.sleep(0.2)
                cleaned = True
        if cleaned or stale:
            cleaned = self._stop_orphan_executors() or cleaned
        if cleaned:
            try:
                self.store.event("已清理上次标注残留的 UiPath 进程", "warning")
            except Exception:
                pass

    def _stop_orphan_executors(self) -> bool:
        """uip 包装进程退出后，UiPath 作业执行器可能仍存活；按项目名清理。"""
        project = Path(getattr(getattr(self.executor, "runner", None), "project_dir", "")).name
        if not project:
            return False
        try:
            result = subprocess.run(["pgrep", "-fl", "UiPath.Executor.NetCore.dll"],
                                    capture_output=True, text=True, timeout=5)
        except Exception:
            return False
        stopped = False
        for line in result.stdout.splitlines():
            pid_text, _, command = line.partition(" ")
            if project not in command:
                continue
            try:
                pid = int(pid_text)
            except ValueError:
                continue
            try:
                os.kill(pid, signal.SIGTERM)
                stopped = True
            except OSError:
                continue
        if stopped:
            time.sleep(1.5)
        return stopped

    def shutdown(self) -> None:
        """服务退出时通知 Main 结束，避免留下占住任务槽的 CLI。"""
        self._advance_cancel.set()
        self._wait_advance(5)
        with self._lock:
            if self.session_id is None:
                return
            try:
                self.executor.request_stop()
            except Exception:
                pass
            self.status = "stopped"
            self._save()

    @staticmethod
    def _frontmost_bundle() -> str | None:
        """快速读取当前前台应用的 bundle id（两次 lsappinfo 调用，约几十毫秒）。"""
        if sys.platform != "darwin":
            return None
        try:
            front = subprocess.run(["/usr/bin/lsappinfo", "front"], capture_output=True,
                                   text=True, timeout=2)
        except Exception:
            return None
        asn = front.stdout.strip().splitlines()[0].strip() if front.stdout.strip() else ""
        if not asn:
            return None
        try:
            info = subprocess.run(["/usr/bin/lsappinfo", "info", "-only", "bundleID", asn],
                                  capture_output=True, text=True, timeout=2)
        except Exception:
            return None
        match = re.search(r'"CFBundleIdentifier"="([^"]+)"', info.stdout)
        return match.group(1) if match else None

    def _activate_wechat(self, wait: float = 0.6) -> None:
        """用 LaunchServices 把微信置前。

        直接调用 NSRunningApplication.activate 或合成点击在 macOS 上可能被前台应用
        抢占策略忽略；open -b 走 LaunchServices，实测可以稳定切到微信。
        微信已经在前台时直接返回，省掉一次 open 与固定等待。
        """
        if self.activator is not None:
            self.activator()
            return
        if sys.platform != "darwin":
            return
        frontmost = self._frontmost_bundle()
        if frontmost == WECHAT_BUNDLE_ID:
            return
        if frontmost:
            # 记下操作前的应用（通常是浏览器），点击完成后恢复前台。
            self._restore_bundle = frontmost
        for command in (["/usr/bin/open", "-b", WECHAT_BUNDLE_ID],
                        ["/usr/bin/open", "-a", "/Applications/WeChat.app"]):
            try:
                result = subprocess.run(command, capture_output=True, timeout=8)
            except Exception:
                continue
            if result.returncode == 0:
                break
        time.sleep(max(0.2, wait))

    def _restore_frontmost(self, wait: float = 0.2) -> None:
        """把点击前的前台应用恢复回来，方便在网页上继续框选。"""
        if self.activator is not None or sys.platform != "darwin":
            return
        bundle = self._restore_bundle
        if not bundle or bundle == WECHAT_BUNDLE_ID:
            return
        if self._frontmost_bundle() == bundle:
            return
        try:
            subprocess.run(["/usr/bin/open", "-b", bundle], capture_output=True,
                           text=True, timeout=8)
        except Exception:
            return
        time.sleep(max(0.1, wait))

    def _open_current(self) -> None:
        task = self.current_task
        if not task:
            self._finish()
            return
        settle = float(self.settings.capture.settle_seconds)
        self.status = "opening"
        self.message = f"正在打开第 {self.index + 1}/{len(self.task_ids)} 家：{task['source_name']}"
        self.frame = None
        self.suggestion = None
        self._frame_rows = None
        self._frame_at = 0.0
        self._save()
        self._activate_wechat()
        try:
            reply = self.executor.open_store(task, self.profile, self.work_dir, settle)
            # 统一成批量采集时的窗口几何，让标注截图与正式执行版面一致。
            reply = self._normalize_window(reply)
            self._set_frame(reply)
            self.status = "ready"
            self.message = "已打开小程序；请框选要点击的位置并确认"
            self._save()
            # OCR 建议放后台，页面先可用，首屏不必等 0.5s。
            self._start_suggestion(reply)
        except Exception as exc:
            self.status = "open_failed"
            self.message = self._describe(exc, "未能打开当前门店的小程序")
            self.frame = None
            self.suggestion = None
            self._frame_rows = None
            self._frame_at = 0.0
            self._save()

    def _normalize_window(self, reply: dict) -> dict:
        """把小程序窗口调整为批量采集使用的尺寸；失败时退回原始打开结果。"""
        selector = self.profile.get("miniapp_selector") or ""
        if not selector:
            return reply
        try:
            from screeninfo import get_monitors
            screens = get_monitors()
            screen = next((item for item in screens if item.is_primary), screens[0])
            self.executor.resize(selector, **production_window_geometry(screen))
            return self.executor.screenshot(selector, "opened-normalized.png")
        except Exception:
            # 窗口调整失败不影响人工标注，只是样本尺寸与生产略有差异。
            if reply.get("path"):
                return reply
            try:
                return self.executor.screenshot(selector, "opened-initial.png")
            except Exception:
                return reply

    def _ensure_safe_start(self) -> str | None:
        """把微信恢复到文件传输助手；selector 可能指向已关闭窗口时做一次重试。"""
        settle = float(self.settings.capture.settle_seconds)
        self._activate_wechat()
        try:
            self.executor.ensure_safe_start(self.profile, settle)
            return None
        except Exception as first:
            if not self.profile.get("miniapp_selector"):
                return type(first).__name__
            self.profile["miniapp_selector"] = ""
            try:
                self.executor.ensure_safe_start(self.profile, settle)
                return None
            except Exception as second:
                return type(second).__name__

    def _close_current(self, prefix: str) -> bool:
        if not self.profile.get("miniapp_selector") or not self.frame:
            self.profile["miniapp_selector"] = ""
            return True
        self.status = "closing"
        self.message = prefix + "；正在关闭小程序并确认返回文件传输助手"
        self._save()
        self._activate_wechat()
        try:
            self.executor.close(self.profile, float(self.settings.capture.settle_seconds))
        except Exception as exc:
            self.status = "close_failed"
            self.message = f"{prefix}；关闭小程序后未确认回到文件传输助手（{type(exc).__name__}）"
            self._save()
            return False
        self.profile["miniapp_selector"] = ""
        return True

    def _advance(self) -> dict:
        self.index += 1
        self.frame = None
        self.suggestion = None
        self._frame_rows = None
        self._frame_at = 0.0
        # 换店后人数选择与隐私勾选状态都从零开始，避免沿用上一家。
        self.people_selected = False
        self.consent_checked = False
        if self.index >= len(self.task_ids):
            self._finish()
            return self.state()
        self._open_current()
        return self.state()

    def _start_advance(self, message: str) -> None:
        self._advance_cancel.clear()
        thread = threading.Thread(target=self._advance_worker, args=(message,),
                                  daemon=True, name="annotation-advance")
        self._advance_thread = thread
        thread.start()

    def _advance_worker(self, message: str) -> None:
        """后台关闭当前小程序并打开下一家；状态仍实时写回 session.json。"""
        try:
            if not self._close_current(message):
                return
            if self._advance_cancel.is_set():
                return
            self._advance()
        except Exception as exc:
            with self._lock:
                self.status = "error"
                self.message = self._describe(exc, "推进下一家门店失败")
                self._save()

    def _wait_advance(self, timeout: float = 30.0) -> None:
        """等待后台推进结束；调用时必须先释放会话锁，避免与后台保存互锁。"""
        thread = self._advance_thread
        if thread is None or thread is threading.current_thread():
            return
        thread.join(timeout)

    def _ensure_not_advancing(self) -> None:
        """后台正在关闭/打开门店时，直接提示稍后，避免把操作打到下一家。"""
        thread = self._advance_thread
        if thread is not None and thread.is_alive():
            raise AnnotationError("正在关闭当前小程序并推进下一家，请稍后再操作")

    def wait_background(self, timeout: float = 30.0) -> None:
        """等待后台的 OCR 建议与门店推进结束（测试、退出流程使用）。"""
        for thread in (self._suggestion_thread, self._advance_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout)

    def _finish(self) -> None:
        self._shutdown_executor()
        self.status = "finished"
        self.message = "全部门店已处理；样本已写入规则样本目录"
        self.finished_at = datetime.now().astimezone().isoformat(timespec="seconds")
        self.frame = None
        self.suggestion = None
        self._frame_rows = None
        self._frame_at = 0.0
        self._save()

    def _shutdown_executor(self) -> None:
        try:
            self.executor.request_stop()
        except Exception:
            pass
        try:
            self.executor.reap()
        except Exception:
            pass
        try:
            self.executor.detach()
        except Exception:
            pass

    def _frame_info(self, reply: dict) -> dict:
        path = Path(reply["path"]).expanduser().resolve()
        try:
            relative = path.relative_to(Path(self.work_dir).resolve()).as_posix()
        except ValueError:
            raise AnnotationError("截图不在标注会话目录内") from None
        width, height = int(reply["width"]), int(reply["height"])
        window_width, window_height = float(reply["window_width"]), float(reply["window_height"])
        return {
            "name": relative,
            "url": f"/api/annotation/frame?session={self.session_id}&name={relative}",
            "image_width": width,
            "image_height": height,
            "window_width": window_width,
            "window_height": window_height,
            "scale_x": round(window_width / width, 6),
            "scale_y": round(window_height / height, 6),
            "captured_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        }

    def _file_info(self, reply: dict, role: str) -> dict:
        path = Path(reply["path"]).expanduser().resolve()
        relative = path.relative_to(Path(self.work_dir).resolve()).as_posix()
        return {"role": role, "name": relative, "path": str(path),
                "url": f"/api/annotation/frame?session={self.session_id}&name={relative}",
                "width": int(reply["width"]), "height": int(reply["height"])}

    def _set_frame(self, reply: dict, rows: list[dict] | None = None) -> None:
        """记录当前画面及其 OCR 结果，并让仍在跑的旧建议自动作废。"""
        self.frame = self._frame_info(reply)
        self._frame_at = time.monotonic()
        self._frame_rows = rows
        self._suggest_generation += 1
        self.suggestion = None
        # 画面已切换，旧画面的决策建议不能再用于归因。
        self.decision = None

    def _suggestion_from_rows(self, reply: dict, rows: list[dict]) -> dict | None:
        suggestion = suggest_target(rows, float(reply["width"]), float(reply["height"]))
        return suggestion or {"category": None, "page_state": "unknown",
                              "note": "未识别到明确目标，请手动画框"}

    def _start_suggestion(self, reply: dict) -> None:
        """本地 OCR 建议放到后台线程，先让页面可操作。"""
        if not self.ocr_reader:
            self.suggestion = None
            return
        generation = self._suggest_generation
        frame_name = self.frame["name"] if self.frame else ""
        path = Path(reply["path"])
        width, height = float(reply["width"]), float(reply["height"])

        def run() -> None:
            try:
                rows = self.ocr_reader(path)
            except Exception as exc:
                rows = []
                suggestion = {"category": None, "page_state": "unknown",
                              "error": f"本地 OCR 建议不可用：{type(exc).__name__}"}
            else:
                suggestion = suggest_target(rows, width, height) or {
                    "category": None, "page_state": "unknown",
                    "note": "未识别到明确目标，请手动画框"}
            with self._lock:
                # 画面已经切换时丢弃这次结果，避免把上一张图的建议显示到新画面上。
                if generation != self._suggest_generation or (self.frame or {}).get("name") != frame_name:
                    return
                self._frame_rows = rows
                self.suggestion = suggestion
                self._save()

        thread = threading.Thread(target=run, daemon=True, name="annotation-suggestion")
        self._suggestion_thread = thread
        thread.start()

    def _reusable_frame(self) -> dict | None:
        """当前画面仍新鲜、文件仍在时返回可复用的截图信息。"""
        if not self.frame or not self.work_dir:
            return None
        if time.monotonic() - self._frame_at > FRAME_REUSE_SECONDS:
            return None
        path = Path(self.work_dir) / self.frame["name"]
        if not path.is_file():
            return None
        return {"path": str(path),
                "width": self.frame["image_width"], "height": self.frame["image_height"],
                "window_width": self.frame["window_width"],
                "window_height": self.frame["window_height"]}

    def _copy_before(self, index: int, reply: dict) -> dict:
        """把复用的当前画面另存为本步的 before，保证样本文件独立可追溯。"""
        directory = Path(self.work_dir) / "evidence"
        directory.mkdir(parents=True, exist_ok=True)
        target = directory / f"step-{index:02d}-before.png"
        source = Path(reply["path"])
        # 上一次点击失败后，当前画面本身就可能已经是这一步的 before；
        # 此时复制到同一个文件会抛 SameFileError，直接复用即可。
        if source.resolve() != target.resolve():
            shutil.copy2(source, target)
        return {**reply, "path": str(target)}

    def _before_frame(self, index: int, selector: str, frame: dict) -> tuple[dict | None, list[dict]]:
        """取点击前截图：优先复用当前画面，窗口尺寸变化时刷新画面并让用户重新框选。"""
        reused = self._reusable_frame()
        if reused is not None:
            before = self._copy_before(index, reused)
            rows = self._frame_rows
            return before, rows if rows is not None else self._ocr(Path(before["path"]))
        before = self.executor.screenshot(selector, f"step-{index:02d}-before.png")
        if ((int(before["width"]), int(before["height"])) != (frame["image_width"], frame["image_height"])
                or abs(float(before["window_width"]) - frame["window_width"]) > 0.5
                or abs(float(before["window_height"]) - frame["window_height"]) > 0.5):
            self._set_frame(before)
            self.status = "ready"
            self.message = "窗口尺寸已变化，截图已刷新，请重新框选"
            self._save()
            self._start_suggestion(before)
            return None, []
        return before, self._ocr(Path(before["path"]))

    def _ocr(self, path: Path) -> list[dict]:
        if not self.ocr_reader:
            return []
        try:
            return self.ocr_reader(path)
        except Exception:
            return []

    @staticmethod
    def _text_changed(before_rows: list[dict], after_rows: list[dict]) -> bool:
        def signature(rows):
            return {re.sub(r"\s+", "", str(row.get("text", ""))) for row in rows
                    if isinstance(row, dict) and str(row.get("text", "")).strip()}
        try:
            return signature(before_rows) != signature(after_rows)
        except Exception:
            return False

    def _save_step(self, step: dict) -> None:
        if not self.work_dir:
            return
        directory = Path(self.work_dir) / "steps"
        directory.mkdir(parents=True, exist_ok=True)
        atomic_json(directory / f"{int(step.get('index', 0)):03d}.json", step)

    def _save(self) -> None:
        if not self.work_dir:
            return
        snapshot = self.state()
        snapshot.update({
            "status": self.status,
            "message": self.message,
            "steps": self.steps,
        })
        atomic_json(Path(self.work_dir) / "session.json", snapshot)

    @staticmethod
    def _describe(exc: Exception, prefix: str) -> str:
        detail = str(exc).strip()
        name = type(exc).__name__
        if not detail:
            return f"{prefix}（{name}）"
        if name in {"NeedsReview", "AnnotationError", "ValueError"}:
            return f"{prefix}：{detail[:300]}"
        return f"{prefix}（{name}）：{detail[:300]}"
