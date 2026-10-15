from __future__ import annotations

import sys


def require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("截图、校准与按键执行需要 Windows；离线图像识别和单元测试可跨平台运行")


class GameWindow:
    def __init__(self, keyword: str):
        require_windows()
        import ctypes

        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
        import mss
        import win32gui

        self.gui = win32gui
        windows = []

        def collect(hwnd, _):
            if win32gui.IsWindowVisible(hwnd) and keyword in win32gui.GetWindowText(hwnd):
                windows.append(hwnd)

        win32gui.EnumWindows(collect, None)
        if len(windows) != 1:
            raise RuntimeError(f"匹配到 {len(windows)} 个游戏窗口，请检查 window_title 并只保留一个匹配窗口")
        self.hwnd = windows[0]
        self.screen = mss.mss()

    def focused(self) -> bool:
        return (self.gui.IsWindow(self.hwnd) and not self.gui.IsIconic(self.hwnd)
                and self.gui.GetForegroundWindow() == self.hwnd)

    def capture(self):
        import numpy as np

        if not self.gui.IsWindow(self.hwnd) or self.gui.IsIconic(self.hwnd):
            raise RuntimeError("游戏窗口已关闭或最小化")
        _, _, width, height = self.gui.GetClientRect(self.hwnd)
        left, top = self.gui.ClientToScreen(self.hwnd, (0, 0))
        return np.array(self.screen.grab(dict(left=left, top=top, width=width, height=height)))[:, :, :3].copy()

    def close(self) -> None:
        self.screen.close()


class Keyboard:
    def __init__(self):
        require_windows()
        from pynput.keyboard import Controller, Key

        self.controller = Controller()
        self.special = {name: getattr(Key, name) for name in ("left", "right", "up", "down", "space", "alt")}

    def press(self, key: str) -> None:
        self.controller.press(self.special.get(key, key))

    def release(self, key: str) -> None:
        self.controller.release(self.special.get(key, key))
