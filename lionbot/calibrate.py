from __future__ import annotations

import json
import time
from dataclasses import asdict, replace
from pathlib import Path

import cv2

from .config import Config, load_config
from .vision import (crop, make_template, measure_world_scale, minimap_position,
                     write_image)
from .windows import GameWindow


def save_config(path: Path, config: Config) -> None:
    config.validate()
    if path.exists():
        backup = path.with_name(f"{path.name}.{time.time_ns()}.bak")
        backup.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已保存：{path}")


def fresh(window: GameWindow, instruction: str):
    cv2.destroyAllWindows()
    print(instruction)
    input("按 Enter 后有 3 秒切回游戏并松开所有按键，然后自动截图：")
    time.sleep(3)
    if not window.focused():
        raise ValueError("截图时游戏必须处于前台，不能有其他窗口遮挡")
    return window.capture()


def select_roi(title: str, frame):
    cv2.namedWindow(title, cv2.WINDOW_NORMAL)
    roi = tuple(map(int, cv2.selectROI(title, frame, showCrosshair=True, fromCenter=False)))
    cv2.destroyWindow(title)
    if roi[2] == 0 or roi[3] == 0:
        raise ValueError("框选已取消")
    return roi


def select_point(title: str, frame) -> tuple[int, int]:
    point = []

    def click(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN:
            point[:] = [x, y]

    cv2.namedWindow(title, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(title, click)
    while not point:
        cv2.imshow(title, frame)
        if cv2.waitKey(20) & 0xFF == 27:
            cv2.destroyWindow(title)
            raise ValueError("点选已取消")
    cv2.destroyWindow(title)
    return tuple(point)


def get_position(frame, config: Config):
    if config.frame_size is not None and tuple(frame.shape[1::-1]) != config.frame_size:
        raise ValueError("客户区大小已改变，请恢复原尺寸或重新校准")
    position = minimap_position(frame, config.minimap_roi)
    if position is None:
        raise ValueError("小地图未找到唯一黄点：重新裁剪，排除标题、按钮、黄色图标")
    return position


def measure_scale(window: GameWindow, config: Config) -> Config:
    first = fresh(window, "在底层平地站定，开始测量攻击距离的坐标比例。")
    first_pos = get_position(first, config)
    first_feet = select_point("Click YOUR character feet / Esc cancels", first)
    print("框选一块包含固定地形的宽区域，不含小地图、UI，尽量避开怪物和角色。")
    background_roi = select_roi("Static terrain for camera compensation", first)
    second = fresh(window, "手动沿一个方向走一小段（小地图 X 改变至少 0.025），站定；不要换层。")
    second_pos = get_position(second, config)
    second_feet = select_point("Click YOUR character feet in SECOND frame", second)
    scale, quality = measure_world_scale(first, second, background_roi,
                                          first_pos, second_pos, first_feet, second_feet)
    print(f"每单位归一化 X = {scale:.1f} 主画面像素；0.086 射程约 {scale * .086:.0f} 像素；配准质量 {quality:.2f}")
    return replace(config, world_pixels_per_x=scale)


def calibrate(path: Path) -> None:
    config = load_config(path)
    window = GameWindow(config.window_title)
    try:
        print("所有选框坐标都基于游戏客户区，不含 Windows 标题栏。框选后 Enter 确认，Esc 取消。")
        frame = fresh(window, "保持原先测量地图坐标时的窗口大小和小地图展开方式。")
        print("框选小地图内部坐标区域，必须与您提供 .029/.659/.759/.130 时的归一化范围一致。")
        minimap_roi = select_roi("Minimap coordinate region", frame)
        config = replace(config, minimap_roi=minimap_roi, frame_size=tuple(frame.shape[1::-1]),
                         world_pixels_per_x=None, teleport_max_dx=None)
        print(f"当前角色小地图位置：{get_position(frame, config)}；在底层时 Y 应接近 0.759。")
        print("框选主画面的游戏区域，排除底部 HP/MP、聊天栏；程序另行排除小地图。")
        combat_roi = select_roi("Combat area / exclude HUD", frame)
        print("框选自己固定的角色名牌文字，不要含会变化的伤害数字或大块背景。")
        player_roi = select_roi("YOUR unique nameplate", frame)
        player_image = crop(frame, player_roi)
        if cv2.cvtColor(player_image, cv2.COLOR_BGR2GRAY).std() < 5:
            raise ValueError("角色名牌模板缺少纹理，请重新框选文字")
        feet = select_point("Click YOUR character feet", frame)
        directory = path.parent / "calibration" / str(time.time_ns())
        player_path = directory / "player.png"
        write_image(player_path, player_image)
        config = replace(config, combat_roi=combat_roi,
                         player_template=str(player_path.relative_to(path.parent)),
                         player_feet_offset=(feet[0] - player_roi[0], feet[1] - player_roi[1]))
        entries = []
        while True:
            print("框选一只蓝独角狮的完整头部或身体，包含蓝白轮廓与双眼；排除名牌、HP 条和宠物。")
            monster_roi = select_roi("Blue lion HEAD or BODY / NOT pet", frame)
            monster_feet = select_point("Click THIS monster's ground contact point", frame)
            image = crop(frame, monster_roi)
            offset = (monster_feet[0] - monster_roi[0], monster_feet[1] - monster_roi[1])
            make_template(image, offset)
            filename = f"lion-{len(entries)}.png"
            write_image(directory / filename, image)
            entries.append(dict(file=filename, feet_offset=offset))
            answer = input("继续采样？Enter=同图另一姿态，n=新截图，q=完成（建议至少 3 种姿态）：").strip().lower()
            if answer == "q":
                break
            if answer == "n":
                frame = fresh(window, "等待另一个站立、行走或受击姿态后采集。")
                if tuple(frame.shape[1::-1]) != config.frame_size:
                    raise ValueError("采样过程中窗口尺寸已改变")
        manifest = directory / "monsters.json"
        manifest.write_text(json.dumps(entries, indent=2), encoding="utf-8")
        config = replace(config, template_manifest=str(manifest.relative_to(path.parent)))
        save_config(path, config)
        # 先保存已完成的采样。比例测量失败时可单独运行 --measure-scale，无需重复模板采集。
        config = measure_scale(window, config)
        save_config(path, config)
        print("模板和射程比例校准完成。再运行 --measure-teleport 测量 F 的安全位移上限。")
    finally:
        window.close()
        cv2.destroyAllWindows()


def measure_teleport(path: Path) -> None:
    config = load_config(path)
    config.require_calibration()
    window = GameWindow(config.window_title)
    distances = []
    try:
        for trial in range(3):
            first = fresh(window, f"瞬移测量 {trial + 1}/3：在底层中间无遮挡平地站定，远离梯子和边界。")
            before = get_position(first, config)
            second = fresh(window, "手动按住左或右方向，短按一次 F 后松开全部键；不要连续瞬移。")
            after = get_position(second, config)
            if abs(after[1] - before[1]) > 0.02 or abs(after[0] - before[0]) < 0.005:
                raise ValueError("未测到有效的同层瞬移，请在无遮挡平地重新测量")
            if abs(before[1] - config.bottom.y) > config.floor_tolerance:
                raise ValueError("瞬移位移请在底层测量")
            distances.append(abs(after[0] - before[0]))
        upper_bound = max(distances) * 1.25 + 2 / config.minimap_roi[2]
        config = replace(config, teleport_max_dx=upper_bound)
        save_config(path, config)
        print(f"实测位移 {distances}；含量化和安全余量的最大位移 {upper_bound:.4f}")
    finally:
        window.close()


def update_scale(path: Path) -> None:
    config = load_config(path)
    if config.minimap_roi is None:
        raise ValueError("请先 --calibrate")
    window = GameWindow(config.window_title)
    try:
        save_config(path, measure_scale(window, config))
    finally:
        window.close()
        cv2.destroyAllWindows()
