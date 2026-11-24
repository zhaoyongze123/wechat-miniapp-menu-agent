"""以可直接执行的参数列表定位本机 UiPath CLI。"""
from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path


def cli_entry() -> list[str]:
    executable = shutil.which("uip")
    if not executable:
        return []
    path = Path(executable)
    if sys.platform != "win32" or path.suffix.casefold() not in {".cmd", ".bat"}:
        return [executable]

    # npm 在 Windows 写入 cmd shim。直接调用它指向的 Node 入口，避免
    # cmd.exe 重新解释带空格、百分号或其他特殊字符的本地素材路径。
    package = path.parent / "node_modules" / "@uipath" / "cli"
    try:
        bins = json.loads((package / "package.json").read_text(encoding="utf-8"))["bin"]
        script = (package / (bins["uip"] if isinstance(bins, dict) else bins)).resolve()
        script.relative_to(package.resolve())
    except (OSError, ValueError, KeyError, TypeError):
        return []
    node = shutil.which("node")
    return [node, str(script)] if node and script.is_file() else []
