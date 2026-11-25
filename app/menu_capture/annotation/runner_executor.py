"""人工标注的执行器适配层。

把 UiPathRunner 的现有能力包成标注会话可替换的执行器；
从 annotation 模块拆出以降低单文件职责，annotation 仍按原名字再导出。
"""
from __future__ import annotations

from pathlib import Path


class AnnotationError(ValueError):
    pass


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
        runner.transport = runner.make_transport(Path(run_directory), Path(output_root))
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

    def prepare_profile(self, profile: dict) -> None:
        self.runner.setup_native_windows_profile(profile)

    def ensure_safe_start(self, profile: dict, settle_seconds: float) -> None:
        self.runner._ensure_file_assistant(profile, self.work_dir, settle_seconds)

    def open_store(self, task: dict, profile: dict, work_dir: Path, settle_seconds: float) -> dict:
        if self.runner.assignment_source is not None and not self.runner.assignment_source(task["id"]):
            raise AnnotationError("该门店分配已收回，请返回工作台重新选择")
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
