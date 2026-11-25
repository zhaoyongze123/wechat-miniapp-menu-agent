"""采集执行器的错误类型：执行、传输与调用方共享的异常契约。

单独成模块，避免使用方为了拿到异常类型而导入整个执行器，也让执行器与传输层
之间保持单向依赖。
"""
from __future__ import annotations

from pathlib import Path


class RobotProtocolError(RuntimeError):
    pass


class RobotTimeout(TimeoutError):
    pass


class RobotCancelled(RuntimeError):
    pass


class RobotCommandError(RuntimeError):
    pass


class NeedsReview(RuntimeError):
    """需要人工或重试的失败；code 决定分流，未给出时由失败文字归类。"""

    code = ""

    def __init__(self, message: str, code: str = ""):
        super().__init__(message)
        self.code = code or type(self).code


class AgentUnqualified(NeedsReview):
    """Agent 给出了带理由的明确不合格终态；evidence 是判定时看到的画面。"""

    def __init__(self, message: str, code: str = "", evidence: str | Path = ""):
        super().__init__(message, code)
        self.evidence = str(evidence or "")


class QrEntryUnavailable(NeedsReview):
    """本轮图片已核实，微信预览和图片菜单均未提供二维码识别入口。"""

    code = "startup_qr_entry_unavailable"


class RunnerPaused(RuntimeError):
    pass


class RunnerTerminated(RuntimeError):
    pass
