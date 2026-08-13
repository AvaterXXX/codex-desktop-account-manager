#!/usr/bin/env python3
"""Windows 任务栏身份与已有窗口唤醒。"""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes

APP_TITLE = "Codex 账户管理器"
APP_USER_MODEL_ID = "Hongjun.CodexAccountManager.1"


def is_current_process_foreground() -> bool:
    """前台窗口是否属于本进程；主窗口和本进程模态对话框都算前台。"""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowThreadProcessId.argtypes = [
            wintypes.HWND,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return False
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        return pid.value == os.getpid()
    except (AttributeError, OSError):
        return False


def set_process_app_identity() -> bool:
    """让任务栏把本程序识别成独立应用，而不是 python/pythonw。"""
    if os.name != "nt":
        return False
    try:
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        fn = shell32.SetCurrentProcessExplicitAppUserModelID
        fn.argtypes = [wintypes.LPCWSTR]
        fn.restype = ctypes.c_long
        return fn(APP_USER_MODEL_ID) == 0
    except (AttributeError, OSError):
        return False


def activate_window_by_title(title: str = APP_TITLE) -> bool:
    """从第二次启动进程立即恢复并前置已有主窗口/活动对话框。"""
    if os.name != "nt":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        user32.GetLastActivePopup.argtypes = [wintypes.HWND]
        user32.GetLastActivePopup.restype = wintypes.HWND
        user32.IsWindow.argtypes = [wintypes.HWND]
        user32.IsWindow.restype = wintypes.BOOL
        user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        user32.ShowWindow.restype = wintypes.BOOL
        user32.BringWindowToTop.argtypes = [wintypes.HWND]
        user32.BringWindowToTop.restype = wintypes.BOOL
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.restype = wintypes.BOOL

        hwnd = user32.FindWindowW(None, title)
        if not hwnd:
            return False
        user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        popup = user32.GetLastActivePopup(hwnd)
        target = popup if popup and user32.IsWindow(popup) else hwnd
        user32.ShowWindow(target, 9)
        user32.BringWindowToTop(target)
        user32.SetForegroundWindow(target)
        return True
    except (AttributeError, OSError):
        return False
