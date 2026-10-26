"""暗影花园：底层步行/水平瞬移，局部清怪，仅用 D。"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, replace
from pathlib import Path

import numpy as np

from .brain import Action, Observation, target_status
from .config import Config, Floor


@dataclass(frozen=True)
class GardenConfig:
    window_title: str = "冒险岛怀旧服"
    frame_size: tuple[int, int] | None = None
    minimap_roi: tuple[int, int, int, int] | None = None
    combat_roi: tuple[int, int, int, int] | None = None
    left_x: float | None = None
    right_x: float | None = None
    bottom_path: tuple[tuple[float, float], ...] = ()
    floor_tolerance: float = .015
    boundary_margin: float = .006
    world_pixels_per_x: float | None = None
    player_height_px: float = 64.
    attack_range_px: float | None = None
    attack_burst_shots: int = 3
    attack_interval: float = .9
    attack_hold: float = .08
    face_hold: float = .06
    alt_interval: float = 190.
    clear_seconds: float = .4
    clear_frames: int = 3
    fps: float = 10.
    walk_stuck_seconds: float = 6.
    teleport_enabled: bool = True
    teleport_max_dx: float | None = None
    teleport_cooldown: float = 1.5
    teleport_settle: float = .3
    template_manifest: str = "calibration/garden/monsters.json"
    screen_anchor: str = "calibration/garden/screen-anchor.json"
    anchor_bank: str = "calibration/garden/anchors.json"
    player_template: str = "calibration/garden/optional-player.png"
    player_feet_offset: tuple[float, float] | None = None
    lie_alert_enabled: bool = True
    lie_alert_template: str = "calibration/lie-alert-title.png"
    lie_alert_interval: float = .25
    lie_alert_threshold: float = .90
    lie_alert_roi: tuple[int, int, int, int] | None = None

    def vision_config(self):
        return Config(behavior="stationary", monster_name="暗影食人花", auto_route=False,
                      bottom=Floor(.5, None, None), top=Floor(0, None, None), turn_inset=0,
                      boundary_margin=0, window_title=self.window_title, frame_size=self.frame_size,
                      minimap_roi=self.minimap_roi, combat_roi=self.combat_roi,
                      world_pixels_per_x=self.world_pixels_per_x, player_height_px=self.player_height_px,
                      attack_range_px=self.attack_range_px, attack_burst_shots=self.attack_burst_shots,
                      attack_interval=self.attack_interval, attack_hold=self.attack_hold, face_hold=self.face_hold,
                      alt_interval=self.alt_interval, player_template=self.player_template,
                      player_feet_offset=self.player_feet_offset, template_manifest=self.template_manifest,
                      screen_anchor=self.screen_anchor, floor_tolerance=self.floor_tolerance,
                      plant_color_fallback=False, stationary_fps=self.fps)

    def validate(self, ready=False):
        if not isinstance(self.window_title, str) or not self.window_title.strip():
            raise ValueError("window_title 不能为空")
        for name in ("left_x", "right_x", "floor_tolerance", "boundary_margin", "world_pixels_per_x",
                     "player_height_px", "attack_range_px", "attack_interval", "attack_hold", "face_hold",
                     "alt_interval", "clear_seconds", "fps", "walk_stuck_seconds", "lie_alert_interval", "lie_alert_threshold",
                     "teleport_max_dx", "teleport_cooldown", "teleport_settle"):
            value = getattr(self, name)
            if value is None and name in ("left_x", "right_x", "world_pixels_per_x", "attack_range_px", "teleport_max_dx"):
                continue
            if type(value) not in (int, float) or not math.isfinite(value):
                raise ValueError(f"{name} 必须为有限数值")
        for name in ("attack_burst_shots", "clear_frames"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError(f"{name} 必须为正整数")
        if self.clear_frames < 2 or not 0 < self.clear_seconds <= 5:
            raise ValueError("清场确认须至少两帧，时间须为 (0, 5] 秒")
        if not 0 < self.floor_tolerance <= .06 or not 0 < self.boundary_margin <= .03 or self.walk_stuck_seconds <= 0:
            raise ValueError("底层容差、边界内缩或步行受阻时间不合法")
        for name, length in (("frame_size", 2), ("minimap_roi", 4), ("combat_roi", 4), ("lie_alert_roi", 4)):
            value = getattr(self, name)
            if value is not None:
                if len(value) != length or any(type(v) is not int for v in value):
                    raise ValueError(f"{name} 必须包含 {length} 个整数")
                if (length == 2 and min(value) <= 0) or (length == 4 and (min(value[:2]) < 0 or min(value[2:]) <= 0)):
                    raise ValueError(f"{name} 尺寸或起点不合法")
                if length == 4 and self.frame_size is not None and (value[0] + value[2] > self.frame_size[0]
                                                                              or value[1] + value[3] > self.frame_size[1]):
                    raise ValueError(f"{name} 超出客户区")
        if (self.left_x is None) != (self.right_x is None):
            raise ValueError("左右边界必须一起设置")
        if self.left_x is not None and not (0 <= self.left_x < self.right_x <= 1
                                             and self.right_x - self.left_x > 4 * self.boundary_margin):
            raise ValueError("左右边界须满足 0 <= left_x < right_x <= 1，并留有掉头空间")
        previous = -1.
        for point in self.bottom_path:
            if len(point) != 2 or any(type(v) not in (int, float) or not math.isfinite(v) or not 0 <= v <= 1 for v in point):
                raise ValueError("底层路径必须是归一化 [X,Y] 点列")
            if point[0] <= previous:
                raise ValueError("底层路径 X 必须递增")
            previous = point[0]
        if self.bottom_path and self.left_x is not None and (self.bottom_path[0][0] > self.left_x + 1e-6
                                                           or self.bottom_path[-1][0] < self.right_x - 1e-6):
            raise ValueError("底层高度路径必须覆盖左右巡逻边界")
        for name in ("template_manifest", "screen_anchor", "anchor_bank", "lie_alert_template"):
            if not isinstance(getattr(self, name), str) or not getattr(self, name).strip():
                raise ValueError(f"{name} 不能为空")
        if self.player_feet_offset is not None and (len(self.player_feet_offset) != 2 or any(
                type(v) not in (int, float) or not math.isfinite(v) for v in self.player_feet_offset)):
            raise ValueError("可选名牌脚底偏移必须为两个有限数值")
        if (type(self.lie_alert_enabled) is not bool or not .1 <= self.lie_alert_interval <= 2
                or not .7 <= self.lie_alert_threshold <= 1):
            raise ValueError("测谎开关/间隔/阈值不合法")
        if (type(self.teleport_enabled) is not bool or self.teleport_cooldown < .6 or self.teleport_settle < .2
                or self.teleport_settle > self.teleport_cooldown
                or (self.teleport_max_dx is not None and not .005 <= self.teleport_max_dx < .5)):
            raise ValueError("瞬移开关/位移不合法；冷却至少 0.6 秒，落点等待至少 0.2 秒且不超过冷却")
        self.vision_config().validate()
        if ready and (self.frame_size is None or self.minimap_roi is None or self.combat_roi is None
                      or self.world_pixels_per_x is None or self.attack_range_px is None
                      or self.left_x is None or len(self.bottom_path) < 2):
            raise ValueError("请先运行 python -m lionbot.garden --setup，完成截图、射程和底层路线采集")


def load_garden(path):
    path = Path(path)
    base = GardenConfig()
    if path.name != "config.garden.local.json":
        folder = Path("calibration") / ("garden-" + path.stem)
        base = replace(base, template_manifest=str(folder / "monsters.json"),
                       screen_anchor=str(folder / "screen-anchor.json"), anchor_bank=str(folder / "anchors.json"),
                       player_template=str(folder / "optional-player.png"))
    raw = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    for name in ("frame_size", "minimap_roi", "combat_roi", "player_feet_offset", "lie_alert_roi"):
        if raw.get(name) is not None:
            raw[name] = tuple(raw[name])
    if "bottom_path" in raw:
        raw["bottom_path"] = tuple(tuple(point) for point in raw["bottom_path"])
    config = replace(base, **raw)
    config.validate()
    return config


def save_garden(path, config):
    from .calibrate import save_json

    config.validate()
    save_json(Path(path), asdict(config))


class GardenCycle:
    def __init__(self, config, start_alt=True):
        self.c = config
        self.vc = config.vision_config()
        self.direction = "right"
        self.next_alt = None if start_alt else float("inf")
        self.busy_until = 0.
        self.completed_round_trips = 0
        self.left_seen = self.right_seen = False
        self.last_burst_direction = ""
        self.burst_direction = ""
        self.shots = 0
        self.target_x = None
        self.clearing = False
        self.next_teleport = 0.
        self.teleport_origin = None
        self.teleport_misses = 0
        self.teleport_fault = False
        self.suspend()

    def suspend(self):
        self.facing = ""
        self.clear_at = self.clear_frame_at = self.pending_at = None
        self.clear_count = 0
        self.walk_x = self.walk_at = None
        self.blocked = False
        # 实际发出的攻击次数及 Alt 计时保留；恢复时重新核对攻击范围。

    def expected_y(self, x):
        return float(np.interp(x, [p[0] for p in self.c.bottom_path], [p[1] for p in self.c.bottom_path]))

    def on_bottom(self, position):
        tolerance = max(self.c.floor_tolerance, 2 / self.c.minimap_roi[3])
        return abs(position[1] - self.expected_y(position[0])) <= tolerance + 1e-9

    def cancel_combat(self):
        self.burst_direction = ""
        self.shots = 0
        self.target_x = None
        self.clearing = False
        self.clear_at = self.clear_frame_at = self.pending_at = None
        self.clear_count = 0

    def decide(self, obs: Observation, now):
        if not obs.focused or obs.position is None:
            self.suspend()
            return Action("wait", reason="失焦或黄点丢失，松键等待")
        x, y = obs.position
        if not self.on_bottom(obs.position):
            self.cancel_combat()
            self.walk_at = self.walk_x = None
            return Action("wait", reason=f"不在已采集底层：Y={y:.4f}，路线期望 Y={self.expected_y(x):.4f}；松键等待回到底层")
        if self.blocked:
            return Action("wait", reason="步行持续受阻，已松键；请检查地形，F8 暂停后恢复")
        if now < self.busy_until:
            return Action("wait", reason="等待 D 施法或转身完成")
        if self.teleport_origin is not None:
            distance = abs(x - self.teleport_origin)
            if distance > self.c.teleport_max_dx + 1 / self.c.minimap_roi[2]:
                self.teleport_fault = True
            self.teleport_misses = self.teleport_misses + 1 if distance < .5 / self.c.minimap_roi[2] else 0
            self.teleport_fault |= self.teleport_misses >= 3
            self.teleport_origin = None
        margin = max(self.c.boundary_margin, 1 / self.c.minimap_roi[2])
        if x <= self.c.left_x + margin:
            if self.direction != "right":
                self.walk_x = self.walk_at = None
            self.direction = "right"
            if self.right_seen:
                self.completed_round_trips += 1
                self.right_seen = False
            self.left_seen = True
        elif x >= self.c.right_x - margin:
            if self.direction != "left":
                self.walk_x = self.walk_at = None
            self.direction = "left"
            if self.left_seen:
                self.right_seen = True
        if x < self.c.left_x or x > self.c.right_x:
            self.cancel_combat()
            return self.walk(x, now, "被击退越过巡逻边界，连续步行回到底层范围")
        if self.next_alt is None or now >= self.next_alt:
            self.walk_at = self.walk_x = None
            return Action("alt", reason="挂机开始或定时 Alt；只按实际发送时间计时")
        if obs.player_feet is None:
            self.cancel_combat()
            return self.walk(x, now, "黄点导航继续巡逻；攻击等待地形脚底定位恢复，可用 --anchor 补参考")
        ready = [mob for mob in obs.mobs if target_status(mob, obs, self.vc) == "ready"]
        if self.burst_direction:
            dx = (self.target_x - x) * self.c.world_pixels_per_x
            same_side = dx >= 0 if self.burst_direction == "right" else dx <= 0
            if abs(dx) > self.c.attack_range_px or not same_side:
                self.burst_direction = ""
                self.shots = 0
        if not self.burst_direction and ready:
            sides = {"right" if mob.feet[0] >= obs.player_feet[0] else "left" for mob in ready}
            opposite = "left" if self.last_burst_direction == "right" else "right"
            nearest = min(ready, key=lambda m: abs(m.feet[0] - obs.player_feet[0]))
            self.burst_direction = opposite if len(sides) == 2 and self.last_burst_direction else (
                "right" if nearest.feet[0] >= obs.player_feet[0] else "left")
            target = min((m for m in ready if (m.feet[0] >= obs.player_feet[0]) == (self.burst_direction == "right")),
                         key=lambda m: abs(m.feet[0] - obs.player_feet[0]))
            self.target_x = x + (target.feet[0] - obs.player_feet[0]) / self.c.world_pixels_per_x
            self.shots = 0
            self.clearing = True
        if self.burst_direction:
            self.walk_at = self.walk_x = None
            self.clear_at = None
            self.clear_count = 0
            if self.facing != self.burst_direction:
                return Action("face", self.burst_direction, "松开步行方向，转向附近已确认的食人花")
            return Action("attack", self.burst_direction, f"只用 D 清怪 {self.shots + 1}/{self.c.attack_burst_shots}；保持原巡逻方向")
        if obs.mobs:
            self.clear_at = None
            self.clear_count = 0
            if self.pending_at is None:
                self.pending_at = now
            if self.clearing or now - self.pending_at < .35:
                self.walk_at = self.walk_x = None
                return Action("wait", reason="松键确认局部花头；不把暂时摆动或受击漏检当成清场")
        else:
            self.pending_at = None
            if self.clearing:
                self.walk_at = self.walk_x = None
                if self.clear_at is None:
                    self.clear_at = now
                if self.clear_frame_at != now:
                    self.clear_frame_at = now
                    self.clear_count += 1
                if self.clear_count < self.c.clear_frames or now - self.clear_at < self.c.clear_seconds:
                    return Action("wait", reason="连续空场确认，清完附近再继续原路线")
                self.clearing = False
        return self.walk(x, now, "沿底层巡逻，途中只识别射程和角色身高内的食人花", can_teleport=not obs.mobs)

    def walk(self, x, now, reason, can_teleport=False):
        if self.walk_at is not None:
            progress = (x - self.walk_x) * (1 if self.direction == "right" else -1)
            if progress >= .5 / self.c.minimap_roi[2]:
                self.walk_at, self.walk_x = now, x
            elif now - self.walk_at >= self.c.walk_stuck_seconds:
                self.blocked = True
                return Action("wait", reason="连续步行没有向目标前进，已松键；F8 暂停后检查并恢复")
        if (can_teleport and self.c.teleport_enabled and self.c.teleport_max_dx is not None
                and not self.teleport_fault and now >= self.next_teleport):
            distance = self.c.right_x - x if self.direction == "right" else x - self.c.left_x
            margin = max(self.c.boundary_margin, 2 / self.c.minimap_roi[2])
            overlap_ok = self.c.teleport_max_dx * self.c.world_pixels_per_x <= 1.8 * self.c.attack_range_px
            if distance > self.c.teleport_max_dx + margin and overlap_ok:
                return Action("teleport", self.direction, "射程内无目标且落点在巡逻范围内，方向键＋F；边界附近改用步行")
        return Action("walk", self.direction, reason)

    def executed(self, action, pressed_at, obs):
        if action.kind == "attack":
            self.shots += 1
            self.busy_until = pressed_at + self.c.attack_interval
            if self.shots >= self.c.attack_burst_shots:
                self.last_burst_direction = self.burst_direction
                self.burst_direction = ""
                self.shots = 0
        elif action.kind == "face":
            self.facing = action.direction
            self.busy_until = pressed_at + self.c.face_hold
        elif action.kind == "walk":
            self.facing = action.direction
            if self.walk_at is None:
                self.walk_at, self.walk_x = pressed_at, obs.position[0]
        elif action.kind == "alt":
            self.next_alt = pressed_at + self.c.alt_interval
            self.busy_until = pressed_at + .15
        elif action.kind == "teleport":
            self.facing = action.direction
            self.teleport_origin = obs.position[0]
            self.next_teleport = pressed_at + self.c.teleport_cooldown
            self.busy_until = pressed_at + self.c.teleport_settle
            self.walk_at = self.walk_x = None
