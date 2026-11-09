"""失败原因码：把笼统的“需检查”拆成可重试、门店不可采、需人工三类。"""

from __future__ import annotations

RETRYABLE = "retryable"
STORE_UNAVAILABLE = "store_unavailable"
MANUAL = "manual"

# code -> (分组, 中文说明)。分组决定失败后是否值得直接重跑。
FAILURE_CODES: dict[str, tuple[str, str]] = {
    "network_error": (RETRYABLE, "网络异常"),
    "miniapp_timeout": (RETRYABLE, "小程序加载超时"),
    "page_blank": (RETRYABLE, "页面白屏"),
    "menu_empty": (RETRYABLE, "菜单为空或未加载"),
    "page_stuck": (RETRYABLE, "页面长时间无变化"),
    "store_closed": (STORE_UNAVAILABLE, "门店已打烊或休息"),
    "menu_unavailable": (STORE_UNAVAILABLE, "门店暂无菜单"),
    "qr_expired": (STORE_UNAVAILABLE, "二维码已失效"),
    "miniapp_unavailable": (STORE_UNAVAILABLE, "小程序到期或未绑定"),
    "scan_order_unsupported": (STORE_UNAVAILABLE, "门店不支持扫码点餐"),
    "qr_external_web": (STORE_UNAVAILABLE, "二维码指向外部网页而非小程序"),
    "phone_only": (STORE_UNAVAILABLE, "需在手机上打开"),
    "customer_service": (STORE_UNAVAILABLE, "客服或长按识别页"),
    "authorization_unavailable": (MANUAL, "授权流程不可用"),
    # 以下属于启动阶段的技术失败：只能说明本轮没能打开小程序，不能据此判定门店不合格。
    "startup_preview_unavailable": (MANUAL, "启动阶段未找到识别入口"),
    "startup_no_miniapp": (MANUAL, "启动阶段未观察到小程序窗口"),
    "login_required": (MANUAL, "需要登录或协议确认"),
    "payment_interface": (MANUAL, "交易或支付界面"),
    "wechat_context_lost": (MANUAL, "微信聊天上下文丢失"),
    "image_not_found": (MANUAL, "未找到本轮图片消息"),
    "preview_icon_missing": (MANUAL, "未找到识别入口"),
    "miniapp_window": (MANUAL, "未确定小程序窗口"),
    "ui_locator": (MANUAL, "界面元素定位失败"),
    "people_count": (MANUAL, "人数页未确认"),
    "no_page_change": (MANUAL, "连续操作无变化"),
    "prepare_limit": (MANUAL, "准备动作超限"),
    "page_limit": (MANUAL, "达到翻页上限"),
    "notification_popup": (MANUAL, "系统通知弹窗未消失"),
    "source_changed": (MANUAL, "素材文件变化或缺失"),
    "output_publish_error": (MANUAL, "结果发布失败"),
    "uipath_timeout": (MANUAL, "UiPath 命令超时"),
    "uipath_error": (MANUAL, "UiPath 执行失败"),
    "terminated": (MANUAL, "用户已终止"),
    "paused": (MANUAL, "已暂停"),
    "unknown": (MANUAL, "未分类失败"),
}

_STAGE_CODES = {
    "authorization_unavailable": "authorization_unavailable",
    "uipath_timeout": "uipath_timeout",
    "uipath_error": "uipath_error",
    "output_publish_error": "output_publish_error",
    "resume_check": "wechat_context_lost",
    "terminated": "terminated",
    "paused": "paused",
    "blocked_not_safe": "wechat_context_lost",
}

# 只在没有显式 code 时兜底；显式 code 始终优先于文字推断。
_MESSAGE_CODES = (
    ("未找到本轮图片", "image_not_found"),
    ("文件传输助手", "wechat_context_lost"),
    ("识别二维码", "preview_icon_missing"),
    ("预览", "preview_icon_missing"),
    ("小程序窗口", "miniapp_window"),
    ("人数", "people_count"),
    ("来源文件", "source_changed"),
    ("素材版本", "source_changed"),
    ("支付", "payment_interface"),
    ("交易", "payment_interface"),
    ("登录", "login_required"),
    ("协议", "login_required"),
    ("max_pages", "page_limit"),
    ("截图边界", "page_limit"),
    ("准备动作上限", "prepare_limit"),
    ("系统通知", "notification_popup"),
    ("无明显变化", "no_page_change"),
    ("未观察到明确变化", "no_page_change"),
    ("关闭小程序", "ui_locator"),
)


def classify(stage: str, message: str = "") -> str:
    """没有显式 code 时，按阶段和失败文字给出稳定的原因码。"""
    if stage in _STAGE_CODES:
        return _STAGE_CODES[stage]
    text = message or ""
    for keyword, code in _MESSAGE_CODES:
        if keyword in text:
            return code
    return "unknown"


def describe(code: str) -> dict:
    group, label = FAILURE_CODES.get(code or "", FAILURE_CODES["unknown"])
    return {"code": code if code in FAILURE_CODES else "unknown", "group": group, "label": label}
