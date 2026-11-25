"""采集执行器与传输层共享的路径、原子写入与窗口几何辅助。

这些函数被执行器和传输层共同使用（原子写命令/回执、校验截图落点、推导运行期
目录、标定批次窗口几何），集中在这里可让传输层不反向依赖整个执行器。
"""
from __future__ import annotations

import json
import uuid
from pathlib import Path


def _atomic_write_json(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        temporary.chmod(0o600)
        json.dump(value, stream, ensure_ascii=False, separators=(",", ":"))
        stream.flush()
        import os

        os.fsync(stream.fileno())
    temporary.replace(path)
    path.chmod(0o600)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def _runtime_root(output_root: str | Path) -> Path:
    """运行期目录（.uipath-runs / .uipath-staging）放在产物目录同级的隐藏目录。

    产物目录可能直接就是“合格”归档目录，运行期目录不能混进门店目录里。
    """
    return Path(output_root).expanduser().resolve().parent


def _staging_root(output_root: str | Path) -> Path:
    """批次统一的暂存根；每家店在下面按 task/attempt 各建一层。"""
    return _runtime_root(output_root) / ".uipath-staging"


def production_window_geometry(screen) -> dict:
    """批量采集统一使用的小程序窗口几何（贴屏幕左侧、全高）。

    人工标注也套用同一尺寸，保证样本截图与正式执行时的版面一致。
    """
    return {"width": 240, "height": max(300, int(screen.height) - 100),
            "x": int(screen.x), "y": int(screen.y) + 35}
