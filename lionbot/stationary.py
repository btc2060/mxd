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
        self.return_failed = False
        self.returning = False

    @property
    def phase(self) -> str:
        if self.return_failed:
            return "回位受阻"
        if self.returning:
            return "击退回位"
        if self.home_frames < 3:
            return "确认站位"
        return "定点清怪" if self.clearing or self.burst is not None else "等待食人花刷新"

    def reset_observation(self) -> None:
        super().reset_observation()
        self.home_frames = 0
        self.return_started = None
        self.return_failed = False
        self.returning = False

    def decide(self, obs: Observation, now: float) -> Action:
        if not obs.focused or obs.position is None:
            self.reset_observation()
            return Action("wait", reason="失焦或小地图黄点丢失，松键等待恢复")
        if self.c.station is None:
            return Action("wait", reason="尚未记录定点，请运行 --calibrate-stationary")
        x, y = obs.position
        sx, sy = self.c.station
        if abs(y - sy) > self.c.floor_tolerance:
            self.burst = None
            self.home_frames = 0
            self.return_started = None
            return Action("wait", reason=f"Y={y:.4f} 不在记录的平台 Y={sy:.4f}，请先手动回到该平台")
        # 最后一次攻击也等完整周期，不用回位抢断施法。
        if now < self.busy_until:
            return Action("wait", reason="等待当前施法或转身完成")
        # 外圈容纳黄点量化和转身位移；一旦开始回位，就走回内圈，不能在边缘停下又转出去。
        pixel = 1 / self.c.minimap_roi[2] if self.c.minimap_roi else .004
        tolerance = max(self.c.station_tolerance, 2 * pixel)
        inner = min(tolerance / 2, .6 * pixel)
        if abs(x - sx) > tolerance or (self.returning and abs(x - sx) > inner):
            self.home_frames = 0
            self.returning = True
            self.burst = None
            self.clearing = False
            self.reset_clear_evidence()
            if self.return_started is None:
                self.return_started = now
            if self.return_failed or now - self.return_started >= self.c.station_recover_timeout:
                self.return_failed = True
                return Action("wait", reason="回位超时，已松键；请回到站位或 F8 暂停后恢复")
            direction = "right" if x < sx else "left"
            # 大位移持续步行，最后几个小地图像素才短按；不夹杂长等待。
            kind = "walk" if abs(x - sx) > 3 * tolerance else "nudge"
            return Action(kind, direction, f"击退回位：当前 X={x:.4f}，目标 X={sx:.4f} ± {tolerance:.4f}")
        self.returning = self.return_failed = False
        self.return_started = None
        self.home_frames += 1
        if self.home_frames < 3:
            return Action("wait", reason=f"松开方向，确认固定站位 {self.home_frames}/3")
        if self.next_alt is None:
            self.next_alt = now + self.c.alt_interval
        if self.startup_alt_pending or now >= self.next_alt:
            return Action("alt", reason="定点挂机启动或 190 秒定时 Alt")
        combat = self.combat_action(obs, now, allow_random=False, require_localized=True)
        return combat if combat is not None else Action("wait", reason="保持站位，等待左右食人花刷新")
