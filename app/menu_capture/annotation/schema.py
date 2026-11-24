"""人工标注类别与错误契约。"""
from __future__ import annotations

CATEGORIES = ("miniapp_home", "store_select", "people_grid", "consent_checkbox",
              "close_icon", "system_notification", "resting_menu", "other")


PAGE_STATES = ("miniapp_home", "store_select", "people_select", "consent", "ad_or_overlay",
               "system_permission", "menu", "loading", "unknown")


CATEGORY_PAGE_STATE = {
    "miniapp_home": "miniapp_home",
    "store_select": "store_select",
    "people_grid": "people_select",
    "consent_checkbox": "consent",
    "close_icon": "ad_or_overlay",
    "system_notification": "system_permission",
    "resting_menu": "menu",
    "other": "unknown",
}


class AnnotationError(ValueError):
    pass
