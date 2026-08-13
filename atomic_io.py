#!/usr/bin/env python3
"""原子写文件 + Windows 单实例锁。"""
from __future__ import annotations

import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path | str, text: str, encoding: str = "utf-8") -> None:
    """写入临时文件后 os.replace，避免半写入配置。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix=path.name + ".",
        suffix=".tmp",
        dir=str(path.parent),
    )
    try:
        with os.fdopen(fd, "w", encoding=encoding, newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_name, path)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path | str, data: Any, *, indent: int = 2) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=indent)
    if not text.endswith("\n"):
        text += "\n"
    atomic_write_text(path, text)


def signal_activation(path: Path | str) -> str:
    """通知已运行的 GUI 恢复窗口；返回本次请求的唯一标记。"""
    token = f"{os.getpid()}:{time.time_ns()}"
    atomic_write_text(path, token)
    return token


def read_activation_token(path: Path | str) -> str:
    """读取单实例唤醒标记；文件不存在或瞬时不可读时返回空串。"""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


class SingleInstanceLock:
    """
    简易单实例锁。
    Windows: msvcrt.locking 文件锁
    其它: fcntl.flock
    """

    def __init__(self, lock_path: Path | str):
        self.lock_path = Path(lock_path)
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None

    def acquire(self, blocking: bool = False) -> bool:
        if self._fh is not None:
            return True
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                fh.write("0")
                fh.flush()
                fh.seek(0)
                flags = msvcrt.LK_NBLCK if not blocking else msvcrt.LK_LOCK
                try:
                    msvcrt.locking(fh.fileno(), flags, 1)
                except OSError:
                    fh.close()
                    return False
            else:
                import fcntl

                flags = fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB)
                try:
                    fcntl.flock(fh.fileno(), flags)
                except OSError:
                    fh.close()
                    return False
            fh.seek(0)
            fh.truncate()
            fh.write(str(os.getpid()))
            fh.flush()
            self._fh = fh
            return True
        except Exception:
            try:
                fh.close()
            except Exception:
                pass
            return False

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt

                self._fh.seek(0)
                try:
                    msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
                except OSError:
                    pass
            else:
                import fcntl

                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        finally:
            try:
                self._fh.close()
            except Exception:
                pass
            self._fh = None

    def __enter__(self) -> "SingleInstanceLock":
        if not self.acquire(blocking=False):
            raise RuntimeError(f"无法获取单实例锁: {self.lock_path}")
        return self

    def __exit__(self, *args: Any) -> None:
        self.release()
