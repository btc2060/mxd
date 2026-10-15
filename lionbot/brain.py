from __future__ import annotations

from dataclasses import dataclass

from .config import Config


@dataclass(frozen=True)
class Mob:
    feet: tuple[float, float]
    score: float = 1.0
    confirmed: bool = True


@dataclass(frozen=True)
class Observation:
    position: tuple[float, float] | None
    player_feet: tuple[float, float] | None
    mobs: tuple[Mob, ...] = ()
    focused: bool = True


@dataclass(frozen=True)
class Action:
    kind: str
    direction: str = ""
    reason: str = ""


WAIT = Action("wait")


class Brain:
    """只使用最新观测；按键执行与决策分开，便于测试时序和路线。"""

    def __init__(self, config: Config, mode: str = "auto"):
        self.c = config
        self.mode = mode
        self.floor: str | None = None
        self.facing = ""
        self.direction = "right"
        self.busy_until = 0.0
        self.next_alt: float | None = None
        self.next_teleport = 0.0
        self.teleport_fault = False
        self.teleport_origin: float | None = None
        self.route = ""
        self.route_started = 0.0
        self.route_disabled = False
        self.climb_progress_at = 0.0
        self.climb_y = 1.0
        self.last_mob_at = 0.0
        self.last_edge = ""
        self.empty_legs = 0
        self.last_scan_x: float | None = None
        self.move_origin: tuple[float, float] | None = None
        self.candidate_floor: str | None = None
        self.floor_frames = 0

    def reset_observation(self) -> None:
        """失焦、暂停或定位丢失后，不继承空场证据和运动基准。"""
        self.move_origin = None
        self.last_scan_x = None
        self.empty_legs = 0
        self.last_edge = ""
        self.candidate_floor = None
        self.floor_frames = 0
        self.facing = ""

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

    def enter_floor(self, floor: str, now: float, x: float) -> None:
        self.floor = floor
        self.last_mob_at = now
        self.empty_legs = 0
        self.last_edge = ""
        self.last_scan_x = x
        self.move_origin = None
        self.direction = "left" if floor == "top" else "right"

    def decide(self, obs: Observation, now: float) -> Action:
        if not obs.focused or obs.position is None or obs.player_feet is None:
            self.reset_observation()
            self.last_mob_at = now
            return Action("wait", reason="失焦或定位丢失，松开按键")
        x, y = obs.position
        if self.next_alt is None:
            self.next_alt = now + self.c.alt_interval
        stable = self.stable_floor(y)
        if now < self.busy_until:
            return Action("wait", reason="等待施法/瞬移/跳跃结束，并重新截图")
        if self.teleport_origin is not None:
            if abs(x - self.teleport_origin) > self.c.teleport_max_dx + 0.008:
                self.teleport_fault = True
            self.teleport_origin = None
        if self.route in ("align", "drop_align") and stable is not None:
            if any(abs(m.feet[1] - obs.player_feet[1]) <= self.c.feet_tolerance_px for m in obs.mobs):
                # 去梯子/出口途中刷新怪物时，先取消换层，重新清理当前层。
                self.route = ""
                self.enter_floor(stable, now, x)
        if self.route:
            return self.route_action(obs, stable, now)
        if stable is None:
            return Action("wait", reason="等待确认底层或顶层；中层不执行台阶逻辑")
        if self.mode != "auto" and stable != self.mode:
            return Action("wait", reason="当前楼层与指定模式不一致")
        floor = getattr(self.c, stable)
        if not floor.configured:
            return Action("wait", reason="请先配置顶层左右端，不能套用底层边界")
        if not floor.left - self.c.boundary_margin <= x <= floor.right + self.c.boundary_margin:
            return Action("wait", reason="坐标超出所配平台，检查小地图裁剪和边界")
        if self.floor != stable:
            self.enter_floor(stable, now, x)
        if now >= self.next_alt:
            return Action("alt", reason="190 秒定时按键")

        same_floor = [m for m in obs.mobs
                      if abs(m.feet[1] - obs.player_feet[1]) <= self.c.feet_tolerance_px]
        if same_floor:
            # 包括尚待第二帧确认的候选，不把识别抖动当成空场。
            self.last_mob_at = now
            self.empty_legs = 0
            self.last_edge = ""
        scale = self.c.world_pixels_per_x
        if scale is None:
            return Action("wait", reason="缺少主画面/小地图距离校准")
        in_range = [m for m in same_floor if m.confirmed and
                    abs(m.feet[0] - obs.player_feet[0]) / scale <= self.c.attack_range]
        if in_range:
            left = [m for m in in_range if m.feet[0] < obs.player_feet[0]]
            right = [m for m in in_range if m.feet[0] >= obs.player_feet[0]]
            if len(left) == len(right):
                direction = self.facing if self.facing in ("left", "right") else self.direction
            else:
                direction = "left" if len(left) > len(right) else "right"
            self.move_origin = None
            if self.facing != direction:
                return Action("face", direction, "朝向射程内怪物；转身后重新确认")
            return Action("attack", direction, "D 火焰箭；每次攻击后重新识别")

        empty = now - self.last_mob_at >= self.c.empty_seconds
        if (self.mode == "auto" and self.c.auto_route and not self.route_disabled
                and empty and self.empty_legs >= 2 * self.c.empty_sweeps
                and (stable == "top" or (not self.teleport_fault and self.c.teleport_max_dx is not None))):
            self.route = "align" if stable == "bottom" else "drop_align"
            self.route_started = now
            self.move_origin = None
            return self.route_action(obs, stable, now)

        low, high = floor.left + self.c.turn_inset, floor.right - self.c.turn_inset
        if x >= high and self.direction == "right":
            self.reach_edge("right")
            self.direction = "left"
        elif x <= low and self.direction == "left":
            self.reach_edge("left")
            self.direction = "right"
        if self.move_origin is not None:
            old_x, commanded_seconds = self.move_origin
            if abs(x - old_x) >= 0.006:
                self.move_origin = (x, 0.0)
            elif commanded_seconds >= 0.8:
                self.direction = "left" if self.direction == "right" else "right"
                self.move_origin = None
                self.empty_legs = 0
                self.last_edge = ""
                return Action("wait", reason="地面移动卡住，反向巡视；暂不处理台阶")
        if self.last_scan_x is None or abs(x - self.last_scan_x) >= self.c.scan_step:
            self.last_scan_x = x
            self.move_origin = None
            return Action("scan", reason="行走定点停下识别")
        return self.travel(obs, self.direction, low, high, now)

    def reach_edge(self, edge: str) -> None:
        if self.last_edge and self.last_edge != edge:
            self.empty_legs += 1
        self.last_edge = edge

    def travel(self, obs: Observation, direction: str, low: float, high: float, now: float) -> Action:
        x = obs.position[0]
        dx = self.c.teleport_max_dx
        if dx is not None and not self.teleport_fault and now >= self.next_teleport:
            projected = x + (dx if direction == "right" else -dx)
            far_enough = all(abs(m.feet[0] - obs.player_feet[0]) / self.c.world_pixels_per_x
                             > dx + 0.025 for m in obs.mobs
                             if abs(m.feet[1] - obs.player_feet[1]) <= self.c.feet_tolerance_px)
            if low + self.c.boundary_margin <= projected <= high - self.c.boundary_margin and far_enough:
                return Action("teleport", direction, "方向键 + F，瞬移后重新定位")
        reason = "瞬移位移超过安全上限，已停用 F，改用步行" if self.teleport_fault else "步行巡视"
        return Action("walk", direction, reason)

    def abort_route(self, reason: str) -> Action:
        self.route = ""
        self.route_disabled = True
        self.empty_legs = 0
        self.move_origin = None
        return Action("wait", reason=reason + "；停用自动换层，本层仍可清怪")

    def route_action(self, obs: Observation, stable: str | None, now: float) -> Action:
        x, y = obs.position
        if now - self.route_started > self.c.climb_timeout:
            return self.abort_route("换层超时，未确认到达")
        if self.route in ("align", "drop_align"):
            target = self.c.ladder_x if self.route == "align" else self.c.top_drop_x
            expected = "bottom" if self.route == "align" else "top"
            if self.route == "drop_align" and y > self.c.top.y + self.c.floor_tolerance:
                self.route = "fall"
                return Action("wait", reason="检测到 Y 增大，已从顶层出口开始下落")
            if stable != expected:
                return Action("wait", reason="等待换层起点稳定")
            if abs(x - target) > self.c.ladder_tolerance:
                return Action("walk", "right" if x < target else "left", "步行精确对齐换层点")
            if self.route == "align":
                return Action("grab", "up", "对齐梯子后，上 + 空格抓梯")
            return Action("drop_walk", self.c.drop_direction, "从顶层左侧出口直接走落，不按跳跃")
        if self.route == "climb":
            if y <= self.c.top.y + 0.008:
                return Action("climb_exit", reason="到达顶层 Y 后松开上键，按空格离梯")
            if self.climb_y - y >= 0.008:
                self.climb_y, self.climb_progress_at = y, now
            if now - self.climb_progress_at > self.c.climb_stuck_seconds:
                return self.abort_route("爬梯 Y 未继续上升")
            return Action("climb", "up", "持续上爬，监测 Y 和超时")
        if self.route == "exit":
            if stable != "top":
                return Action("wait", reason="离梯后等待顶层坐标稳定")
            landing = x - self.c.teleport_max_dx
            if not (self.c.top.left + self.c.boundary_margin <= landing
                    <= self.c.top.right - self.c.boundary_margin):
                return self.abort_route("顶层左瞬移落点超出安全范围")
            return Action("top_teleport", "left", "确认到顶后向左瞬移一次")
        if self.route == "top_settle":
            if stable == "top":
                self.route = ""
                self.enter_floor("top", now, x)
            return Action("wait", reason="确认顶层瞬移落地")
        if self.route == "fall":
            if stable == "bottom":
                self.route = ""
                self.enter_floor("bottom", now, x)
                return Action("wait", reason="已确认落回底层")
            if abs(y - self.c.top.y) <= self.c.floor_tolerance:
                return Action("walk", self.c.drop_direction, "走出已确认的出口")
            return Action("wait", reason="等待落回底层；不在中间层攻击或移动")
        return self.abort_route("未知换层状态")

    def executed(self, action: Action, pressed_at: float, obs: Observation) -> None:
        """使用真正按下按键的时间计时，避免截图和转身耗时造成间隔不足。"""
        kind = action.kind
        if kind == "attack":
            self.busy_until = pressed_at + self.c.attack_interval
        elif kind == "alt":
            self.next_alt = pressed_at + self.c.alt_interval
            self.busy_until = pressed_at + 0.20
        elif kind == "face":
            self.facing = action.direction
            self.busy_until = pressed_at + 0.12
        elif kind == "scan":
            self.busy_until = pressed_at + self.c.scan_seconds
        elif kind in ("teleport", "top_teleport"):
            self.facing = action.direction
            self.teleport_origin = obs.position[0]
            self.next_teleport = pressed_at + self.c.teleport_cooldown
            self.busy_until = pressed_at + self.c.teleport_settle
            if kind == "top_teleport":
                self.route = "top_settle"
        elif kind == "grab":
            self.route = "climb"
            self.climb_y = obs.position[1]
            self.climb_progress_at = pressed_at
            self.busy_until = pressed_at + 0.40
        elif kind == "climb_exit":
            self.route = "exit"
            self.busy_until = pressed_at + 0.60
        elif kind == "drop_walk":
            self.route = "fall"
            self.busy_until = pressed_at + 0.45
        if kind in ("walk", "teleport"):
            self.facing = action.direction
            if self.move_origin is None:
                self.move_origin = (obs.position[0], 0.0)
            old_x, commanded = self.move_origin
            if abs(obs.position[0] - old_x) >= 0.006:
                old_x, commanded = obs.position[0], 0.0
            self.move_origin = (old_x, commanded + (0.06 if kind == "walk" else 0.11))
        elif kind != "wait":
            self.move_origin = None
