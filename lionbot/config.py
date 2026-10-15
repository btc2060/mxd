from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Floor:
    y: float
    left: float | None
    right: float | None

    @property
    def configured(self) -> bool:
        return self.left is not None and self.right is not None


@dataclass(frozen=True)
class Config:
    window_title: str = "冒险岛怀旧服"
    # 以下 ROI 均为游戏客户区像素坐标 [x, y, width, height]。
    minimap_roi: tuple[int, int, int, int] | None = None
    combat_roi: tuple[int, int, int, int] | None = None
    frame_size: tuple[int, int] | None = None
    player_template: str = "calibration/player.png"
    player_feet_offset: tuple[float, float] | None = None
    template_manifest: str = "calibration/monsters.json"
    world_pixels_per_x: float | None = None
    bottom: Floor = Floor(0.759, 0.029, 0.659)
    top: Floor = Floor(0.130, 0.045, 0.212)
    floor_tolerance: float = 0.035
    # 只向内缩 0.055，仍可覆盖距边缘 0.086 内的怪物。
    turn_inset: float = 0.055
    attack_range: float = 0.086
    attack_interval: float = 0.9
    attack_hold: float = 0.08
    alt_interval: float = 190.0
    teleport_max_dx: float | None = None
    teleport_cooldown: float = 0.65
    teleport_settle: float = 0.35
    boundary_margin: float = 0.012
    scan_step: float = 0.05
    scan_seconds: float = 0.22
    empty_seconds: float = 3.0
    empty_sweeps: int = 2
    feet_tolerance_px: float = 22.0
    confirmation_frames: int = 2
    player_threshold: float = 0.82
    monster_threshold: float = 0.82
    edge_threshold: float = 0.40
    # 用户已确认 X=.212 直通顶层，顶层 X=.045 可向左走落。
    auto_route: bool = True
    ladder_x: float = 0.212
    ladder_tolerance: float = 0.008
    climb_timeout: float = 18.0
    climb_stuck_seconds: float = 1.5
    top_drop_x: float | None = 0.045
    drop_direction: str = "left"

    def validate(self) -> None:
        for floor in (self.bottom, self.top):
            if not 0 <= floor.y <= 1:
                raise ValueError("楼层 Y 必须是小地图归一化坐标")
            if (floor.left is None) != (floor.right is None):
                raise ValueError("楼层左右端必须一起设置")
            if floor.configured and not 0 <= floor.left < floor.right <= 1:
                raise ValueError("楼层左右端必须满足 0 <= left < right <= 1")
            if floor.configured and 2 * self.turn_inset >= floor.right - floor.left:
                raise ValueError("掉头内缩量超过平台宽度")
        for roi in (self.minimap_roi, self.combat_roi):
            if roi is not None and (len(roi) != 4 or min(roi[:2]) < 0 or min(roi[2:]) <= 0):
                raise ValueError("ROI 必须为 [x, y, width, height]")
        if not 0 < self.attack_hold < self.attack_interval or self.attack_interval < 0.9:
            raise ValueError("攻击间隔至少 0.9 秒，按住时间必须小于间隔")
        if self.alt_interval <= 0 or not 0 < self.attack_range <= 1:
            raise ValueError("Alt 周期和攻击距离必须为正数")
        if self.world_pixels_per_x is not None and self.world_pixels_per_x <= 0:
            raise ValueError("主画面/小地图比例必须为正数")
        if self.teleport_max_dx is not None and not 0 < self.teleport_max_dx < 1:
            raise ValueError("瞬移最大位移必须在 (0, 1) 内")
        if self.confirmation_frames < 2 or self.empty_sweeps < 1:
            raise ValueError("至少连续两帧确认怪物，至少巡视一个往返")
        if not 0 <= self.turn_inset <= self.attack_range - self.boundary_margin:
            raise ValueError("掉头点必须在边缘攻击范围内，并留出余量")
        if self.auto_route:
            if not self.top.configured or self.top_drop_x is None:
                raise ValueError("自动换层需要顶层左右端和 top_drop_x")
            if not self.top.left <= self.top_drop_x <= self.top.right:
                raise ValueError("top_drop_x 必须位于顶层范围内")
            if not self.bottom.left <= self.ladder_x <= self.bottom.right:
                raise ValueError("梯子不在底层范围内")
            if self.drop_direction not in ("left", "right"):
                raise ValueError("drop_direction 只能是 left 或 right")

    def require_calibration(self) -> None:
        self.validate()
        required = {"小地图范围": self.minimap_roi, "主画面范围": self.combat_roi,
                    "窗口尺寸": self.frame_size, "角色脚底位置": self.player_feet_offset}
        missing = [name for name, value in required.items() if value is None]
        if missing:
            raise ValueError("尚缺少" + "、".join(missing)
                             + "；请运行 python -m lionbot --calibrate，已保存的步骤会自动续做")
        if self.world_pixels_per_x is None:
            raise ValueError("截图区域和角色位置已保存，但射程比例尚未完成；请先运行 "
                             "python -m lionbot --measure-scale，再运行 --measure-teleport")


def load_config(path: str | Path) -> Config:
    source = Path(path)
    raw = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
    for name in ("bottom", "top"):
        if name in raw:
            raw[name] = Floor(**raw[name])
    for name in ("minimap_roi", "combat_roi", "frame_size", "player_feet_offset"):
        if raw.get(name) is not None:
            raw[name] = tuple(raw[name])
    config = Config(**raw)
    config.validate()
    return config
