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
    box: tuple[int, int, int, int] | None = None


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


def attack_vertical_bounds(player_feet, config: Config) -> tuple[float, float]:
    """图像 Y 向下增加；攻击带上端是角色头顶，下端是脚底。"""
    return player_feet[1] - config.player_height_px, player_feet[1]


def monster_vertical_bounds(monster_feet, box=None) -> tuple[float, float]:
    """头像模板加脚底偏移即可估算身体纵向范围，无需完整身体模板。"""
    if box is None:
        return monster_feet[1], monster_feet[1]
    _, y, _, h = box
    return min(y, monster_feet[1]), max(y + h - 1, monster_feet[1])


def within_attack_height(player_feet, monster_feet, config: Config, box=None) -> bool:
    if player_feet is None:
        return False
    top, bottom = attack_vertical_bounds(player_feet, config)
    monster_top, monster_bottom = monster_vertical_bounds(monster_feet, box)
    return monster_bottom >= top and monster_top <= bottom


def target_status(mob: Mob, obs: Observation, config: Config, direction: str = "") -> str:
    # direction 保留旧调用兼容；两侧目标使用同一组高度/射程条件。
    if not obs.focused or obs.position is None or obs.player_feet is None:
        return "unlocalized"
    dx = mob.feet[0] - obs.player_feet[0]
    if not within_attack_height(obs.player_feet, mob.feet, config, mob.box):
        return "height"
    if not mob.confirmed:
        return "pending"
    if config.world_pixels_per_x is None:
        return "uncalibrated"
    return "ready" if abs(dx) / config.world_pixels_per_x <= config.attack_range else "range"


class Brain:
    """巡逻方向与战斗朝向独立；周围清理确认后才恢复原路线。"""

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
        self.clearing = False
        self.clear_since: float | None = None
        self.clear_count = 0
        self.clear_frame_at: float | None = None
        self.visual_lost_at: float | None = None
        self.last_clear_at: float | None = None
        self.last_burst_direction = ""
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
        self.grab_attempts = 0
        self.ladder_align_failures = 0
        self.ladder_aligned_frames = 0
        self.ladder_align_started = 0.0
        self.grab_started = 0.0
        self.grab_origin_y = 0.0
        self.grab_last_y = 0.0
        self.grab_late_rises = 0
        self.retry_started = 0.0

    def reset_observation(self) -> None:
        """完成的往返保留；失焦/黄点丢失不能补算未完成的半程。"""
        self.burst = None
        self.clearing = False
        self.reset_clear_evidence()
        self.visual_lost_at = None
        self.ladder_aligned_frames = 0
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
            self.move_origin = None
            self.direction = direction

    def enter_floor(self, floor: str, now: float, x: float) -> None:
        self.floor = floor
        self.burst = None
        self.clearing = False
        self.reset_clear_evidence()
        self.visual_lost_at = None
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
                self.reset_clear_evidence()
                return Action("wait", reason="当前楼层与指定模式不一致")
            floor = getattr(self.c, stable)
            if not floor.configured or not floor.left - self.c.boundary_margin <= x <= floor.right + self.c.boundary_margin:
                self.reset_clear_evidence()
                return Action("wait", reason="黄点超出所配平台，检查地图边界")
            if self.startup_alt_pending:
                return Action("alt", reason="挂机开始，先按一次 Alt")
            if now >= self.next_alt:
                return Action("alt", reason="190 秒定时按键")
        if self.route:
            return self.route_action(obs, stable, now)
        if stable is None:
            self.burst = None
            self.reset_clear_evidence()
            return Action("wait", reason="等待黄点确认底层或顶层；不处理中间台阶")

        combat = self.combat_action(obs, now)
        if combat is not None:
            self.move_origin = None
            return combat

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
                self.grab_attempts = 0
                self.ladder_align_failures = 0
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

        stuck = self.check_stuck(x)
        if stuck:
            self.set_direction("right" if self.direction == "left" else "left")
            self.lap_started = self.right_reached = False
            return Action("wait", reason="地面步行卡住，反向巡视；本段不计完整往返")
        if self.last_scan_x is None or abs(x - self.last_scan_x) >= self.c.scan_step:
            self.last_scan_x = x
            return Action("scan", reason="步行定点扫描周围")
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

    def reset_clear_evidence(self) -> None:
        self.clear_since = None
        self.clear_count = 0
        self.clear_frame_at = None

    def combat_direction(self, mob: Mob, obs: Observation) -> str:
        dx = mob.feet[0] - obs.player_feet[0]
        if abs(dx) <= 6:
            return self.facing or self.direction
        return "right" if dx > 0 else "left"

    def combat_action(self, obs: Observation, now: float, *, force_check: bool = False,
                      allow_random: bool = True, require_localized: bool = False) -> Action | None:
        x, y = obs.position
        candidates = [m for m in obs.mobs
                      if target_status(m, obs, self.c) in ("ready", "pending")
                      and abs(m.feet[0] - obs.player_feet[0]) <= self.c.attack_range * self.c.world_pixels_per_x]
        ready = [m for m in candidates if m.confirmed and not m.recovered]
        if candidates or force_check or (require_localized and obs.player_feet is None):
            if not self.clearing:
                self.last_burst_direction = ""
            self.clearing = True
        if candidates:
            self.reset_clear_evidence()
        if force_check and self.burst is not None and self.burst.random:
            self.burst = None
        if self.burst is not None:
            burst = self.burst
            if (abs(y - burst.floor_y) > self.c.floor_tolerance
                    or abs(x - burst.origin_x) > self.c.attack_range / 2):
                self.burst = None
            elif burst.shots >= burst.limit:
                self.burst = None
                self.attack_walked = 0.0
                self.last_burst_direction = burst.direction
                self.schedule_random(now)
            else:
                if self.facing != burst.direction:
                    return Action("face", burst.direction, "停步转向周围目标，保留原巡逻方向")
                label = "步行途中随机攻击" if burst.random else "清理周围目标"
                return Action("attack", burst.direction, f"{label} {burst.shots + 1}/{burst.limit}")
        if ready:
            # 两侧都有怪时按组轮换，避免持续刷新的一侧饿死另一侧。
            other = "left" if self.last_burst_direction == "right" else "right"
            sides = {side: [m for m in ready if self.combat_direction(m, obs) == side]
                     for side in ("left", "right")}
            preferences = (other if self.last_burst_direction else "", self.facing, self.direction, "left", "right")
            preferred = next(side for side in preferences if sides.get(side))
            self.burst = AttackBurst(preferred, y, x, self.c.attack_burst_shots)
            return self.combat_action(obs, now, allow_random=allow_random,
                                      require_localized=require_localized)
        if self.clearing:
            self.move_origin = None
            if obs.player_feet is None:
                self.reset_clear_evidence()
                if self.visual_lost_at is None:
                    self.visual_lost_at = now
                if require_localized or now - self.visual_lost_at < self.c.combat_localization_grace:
                    return Action("wait", reason="主画面定位暂失，等待核对周围怪物；不把丢定位当成清空")
                self.clearing = False
                self.visual_lost_at = None
                return Action("wait", reason="暂时无法确认清场，恢复原路线重新识别")
            self.visual_lost_at = None
            if candidates:
                return Action("wait", reason="停步确认候选或受击姿态，周围尚未确认清空")
            if self.clear_frame_at is None or now - self.clear_frame_at > max(.5, 2 * self.c.confirmation_gap):
                self.clear_since, self.clear_count = now, 0
            self.clear_frame_at = now
            self.clear_count += 1
            if (self.clear_count < self.c.clear_frames
                    or now - self.clear_since < max(self.c.clear_seconds, self.c.confirmation_gap)):
                return Action("wait", reason=f"停步复查周围空场 {self.clear_count}/{self.c.clear_frames}")
            self.clearing = False
            self.last_clear_at = now
            self.reset_clear_evidence()
            self.schedule_random(now)
            return None
        if (allow_random and self.attack_walked >= max(.006, self.c.attack_range * .2)
                and self.next_random is not None and now >= self.next_random
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
        return Action("walk", self.direction, "周围清理后，按原路线步行巡视")

    def abort_route(self, reason: str) -> Action:
        self.route = ""
        self.route_disabled = True
        self.burst = None
        self.clearing = False
        self.reset_clear_evidence()
        self.move_origin = None
        return Action("wait", reason=reason + "；停用本次自动换层")

    def retry_grab(self, now: float, reason: str) -> Action:
        self.burst = None
        self.clearing = False
        self.reset_clear_evidence()
        self.ladder_aligned_frames = 0
        if self.grab_attempts >= self.c.ladder_max_attempts:
            return self.abort_route(reason + f"，已尝试 {self.grab_attempts} 次")
        self.route = "grab_retry"
        self.retry_started = now
        return Action("wait", reason=reason + "；松键等待落地，再清怪、对齐、重新抓绳")

    def ladder_grounded(self, y: float, stable: str | None) -> bool:
        pixel = 1 / self.c.minimap_roi[3] if self.c.minimap_roi else .008
        tolerance = min(self.c.floor_tolerance, max(.008, pixel))
        return stable == "bottom" and abs(y - self.c.bottom.y) <= tolerance

    def ladder_alignment_tolerance(self) -> float:
        # 已有配置可能放宽到多个小地图像素；抓绳要靠近中心，而非停在范围边缘。
        pixel_tolerance = .6 / self.c.minimap_roi[2] if self.c.minimap_roi else self.c.ladder_tolerance
        return min(self.c.ladder_tolerance, pixel_tolerance)

    def route_action(self, obs: Observation, stable: str | None, now: float) -> Action:
        x, y = obs.position
        if (self.route in ("climb", "exit", "top_settle", "fall")
                and now - self.route_started > self.c.climb_timeout):
            return self.abort_route("换层超时，黄点未确认到达")
        if self.route == "align":
            if stable != "bottom":
                return Action("wait", reason="等待底层黄点稳定后前往爬绳入口")
            self.set_direction("right" if x < self.c.ladder_x else "left")
            approach = max(3 * self.c.ladder_tolerance, self.c.attack_range / 2)
            if abs(x - self.c.ladder_x) <= approach:
                self.route = "ladder_clear"
                self.reset_clear_evidence()
                return self.route_action(obs, stable, now)
            combat = self.combat_action(obs, now)
            if combat is not None:
                self.move_origin = None
                return combat
            if self.check_stuck(x):
                return self.abort_route("前往爬绳入口步行卡住")
            return Action("walk", self.direction, "清理周围后，按原路线前往爬绳入口")
        if self.route == "ladder_clear":
            if not self.ladder_grounded(y, stable):
                return Action("wait", reason="抓绳前等待底层落地稳定")
            combat = self.combat_action(obs, now, force_check=True, allow_random=False, require_localized=True)
            if combat is not None:
                self.move_origin = None
                return combat
            self.route = "ladder_align"
            self.ladder_align_started = now
            self.ladder_aligned_frames = 0
        if self.route == "ladder_align":
            if not self.ladder_grounded(y, stable):
                self.ladder_aligned_frames = 0
                return Action("wait", reason="微调前等待底层坐标稳定")
            combat = self.combat_action(obs, now, allow_random=False, require_localized=True)
            if combat is not None:
                self.route = "ladder_clear"
                self.ladder_aligned_frames = 0
                return combat
            if now - self.ladder_align_started > self.c.ladder_align_timeout:
                self.ladder_align_failures += 1
                if self.ladder_align_failures >= self.c.ladder_max_attempts:
                    return self.abort_route("爬绳入口多次微调仍未稳定对齐")
                self.route = "ladder_clear"
                return Action("wait", reason="微调超时，重新清怪并校准爬绳入口")
            tolerance = self.ladder_alignment_tolerance()
            if abs(x - self.c.ladder_x) > tolerance:
                self.ladder_aligned_frames = 0
                return Action("nudge", "right" if x < self.c.ladder_x else "left",
                              f"短按微调爬绳 X={self.c.ladder_x:.4f} ± {tolerance:.4f}，当前 X={x:.4f}")
            self.ladder_aligned_frames += 1
            if self.ladder_aligned_frames < self.c.ladder_align_frames:
                return Action("wait", reason=f"松开方向，确认爬绳对齐 {self.ladder_aligned_frames}/{self.c.ladder_align_frames}")
            return Action("grab", "up", "周围清理完、X 连续对齐后，上＋空格抓绳")
        if self.route == "grab_check":
            elapsed = now - self.grab_started
            step = max(.004, .5 / self.c.minimap_roi[3]) if self.c.minimap_roi else .004
            delta = self.grab_last_y - y
            if delta < -step:
                self.grab_late_rises = 0
            elif delta >= step and elapsed >= self.c.grab_confirm_seconds * .75:
                self.grab_late_rises += 1
            self.grab_last_y = y
            minimum = max(self.c.grab_min_rise, 2 / self.c.minimap_roi[3]) if self.c.minimap_roi else self.c.grab_min_rise
            aligned = abs(x - self.c.ladder_x) <= 2 * self.ladder_alignment_tolerance()
            if (aligned and elapsed >= self.c.grab_confirm_seconds
                    and self.grab_origin_y - y >= minimum and self.grab_late_rises >= 2):
                self.route = "climb"
                self.climb_y, self.climb_progress_at = y, now
                return Action("climb", "up", "已确认黄点持续上升并保持绳索 X，继续上爬")
            if elapsed >= self.c.grab_timeout or not aligned:
                return self.retry_grab(now, "未确认持续上升，抓绳未成功或已偏离")
            return Action("climb", "up", "保持上键，验证跳跃后黄点仍持续上升")
        if self.route == "grab_retry":
            if self.ladder_grounded(y, stable):
                self.route = "ladder_clear"
                return self.route_action(obs, stable, now)
            if now - self.retry_started > self.c.grab_timeout + self.c.grab_confirm_seconds:
                return self.abort_route("抓绳重试前未确认回到底层，不能在空中微调")
            return Action("wait", reason="抓绳未确认，松键等待底层落地后重试")
        if self.route == "drop_align":
            if y > self.c.top.y + self.c.floor_tolerance:
                self.route = "fall"
                self.route_started = now
                return Action("wait", reason="检测到 Y 增大，等待落回底层")
            if stable != "top":
                return Action("wait", reason="等待顶层黄点稳定")
            self.set_direction(self.c.drop_direction)
            combat = self.combat_action(obs, now)
            if combat is not None:
                return combat
            if abs(x - self.c.top_drop_x) <= self.c.ladder_tolerance:
                return Action("drop_walk", self.c.drop_direction, "从顶层左侧走落")
            if self.check_stuck(x):
                return self.abort_route("前往换层点步行卡住")
            return Action("walk", self.direction, "周围清理后继续向顶层出口走落")
        if self.route == "climb":
            if stable == "top" and y <= self.c.top.y + .008:
                return Action("climb_exit", reason="到顶层 Y 后空格离梯")
            if self.ladder_grounded(y, stable):
                return self.retry_grab(now, "上爬过程中已回到底层")
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
        elif kind == "nudge":
            self.facing = action.direction
            self.busy_until = pressed_at + self.c.ladder_nudge_seconds + .04
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
            self.route, self.route_started = "grab_check", pressed_at
            self.grab_attempts += 1
            self.grab_started = pressed_at
            self.grab_origin_y = self.grab_last_y = obs.position[1]
            self.grab_late_rises = 0
            self.climb_y, self.climb_progress_at = obs.position[1], pressed_at
            self.busy_until = pressed_at + .18
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
