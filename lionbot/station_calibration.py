"""独立保存散步路2的模板、黄点站位和像素射程，不覆盖旧地图。"""
from __future__ import annotations

import time
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

from .calibrate import calibrate, fresh, get_position, save_config, select_point
from .config import Config, Floor, load_config
from .screen_map import save_screen_anchor
from .windows import GameWindow


def new_stationary_config() -> Config:
    directory = Path("calibration") / ("plants-" + str(time.time_ns()))
    return Config(behavior="stationary", monster_name="食人花", auto_route=False,
                  bottom=Floor(.5, None, None), top=Floor(0, None, None),
                  turn_inset=0, boundary_margin=0, attack_burst_shots=3,
                  player_template=str(directory / "player.png"),
                  template_manifest=str(directory / "monsters.json"),
                  screen_anchor=str(directory / "screen-anchor.json"))


def record_station(path: Path) -> None:
    config = load_config(path)
    if config.behavior != "stationary":
        raise ValueError("此命令只记录独立定点配置，请使用 --config config.plants.local.json")
    if config.minimap_roi is None or config.combat_roi is None or config.world_pixels_per_x is None:
        raise ValueError("请先运行 --calibrate-stationary 完成截图和比例采集")
    window = GameWindow(config.window_title)
    try:
        if config.attack_range_px is None:
            print("若没有同尺寸窗口的旧射程资料，请先手动用 D 确认最远可命中位置，截图后会提示点选。")
        frame = fresh(window, "手动站到散步路2想挂机的地面位置，松开方向键；程序会自动记录当前黄点 X/Y。")
        positions = [get_position(frame, config)]
        for _ in range(4):
            time.sleep(.1)
            if not window.focused():
                raise ValueError("记录定点时游戏失焦，请切回游戏后重试 --record-station")
            positions.append(get_position(window.capture(), config))
        jitter = np.ptp(positions, axis=0)
        if jitter[0] > 1 / config.minimap_roi[2] or jitter[1] > 1 / config.minimap_roi[3]:
            raise ValueError("采集时黄点仍在变化，可能被击退；请清理周围后站稳，再运行 --record-station。模板已保留")
        # 使用点脚底的同一张冻结截图对应黄点，禁止将后来的位置配到旧画面。
        position = positions[0]
        print(f"记录站位 X={position[0]:.4f}，Y={position[1]:.4f}。在同一截图依次点击自己的脚底和头顶。")
        feet = select_point("Click YOUR feet at the fixed spot", frame)
        head = select_point("Click YOUR head top / same character", frame)
        height = feet[1] - head[1]
        if not 12 <= height <= 512:
            raise ValueError("身高点位有误，请先点脚底，再点同一角色头顶；原站位未修改")
        reach = config.attack_range_px
        old_path = path.parent / "config.local.json"
        if reach is None and old_path.exists() and old_path.resolve() != path.resolve():
            try:
                old = load_config(old_path)
                if old.frame_size == config.frame_size and old.attack_reach_px is not None:
                    reach = old.attack_reach_px
                    print(f"沿用同尺寸窗口下火焰箭的像素射程 {reach:.0f}px；不沿用旧地图的 X/Y。")
            except (ValueError, TypeError, OSError):
                print("旧配置未能读取，将单独采集食人花的射程。")
        if reach is None:
            print("先手动确认火焰箭能打到的同层最远位置；在当前截图点击该位置。")
            edge = select_point("Click farthest reachable SAME LEVEL target", frame)
            reach = abs(edge[0] - feet[0])
            if reach < 24:
                raise ValueError("攻击射程过短，请选择已用 D 验证能命中的较远位置；模板已保留")
        updated = replace(config, station=position, bottom=Floor(position[1], None, None),
                          floor_tolerance=max(.008, 1.5 / config.minimap_roi[3]),
                          player_height_px=height, attack_range_px=reach,
                          attack_burst_shots=3, teleport_max_dx=None, auto_route=False)
        updated.validate()
        save_screen_anchor(updated, path.parent, frame, position, feet)
        save_config(path, updated)
        print(f"定点已保存：X={position[0]:.4f}，Y={position[1]:.4f}，射程={reach:.0f}px；左右识别，每组 3 次。")
        print(f'运行：python -m lionbot --config "{path.name}" --run --show')
    finally:
        window.close()
        cv2.destroyAllWindows()


def calibrate_stationary(path: Path) -> None:
    if path.exists():
        if load_config(path).behavior != "stationary":
            raise ValueError("此路径是原地图配置，不能覆盖；请使用 --config config.plants.local.json --calibrate-stationary")
    else:
        config = new_stationary_config()
        old_path = path.parent / "config.local.json"
        if old_path.exists():
            try:
                config = replace(config, window_title=load_config(old_path).window_title)
            except (ValueError, TypeError, OSError):
                pass
        save_config(path, config)
    calibrate(path)
    config = load_config(path)
    if config.station is None or config.attack_range_px is None or not (path.parent / config.screen_anchor).exists():
        record_station(path)
    else:
        print("已保留原定点；如需改站位，运行 --config config.plants.local.json --record-station。")
