"""失败原因码契约测试。"""

import pytest

from menu_capture import failures


@pytest.mark.parametrize("stage,message,code", [
    ("uipath_timeout", "", "uipath_timeout"),
    ("needs_review", "未找到本轮图片", "image_not_found"),
    ("needs_review", "页面需要登录、手机号或协议确认；自动授权未开启", "login_required"),
    ("needs_review", "达到准备动作上限", "prepare_limit"),
    ("needs_review", "完全看不懂的状态", "unknown"),
])
def test_classify_uses_stage_then_message(stage, message, code):
    assert failures.classify(stage, message) == code


def test_failure_groups_split_retry_store_and_manual():
    assert failures.describe("network_error")["group"] == failures.RETRYABLE
    assert failures.describe("store_closed")["group"] == failures.STORE_UNAVAILABLE
    assert failures.describe("payment_interface")["group"] == failures.MANUAL
    assert failures.describe("not_a_code")["code"] == "unknown"

    assert failures.describe("agent_unqualified")["group"] == failures.STORE_UNAVAILABLE
    assert failures.describe("agent_needs_review")["group"] == failures.MANUAL
    assert failures.describe("agent_runtime")["group"] == failures.MANUAL
