from __future__ import annotations

import random
from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True)
class Mob:
    feet: tuple[float, float]
    score: float = 1.0
    confirmed: bool = True
    recovered: bool = False


@dataclass(frozen=True)
class Observation:
    position: tuple[float, float] | None
    player_feet: tuple[float, float] | None
    mobs: tuple[Mob, ...] = ()
    focused: bool = True
    location_source: str = "visual"


@dataclass(frozen=True)
class Action:
    kind: str
    direction: str = ""
    reason: str = ""


@dataclass
class AttackBurst:
    direction: str
    floor_y: float
    origin_x: float
    limit: int
    random: bool = False
    shots: int = 0


WAIT = Action("wait")


def within_attack_height(player_feet, monster_feet, config: Config) -> bool:
    return player_feet is not None and abs(monster_feet[1] - player_feet[1]) <= config.feet_tolerance_px


def target_status(mob: Mob, obs: Observation, config: Config, direction: str = "") -> str:
    if not obs.focused or obs.position is None or obs.player_feet is None:
        return "unlocalized"
    dx = mob.feet[0] - obs.player_feet[0]
    if (direction == "right" and dx < 0) or (direction == "left" and dx > 0):
        return "behind"
    if not within_attack_height(obs.player_feet, mob.feet, config):
        return "height"
    if not mob.confirmed:
        return "pending"
    if config.world_pixels_per_x is None:
        return "uncalibrated"
    return "ready" if abs(dx) / config.world_pixels_per_x <= config.attack_range else "range"


class Brain:
    """黄点导航；只朝行进方向施法，每组结束后实际步行再开下一组。"""

    def __init__(self, config: Config, mode: str = "auto", start_alt: bool = True, rng=None):
        self.c, self.mode = config, mode
        self.rng = rng or random.Random()
        self.floor: str | None = None
        self.direction = "left"
        self.facing = ""
        self.busy_until = 0.0
        self.next_alt: float | None = None
        self.startup_alt_pending = start_alt
        self.candidate_floor: str | None = None
        self.floor_frames = 0
        self.burst: AttackBurst | None = None
        self.pending_since: float | None = None
        self.attack_walked = float("inf")
        self.next_random: float | None = None
        self.completed_round_trips = 0
        self.lap_started = False
        self.right_reached = False
        self.last_scan_x: float | None = None
        self.move_origin: tuple[float, float] | None = None
        self.walk_position: float | None = None
        self.walked_since_teleport = 0.0
        self.next_teleport: float | None = None
        self.teleport_fault = False
        self.teleport_origin: float | None = None
        self.last_combat_at = float("-inf")
        self.route = ""
        self.route_started = 0.0
        self.route_disabled = False
        self.climb_y = 1.0
        self.climb_progress_at = 0.0

    def reset_observation(self) -> None:
        """完成的往返保留；失焦/黄点丢失不能补算未完成的半程。"""
        self.burst = None
        self.pending_since = None
        self.move_origin = None
        self.walk_position = None
        self.walked_since_teleport = 0.0
        self.last_scan_x = None
        self.candidate_floor = None
        self.floor_frames = 0
        self.facing = ""
        self.lap_started = self.right_reached = False
        self.direction = "left"

    def classify_floor(self, y: float) -> str | None:
        for name in ("bottom", "top"):
            if abs(y - getattr(self.c, name).y) <= self.c.floor_tolerance:
                return name
        return None

    def stable_floor(self, y: float) -> str | None:
        candidate = self.classify_floor(y)
        if candidate != self.candidate_floor:
            self.candidate_floor, self.floor_frames = candidate, 1
        else:
            self.floor_frames += 1
        return candidate if self.floor_frames >= 3 else None

    def schedule_random(self, now: float) -> None:
        self.next_random = now + self.rng.uniform(self.c.random_attack_min_seconds,
                                                 self.c.random_attack_max_seconds)

    def set_direction(self, direction: str) -> None:
        if direction != self.direction:
            self.burst = None
            self.pending_since = None
            self.move_origin = None
            self.direction = direction

    def enter_floor(self, floor: str, now: float, x: float) -> None:
        self.floor = floor
        self.burst = None
        self.last_scan_x = x
        self.move_origin = None
        self.attack_walked = float("inf")
        self.lap_started = self.right_reached = False
        self.direction = "left"
        if floor == "bottom":
            self.completed_round_trips = 0
            if x <= self.c.bottom.left + self.c.turn_inset:
                self.lap_started = True
                self.direction = "right"
        self.schedule_random(now)

    def decide(self, obs: Observation, now: float) -> Action:
        # 主画面角色定位只影响图像筛选，不能阻断黄点导航。
        if not obs.focused or obs.position is None:
            self.reset_observation()
            reason = "游戏失焦" if not obs.focused else "小地图黄点丢失"
            return Action("wait", reason=reason + "，松开按键")
        x, y = obs.position
        if self.next_alt is None:
            self.next_alt = now + self.c.alt_interval
        if self.next_teleport is None:
            self.next_teleport = now + max(12.0, self.c.teleport_cooldown)
        if self.walk_position is not None:
            distance = min(abs(x - self.walk_position), self.c.scan_step)
            self.walked_since_teleport += distance
            self.attack_walked += distance
            self.walk_position = None
        stable = self.stable_floor(y)
        if now < self.busy_until:
            return Action("wait", reason="等待施法/瞬移/跳跃周期")
        if self.teleport_origin is not None:
            if abs(x - self.teleport_origin) > self.c.teleport_max_dx + .008:
                self.teleport_fault = True
            self.teleport_origin = None
        if stable is not None and self.floor != stable and not self.route:
            self.enter_floor(stable, now, x)
        if stable is not None:
            if self.mode != "auto" and stable != self.mode:
                return Action("wait", reason="当前楼层与指定模式不一致")
            floor = getattr(self.c, stable)
            if not floor.configured or not floor.left - self.c.boundary_margin <= x <= floor.right + self.c.boundary_margin:
                return Action("wait", reason="黄点超出所配平台，检查地图边界")
            if self.startup_alt_pending:
                return Action("alt", reason="挂机开始，先按一次 Alt")
            if now >= self.next_alt:
                return Action("alt", reason="190 秒定时按键")
        if self.route:
            return self.route_action(obs, stable, now)
        if stable is None:
            self.burst = None
            return Action("wait", reason="等待黄点确认底层或顶层；不处理中间台阶")

        floor = getattr(self.c, stable)
        low, high = floor.left + self.c.turn_inset, floor.right - self.c.turn_inset
        if stable == "bottom":
            if x <= low and self.direction == "left":
                if self.lap_started and self.right_reached:
                    self.completed_round_trips += 1
                self.lap_started, self.right_reached = True, False
                self.set_direction("right")
            elif x >= high and self.direction == "right":
                self.right_reached = self.lap_started
                self.set_direction("left")
            if (self.mode == "auto" and self.c.auto_route and not self.route_disabled
                    and self.completed_round_trips >= self.c.patrol_round_trips
                    and self.c.teleport_max_dx is not None and not self.teleport_fault):
                self.route = "align"
                self.burst = None
                return self.route_action(obs, stable, now)
        elif self.mode == "auto" and self.c.auto_route and not self.route_disabled:
            # 顶层从梯子左侧一路向左清怪，到内缩点后继续沿出口清怪走落。
            self.set_direction("left")
            if x <= low:
                self.route = "drop_align"
                return self.route_action(obs, stable, now)
        elif x <= low and self.direction == "left":
            self.set_direction("right")
        elif x >= high and self.direction == "right":
            self.set_direction("left")

        combat = self.combat_action(obs, now)
        if combat is not None:
            return combat
        stuck = self.check_stuck(x)
        if stuck:
            self.set_direction("right" if self.direction == "left" else "left")
            self.lap_started = self.right_reached = False
            return Action("wait", reason="地面步行卡住，反向巡视；本段不计完整往返")
        if self.last_scan_x is None or abs(x - self.last_scan_x) >= self.c.scan_step:
            self.last_scan_x = x
            return Action("scan", reason="步行定点扫描前方")
        return self.travel(obs, low, high, now)

    def check_stuck(self, x: float) -> bool:
        if self.move_origin is None:
            return False
        old_x, seconds = self.move_origin
        if abs(x - old_x) >= .006:
            self.move_origin = (x, 0.0)
        elif seconds >= .8:
            self.move_origin = None
            return True
        return False

    def combat_action(self, obs: Observation, now: float) -> Action | None:
        x, y = obs.position
        if self.burst is not None:
            burst = self.burst
            if (burst.direction != self.direction or abs(y - burst.floor_y) > self.c.floor_tolerance
                    or abs(x - burst.origin_x) > self.c.attack_range / 2):
                self.burst = None
            elif burst.shots >= burst.limit:
                self.burst = None
                self.attack_walked = 0.0
                self.schedule_random(now)
            else:
                if self.facing != self.direction:
                    return Action("face", self.direction, "保持行进方向，不回头追怪")
                label = "步行途中随机攻击" if burst.random else "前方目标三次攻击"
                return Action("attack", self.direction, f"{label} {burst.shots + 1}/{burst.limit}")
        # 一组结束后必须有实际步行位移，不能在同一点反复重开三连。
        if self.attack_walked < max(.006, self.c.attack_range * .2):
            return None
        ready = [m for m in obs.mobs if target_status(m, obs, self.c, self.direction) == "ready"
                 and not m.recovered]
        if ready:
            self.burst = AttackBurst(self.direction, y, x, self.c.attack_burst_shots)
            self.pending_since = None
            return self.combat_action(obs, now)
        pending = any(target_status(m, obs, self.c, self.direction) == "pending"
                      and abs(m.feet[0] - obs.player_feet[0]) <= self.c.attack_range * self.c.world_pixels_per_x
                      for m in obs.mobs)
        if pending:
            if self.pending_since is None:
                self.pending_since = now
            if now - self.pending_since < .3:
                return Action("wait", reason="停步确认前方候选")
        else:
            self.pending_since = None
        if (self.next_random is not None and now >= self.next_random
                and self.walked_since_teleport >= self.c.scan_step):
            self.burst = AttackBurst(self.direction, y, x,
                                     self.rng.randint(1, self.c.random_attack_max_shots), random=True)
            return self.combat_action(obs, now)
        return None

    def travel(self, obs: Observation, low: float, high: float, now: float) -> Action:
        x = obs.position[0]
        dx = self.c.teleport_max_dx
        if (dx is not None and not self.teleport_fault and now >= self.next_teleport
                and not obs.mobs and now - self.last_combat_at >= 3
                and self.walked_since_teleport >= max(3 * self.c.scan_step, 1.5 * self.c.attack_range)):
            projected = x + (dx if self.direction == "right" else -dx)
            if low + self.c.boundary_margin <= projected <= high - self.c.boundary_margin:
                return Action("teleport", self.direction, "步行一段后偶尔瞬移")
        return Action("walk", self.direction, "黄点导航，步行巡视前方")

    def abort_route(self, reason: str) -> Action:
        self.route = ""
        self.route_disabled = True
        self.burst = None
        self.move_origin = None
        return Action("wait", reason=reason + "；停用本次自动换层")

    def route_action(self, obs: Observation, stable: str | None, now: float) -> Action:
        x, y = obs.position
        if self.route not in ("align", "drop_align") and now - self.route_started > self.c.climb_timeout:
            return self.abort_route("换层超时，黄点未确认到达")
        if self.route in ("align", "drop_align"):
            target = self.c.ladder_x if self.route == "align" else self.c.top_drop_x
            expected = "bottom" if self.route == "align" else "top"
            if self.route == "drop_align" and y > self.c.top.y + self.c.floor_tolerance:
                self.route = "fall"
                self.route_started = now
                return Action("wait", reason="检测到 Y 增大，等待落回底层")
            if stable != expected:
                return Action("wait", reason="等待黄点确认换层起点")
            if abs(x - target) <= self.c.ladder_tolerance:
                self.burst = None
                if self.route == "align":
                    return Action("grab", "up", "到达梯子 X，上＋空格抓梯")
                return Action("drop_walk", self.c.drop_direction, "从顶层左侧走落")
            self.set_direction("right" if x < target else "left")
            combat = self.combat_action(obs, now)
            if combat is not None:
                return combat
            if self.check_stuck(x):
                return self.abort_route("前往换层点步行卡住")
            return Action("walk", self.direction, "沿途只攻击前方，步行前往换层点")
        if self.route == "climb":
            if y <= self.c.top.y + .008:
                return Action("climb_exit", reason="到顶层 Y 后空格离梯")
            if self.climb_y - y >= .008:
                self.climb_y, self.climb_progress_at = y, now
            if now - self.climb_progress_at > self.c.climb_stuck_seconds:
                return self.abort_route("爬梯 Y 未继续上升")
            return Action("climb", "up", "按黄点 Y 持续爬梯")
        if self.route == "exit":
            if stable != "top":
                return Action("wait", reason="离梯后等待顶层黄点稳定")
            landing = x - self.c.teleport_max_dx
            if not self.c.top.left + self.c.boundary_margin <= landing <= self.c.top.right - self.c.boundary_margin:
                return self.abort_route("顶层左瞬移落点超出安全范围")
            return Action("top_teleport", "left", "顶层离梯后向左瞬移一次")
        if self.route == "top_settle":
            if stable == "top":
                self.route = ""
                self.enter_floor("top", now, x)
            return Action("wait", reason="确认顶层落地，向左清怪")
        if self.route == "fall":
            if stable == "bottom":
                self.route = ""
                self.enter_floor("bottom", now, x)
                return Action("wait", reason="落回底层，重新开始三轮巡逻")
            if abs(y - self.c.top.y) <= self.c.floor_tolerance:
                return Action("walk", self.c.drop_direction, "继续走出顶层出口")
            return Action("wait", reason="等待黄点确认落回底层")
        return self.abort_route("未知换层状态")

    def executed(self, action: Action, pressed_at: float, obs: Observation) -> None:
        kind = action.kind
        if kind == "attack":
            self.busy_until = pressed_at + self.c.attack_interval
            self.last_combat_at = pressed_at
            self.move_origin = None
            if self.burst is not None:
                self.burst.shots += 1
        elif kind == "alt":
            self.startup_alt_pending = False
            self.next_alt = pressed_at + self.c.alt_interval
            self.busy_until = pressed_at + .2
        elif kind == "face":
            self.facing = action.direction
            self.busy_until = pressed_at + .12
        elif kind == "scan":
            self.busy_until = pressed_at + self.c.scan_seconds
        elif kind in ("teleport", "top_teleport"):
            self.facing = action.direction
            self.teleport_origin = obs.position[0]
            self.next_teleport = pressed_at + max(12.0, self.c.teleport_cooldown)
            self.walked_since_teleport = 0.0
            self.busy_until = pressed_at + self.c.teleport_settle
            self.burst = None
            if kind == "top_teleport":
                self.route = "top_settle"
        elif kind == "grab":
            self.route, self.route_started = "climb", pressed_at
            self.climb_y, self.climb_progress_at = obs.position[1], pressed_at
            self.busy_until = pressed_at + .4
        elif kind == "climb_exit":
            self.route = "exit"
            self.busy_until = pressed_at + .6
        elif kind == "drop_walk":
            self.route, self.route_started = "fall", pressed_at
            self.busy_until = pressed_at + .45
        if kind == "walk":
            self.facing = action.direction
            self.walk_position = obs.position[0]
            if self.move_origin is None:
                self.move_origin = (obs.position[0], 0.0)
            old_x, seconds = self.move_origin
            self.move_origin = (old_x, seconds + .06)
