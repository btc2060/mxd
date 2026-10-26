"""暗影花园独立校准；只截图与读取黄点，采样过程不自动发送游戏按键。"""
from __future__ import annotations

import json
import time
from dataclasses import replace
from pathlib import Path

import cv2

from .calibrate import fresh, get_position, save_json, select_point, select_roi
from .garden_cycle import GardenConfig, GardenCycle, load_garden, save_garden
from .garden_vision import (GardenReferenceError, anchor_metadata_valid, combat_area_problem,
                            save_garden_anchor)
from .vision import crop, make_template, measure_world_scale, write_image
from .windows import GameWindow


def choose_combat_area(config, frame):
    print("现在框选【主画面游戏场景】，不是小地图。包含角色、平台、树木或岩壁；排除聊天和底部状态栏。")
    print(f"当前主画面={config.combat_roi}；小地图={config.minimap_roi}。橙框为旧主画面，红框为小地图。")
    preview = frame.copy()
    for roi, color in ((config.combat_roi, (0, 165, 255)), (config.minimap_roi, (0, 0, 255))):
        if roi is not None:
            x, y, w, h = roi
            cv2.rectangle(preview, (x, y), (x + w - 1, y + h - 1), color, 2)
    roi = select_roi("MAIN GAME FIELD / NOT minimap / include character and terrain", preview)
    updated = replace(config, combat_roi=roi)
    updated.validate()
    problem = combat_area_problem(updated, frame)
    if problem:
        raise ValueError(problem + "；原主画面设置未修改，可用 --select-area 重选")
    return updated


def save_reference(config, path, frame, position, feet):
    try:
        save_garden_anchor(config, path.parent, frame, position, feet)
        return config
    except GardenReferenceError as error:
        print(str(error))
        print("下面可在同一张截图重选主画面，无需重做地图比例、身高或怪物模板。")
        updated = choose_combat_area(config, frame)
        save_garden_anchor(updated, path.parent, frame, position, feet)
        return updated


def select_area(path):
    config = load_garden(path)
    if config.minimap_roi is None or config.frame_size is None:
        raise ValueError("请先 --setup 完成小地图范围")
    window = GameWindow(config.window_title, frame_size=config.frame_size)
    try:
        frame = fresh(window, "准备重选主画面；保留已采集的小地图、射程、怪物和路线。请在底层站稳。")
        position = get_position(frame, config)
        updated = choose_combat_area(config, frame)
        if updated.world_pixels_per_x is not None:
            feet = select_point("Click YOUR feet inside the NEW game field", frame)
            save_garden_anchor(updated, path.parent, frame, position, feet)
        save_garden(path, updated)
        print("主画面已更新；小地图、比例、身高、怪物、左右边界和 F 参数保留。继续 --setup 续做未完成步骤。")
    finally:
        window.close()
        cv2.destroyAllWindows()


def previous_configs(root):
    for name in ("config.plants.local.json", "config.local.json", "config.simple.local.json"):
        path = root / name
        if path.exists():
            try:
                yield json.loads(path.read_text(encoding="utf-8"))
            except (ValueError, OSError):
                continue


def inherit_pixels(config, root):
    """只沿用同窗口像素单位的身高/射程等，不沿用旧地图归一化坐标。"""
    for old in previous_configs(root):
        if old.get("frame_size") != list(config.frame_size):
            continue
        reach = old.get("attack_range_px")
        if reach is None and old.get("world_pixels_per_x"):
            reach = old.get("attack_range", .086) * old["world_pixels_per_x"]
        if config.attack_range_px is None and isinstance(reach, (int, float)) and 24 <= reach <= 4096:
            config = replace(config, attack_range_px=float(reach))
            print(f"沿用同尺寸窗口的 D 像素射程 {reach:.0f}px，不复用旧地图的 X/Y。")
        if (config.player_feet_offset is None and old.get("player_feet_offset")
                and old.get("player_template") and (root / old["player_template"]).is_file()):
            config = replace(config, player_template=old["player_template"],
                             player_feet_offset=tuple(old["player_feet_offset"]))
    return config


def sample_plant(path, window=None):
    config = load_garden(path)
    if config.minimap_roi is None or config.combat_roi is None:
        raise ValueError("请先 --setup 框选当前地图区域")
    own_window = window is None
    window = window or GameWindow(config.window_title, frame_size=config.frame_size)
    try:
        frame = fresh(window, "请让一只紫红花头的食人花完整可见，避开攻击特效；左右姿态会自动镜像。")
        get_position(frame, config)
        print("框选一只完整食人花，包含紫红花头和邻近绿叶；不含角色、宠物、伤害数字。")
        roi = select_roi("ONE magenta plant INCLUDING green leaves / Enter", frame)
        feet = select_point("Click THIS plant root touching the ground", frame)
        image = crop(frame, roi)
        template = make_template(image, (feet[0] - roi[0], feet[1] - roi[1]))
        from .plant_vision import PlantColorDetector

        if not PlantColorDetector([template], "magenta").models:
            raise ValueError("没有可用的紫红花头，请重新框选清晰完整食人花；旧模板未修改")
        manifest = path.parent / config.template_manifest
        entries = json.loads(manifest.read_text(encoding="utf-8")) if manifest.exists() else []
        filename = f"plant-{time.time_ns()}.png"
        write_image(manifest.parent / filename, image)
        entries.append({"file": filename, "feet_offset": template.feet_offset})
        save_json(manifest, entries)
        print("食人花样本已保存；自动支持左右朝向，并用紫红花头/绿叶补充动画识别。")
    finally:
        if own_window:
            window.close()
        cv2.destroyAllWindows()


def record_anchor(path):
    config = load_garden(path)
    if config.world_pixels_per_x is None:
        raise ValueError("请先 --setup 完成当前地图比例")
    window = GameWindow(config.window_title, frame_size=config.frame_size)
    try:
        frame = fresh(window, "在暗影花园底层站稳，松开全部键；这里只补脚底参考，不需要名牌。")
        position = get_position(frame, config)
        feet = select_point("Click YOUR feet on the bottom platform", frame)
        config = save_reference(config, path, frame, position, feet)
        save_garden(path, config)
        print("当前脚底/地形参考已保存；边界、模板和射程未修改。")
    finally:
        window.close()
        cv2.destroyAllWindows()


def record_route(path, window=None):
    config = load_garden(path)
    if config.world_pixels_per_x is None:
        raise ValueError("请先 --setup 完成截图和地图比例")
    own_window = window is None
    window = window or GameWindow(config.window_title, frame_size=config.frame_size)
    samples = []
    try:
        for side in ("LEFT", "RIGHT"):
            label = "左" if side == "LEFT" else "右"
            frame = fresh(window, f"手动站到暗影花园底层{label}侧安全掉头点，远离传送门/绳索；这里记录黄点，无需手算坐标。")
            position = get_position(frame, config)
            feet = select_point(f"Click YOUR feet at {side} turn point", frame)
            samples.append((position, feet, frame))
            print(f"{label}掉头点：X={position[0]:.4f}，Y={position[1]:.4f}")
        left, right = samples[0][0], samples[1][0]
        if right[0] - left[0] < max(.08, 6 / config.minimap_roi[2]):
            raise ValueError("左右点顺序不对或过近，请重新 --record-route；旧边界保留")
        if abs(right[1] - left[1]) > max(config.floor_tolerance, 2 / config.minimap_roi[3]) + 1e-9:
            raise ValueError("左右点不在同一底层平面，请检查是否走到上层；旧边界保留")
        y = (left[1] + right[1]) / 2
        updated = replace(config, left_x=left[0], right_x=right[0], bottom_path=((left[0], y), (right[0], y)))
        updated.validate()
        for position, feet, frame in samples:
            updated = save_reference(updated, path, frame, position, feet)
        save_garden(path, updated)
        print(f"底层边界已保存：{left[0]:.4f}～{right[0]:.4f}，Y≈{y:.4f}；只在此范围无限往返。")
    finally:
        if own_window:
            window.close()
        cv2.destroyAllWindows()


def measure_teleport(path, window=None):
    config = load_garden(path)
    config.validate(ready=True)
    own_window = window is None
    window = window or GameWindow(config.window_title, frame_size=config.frame_size)
    distances = []
    route = GardenCycle(config, start_alt=False)
    try:
        for trial in range(3):
            first = fresh(window, f"水平 F 测量 {trial + 1}/3：手动在底层中间无遮挡处站稳，远离两侧边界。")
            before = get_position(first, config)
            if not route.on_bottom(before) or not config.left_x <= before[0] <= config.right_x:
                raise ValueError("F 测量起点不在已采集底层，请在底层中段重试")
            second = fresh(window, "手动按住左或右方向，短按一次 F，再松开全部键；只瞬移一次，不跳跃。")
            after = get_position(second, config)
            dx, dy = abs(after[0] - before[0]), abs(after[1] - before[1])
            print(f"水平瞬移实测：ΔX={dx:.4f}，ΔY={dy:.4f}")
            edge_margin = max(.015, 2 / config.minimap_roi[2])
            if (dx < .005 or dy > max(.015, 2 / config.minimap_roi[3]) or not route.on_bottom(after)
                    or not config.left_x + edge_margin < after[0] < config.right_x - edge_margin):
                raise ValueError("没有测到同层水平瞬移，请在底层中段重新测量；原配置保留")
            distances.append(dx)
        upper = max(distances) * 1.25 + 2 / config.minimap_roi[2]
        updated = replace(config, teleport_max_dx=upper)
        updated.validate()
        save_garden(path, updated)
        print(f"F 位移上限已保存为 {upper:.4f}（包含安全余量）；离边界不足此距离时只步行。")
    finally:
        if own_window:
            window.close()
        cv2.destroyAllWindows()


def setup_garden(path):
    config = load_garden(path)
    if not path.exists():
        sources = list(previous_configs(path.parent))
        if sources:
            source = sources[-1]  # simple 配置若存在，沿用已验证的窗口和测谎设置。
            names = ("window_title", "alt_interval", "attack_interval", "attack_hold", "face_hold",
                     "lie_alert_enabled", "lie_alert_template", "lie_alert_interval", "lie_alert_threshold", "lie_alert_roi")
            config = replace(config, **{name: source[name] for name in names if name in source})
        save_garden(path, config)
    window = GameWindow(config.window_title, frame_size=config.frame_size)
    try:
        frame = fresh(window, "请在暗影花园最下层平地站稳，固定游戏窗口和小地图大小；校准中不自动发送按键。")
        if config.frame_size is not None and tuple(frame.shape[1::-1]) != config.frame_size:
            raise ValueError("窗口尺寸已变化，请恢复原尺寸，或用 --config 指定新的独立配置后 --setup")
        if config.minimap_roi is None:
            print("框选当前地图的小地图内部，排除标题和按钮；不要沿用旧地图归一化坐标。")
            config = replace(config, minimap_roi=select_roi("Minimap INSIDE only / Enter", frame),
                             frame_size=tuple(frame.shape[1::-1]))
            get_position(frame, config)
            save_garden(path, config)
        problem = combat_area_problem(config, frame)
        if problem:
            print(f"主画面范围需要重选：{problem}")
            config = choose_combat_area(config, frame)
            save_garden(path, config)
        else:
            print(f"复用已保存主画面范围 {config.combat_roi}；如需重选，使用 --select-area。")
        config = inherit_pixels(config, path.parent)
        save_garden(path, config)
        if config.world_pixels_per_x is None or config.attack_range_px is None:
            first = fresh(window, "在底层平地站稳；这一帧用于脚底、身高和射程。")
            before = get_position(first, config)
            feet = select_point("Click YOUR feet", first)
            head = select_point("Click YOUR head TOP", first)
            height = feet[1] - head[1]
            if not 12 <= height <= 512:
                raise ValueError("身高不合法，请依次点击同一角色的脚底、头顶")
            reach = config.attack_range_px
            if reach is None:
                print("点击已经手动用 D 验证能命中的同层最远位置；这里只记录像素距离。")
                edge = select_point("Click farthest SAME-LEVEL position reachable with D", first)
                reach = abs(edge[0] - feet[0])
                if reach < 24:
                    raise ValueError("射程过短，请选择已手动确认能命中的远处位置")
            if config.world_pixels_per_x is None:
                print("框选宽一些的固定地形，用于扣除镜头移动；不含角色、小地图和技能特效。")
                terrain = select_roi("STATIC terrain for camera movement", first)
                second = fresh(window, "手动沿一方向走一小段，黄点 X 至少改变 0.025，仍在同一平地；站稳后截图。")
                after = get_position(second, config)
                after_feet = select_point("Click YOUR feet in SECOND screenshot", second)
                print(f"坐标比例测量：起点 {before}，终点 {after}，ΔX={after[0] - before[0]:.4f}")
                scale, quality = measure_world_scale(first, second, terrain, before, after, feet, after_feet)
                config = replace(config, world_pixels_per_x=scale)
                print(f"当前地图比例 {scale:.1f}px/归一化X，质量 {quality:.2f}；不复用旧地图比例。")
            config = replace(config, player_height_px=height, attack_range_px=reach)
            save_garden(path, config)
            config = save_reference(config, path, first, before, feet)
            save_garden(path, config)
        if not anchor_metadata_valid(config, path.parent):
            anchor_frame = fresh(window, "在底层站稳，补一张缺失的脚底/地形参考；不重做比例。")
            anchor_position = get_position(anchor_frame, config)
            anchor_feet = select_point("Click YOUR feet for terrain reference", anchor_frame)
            config = save_reference(config, path, anchor_frame, anchor_position, anchor_feet)
            save_garden(path, config)
        if not (path.parent / config.template_manifest).exists():
            sample_plant(path, window)
        if config.left_x is None or len(config.bottom_path) < 2:
            record_route(path, window)
            config = load_garden(path)
        if config.teleport_enabled and config.teleport_max_dx is None:
            measure_teleport(path, window)
        print("暗影花园校准完成。先 --observe --show 检查，再 --run --show 开始；只用 D、水平 F 和定时 Alt。")
    finally:
        window.close()
        cv2.destroyAllWindows()
