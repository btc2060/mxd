"""重测地图点位，保留截图范围、角色/怪物模板及同一坐标系下的距离测量。"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

from .calibrate import fresh, get_position, print_position, save_config, select_point
from .config import Config, Floor, load_config
from .vision import crop
from .windows import GameWindow


def apply_map_profile(path: Path) -> None:
    """应用用户实测坐标，并把原地图的 X/Y 单位转换到当前 ROI。可重复执行。"""
    config = load_config(path)
    expected_roi = (24, 73, 137, 103)
    if config.minimap_roi != expected_roi:
        raise ValueError(f"此组实测坐标仅适用于小地图范围 {expected_roi}，当前为 {config.minimap_roi}；"
                         "请用 --calibrate-map 按当前范围重测，不要套用此预设")
    # 原边界来自最初地图测量；新边界与两层 Y 由用户在同一 ROI 下确认。
    old_left, old_right = .029, .659
    new_left, new_right = .0182, .9745
    x_scale = (new_right - new_left) / (old_right - old_left)
    y_scale = (.8107 - .4806) / (.759 - .130)

    def convert_x(x):
        return new_left + (x - old_left) * x_scale

    updated = replace(config, bottom=Floor(.8107, new_left, new_right),
                      top=Floor(.4806, convert_x(.045), convert_x(.212)),
                      ladder_x=convert_x(.212), top_drop_x=convert_x(.045),
                      attack_range=.086 * x_scale, turn_inset=.055 * x_scale,
                      boundary_margin=.012 * x_scale, ladder_tolerance=.008 * x_scale,
                      scan_step=.05 * x_scale, floor_tolerance=.035 * y_scale)
    updated.validate()
    save_config(path, updated)
    print(f"已应用实测地图：底层 Y={updated.bottom.y:.4f}，顶层 Y={updated.top.y:.4f}；"
          f"底层 X={new_left:.4f}～{new_right:.4f}。")
    print(f"按边界换算：梯子 X={updated.ladder_x:.4f}，顶层左出口 X={updated.top_drop_x:.4f}；"
          f"射程 0.086（原坐标）→{updated.attack_range:.4f}（当前坐标）。")
    print("角色/怪物模板、当前 ROI 下已测量的像素比例和瞬移位移均保留，不进行重复缩放。")


def standing_sample(window: GameWindow, config: Config, instruction: str):
    frame = fresh(window, instruction)
    positions = [get_position(frame, config)]
    for _ in range(4):
        time.sleep(.10)
        if not window.focused():
            raise ValueError("楼层采样时游戏失焦，请切回游戏，松开按键后再测量")
        frame = window.capture()
        positions.append(get_position(frame, config))
    if np.ptp([p[1] for p in positions]) > .025:
        raise ValueError("采样时 Y 仍在变化，请落地站稳后重测；不要在爬梯或跳跃中记录楼层")
    position = tuple(float(v) for v in np.median(positions, axis=0))
    print_position("站立采样", position, config)
    return frame, position


def pick_minimap_x(title: str, frame, config: Config) -> float:
    minimap = crop(frame, config.minimap_roi)
    h, w = minimap.shape[:2]
    zoom = min(5, 650 / w, 550 / h)
    enlarged = cv2.resize(minimap, (round(w * zoom), round(h * zoom)), interpolation=cv2.INTER_NEAREST)
    # 只取 X，点击地图上对应平台边缘或梯子即可，不用猜归一化数值。
    point = select_point(title, enlarged)
    if not (0 <= point[0] < enlarged.shape[1] and 0 <= point[1] < enlarged.shape[0]):
        raise ValueError("请在放大的小地图内部点击点位")
    value = point[0] / enlarged.shape[1]
    print(f"{title}：X={value:.4f}")
    return value


def bottom_config(config: Config, position, left: float, right: float, ladder: float) -> Config:
    if not 0 <= left < right <= 1 or not left <= ladder <= right:
        raise ValueError(f"底层点位顺序不符：left={left:.4f}，ladder={ladder:.4f}，right={right:.4f}")
    if not left - config.boundary_margin <= position[0] <= right + config.boundary_margin:
        raise ValueError("采样的角色 X 不在所选底层左右端之间，请检查点位")
    updated = replace(config, bottom=Floor(position[1], left, right), ladder_x=ladder, auto_route=False)
    updated.validate()
    return updated


def top_config(config: Config, position, left: float, right: float) -> Config:
    if position[1] >= config.bottom.y - 2 * config.floor_tolerance:
        raise ValueError("顶层与底层的 Y 太接近，请先手动爬到最上层、跳离梯子并站稳")
    if not 0 <= left < right <= 1:
        raise ValueError("顶层左出口 X 必须小于右侧安全边界 X")
    if right > config.ladder_x + config.ladder_tolerance:
        raise ValueError("按此地图路线，顶层右侧安全边界不能超过所选梯子的 X")
    if not left - config.boundary_margin <= position[0] <= right + config.boundary_margin:
        raise ValueError("采样角色 X 不在所选顶层左右端之间，请检查是否站在正确平台")
    updated = replace(config, top=Floor(position[1], left, right), top_drop_x=left, auto_route=True)
    updated.validate()
    return updated


def calibrate_map(path: Path, bottom_only: bool = False) -> None:
    config = load_config(path)
    if config.minimap_roi is None or config.frame_size is None:
        raise ValueError("请先通过 --calibrate 保存小地图范围和窗口尺寸，再校准地图点位")
    window = GameWindow(config.window_title)
    try:
        print("地图校准不重采角色或怪物图片，也不改变小地图框选范围。")
        frame, position = standing_sample(window, config, "先在最下层平地落地站稳，再开始截图。")
        total = 3 if bottom_only else 5
        print("在放大的小地图中，单击底层角色可行走区域的左端。")
        left = pick_minimap_x(f"1/{total} Bottom LEFT walkable edge", frame, config)
        print("单击底层角色可行走区域的右端。不要把二层平台右端当成底层边界。")
        right = pick_minimap_x(f"2/{total} Bottom RIGHT walkable edge", frame, config)
        print("单击左侧那根直通顶层的梯子，其横向中心位置。")
        ladder = pick_minimap_x(f"3/{total} Ladder horizontal center", frame, config)
        config = bottom_config(config, position, left, right, ladder)
        save_config(path, config)
        print(f"底层已保存：Y={config.bottom.y:.4f}，X={left:.4f}～{right:.4f}，梯子 X={ladder:.4f}。")
        print("自动换层暂时关闭；即使后续取消，底层点位和原模板也会保留。")
        if bottom_only:
            print("可以继续 --measure-teleport，再用 --floor bottom --run 单独验证底层。")
            return

        frame, position = standing_sample(
            window, config, "手动爬到最上层，跳离梯子并站稳；准备好后再在截图菜单中按 Enter。")
        print("单击顶层左侧向外走即可掉落的出口边缘。无需让角色实际走下去。")
        left = pick_minimap_x("4/5 Top LEFT walk-off edge", frame, config)
        print("单击顶层右侧安全边界，不得超过刚才所选梯子的 X。")
        right = pick_minimap_x("5/5 Top RIGHT safe edge", frame, config)
        config = top_config(config, position, left, right)
        save_config(path, config)
        print(f"顶层已保存：Y={config.top.y:.4f}，X={left:.4f}～{right:.4f}。自动换层已启用。")
        print(f"当前 attack_range={config.attack_range:.4f}；地图点位校准不会自动推断技能的实际射程。")
    finally:
        window.close()
        cv2.destroyAllWindows()
