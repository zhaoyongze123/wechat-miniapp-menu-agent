"""批量采集连续故障熔断器。

从 uipath_runner.py 抽离为独立可测组件，并统一通过标准 logging 输出结构化熔断日志。
覆盖两类成批异常：
1. 微信输入环境故障（wechat_input_env_error 等）：连续 3 家即熔断；
2. 通用运行/启动/同原因重复故障（如连续出现“未找到图片上方的发送文本气泡”）：连续 5 家即熔断。
"""

from __future__ import annotations

import logging
import re

logger = logging.getLogger(__name__)

# 微信输入环境故障码集合：连续触发达到阈值时自动暂停熔断，不归为门店质量问题。
WECHAT_INPUT_ENV_CODES = frozenset({"wechat_input_env_error", "wechat_context_lost", "wechat_window_frame"})
MAX_CONSECUTIVE_INPUT_ENV_FAILURES = 3
MAX_CONSECUTIVE_BATCH_FAILURES = 5

_DIGITS_RE = re.compile(r"\d+")


def _normalize_reason(reason: str) -> str:
    cleaned = " ".join((reason or "").strip().split())
    if not cleaned:
        return ""
    return _DIGITS_RE.sub("#", cleaned)[:48]


class ConsecutiveFailureBreaker:
    """追踪连续环境/运行/同原因故障次数，达到阈值时触发熔断，避免批量采集在异常环境下空转。"""

    def __init__(
        self,
        *,
        codes: frozenset[str] = WECHAT_INPUT_ENV_CODES,
        threshold: int = MAX_CONSECUTIVE_INPUT_ENV_FAILURES,
        batch_threshold: int = MAX_CONSECUTIVE_BATCH_FAILURES,
    ) -> None:
        self.codes = codes
        self.threshold = max(1, int(threshold))
        self.batch_threshold = max(1, int(batch_threshold))
        self.streak = 0
        self.runtime_streak = 0
        self.same_reason_streak = 0
        self._last_reason_key = ""
        self.trip_kind = ""
        self.last_reason = ""
        self.last_code = ""

    def record(
        self,
        *,
        succeeded: bool,
        failure_code: str = "",
        failure_reason: str = "",
        business_rejected: bool = False,
        task_id: str = "",
    ) -> bool:
        """记录单店结果；返回 True 表示已达到熔断阈值。"""
        if succeeded:
            self.streak = 0
            self.runtime_streak = 0
            self.same_reason_streak = 0
            self._last_reason_key = ""
            self.trip_kind = ""
            return False

        code = (failure_code or "").strip()
        reason = (failure_reason or "").strip()
        self.last_code = code
        self.last_reason = reason

        # 1. 微信输入环境故障快速熔断（默认连续 3 家）
        if code in self.codes:
            self.streak += 1
        else:
            self.streak = 0

        # 2. 任意运行/启动层异常码连续熔断（默认连续 5 家）
        if code:
            self.runtime_streak += 1
        else:
            self.runtime_streak = 0

        # 3. 非业务明确不合格的同原因连续失败熔断（默认连续 5 家）
        reason_key = "" if business_rejected else _normalize_reason(reason)
        if reason_key and reason_key == self._last_reason_key:
            self.same_reason_streak += 1
        elif reason_key:
            self._last_reason_key = reason_key
            self.same_reason_streak = 1
        else:
            self._last_reason_key = ""
            self.same_reason_streak = 0

        if self.streak >= self.threshold:
            self.trip_kind = "wechat_input_env_circuit_breaker"
            logger.warning(
                "wechat_input_circuit_breaker_tripped streak=%d threshold=%d failure_code=%s task_id=%s",
                self.streak, self.threshold, code, task_id,
            )
            return True

        if self.runtime_streak >= self.batch_threshold:
            self.trip_kind = "batch_runtime_circuit_breaker"
            logger.warning(
                "batch_runtime_circuit_breaker_tripped streak=%d threshold=%d failure_code=%s task_id=%s",
                self.runtime_streak, self.batch_threshold, code, task_id,
            )
            return True

        if self.same_reason_streak >= self.batch_threshold:
            self.trip_kind = "batch_repeated_failure_circuit_breaker"
            logger.warning(
                "batch_repeated_failure_circuit_breaker_tripped streak=%d threshold=%d reason=%s task_id=%s",
                self.same_reason_streak, self.batch_threshold, reason[:80], task_id,
            )
            return True

        return False

    def trip_message(self) -> str:
        """返回面向控制台与日志的中文熔断暂停说明。"""
        if self.trip_kind == "wechat_input_env_circuit_breaker":
            return (
                f"连续 {self.streak} 家门店触发微信输入环境故障，"
                "已自动暂停批量采集；请检查微信窗口、输入法与显示缩放后恢复"
            )
        detail = f"（{self.last_reason[:60]}）" if self.last_reason else (f"（{self.last_code}）" if self.last_code else "")
        count = max(self.runtime_streak, self.same_reason_streak, self.streak)
        return f"连续 {count} 家门店出现同类异常{detail}，已自动暂停批量采集；请排查环境后恢复"
