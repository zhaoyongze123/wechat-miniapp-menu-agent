"""让一个本地服务独占本机桌面任务。"""
from __future__ import annotations

import errno
import os
from contextlib import contextmanager
from pathlib import Path


class InstanceLockedError(RuntimeError):
    pass


@contextmanager
def instance_lock(path: Path):
    # 先锁再创建任务服务。追加模式避免第二个实例在锁定前截断锁文件。
    with path.open("a+b") as stream:
        if os.name == "nt":
            import msvcrt

            stream.seek(0)
            try:
                # Windows 允许锁定超出文件末尾的字节，空文件无需先写入。
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                raise InstanceLockedError("桌面已被另一个实例锁定") from exc
            try:
                yield
            finally:
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            try:
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise InstanceLockedError("桌面已被另一个实例锁定") from exc
            yield  # 文件描述符关闭时释放 flock。
