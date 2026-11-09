"""准备阶段的页面状态识别：区分“等一会儿会好”和“这店采不了”。"""

from __future__ import annotations

import re
import unicodedata

# 顺序有意义：先判定谁能解释当前页面，再判定网络类可重试异常。
_PATTERNS: tuple[tuple[str, re.Pattern], ...] = (
    ("phone_only", re.compile(r"请在手机上|需要在手机上打开|该功能需要.{0,6}手机|仅支持手机|请用手机扫码|长按识别")),
    # 二维码里的地址无法被小程序识别时，微信会报“无效的 URI: URI 方案无效”，
    # 属于二维码本身无效，人工也无法绕过。
    ("qr_expired", re.compile(r"无效的\s*(?:URI|URL)|(?:URI|URL)\s*方案无效|二维码(?:已)?(?:失效|过期|无效|停用|不存在|已损坏)|二维码.{0,4}(?:失效|过期|无效)|二维码识别失败")),
    ("miniapp_unavailable", re.compile(r"小程序(?:已)?(?:到期|过期|失效|停止服务|下线|暂停服务)|未绑定小程序|购买的小程序")),
    # 门店要求到店后才能扫码/点餐，线上无法看到菜单；这类由商家设置决定，人工也无法绕过。
    ("scan_order_unsupported", re.compile(
        r"(?:暂不支持|不支持).{0,8}(?:扫码|自助|在线|线上|手机|微信)?点餐|请联系服务员|"
        r"请到店(?:再|后)?扫码点餐|到店(?:后|再)?扫码点餐|请(?:您)?到店(?:再|后)?点餐|"
        r"需到店(?:扫码)?点餐|堂食.{0,6}到店点餐|"
        r"扫码点餐服务.{0,6}(?:暂时)?关闭|点餐服务.{0,6}(?:暂时)?关闭|暂不提供.{0,6}扫码点餐")),
    ("customer_service", re.compile(r"在线客服|联系客服|客服中心|联系商家")),
    # 自提/点餐渠道在该城市未开通，换城市或重试都无法看到菜单，属于商家侧配置。
    ("pickup_unavailable", re.compile(r"所选城市暂未开通|城市暂未开通此业务|您检索的城市暂未开通|暂未开通自提")),
    # 门店没有配置点餐/装修数据属于商家侧配置缺失，人工无法绕过。
    ("menu_not_configured", re.compile(r"未设置点餐装修数据|未配置点餐|未设置点餐|未开通点餐|未配置菜单|未设置菜单|未上传菜单")),
    ("menu_unavailable", re.compile(r"暂无菜品|暂无菜单|暂无此类餐饮|暂无商品|暂未上架|商家暂未|没有可点餐品")),
    ("store_closed", re.compile(r"已打烊|已休息|休息中|暂停营业|暂停接单|非营业时间|营业已结束|今日已闭店")),
    ("network_error", re.compile(r"网络异常|网络有点异常|网络错误|网络连接|请稍后重试|加载失败|请求失败|系统繁忙|服务器.{0,4}开小差")),
    ("loading", re.compile(r"正在加载|加载中|请稍候|正在进入")),
)

# 这些状态由门店侧配置决定，人工无法绕过；判定后直接记不合格，不再进入待处理队列。
REJECT_REASONS: dict[str, str] = {
    "store_closed": "store_closed",
    "qr_expired": "invalid_qr",
    "miniapp_unavailable": "miniapp_unavailable",
    "scan_order_unsupported": "scan_order_unsupported",
    "qr_external_web": "qr_external_web",
    "menu_not_configured": "menu_unavailable",
    # 该城市的自提/点餐业务未开通，换城市重试都无法进入菜单。
    "pickup_unavailable": "menu_unavailable",
    # 要求改用手机打开/长按识别的页面，桌面小程序无论如何重试都进不去。
    "phone_only": "phone_only",
    # 连续多轮仍提示网络异常、重试也进不去菜单，按门店不可采处理，不再反复占用人工。
    "network_error": "network_unavailable",
    # 小程序长时间一直加载不出可读内容（菜单页始终不出现），重试也无效，按不可采处理。
    "loading_timeout": "menu_unavailable",
    # 需要验证码/扫码授权才能进入；手机主人可以进，自动化流程永远进不去，按不可采处理。
    "authorization_unavailable": "authorization_unavailable",
}

_PRICE = re.compile(r"[¥￥]\s*\d|\d+(?:\.\d+)?\s*元")
# 公众号文章页的强特征；只要命中一个且页面没有价格锚点，就可以确认不是点餐菜单。
_ARTICLE_HINT = re.compile(r"阅读原文|关注公众号|公众号\s*[·|｜:：]|发布于\s*\d{4}|写留言|留言精选|原创\s*[|｜]")
# 弱特征需要同时出现两类（阅读量、互动栏）才判定，避免把普通菜单页尾的“分享”误判。
_ARTICLE_WEAK = (
    re.compile(r"阅读\s*\d+(?:\.\d+)?\s*万?"),
    re.compile(r"在看|点赞|分享到|收藏\s*\|"),
)


def page_text(rows) -> str:
    return " ".join(unicodedata.normalize("NFKC", str(row.get("text", ""))) for row in rows or [])


def is_article_page(rows) -> bool:
    """轻量判断：公众号文章/图文页，属于二维码指向了非点餐内容。"""
    text = page_text(rows)
    if not text or _PRICE.search(text):
        return False
    if _ARTICLE_HINT.search(text):
        return True
    return sum(1 for pattern in _ARTICLE_WEAK if pattern.search(text)) >= 2


def has_menu_evidence(rows) -> bool:
    """有价格锚点才说明菜单真的可见；打烊提示不影响继续滚菜单。"""
    return bool(_PRICE.search(page_text(rows)))


def detect(rows) -> str | None:
    """返回当前页面状态码；None 表示未命中已知状态。"""
    text = page_text(rows)
    if not text:
        return None
    has_price = bool(_PRICE.search(text))
    # 扫码进入公众号文章/图文页，说明二维码不是点餐码，人工也无法让它变成菜单。
    if not has_price and is_article_page(rows):
        return "qr_external_web"
    for state, pattern in _PATTERNS:
        if not pattern.search(text):
            continue
        # 正常菜单页脚常写“如有疑问请及时联系商家”，有价格锚点说明菜单可见，
        # 不能把页脚文案当成客服页。
        if state == "customer_service" and has_price:
            continue
        return state
    return None
