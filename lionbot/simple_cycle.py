"""朝右 D/A 定点攻击，按总攻击次数定期左移、朝左补打并回位。"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, replace
from pathlib import Path

from .brain import Action


@dataclass(frozen=True)
class SimpleConfig:
    window_title: str = "冒险岛怀旧服"
    minimap_roi: tuple[int, int, int, int] | None = None
    frame_size: tuple[int, int] | None = None
    station_x: float | None = None
    x_tolerance: float = .008
    right_attack_shots: int = 3
    attack_interval: float = .9
    group_pause: float = 3.
    recovery_shots: int = 3
    alt_interval: float = 190.
    attack_hold: float = .08
    face_hold: float = .08
    d_shots_before_a: int = 6
    a_attack_shots: int = 4
    move_attack_min: int = 15
    move_attack_max: int = 25
    move_left_taps_min: int = 5
    move_left_taps_max: int = 15
    move_attack_shots: int = 3
    move_tap_hold: float = .04
    move_tap_gap: float = .06

    def validate(self, ready=False):
        for name in ("right_attack_shots", "recovery_shots", "d_shots_before_a", "a_attack_shots",
                     "move_attack_min", "move_attack_max", "move_left_taps_min", "move_left_taps_max", "move_attack_shots"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} 必须为正整数")
        for name in ("station_x", "x_tolerance", "attack_interval", "group_pause",
                     "alt_interval", "attack_hold", "face_hold", "move_tap_hold", "move_tap_gap"):
            value = getattr(self, name)
            if value is None and name == "station_x":
                continue
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} 必须为有限数值")
        if self.station_x is not None and not 0 <= self.station_x <= 1:
            raise ValueError("station_x 必须为小地图归一化 X，范围 0～1")
        if not 0 < self.x_tolerance <= .05:
            raise ValueError("x_tolerance 必须为 (0, 0.05]")
        if self.attack_interval < .9 or not 0 < self.attack_hold < self.attack_interval:
            raise ValueError("单次攻击间隔至少 0.9 秒，attack_hold 必须小于攻击间隔")
        if self.group_pause < 0 or self.alt_interval <= 0 or not .03 <= self.face_hold <= .2:
            raise ValueError("group_pause 不能为负，alt_interval 必须为正，face_hold 为 0.03～0.2 秒")
        if self.move_attack_min > self.move_attack_max or self.move_left_taps_min > self.move_left_taps_max:
            raise ValueError("定期移动的攻击次数、左键短按次数必须先小后大")
        if not .005 <= self.move_tap_hold <= .05 or not .01 <= self.move_tap_gap <= 1:
            raise ValueError("左键短按持续时间为 0.005～0.05 秒，松开后的间隔为 0.01～1 秒")
        for name, length in (("minimap_roi", 4), ("frame_size", 2)):
            value = getattr(self, name)
            if value is not None and (len(value) != length or any(type(v) is not int for v in value)):
                raise ValueError(f"{name} 必须包含 {length} 个整数")
        if self.frame_size is not None and min(self.frame_size) <= 0:
            raise ValueError("frame_size 的宽高必须为正")
        if self.minimap_roi is not None:
            x, y, w, h = self.minimap_roi
            if min(x, y) < 0 or min(w, h) <= 0:
                raise ValueError("minimap_roi 必须是 [x, y, 正宽度, 正高度]")
            if self.frame_size is not None and (x + w > self.frame_size[0] or y + h > self.frame_size[1]):
                raise ValueError("小地图框超出客户区")
        if ready and (self.minimap_roi is None or self.frame_size is None or self.station_x is None):
            raise ValueError("请先运行 python -m lionbot.simple --setup，记录小地图和新的挂机 X")


def load_simple(path: Path) -> SimpleConfig:
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    # 兼容旧简单配置，忽略已删除的聊天和左右轮次参数。
    for name in ("left_attack_shots", "groups_per_direction", "alternate_every_rounds", "alternate_shots",
                 "pet_chat_min_seconds", "pet_chat_max_seconds", "pet_commands"):
        raw.pop(name, None)
    for name in ("minimap_roi", "frame_size"):
        if raw.get(name) is not None:
            raw[name] = tuple(raw[name])
    result = SimpleConfig(**raw)
    result.validate()
    return result


class SimpleCycle:
    def __init__(self, config: SimpleConfig, rng=None):
        self.c = config
        self.rng = rng if rng is not None else random.Random()
        self.busy_until = self.group_ready_at = 0.
        self.next_alt = None
        self.d_since_a = self.a_shots = 0
        self.reset_cycle()

    def reset_cycle(self):
        self.facing = ""
        self.d_groups = self.shots = 0
        self.recovery = ""
        self.recovery_done = 0
        self.return_error = None
        self.move_phase = ""
        self.move_taps = self.move_taps_done = self.move_shots_done = 0
        self.attacks_since_move = 0
        self.next_move_at = self.rng.randint(self.c.move_attack_min, self.c.move_attack_max)

    def set_station(self, x):
        updated = replace(self.c, station_x=x)
        updated.validate()
        self.c = updated
        self.reset_cycle()
        self.group_ready_at = 0.
        # 新站位取消旧移动流程并重新抽取移动阈值，保留施法冷却、Alt 和 D/A 进度。

    def suspend(self):
        self.facing = ""
        self.return_error = None

    @property
    def outer_tolerance(self):
        return max(self.c.x_tolerance, 2 / self.c.minimap_roi[2])

    @property
    def inner_tolerance(self):
        return max(self.c.x_tolerance / 2, 1 / self.c.minimap_roi[2])

    def decide(self, x, now, focused=True):
        if not focused or x is None:
            self.suspend()
            return Action("wait", reason="失焦或黄点丢失，松键等待")
        if self.c.station_x is None:
            return Action("wait", reason="尚未设置挂机 X")
        if now < self.busy_until:
            return Action("wait", reason="等待本次施法或转身完成")
        if self.move_phase:
            return self.move_action(x)
        if self.attacks_since_move >= self.next_move_at:
            self.move_phase = "left"
            self.move_taps = self.rng.randint(self.c.move_left_taps_min, self.c.move_left_taps_max)
            self.move_taps_done = self.move_shots_done = 0
            self.recovery, self.recovery_done, self.return_error = "", 0, None
            return Action("wait", reason=f"累计 D/A {self.attacks_since_move} 次，开始短按左方向键 {self.move_taps} 次")
        if self.next_alt is None or now >= self.next_alt:
            return Action("alt", reason="启动或定时 Alt")
        error = x - self.c.station_x
        if not self.recovery and abs(error) > self.outer_tolerance + 1e-9:
            self.recovery, self.recovery_done = "attack", 0
        if self.recovery == "attack":
            if self.facing != "right":
                return Action("face", "right", "偏离后朝右清理")
            return self.attack_action(recovering=True)
        if self.recovery == "return":
            crossed = self.return_error is not None and error * self.return_error <= 0
            arrived = (abs(error) <= self.inner_tolerance
                       or (crossed and abs(error) <= self.outer_tolerance))
            if arrived:
                self.recovery, self.return_error = "", None
                return Action("wait", reason="已回到挂机 X，松键后继续原攻击循环")
            self.return_error = error
            return Action("walk", "left" if error > 0 else "right", "向右清理后，连续步行回位")
        if now < self.group_ready_at:
            return Action("wait", reason=f"组间等待 {self.c.group_pause:g} 秒")
        if self.facing != "right":
            return Action("face", "right", "朝右攻击")
        return self.attack_action()

    def move_action(self, x):
        if self.move_phase == "left":
            return Action("nudge", "left", f"定期左移：短按 {self.move_taps_done + 1}/{self.move_taps}")
        if self.move_phase == "attack":
            if self.facing != "left":
                return Action("face", "left", "定期左移后面朝左侧")
            return Action("attack", "left", f"定期左侧 D：{self.move_shots_done + 1}/{self.c.move_attack_shots}")
        error = x - self.c.station_x
        crossed = self.return_error is not None and error * self.return_error <= 0
        if abs(error) <= self.inner_tolerance or (crossed and abs(error) <= self.outer_tolerance):
            self.move_phase, self.return_error = "", None
            self.attacks_since_move = 0
            self.next_move_at = self.rng.randint(self.c.move_attack_min, self.c.move_attack_max)
            return Action("wait", reason=f"定期移动完成，已回挂机点；下次累计 D/A {self.next_move_at} 次再移动")
        self.return_error = error
        return Action("walk", "left" if error > 0 else "right", "定期左侧打完，连续步行回挂机 X")

    def attack_action(self, recovering=False):
        if self.d_since_a >= self.c.d_shots_before_a:
            return Action("attack_a", "right", f"向右 A：{self.a_shots + 1}/{self.c.a_attack_shots}，完成后恢复 D")
        label = (f"偏离后向右 D：{self.recovery_done + 1}/{self.c.recovery_shots}" if recovering
                 else f"向右 D：{self.shots + 1}/{self.c.right_attack_shots}")
        return Action("attack", "right", label)

    def executed(self, action, at):
        if action.kind == "alt":
            self.next_alt = at + self.c.alt_interval
            self.busy_until = at + .1
        elif action.kind == "face":
            self.facing = action.direction
            self.busy_until = at + self.c.face_hold + .02
        elif action.kind == "walk":
            self.facing = action.direction
        elif action.kind == "nudge" and self.move_phase == "left":
            self.facing = action.direction
            self.move_taps_done += 1
            self.busy_until = at + self.c.move_tap_hold + self.c.move_tap_gap
            if self.move_taps_done >= self.move_taps:
                self.move_phase = "attack"
        elif action.kind == "attack_a":
            self.busy_until = at + self.c.attack_interval
            self.attacks_since_move += 1
            self.a_shots += 1
            if self.a_shots >= self.c.a_attack_shots:
                self.a_shots = self.d_since_a = 0
                self.group_ready_at = at + self.c.attack_hold + self.c.group_pause
        elif action.kind == "attack":
            self.busy_until = at + self.c.attack_interval
            if self.move_phase == "attack":
                self.move_shots_done += 1
                if self.move_shots_done >= self.c.move_attack_shots:
                    self.move_phase = "return"
                    self.return_error = None
                return
            self.attacks_since_move += 1
            self.d_since_a += 1
            if self.recovery == "attack":
                self.recovery_done += 1
                if self.recovery_done >= self.c.recovery_shots:
                    self.recovery = "return"
                    self.recovery_done = 0
                return
            self.shots += 1
            if self.shots < self.c.right_attack_shots:
                return
            self.shots = 0
            self.group_ready_at = at + self.c.attack_hold + self.c.group_pause
            self.d_groups += 1
