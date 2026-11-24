"""失败原因码契约测试。"""

import pytest

from menu_capture.core import failures


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


def test_every_known_reason_maps_to_a_bucket():
    """所有已定义的原因码都必须归入 6 个处置桶之一，避免前端筛选漏项。"""
    from menu_capture.materials import REJECTION_REASONS

    runner_codes = {"runtime", "capture", "invalid_action", "invalid_terminal", "disallowed_action",
                    "no_observation", "insufficient_observation", "no_terminal", "manual_rejected",
                    "wechat_window_frame"}
    known = set(failures.FAILURE_CODES) | set(REJECTION_REASONS) | runner_codes
    missing = sorted(known - set(failures.CODE_BUCKETS))
    assert not missing, f"以下原因码没有登记处置桶: {missing}"
    assert set(failures.BUCKETS) == {failures.MATERIAL, failures.STORE, failures.TECHNICAL,
                                     failures.AUTH, failures.ENVIRONMENT, failures.OTHER}
    for code in known:
        assert failures.bucket_of(code) in failures.BUCKETS


def test_unknown_reason_falls_back_to_other():
    """没有结构化 code 或出现新文案时兜底为“其他·未分类”，保证全覆盖。"""
    assert failures.bucket_of("", "", "") == failures.OTHER
    assert failures.bucket_of("no_such_code") == failures.OTHER
    assert failures.bucket_of("", "", "模型没有给出原因") == failures.OTHER
    assert failures.bucket_of("", "", "需要同意隐私协议") == failures.AUTH
    assert failures.bucket_of("", "", "二维码无法识别") == failures.MATERIAL
    assert failures.bucket_of("", "", "门店已打烊") == failures.STORE
