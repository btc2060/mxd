"""只朝右 D 攻击、随机宠物说话，仅用黄点 X 回位。"""
from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, replace
from pathlib import Path

from .brain import Action


PET_COMMANDS = ("坐", "不要", "不行", "不许做", "笨蛋", "傻瓜", "讨厌", "爱你", "嘘嘘",
                "说", "说话", "说吧", "叫", "趴下", "站", "站起来", "起来")


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
    pet_chat_min_seconds: float = 10.
    pet_chat_max_seconds: float = 30.
    pet_commands: tuple[str, ...] = PET_COMMANDS

    def validate(self, ready=False):
        for name in ("right_attack_shots", "recovery_shots"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} 必须为正整数")
        for name in ("station_x", "x_tolerance", "attack_interval", "group_pause",
                     "alt_interval", "attack_hold", "face_hold", "pet_chat_min_seconds", "pet_chat_max_seconds"):
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
        if not 0 < self.pet_chat_min_seconds <= self.pet_chat_max_seconds:
            raise ValueError("宠物说话间隔必须为正数，且最小值不大于最大值")
        if (not isinstance(self.pet_commands, (list, tuple)) or not self.pet_commands
                or any(not isinstance(word, str) or not word.strip() or len(word) > 32
                       or any(ord(ch) < 32 for ch in word) for word in self.pet_commands)):
            raise ValueError("pet_commands 必须为非空词语列表，每句最多 32 字且不能含换行/控制字符")
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
    # 已有简单模式配置直接可用，移除的左侧/A 循环参数不再生效。
    for name in ("left_attack_shots", "groups_per_direction", "alternate_every_rounds", "alternate_shots"):
        raw.pop(name, None)
    for name in ("minimap_roi", "frame_size"):
        if raw.get(name) is not None:
            raw[name] = tuple(raw[name])
    if isinstance(raw.get("pet_commands"), list):
        raw["pet_commands"] = tuple(raw["pet_commands"])
    result = SimpleConfig(**raw)
    result.validate()
    return result


class SimpleCycle:
    def __init__(self, config: SimpleConfig, rng=None):
        self.c = config
        self.rng = rng if rng is not None else random.Random()
        self.busy_until = self.group_ready_at = 0.
        self.next_alt = None
        self.next_chat = None
        self.reset_cycle()

    def reset_cycle(self):
        self.facing = ""
        self.d_groups = self.shots = 0
        self.recovery = ""
        self.recovery_done = 0
        self.return_error = None

    def set_station(self, x):
        updated = replace(self.c, station_x=x)
        updated.validate()
        self.c = updated
        self.reset_cycle()
        self.group_ready_at = 0.
        # 更新站位重新计数，保留刚施法的冷却、Alt 和宠物说话定时。

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
        if self.next_chat is None:
            self.schedule_chat(now)
        if now < self.busy_until:
            return Action("wait", reason="等待本次施法或转身完成")
        if self.next_alt is None or now >= self.next_alt:
            return Action("alt", reason="启动或定时 Alt")
        error = x - self.c.station_x
        if not self.recovery and abs(error) > self.outer_tolerance + 1e-9:
            self.recovery, self.recovery_done = "attack", 0
        if self.recovery == "attack":
            if self.facing != "right":
                return Action("face", "right", "偏离后朝右清理")
            return Action("attack", "right", f"偏离后向右 D：{self.recovery_done + 1}/{self.c.recovery_shots}")
        if self.recovery == "return":
            crossed = self.return_error is not None and error * self.return_error <= 0
            arrived = (abs(error) <= self.inner_tolerance
                       or (crossed and abs(error) <= self.outer_tolerance))
            if arrived:
                self.recovery, self.return_error = "", None
                return Action("wait", reason="已回到挂机 X，松键后继续原攻击循环")
            self.return_error = error
            return Action("walk", "left" if error > 0 else "right", "向右清理后，连续步行回位")
        if now >= self.next_chat:
            word = self.rng.choice(self.c.pet_commands).strip()
            return Action("pet_chat", reason=f"宠物训练：{word}", text=word)
        if now < self.group_ready_at:
            return Action("wait", reason=f"组间等待 {self.c.group_pause:g} 秒")
        if self.facing != "right":
            return Action("face", "right", "朝右攻击")
        return Action("attack", "right", f"向右 D：{self.shots + 1}/{self.c.right_attack_shots}")

    def schedule_chat(self, at):
        self.next_chat = at + self.rng.uniform(self.c.pet_chat_min_seconds, self.c.pet_chat_max_seconds)

    def executed(self, action, at):
        if action.kind == "alt":
            self.next_alt = at + self.c.alt_interval
            self.busy_until = at + .1
        elif action.kind == "pet_chat":
            # at 为提交文字的时刻；一次完成后重新独立抽取间隔，不补发过期消息。
            self.schedule_chat(at)
            self.busy_until = at + .15
        elif action.kind == "face":
            self.facing = action.direction
            self.busy_until = at + self.c.face_hold + .02
        elif action.kind == "walk":
            self.facing = action.direction
        elif action.kind == "attack":
            self.busy_until = at + self.c.attack_interval
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
