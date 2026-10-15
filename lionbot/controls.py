from __future__ import annotations

import time
import threading

from .brain import Action
from .config import Config


class InputCancelled(Exception):
    pass


class InputExecutor:
    """连续步行保持方向；其他动作松键执行，后台守卫处理失焦和超时。"""

    def __init__(self, keyboard, config: Config, active=lambda: True,
                 clock=time.monotonic, sleep=time.sleep):
        self.keyboard = keyboard
        self.c = config
        self.active = active
        self.clock = clock
        self.sleep = sleep
        self.held: set[str] = set()
        self.lock = threading.RLock()
        self.hold_deadline: float | None = None

    def check(self) -> None:
        if not self.active():
            raise InputCancelled("游戏失焦、暂停或退出")

    def press(self, key: str) -> float:
        with self.lock:
            self.check()
            if key not in self.held:
                self.held.add(key)
                self.keyboard.press(key)
            return self.clock()

    def release(self, key: str) -> None:
        with self.lock:
            try:
                self.keyboard.release(key)
            finally:
                self.held.discard(key)

    def stop(self) -> None:
        with self.lock:
            self.hold_deadline = None
            first_error = None
            for key in tuple(self.held):
                try:
                    self.release(key)
                except Exception as error:
                    first_error = first_error or error
            if first_error is not None:
                raise first_error

    def guard_once(self) -> None:
        with self.lock:
            if self.held and (not self.active() or (self.hold_deadline is not None
                                                  and self.clock() >= self.hold_deadline)):
                self.stop()

    def start_guard(self, stopped: threading.Event) -> threading.Thread:
        def watch():
            while not stopped.wait(.02):
                self.guard_once()
        thread = threading.Thread(target=watch, name="input-guard", daemon=True)
        thread.start()
        return thread

    def delay(self, seconds: float) -> None:
        end = self.clock() + seconds
        while self.clock() < end:
            self.check()
            self.sleep(max(0.0, min(0.01, end - self.clock())))

    def execute(self, action: Action) -> float:
        if action.kind != "walk" or self.held != {action.direction}:
            self.stop()
        keep_walking = False
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
            if action.kind == "walk":
                self.check()
                with self.lock:
                    self.hold_deadline = self.clock() + .25
                keep_walking = True
            return pressed_at
        finally:
            if not keep_walking:
                self.stop()
