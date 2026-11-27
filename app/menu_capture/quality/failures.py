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
    "agent_unqualified": (STORE_UNAVAILABLE, "GUI Agent 判定不合格"),
    "agent_needs_review": (MANUAL, "GUI Agent 待人工检查"),
    "agent_runtime": (MANUAL, "GUI Agent 运行或交接失败"),
    # 以下属于启动阶段的技术失败：只能说明本轮没能打开小程序，不能据此判定门店不合格。
    "startup_preview_unavailable": (MANUAL, "启动阶段未找到识别入口"),
    "startup_no_miniapp": (MANUAL, "启动阶段未观察到小程序窗口"),
    "startup_qr_entry_unavailable": (STORE_UNAVAILABLE, "微信未提供二维码识别入口"),
    "login_required": (MANUAL, "需要登录或协议确认"),
    "payment_interface": (MANUAL, "交易或支付界面"),
    "wechat_context_lost": (MANUAL, "微信聊天上下文丢失"),
    "wechat_input_env_error": (RETRYABLE, "微信输入环境异常"),
    "wechat_window_frame": (RETRYABLE, "微信窗口版面异常"),
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

# 处置桶：把失败码、不合格原因和 Agent 执行器专用码收敛成前端可筛选的固定分类。
# 新增原因码必须同步登记到 CODE_BUCKETS，tests/test_failure_codes.py 会拦截遗漏。
MATERIAL = "material"
STORE = "store_unavailable"
TECHNICAL = "technical"
AUTH = "auth"
ENVIRONMENT = "environment"
OTHER = "other"

BUCKETS: dict[str, str] = {
    MATERIAL: "素材问题",
    STORE: "门店不可采",
    TECHNICAL: "技术重试",
    AUTH: "授权交互",
    ENVIRONMENT: "环境异常",
    OTHER: "其他·未分类",
}

CODE_BUCKETS: dict[str, str] = {
    # 素材问题：二维码、图片与素材入口本身不可用或选错。
    "qr_expired": MATERIAL,
    "qr_external_web": MATERIAL,
    "startup_preview_unavailable": MATERIAL,
    "startup_qr_entry_unavailable": MATERIAL,
    "image_not_found": MATERIAL,
    "preview_icon_missing": MATERIAL,
    "source_changed": MATERIAL,
    "blur": MATERIAL,
    "invalid_qr": MATERIAL,
    "store_mismatch": MATERIAL,
    "not_ordering": MATERIAL,
    "duplicate": MATERIAL,
    # 门店不可采：门店/小程序侧的业务性阻断，重跑无意义。
    "store_closed": STORE,
    "menu_unavailable": STORE,
    "miniapp_unavailable": STORE,
    "scan_order_unsupported": STORE,
    "phone_only": STORE,
    "customer_service": STORE,
    "agent_unqualified": STORE,
    # 技术重试：页面、定位、模型输出或截图流程失败，修好后可重跑。
    "miniapp_timeout": TECHNICAL,
    "page_blank": TECHNICAL,
    "menu_empty": TECHNICAL,
    "page_stuck": TECHNICAL,
    "agent_runtime": TECHNICAL,
    "startup_no_miniapp": TECHNICAL,
    "miniapp_window": TECHNICAL,
    "ui_locator": TECHNICAL,
    "people_count": TECHNICAL,
    "no_page_change": TECHNICAL,
    "prepare_limit": TECHNICAL,
    "page_limit": TECHNICAL,
    "output_publish_error": TECHNICAL,
    "uipath_timeout": TECHNICAL,
    "uipath_error": TECHNICAL,
    "runtime": TECHNICAL,
    "capture": TECHNICAL,
    "invalid_action": TECHNICAL,
    "invalid_terminal": TECHNICAL,
    "disallowed_action": TECHNICAL,
    "no_observation": TECHNICAL,
    "insufficient_observation": TECHNICAL,
    "no_terminal": TECHNICAL,
    # 授权交互：隐私/登录/通知/支付等需要人工或策略处理的交互阻断。
    "authorization_unavailable": AUTH,
    "login_required": AUTH,
    "payment_interface": AUTH,
    "notification_popup": AUTH,
    # 环境异常：网络、微信上下文、显示缩放等本机或链路环境问题。
    "network_error": ENVIRONMENT,
    "network_unavailable": ENVIRONMENT,
    "wechat_context_lost": ENVIRONMENT,
    "wechat_input_env_error": ENVIRONMENT,
    "wechat_window_frame": ENVIRONMENT,
    # 其他·未分类：兜底与人工介入类，保证任何原因都有归属。
    "agent_needs_review": OTHER,
    "terminated": OTHER,
    "paused": OTHER,
    "manual_rejected": OTHER,
    "other": OTHER,
    "unknown": OTHER,
}

# 没有结构化 code 时，按失败文字给桶兜底；顺序即优先级。
_BUCKET_KEYWORDS = (
    ("隐私", AUTH), ("授权", AUTH), ("登录", AUTH), ("验证码", AUTH), ("通知", AUTH),
    ("协议", AUTH), ("手机号", AUTH), ("支付", AUTH), ("交易", AUTH), ("弹窗", AUTH),
    ("弹层", AUTH), ("会员", AUTH), ("优惠券", AUTH), ("权限", AUTH), ("同意", AUTH),
    ("二维码", MATERIAL), ("素材", MATERIAL), ("图片", MATERIAL), ("识别入口", MATERIAL),
    ("门店已", STORE), ("打烊", STORE), ("未开通", STORE), ("暂无菜单", STORE),
    ("未设置点餐", STORE), ("到期", STORE), ("超出点餐距离", STORE), ("不支持扫码", STORE),
    ("网络", ENVIRONMENT), ("窗口", ENVIRONMENT), ("缩放", ENVIRONMENT), ("显示器", ENVIRONMENT),
    ("输入框", ENVIRONMENT),
    ("截图", TECHNICAL), ("滚动", TECHNICAL), ("步数", TECHNICAL), ("定位", TECHNICAL),
    ("加载", TECHNICAL), ("空白", TECHNICAL), ("白屏", TECHNICAL), ("点击", TECHNICAL),
    ("无变化", TECHNICAL), ("未进入", TECHNICAL), ("未找到", TECHNICAL),
)

# 这些码只代表“模型没走完/暂时未知”，message 往往能给出更具体的归属。
_GENERIC_CODES = frozenset({"agent_needs_review", "unknown", "other"})


def bucket_of(code: str = "", stage: str = "", message: str = "") -> str:
    """把任意失败信息归入固定处置桶；无法判断时兜底为 OTHER，保证全覆盖。"""
    for candidate in (code, stage):
        if candidate and candidate not in _GENERIC_CODES:
            bucket = CODE_BUCKETS.get(candidate)
            if bucket:
                return bucket
    text = message or ""
    for keyword, bucket in _BUCKET_KEYWORDS:
        if keyword in text:
            return bucket
    for candidate in (code, stage):
        bucket = CODE_BUCKETS.get(candidate or "")
        if bucket:
            return bucket
    return OTHER


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
    ("输入框残留", "wechat_input_env_error"),
    ("未进入微信输入框", "wechat_input_env_error"),
    ("未附着到微信输入框", "wechat_input_env_error"),
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


def describe(code: str, stage: str = "", message: str = "") -> dict:
    group, label = FAILURE_CODES.get(code or "", FAILURE_CODES["unknown"])
    bucket = bucket_of(code, stage, message)
    return {"code": code if code in FAILURE_CODES else "unknown", "group": group,
            "bucket": bucket, "bucket_label": BUCKETS[bucket], "label": label}
