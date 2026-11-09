"""当前用户的本机服务托管；只托管 Web 服务，不自动启动采集任务。"""
from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

from .config import ROOT

LABEL = "com.yipu.menu-capture"


def install() -> None:
    if sys.platform != "darwin":
        raise SystemExit("此服务托管入口适用于 macOS")
    directory = Path.home() / "Library/LaunchAgents"
    directory.mkdir(parents=True, exist_ok=True)
    ROOT.joinpath("data").mkdir(exist_ok=True)
    spec = {
        "Label": LABEL,
        "ProgramArguments": [str(ROOT / ".venv/bin/python"), "-m", "menu_capture.server"],
        "WorkingDirectory": str(ROOT),
        "EnvironmentVariables": {"PATH": str(Path.home() / ".dotnet") + ":/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin",
                                 "DOTNET_ROOT": str(Path.home() / ".dotnet")},
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "LimitLoadToSessionType": "Aqua",
        "StandardOutPath": "/dev/null",
        "StandardErrorPath": "/dev/null",
    }
    path = directory / (LABEL + ".plist")
    path.write_bytes(plistlib.dumps(spec))
    path.chmod(0o600)
    domain = f"gui/{os.getuid()}"
    target = domain + "/" + LABEL
    loaded = subprocess.run(["launchctl", "print", target], capture_output=True, check=False)
    if loaded.returncode != 0:
        subprocess.run(["launchctl", "bootstrap", domain, str(path)], check=True)
    else:
        # 不使用 -k：运行中的服务/采集不会被重复启动入口强制重启。
        subprocess.run(["launchctl", "kickstart", target], check=True)
    print("本机采集服务已托管：http://127.0.0.1:8765；异常退出后自动恢复，任务不会自动重放。")


if __name__ == "__main__":
    install()
