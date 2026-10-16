"""散步路2：单站位、左右清怪，击退后回位；不参与原地图巡逻和爬绳。"""
from __future__ import annotations

from .brain import Action, Brain, Observation
from .config import Config


class StationaryBrain(Brain):
    def __init__(self, config: Config, start_alt: bool = True):
        super().__init__(config, "stationary", start_alt=start_alt)
        self.floor = "stationary"
        self.home_frames = 0
        self.return_started: float | None = None
        self.return_last_x: float | None = None
        self.return_failed = False
        self.returning = False
        self.return_fighting = False
        self.off_platform = False
        self.step_phase = ""
        self.step_started = 0.0
        self.step_attempts = 0
        self.step_jump_at = 0.0
        self.step_height_y = 0.0
        self.step_height_since = 0.0
        self.step_height_frames = 0
        self.step_progress_x = 0.0
        self.step_progress_at = 0.0
        self.step_error = ""

    @property
    def needs_monsters(self) -> bool:
        return not self.step_phase or self.step_phase in ("landing", "approach")

    @property
    def phase(self) -> str:
        if self.step_phase:
            if self.return_fighting:
                return "下层先清怪"
            return {"landing": "等待下层落地", "approach": "向左返回台阶",
                    "jump": "左跳返回原平台", "blocked": "台阶返回受阻"}[self.step_phase]
        if self.off_platform:
            return "平台高度变化"
        if self.return_failed:
            return "回位受阻"
        if self.return_fighting:
            return "偏离后先清怪"
        if self.returning:
            return "击退回位"
        if self.home_frames < 3:
            return "确认站位"
        return "定点清怪" if self.clearing or self.burst is not None else "等待食人花刷新"

    def reset_observation(self) -> None:
        super().reset_observation()
        self.home_frames = 0
        self.return_started = None
        self.return_last_x = None
        self.return_failed = False
        self.returning = False
        self.return_fighting = False
        self.off_platform = False
        self.step_phase = ""
        self.step_attempts = 0
        self.step_error = ""

    def decide(self, obs: Observation, now: float) -> Action:
        if not obs.focused or obs.position is None:
            self.reset_observation()
            return Action("wait", reason="失焦或小地图黄点丢失，松键等待恢复")
        if self.c.station is None:
            return Action("wait", reason="尚未记录定点，请运行 --calibrate-stationary")
        x, y = obs.position
        sx, sy = self.c.station
        if self.step_phase:
            return self.step_action(obs, now)
        if abs(y - sy) > self.c.floor_tolerance:
            self.off_platform = True
            self.burst = None
            self.clearing = False
            self.reset_clear_evidence()
            self.home_frames = 0
            self.return_started = None
            self.return_last_x = None
            self.returning = self.return_failed = False
            self.return_fighting = False
            if y > sy and self.c.station_step_recovery:
                self.step_phase = "landing"
                self.step_started = now
                self.step_attempts = 0
                self.reset_step_height(y, now)
                return Action("wait", reason="检测到黄点 Y 增大，先松键等待下层落地；落地后向左返回台阶")
            return Action("wait", reason=f"平台高度变化：Y={y:.4f}，原站位 Y={sy:.4f}；已松键。"
                          "尚未记录下层返回路线，请先手动回到原平台")
        self.off_platform = False
        # 最后一次攻击也等完整周期，不用回位抢断施法。
        if now < self.busy_until:
            return Action("wait", reason="等待当前施法或转身完成")
        # 外圈容纳黄点量化和转身位移；回位停在一个像素内，不追逐不可分辨的亚像素位置。
        pixel = 1 / self.c.minimap_roi[2] if self.c.minimap_roi else .004
        tolerance = max(self.c.station_tolerance, 2 * pixel)
        inner = min(.75 * tolerance, max(pixel, .5 * self.c.station_tolerance))
        if abs(x - sx) > tolerance or self.returning:
            # 偏离站位时先就地清理射程内的两侧，避免一边回位一边持续被打退。
            combat = self.combat_action(obs, now, allow_random=False)
            if combat is not None:
                self.returning = self.return_fighting = True
                self.home_frames = 0
                self.return_started = self.return_last_x = None
                return combat
        self.return_fighting = False
        crossed = (self.returning and self.return_last_x is not None
                   and (x - sx) * (self.return_last_x - sx) <= 0)
        arrived = abs(x - sx) <= inner or (crossed and abs(x - sx) <= tolerance)
        if abs(x - sx) > tolerance or (self.returning and not arrived):
            self.home_frames = 0
            self.returning = True
            self.burst = None
            self.clearing = False
            self.reset_clear_evidence()
            if self.return_started is None:
                self.return_started = now
                self.return_last_x = x
            elif abs(x - self.return_last_x) >= .5 * pixel:
                # 只累计朝目标移动的进展；再次被击退后，新的回程也可刷新计时。
                if abs(x - sx) < abs(self.return_last_x - sx):
                    self.return_started = now
                self.return_last_x = x
            if self.return_failed or now - self.return_started >= self.c.station_recover_timeout:
                self.return_failed = True
                return Action("wait", reason=f"连续 {self.c.station_recover_timeout:g} 秒无回位进展，已松键；"
                              "请回到站位或 F8 暂停后恢复")
            direction = "right" if x < sx else "left"
            if crossed:
                return Action("wait", reason="回位已越过目标，先松键确认落点，再决定是否需要纠正")
            return Action("walk", direction, f"连续步行回位：当前 X={x:.4f}，目标 X={sx:.4f}，"
                          f"停止范围 ± {inner:.4f}")
        self.returning = self.return_failed = False
        self.return_started = None
        self.return_last_x = None
        self.home_frames += 1
        if self.home_frames < 3:
            return Action("wait", reason=f"松开方向，确认固定站位 {self.home_frames}/3")
        if self.next_alt is None:
            self.next_alt = now + self.c.alt_interval
        if self.startup_alt_pending or now >= self.next_alt:
            return Action("alt", reason="定点挂机启动或 190 秒定时 Alt")
        combat = self.combat_action(obs, now, allow_random=False, require_localized=True)
        return combat if combat is not None else Action("wait", reason="保持站位，等待左右食人花刷新")

    def reset_step_height(self, y: float, now: float) -> None:
        self.step_height_y = y
        self.step_height_since = now
        self.step_height_frames = 1

    def block_step(self, reason: str) -> Action:
        self.step_phase = "blocked"
        self.return_fighting = False
        self.step_error = reason
        return Action("wait", reason=reason + "；已松键，请手动回到原平台或 F8 暂停后重试")

    def step_action(self, obs: Observation, now: float) -> Action:
        x, y = obs.position
        sx, sy = self.c.station
        pixel_x = 1 / self.c.minimap_roi[2] if self.c.minimap_roi else .004
        pixel_y = 1 / self.c.minimap_roi[3] if self.c.minimap_roi else .004
        if abs(y - self.step_height_y) > pixel_y + 1e-9:
            self.reset_step_height(y, now)
        else:
            self.step_height_frames += 1
        height_stable = self.step_height_frames >= 3 and now - self.step_height_since >= .2 - 1e-9
        near_home_y = abs(y - sy) <= min(self.c.floor_tolerance, max(.004, 1.1 * pixel_y))
        jump_settled = self.step_phase != "jump" or now - self.step_jump_at >= .6
        if near_home_y and height_stable and jump_settled:
            self.step_phase = ""
            self.step_attempts = 0
            self.off_platform = False
            self.return_fighting = False
            self.burst = None
            self.clearing = False
            self.reset_clear_evidence()
            self.home_frames = 0
            self.return_started = self.return_last_x = None
            return Action("wait", reason="已重新确认原平台 Y，松开方向后按正常逻辑回到攻击站位")
        if self.step_phase == "blocked":
            return Action("wait", reason=self.step_error + "；已松键，请手动回到原平台或 F8 暂停后重试")
        if now - self.step_started > self.c.station_step_timeout:
            return self.block_step("台阶返回超时，未确认重新站上原平台")
        if now < self.busy_until:
            if self.return_fighting:
                self.step_started = now
            return Action("wait", reason="平台变化后等待上一招结束，再开始返回")
        if near_home_y:
            return (Action("walk", "left", "跳跃已接近原平台，保持向左并等待落地确认")
                    if self.step_phase == "jump" else Action("wait", reason="等待原平台高度连续稳定"))
        if self.step_phase == "jump":
            if now - self.step_jump_at < 1.2:
                return Action("walk", "left", "保持左方向完成台阶跳跃，不重复按空格")
            self.step_phase = "landing"
            self.reset_step_height(y, now)
            return Action("wait", reason="本次跳跃未确认上台阶，先松键等落地，再决定重试")
        if self.step_phase in ("landing", "approach") and not height_stable:
            self.step_phase = "landing"
            self.return_fighting = False
            self.burst = None
            self.clearing = False
            self.reset_clear_evidence()
            return Action("wait", reason="高度仍在变化，先松键等落地，再清怪和返回台阶")
        if self.step_phase == "landing":
            if not height_stable or y <= sy + self.c.floor_tolerance:
                return Action("wait", reason="等待下层黄点 Y 稳定，不在下落途中左右移动或跳跃")
        if self.step_phase in ("landing", "approach"):
            combat = self.combat_action(obs, now, allow_random=False)
            if combat is not None:
                self.return_fighting = True
                self.step_started = now
                self.step_progress_x, self.step_progress_at = x, now
                return combat
            self.return_fighting = False
        if self.step_phase == "landing":
            self.step_phase = "approach"
            self.step_progress_x, self.step_progress_at = x, now
        if x < sx - max(self.c.station_tolerance, 2 * pixel_x):
            return self.block_step("已向左越过站位仍在下层，与已确认的单台阶路线不符")
        if self.step_progress_x - x >= .5 * pixel_x:
            self.step_progress_x, self.step_progress_at = x, now
        elif x - self.step_progress_x >= .5 * pixel_x:
            # 下层再次被击退时重新观察向左步行，不把击退瞬间判作台阶边。
            self.step_progress_x, self.step_progress_at = x, now
        if now - self.step_progress_at >= .4:
            if self.step_attempts >= self.c.station_step_max_attempts:
                return self.block_step(f"已尝试 {self.step_attempts} 次左跳，仍未确认上台阶")
            return Action("step_jump", "left", "向左步行已在台阶边受阻，保持左键并按一次空格")
        return Action("walk", "left", "下层已落地，连续向左走到台阶边")

    def executed(self, action: Action, pressed_at: float, obs: Observation) -> None:
        if action.kind == "step_jump":
            self.step_phase = "jump"
            self.step_attempts += 1
            self.step_jump_at = pressed_at
            self.facing = "left"
            self.reset_step_height(obs.position[1], pressed_at)
        else:
            super().executed(action, pressed_at, obs)
