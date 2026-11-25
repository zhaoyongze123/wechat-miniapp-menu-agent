"""旧版守护进程兼容入口的回归测试。

v0.2.x 的启动器使用 ``-m menu_capture.server``；升级到新版后守护进程仍会沿用该参数，
必须保证模块存在且真正转发到唯一实现 ``menu_capture.api.server``。
"""
from __future__ import annotations


def test_legacy_server_module_forwards_to_api_server():
    from menu_capture import server as legacy
    from menu_capture.api import server as api_server

    assert legacy.main is api_server.main
