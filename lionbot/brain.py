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
    if config.attack_reach_px is None:
        return "uncalibrated"
    return "ready" if abs(dx) <= config.attack_reach_px else "range"


class Brain:
    """巡逻方向与战斗朝向独立；周围清理确认后才恢复原路线。"""

    GROUND_ROUTES = frozenset(("align", "ladder_clear", "ladder_align", "drop_align"))
    ROUTE_LABELS = {"align": "前往绳索", "ladder_clear": "入口清怪",
                    "ladder_align": "入口微调", "grab_check": "抓绳验证",
                    "grab_retry": "等待落地重试", "climb": "上爬", "descend": "沿绳下返",
                    "exit": "离绳落地", "top_settle": "顶层落地",
                    "drop_align": "前往下落点", "fall": "下落"}

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
        self.route_interrupted = False
        self.descent_y = 0.0
        self.descent_progress_at = 0.0
        self.descent_grounded_at: float | None = None

    @property
    def phase(self) -> str:
        if self.route:
            return self.ROUTE_LABELS.get(self.route, self.route)
        return "清怪" if self.clearing or self.burst is not None else "巡逻"

    @property
    def needs_monsters(self) -> bool:
        return not self.route or self.route in self.GROUND_ROUTES

    def reset_floor_evidence(self) -> None:
        self.candidate_floor = None
        self.floor_frames = 0

    def reset_observation(self) -> None:
        """完成的往返保留；失焦/黄点丢失不能补算未完成的半程。"""
        self.burst = None
        self.clearing = False
        self.reset_clear_evidence()
        self.visual_lost_at = None
        self.ladder_aligned_frames = 0
        self.route_interrupted = bool(self.route)
        self.move_origin = None
        self.walk_position = None
        self.walked_since_teleport = 0.0
        self.reset_floor_evidence()
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
        if (self.route and self.route not in self.GROUND_ROUTES and candidate is not None
                and not self.at_floor_height(y, candidate)):
            candidate = None
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
        if self.route_interrupted:
            return self.resume_route(obs, stable, now)
        if now < self.busy_until:
            if self.route in ("grab_check", "climb", "descend"):
                direction = "down" if self.route == "descend" else "up"
                return Action("climb", direction, "换层动作尚在进行，持续保持绳索方向")
            if self.route in ("exit", "top_settle"):
                self.reset_floor_evidence()
            return Action("wait", reason="等待施法/瞬移/跳跃周期")
        if self.teleport_origin is not None:
            if abs(x - self.teleport_origin) > self.c.teleport_max_dx + .008:
                self.teleport_fault = True
            self.teleport_origin = None
        if stable is not None and self.floor != stable and not self.route:
            self.enter_floor(stable, now, x)
        if stable is not None and not self.route:
            if self.mode != "auto" and stable != self.mode:
                self.reset_clear_evidence()
                return Action("wait", reason="当前楼层与指定模式不一致")
            floor = getattr(self.c, stable)
            if not floor.configured or not floor.left - self.c.boundary_margin <= x <= floor.right + self.c.boundary_margin:
                self.reset_clear_evidence()
                return Action("wait", reason="黄点超出所配平台，检查地图边界")
        # 空中/绳上只有换层控制器能发动作，定时键顺延到落地，不能抢上键。
        if (stable is not None and self.at_floor_height(y, stable)
                and (not self.route or self.route in self.GROUND_ROUTES)):
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

        return self.patrol_action(obs, stable, now)

    def patrol_action(self, obs: Observation, stable: str, now: float) -> Action:
        x = obs.position[0]
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
                    and self.completed_round_trips >= self.c.patrol_round_trips):
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
        # 主循环每帧已经识别两侧，无目标时保持步行，不再定距停步打断移动。
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
        if dx == 0:
            return self.facing or self.direction
        return "right" if dx > 0 else "left"

    def combat_action(self, obs: Observation, now: float, *, force_check: bool = False,
                      allow_random: bool = True, require_localized: bool = False) -> Action | None:
        x, y = obs.position
        candidates = [m for m in obs.mobs
                      if target_status(m, obs, self.c) in ("ready", "pending")
                      and self.c.attack_reach_px is not None
                      and abs(m.feet[0] - obs.player_feet[0]) <= self.c.attack_reach_px]
        ready = [m for m in candidates if m.confirmed and not m.recovered]
        if candidates or force_check or (require_localized and obs.player_feet is None):
            if not self.clearing:
                self.last_burst_direction = ""
            self.clearing = True
        if candidates:
            self.reset_clear_evidence()
        if (force_check or candidates) and self.burst is not None and self.burst.random:
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
            nearest = min(ready, key=lambda m: (abs(m.feet[0] - obs.player_feet[0]),
                                               self.combat_direction(m, obs) != self.facing,
                                               self.combat_direction(m, obs) != self.direction))
            preferences = (other if self.last_burst_direction else "", self.combat_direction(nearest, obs),
                           self.facing, self.direction, "left", "right")
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
        self.route_interrupted = False
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
        return stable == "bottom" and self.at_floor_height(y, "bottom")

    def at_floor_height(self, y: float, floor: str) -> bool:
        pixel = 1 / self.c.minimap_roi[3] if self.c.minimap_roi else .008
        tolerance = min(self.c.floor_tolerance, max(.008, pixel))
        return abs(y - getattr(self.c, floor).y) <= tolerance

    def ladder_alignment_tolerance(self) -> float:
        # 已有配置可能放宽到多个小地图像素；抓绳要靠近中心，而非停在范围边缘。
        pixel_tolerance = .6 / self.c.minimap_roi[2] if self.c.minimap_roi else self.c.ladder_tolerance
        return min(self.c.ladder_tolerance, pixel_tolerance)

    def route_action(self, obs: Observation, stable: str | None, now: float) -> Action:
        # 一帧只由一个阶段决定动作。绳上和空中阶段不调用地面战斗逻辑。
        handlers = {"align": self.approach_ladder, "ladder_clear": self.clear_ladder,
                    "ladder_align": self.align_ladder, "grab_check": self.check_grab,
                    "grab_retry": self.wait_for_retry, "climb": self.climb_rope,
                    "descend": self.descend_rope, "exit": self.exit_rope,
                    "top_settle": self.settle_top, "drop_align": self.approach_drop,
                    "fall": self.wait_for_fall}
        if (self.route in ("exit", "top_settle", "fall")
                and now - self.route_started > self.c.climb_timeout):
            return self.abort_route("换层超时，黄点未确认到达")
        handler = handlers.get(self.route)
        return handler(obs, stable, now) if handler else self.abort_route("未知换层状态")

    def resume_route(self, obs: Observation, stable: str | None, now: float) -> Action:
        """失焦/黄点丢失后重新判断所在位置，不沿用暂停前的抓绳和落地证据。"""
        if self.floor_frames < 3:
            return Action("wait", reason="换层已中断，重新确认当前黄点位置")
        x, y = obs.position
        old_route = self.route
        self.route_interrupted = False
        self.route_started = now
        if self.ladder_grounded(y, stable):
            if (old_route in ("grab_check", "climb", "descend")
                    and abs(x - self.c.ladder_x) <= 2 * self.ladder_alignment_tolerance()):
                self.route = "descend"
                self.descent_y, self.descent_progress_at = y, now
                self.descent_grounded_at = None
                return self.descend_rope(obs, stable, now)
            if old_route in ("fall", "drop_align", "top_settle", "exit"):
                self.route = "fall"
            else:
                self.route = "align"
            return self.route_action(obs, stable, now)
        if stable == "top" and self.at_floor_height(y, "top"):
            if old_route in ("grab_check", "climb", "descend"):
                return Action("climb_exit", reason="恢复后确认已到顶，先跳离绳索")
            self.route = "drop_align" if old_route in ("fall", "drop_align") else "exit"
            return self.route_action(obs, stable, now)
        if abs(x - self.c.ladder_x) <= 2 * self.ladder_alignment_tolerance():
            if old_route == "descend":
                self.descent_y, self.descent_progress_at = y, now
                self.descent_grounded_at = None
                return Action("climb", "down", "恢复后沿绳继续下返")
            if old_route in ("grab_check", "climb"):
                self.route = "grab_check"
                self.grab_started = now
                self.grab_origin_y = self.grab_last_y = y
                self.grab_late_rises = 0
                return Action("climb", "up", "恢复后先保持上键，重新验证持续上升")
        if old_route in ("fall", "drop_align", "exit", "top_settle"):
            self.route = "fall"
            return self.wait_for_fall(obs, stable, now)
        return self.retry_grab(now, "恢复后未确认在绳上")

    def approach_ladder(self, obs: Observation, stable: str | None, now: float) -> Action:
        x, _ = obs.position
        if stable != "bottom":
            return Action("wait", reason="等待底层黄点稳定后前往爬绳入口")
        self.set_direction("right" if x < self.c.ladder_x else "left")
        approach = max(3 * self.c.ladder_tolerance, self.c.attack_range / 2)
        if abs(x - self.c.ladder_x) <= approach:
            self.route = "ladder_clear"
            self.reset_clear_evidence()
            return self.clear_ladder(obs, stable, now)
        combat = self.combat_action(obs, now)
        if combat is not None:
            self.move_origin = None
            return combat
        if self.check_stuck(x):
            return self.abort_route("前往爬绳入口步行卡住")
        return Action("walk", self.direction, "清理周围后，按原路线前往爬绳入口")

    def clear_ladder(self, obs: Observation, stable: str | None, now: float) -> Action:
        if not self.ladder_grounded(obs.position[1], stable):
            return Action("wait", reason="抓绳前等待底层落地稳定")
        combat = self.combat_action(obs, now, force_check=True, allow_random=False, require_localized=True)
        if combat is not None:
            self.move_origin = None
            return combat
        self.route = "ladder_align"
        self.ladder_align_started = now
        self.ladder_aligned_frames = 0
        return self.align_ladder(obs, stable, now)

    def align_ladder(self, obs: Observation, stable: str | None, now: float) -> Action:
        x, y = obs.position
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
        return Action("grab", "up", "周围清理完、X 连续对齐后，上＋空格抓绳并保持上键")

    def check_grab(self, obs: Observation, stable: str | None, now: float) -> Action:
        x, y = obs.position
        elapsed = now - self.grab_started
        step = max(.004, .5 / self.c.minimap_roi[3]) if self.c.minimap_roi else .004
        delta = self.grab_last_y - y
        if abs(delta) >= step:
            # 累计不足阈值的小位移，不把高帧率下每帧的小幅上升丢掉。
            self.grab_last_y = y
            if delta < 0:
                self.grab_late_rises = 0
            elif elapsed >= self.c.grab_confirm_seconds * .75:
                self.grab_late_rises += 1
        minimum = max(self.c.grab_min_rise, 2 / self.c.minimap_roi[3]) if self.c.minimap_roi else self.c.grab_min_rise
        aligned = abs(x - self.c.ladder_x) <= 2 * self.ladder_alignment_tolerance()
        at_top = stable == "top" and self.at_floor_height(y, "top")
        if (aligned and elapsed >= self.c.grab_confirm_seconds
                and (at_top or (self.grab_origin_y - y >= minimum and self.grab_late_rises >= 2))):
            self.route = "climb"
            self.climb_y, self.climb_progress_at = y, now
            return self.climb_rope(obs, stable, now)
        if elapsed >= self.c.grab_timeout or not aligned:
            return self.retry_grab(now, "未确认持续上升，抓绳未成功或已偏离")
        return Action("climb", "up", "保持上键，验证跳跃后黄点仍持续上升")

    def wait_for_retry(self, obs: Observation, stable: str | None, now: float) -> Action:
        if self.ladder_grounded(obs.position[1], stable):
            self.route = "ladder_clear"
            return self.clear_ladder(obs, stable, now)
        if now - self.retry_started > self.c.grab_timeout + self.c.grab_confirm_seconds:
            return self.abort_route("抓绳重试前未确认回到底层，不能在空中微调")
        return Action("wait", reason="抓绳未确认，松键等待底层落地后重试")

    def climb_rope(self, obs: Observation, stable: str | None, now: float) -> Action:
        x, y = obs.position
        if abs(x - self.c.ladder_x) > 2 * self.ladder_alignment_tolerance():
            return self.retry_grab(now, "上爬时已偏离绳索，等待落地")
        if stable == "top" and self.at_floor_height(y, "top"):
            return Action("climb_exit", reason="到顶层 Y 连续稳定后，松上并空格离绳")
        if self.ladder_grounded(y, stable):
            return self.retry_grab(now, "上爬过程中已回到底层")
        if self.climb_y - y >= .008:
            self.climb_y, self.climb_progress_at = y, now
        if (now - self.climb_progress_at > self.c.climb_stuck_seconds
                or now - self.route_started > self.c.climb_timeout):
            if self.grab_attempts >= self.c.ladder_max_attempts:
                return self.abort_route("上爬未完成，已用完抓绳尝试次数")
            self.route, self.route_started = "descend", now
            self.descent_y, self.descent_progress_at = y, now
            self.descent_grounded_at = None
            return Action("climb", "down", "上爬停滞，先沿绳下返到底层再清怪重试")
        return Action("climb", "up", "按黄点 Y 持续爬绳，不插入地面动作")

    def descend_rope(self, obs: Observation, stable: str | None, now: float) -> Action:
        x, y = obs.position
        if self.ladder_grounded(y, stable):
            if self.descent_grounded_at is None:
                self.descent_grounded_at = now
            if now - self.descent_grounded_at < .2:
                return Action("climb", "down", "黄点已到底层，短暂保持下键走完最后一个像素")
            self.route = "ladder_clear"
            return self.clear_ladder(obs, stable, now)
        self.descent_grounded_at = None
        if abs(x - self.c.ladder_x) > 2 * self.ladder_alignment_tolerance():
            return self.retry_grab(now, "下返时已离绳，等待底层落地")
        if y - self.descent_y >= .008:
            self.descent_y, self.descent_progress_at = y, now
        if (now - self.descent_progress_at > self.c.climb_stuck_seconds
                or now - self.route_started > self.c.climb_timeout):
            return self.abort_route("沿绳下返也未取得进展，无法确认底层重试位置")
        return Action("climb", "down", "保持下键，确认回到底层后再重试")

    def exit_rope(self, obs: Observation, stable: str | None, now: float) -> Action:
        x, y = obs.position
        if stable == "bottom":
            self.route = "fall"
            return self.wait_for_fall(obs, stable, now)
        if stable != "top" or not self.at_floor_height(y, "top"):
            return Action("wait", reason="离绳后等待新的顶层落地观测，不沿用跳跃前的坐标")
        dx = self.c.teleport_max_dx
        if (dx is not None and not self.teleport_fault
                and self.c.top.left + self.c.boundary_margin <= x - dx <= self.c.top.right - self.c.boundary_margin):
            return Action("top_teleport", "left", "顶层离绳落地后向左瞬移一次")
        self.route = "top_settle"
        return self.settle_top(obs, stable, now)

    def settle_top(self, obs: Observation, stable: str | None, now: float) -> Action:
        x, y = obs.position
        if stable == "bottom":
            self.route = "fall"
            return self.wait_for_fall(obs, stable, now)
        if stable != "top" or not self.at_floor_height(y, "top"):
            return Action("wait", reason="确认离绳后顶层落地，暂不攻击或继续瞬移")
        if x > self.c.ladder_x - max(2 * self.ladder_alignment_tolerance(), self.c.boundary_margin):
            return Action("walk", "left", "向左走离绳索；无瞬移校准或瞬移未生效也可继续")
        self.route = ""
        self.enter_floor("top", now, x)
        return Action("wait", reason="确认顶层落地，向左清怪")

    def approach_drop(self, obs: Observation, stable: str | None, now: float) -> Action:
        x, y = obs.position
        if y > self.c.top.y + self.c.floor_tolerance:
            self.route, self.route_started = "fall", now
            return self.wait_for_fall(obs, stable, now)
        if stable != "top":
            return Action("wait", reason="等待顶层黄点稳定")
        self.set_direction(self.c.drop_direction)
        combat = self.combat_action(obs, now)
        if combat is not None:
            return combat
        reached = (x <= self.c.top_drop_x + self.c.ladder_tolerance if self.c.drop_direction == "left"
                   else x >= self.c.top_drop_x - self.c.ladder_tolerance)
        if reached:
            return Action("drop_walk", self.c.drop_direction, "从顶层出口继续走落，观察 Y 增大")
        if self.check_stuck(x):
            return self.abort_route("前往换层点步行卡住")
        return Action("walk", self.direction, "周围清理后继续向顶层出口走落")

    def wait_for_fall(self, obs: Observation, stable: str | None, now: float) -> Action:
        _, y = obs.position
        if self.ladder_grounded(y, stable):
            self.route = ""
            self.enter_floor("bottom", now, obs.position[0])
            return Action("wait", reason="落回底层，重新开始三轮巡逻")
        if abs(y - self.c.top.y) <= self.c.floor_tolerance:
            return Action("walk", self.c.drop_direction, "继续走出顶层出口，确认下落后再松方向")
        return Action("wait", reason="等待黄点确认落回底层")

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
            self.busy_until = pressed_at + self.c.face_hold + .04
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
                self.route_started = pressed_at
                self.reset_floor_evidence()
        elif kind == "grab":
            self.route, self.route_started = "grab_check", pressed_at
            self.grab_attempts += 1
            self.grab_started = pressed_at
            self.grab_origin_y = self.grab_last_y = obs.position[1]
            self.grab_late_rises = 0
            self.climb_y, self.climb_progress_at = obs.position[1], pressed_at
            self.busy_until = pressed_at + .18
            self.reset_floor_evidence()
        elif kind == "climb_exit":
            self.route = "exit"
            self.route_started = pressed_at
            self.busy_until = pressed_at + .6
            self.reset_floor_evidence()
        elif kind == "drop_walk":
            self.route, self.route_started = "fall", pressed_at
            self.reset_floor_evidence()
        if kind == "walk":
            self.facing = action.direction
            self.walk_position = obs.position[0]
            if self.move_origin is None:
                self.move_origin = (obs.position[0], 0.0)
            old_x, seconds = self.move_origin
            self.move_origin = (old_x, seconds + .06)
