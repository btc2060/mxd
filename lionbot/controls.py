from __future__ import annotations

import time

from .brain import Action
from .config import Config


class InputCancelled(Exception):
    pass


class InputExecutor:
    """短动作执行器。暂停/失焦在每次按键前和等待中检查，finally 松开所有键。"""

    def __init__(self, keyboard, config: Config, active=lambda: True,
                 clock=time.monotonic, sleep=time.sleep):
        self.keyboard = keyboard
        self.c = config
        self.active = active
        self.clock = clock
        self.sleep = sleep
        self.held: set[str] = set()

    def check(self) -> None:
        if not self.active():
            raise InputCancelled("游戏失焦、暂停或退出")

    def press(self, key: str) -> float:
        self.check()
        self.held.add(key)
        self.keyboard.press(key)
        return self.clock()

    def release(self, key: str) -> None:
        try:
            self.keyboard.release(key)
        finally:
            self.held.discard(key)

    def stop(self) -> None:
        for key in tuple(self.held):
            self.release(key)

    def delay(self, seconds: float) -> None:
        end = self.clock() + seconds
        while self.clock() < end:
            self.check()
            self.sleep(max(0.0, min(0.01, end - self.clock())))

    def execute(self, action: Action) -> float:
        self.stop()
        pressed_at = self.clock()
        try:
            self.check()
            if action.kind == "attack":
                pressed_at = self.press("d")
                self.delay(self.c.attack_hold)
            elif action.kind == "alt":
                pressed_at = self.press("alt")
                self.delay(0.10)
            elif action.kind in ("walk", "face"):
                pressed_at = self.press(action.direction)
                self.delay(0.025 if action.kind == "face" else 0.06)
            elif action.kind in ("teleport", "top_teleport"):
                self.press(action.direction)
                self.delay(0.04)
                pressed_at = self.press("f")
                self.delay(0.07)
            elif action.kind == "grab":
                self.press("up")
                self.delay(0.04)
                pressed_at = self.press("space")
                self.delay(0.10)
            elif action.kind == "climb":
                pressed_at = self.press("up")
                self.delay(0.07)
            elif action.kind == "climb_exit":
                pressed_at = self.press("space")
                self.delay(0.10)
            elif action.kind == "drop_walk":
                pressed_at = self.press(action.direction)
                self.delay(0.08)
            elif action.kind not in ("wait", "scan"):
                raise ValueError(f"未知动作：{action.kind}")
            return pressed_at
        finally:
            self.stop()
