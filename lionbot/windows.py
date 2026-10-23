from __future__ import annotations

import sys
from dataclasses import dataclass


@dataclass(frozen=True)
class WindowInfo:
    hwnd: int
    title: str
    class_name: str
    size: tuple[int, int]
    owner: int

    def describe(self):
        return (f"HWND=0x{self.hwnd:X}，标题={self.title!r}，类名={self.class_name!r}，"
                f"客户区={self.size[0]}x{self.size[1]}，所属窗口=0x{self.owner:X}")


TERMINAL_CLASSES = frozenset({"consolewindowclass", "cascadia_hosting_window_class",
                              "pseudoconsolewindow", "virtualconsoleclass"})


def select_game_window(gui, keyword, frame_size=None):
    """筛选真正的游戏窗口；无法唯一确定时列出证据，不随意选择第一个。"""
    if not isinstance(keyword, str) or not keyword.strip():
        raise ValueError("window_title 不能为空，请填写游戏标题栏文字")
    matches = []

    def collect(hwnd, _):
        try:
            if not gui.IsWindowVisible(hwnd):
                return
            title = gui.GetWindowText(hwnd)
            if keyword not in title:
                return
            _, _, width, height = gui.GetClientRect(hwnd)
            matches.append(WindowInfo(hwnd, title, gui.GetClassName(hwnd), (width, height),
                                      gui.GetWindow(hwnd, 4)))  # GW_OWNER
        except Exception:
            pass  # 枚举期间窗口可能已经关闭。

    gui.EnumWindows(collect, None)
    candidates = [item for item in matches if item.class_name.casefold() not in TERMINAL_CLASSES
                  and min(item.size) > 0]
    native = [item for item in candidates if item.class_name.casefold() == "maplestoryclass"]
    if native:
        candidates = native
    if frame_size is not None:
        same_size = [item for item in candidates if item.size == tuple(frame_size)]
        if same_size:
            candidates = same_size
    handles = {item.hwnd for item in candidates}

    def has_matched_owner(item):
        owner, visited = item.owner, {item.hwnd}
        while owner and owner not in visited:
            if owner in handles:
                return True
            visited.add(owner)
            try:
                owner = gui.GetWindow(owner, 4)
            except Exception:
                return False
        return False

    # 同一个游戏的有主窗口/附属窗口，不计作两个独立客户端。
    candidates = [item for item in candidates if not has_matched_owner(item)]
    exact = [item for item in candidates if item.title == keyword]
    if exact:
        candidates = exact
    if len(candidates) != 1:
        details = "\n".join(f"  {item.describe()}" + ("（终端窗口，已排除）" if item.class_name.casefold()
                           in TERMINAL_CLASSES else "") for item in matches) or "  没有标题匹配的可见窗口"
        raise RuntimeError(f"无法唯一确定游戏窗口：{len(matches)} 个标题包含 {keyword!r}，"
                           f"筛选后剩余 {len(candidates)} 个候选。\n{details}\n"
                           "请将 window_title 设置为游戏标题栏的完整文字；同名游戏请仅保留一个。")
    return candidates[0]


def require_windows() -> None:
    if sys.platform != "win32":
        raise RuntimeError("截图、校准与按键执行需要 Windows；离线图像识别和单元测试可跨平台运行")


class GameWindow:
    def __init__(self, keyword: str, *, hwnd=None, frame_size=None):
        require_windows()
        import ctypes

        try:
            ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
        except (AttributeError, OSError):
            ctypes.windll.user32.SetProcessDPIAware()
        import mss
        import win32gui

        self.gui = win32gui
        if hwnd is None:
            selected = select_game_window(win32gui, keyword, frame_size)
            self.hwnd = selected.hwnd
            print(f"已绑定游戏窗口：{selected.describe()}", flush=True)
        else:
            # 测谎线程复用主循环已选定的 HWND，MSS 仍在各自线程内创建。
            if not win32gui.IsWindow(hwnd) or keyword not in win32gui.GetWindowText(hwnd):
                raise RuntimeError(f"原游戏窗口 HWND=0x{hwnd:X} 已关闭或标题改变，请重新启动程序")
            self.hwnd = hwnd
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

    def capture_region(self, roi, frame_size):
        """简单定点只截取小地图；客户区尺寸变化时不沿用旧坐标。"""
        import numpy as np

        if not self.gui.IsWindow(self.hwnd) or self.gui.IsIconic(self.hwnd):
            raise RuntimeError("游戏窗口已关闭或最小化")
        _, _, width, height = self.gui.GetClientRect(self.hwnd)
        if (width, height) != tuple(frame_size):
            raise ValueError("客户区大小已改变，请恢复原尺寸或重新运行 python -m lionbot.simple --setup")
        x, y, w, h = roi
        if min(x, y) < 0 or min(w, h) <= 0 or x + w > width or y + h > height:
            raise ValueError("小地图框超出客户区，请重新运行 python -m lionbot.simple --setup")
        left, top = self.gui.ClientToScreen(self.hwnd, (0, 0))
        return np.array(self.screen.grab(dict(left=left + x, top=top + y, width=w, height=h)))[:, :, :3].copy()


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
