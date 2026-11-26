"""旧版守护进程的兼容启动入口。

v0.2.x 安装目录中的 ``Launch.ps1`` 以 ``-m menu_capture.server`` 启动采集端。该守护脚本已在
Velopack 迁移中删除，但仍在运行的旧实例可能继续调用此模块；保留同名转发入口，业务实现仍然
只有 ``menu_capture.api.server`` 一份。
"""
from __future__ import annotations

from menu_capture.api.server import main

if __name__ == "__main__":
    main()
