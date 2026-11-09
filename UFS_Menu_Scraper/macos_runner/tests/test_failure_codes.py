"""失败原因码与页面状态识别的契约测试。"""

import pytest

from menu_capture import failures
from menu_capture import page_state


def rows(*texts):
    return [{"text": text} for text in texts]


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


@pytest.mark.parametrize("text,state", [
    ("网络有点异常，请稍后重试", "network_error"),
    ("本店已休息", "store_closed"),
    ("暂无菜品", "menu_unavailable"),
    ("该功能需要在手机上打开", "phone_only"),
    ("长按识别二维码", "phone_only"),
    ("请联系在线客服处理", "customer_service"),
])
def test_page_state_detects_known_pages(text, state):
    assert page_state.detect(rows(text)) == state


def test_closed_notice_with_visible_menu_keeps_collecting():
    visible = rows("本店已休息", "宫保鸡丁", "¥28", "米饭", "¥3")
    assert page_state.detect(visible) == "store_closed"
    assert page_state.has_menu_evidence(visible) is True
    assert page_state.has_menu_evidence(rows("本店已休息")) is False
