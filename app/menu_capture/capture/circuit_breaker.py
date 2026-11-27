"""批量采集连续环境故障熔断器。

从 uipath_runner.py 抽离为独立可测组件，并统一通过标准 logging 输出结构化熔断日志。
"""

from __future__ import annotations

import logging

logger = logging.getLogger(__name__)

# 微信输入环境故障码集合：连续触发达到阈值时自动暂停熔断，不归为门店质量问题。
WECHAT_INPUT_ENV_CODES = frozenset({"wechat_input_env_error", "wechat_context_lost", "wechat_window_frame"})
MAX_CONSECUTIVE_INPUT_ENV_FAILURES = 3


class ConsecutiveFailureBreaker:
    """追踪连续环境故障次数，达到阈值时触发熔断，避免批量采集在异常环境下空转。"""

    def __init__(
        self,
        *,
        codes: frozenset[str] = WECHAT_INPUT_ENV_CODES,
        threshold: int = MAX_CONSECUTIVE_INPUT_ENV_FAILURES,
    ) -> None:
        self.codes = codes
        self.threshold = max(1, int(threshold))
        self.streak = 0

    def record(self, *, succeeded: bool, failure_code: str = "", task_id: str = "") -> bool:
        """记录单店结果；返回 True 表示已达到熔断阈值。"""
        if not succeeded and failure_code in self.codes:
            self.streak += 1
            if self.streak >= self.threshold:
                logger.warning(
                    "wechat_input_circuit_breaker_tripped streak=%d threshold=%d failure_code=%s task_id=%s",
                    self.streak, self.threshold, failure_code, task_id,
                )
                return True
            return False
        self.streak = 0
        return False
