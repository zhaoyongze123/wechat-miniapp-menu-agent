"""旧版守护进程的兼容启动入口。

v0.2.x 安装目录中的 ``Launch.ps1`` 以 ``-m menu_capture.server`` 启动采集端。升级时守护进程
仍在内存里执行旧脚本，若新版把它删除，升级后工作台会以 “No module named menu_capture.server”
启动失败。因此这里保留同名转发入口，业务实现仍然只有 ``menu_capture.api.server`` 一份。
"""
from __future__ import annotations

from menu_capture.api.server import main

if __name__ == "__main__":
    main()
