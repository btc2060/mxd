from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, replace
from pathlib import Path

import cv2
import numpy as np

from .config import Config, load_config
from .vision import (crop, detect_monsters, locate_player, make_template, measure_world_scale,
                     minimap_position, read_image, write_image)
from .windows import GameWindow


def save_json(path: Path, value) -> None:
    """同目录替换，避免中断留下半个 JSON；不覆盖任何采样图片。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{time.time_ns()}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def save_config(path: Path, config: Config) -> None:
    config.validate()
    if path.exists() and load_config(path) != config:
        backup = path.with_name(f"{path.name}.{time.time_ns()}.bak")
        backup.write_bytes(path.read_bytes())
    save_json(path, asdict(config))
    print(f"已保存：{path}")


def choose(title: str, message: str, choices) -> str:
    """菜单键由当前 OpenCV 窗口处理，同时支持鼠标按钮。"""
    selected = []
    panel = np.full((200, 700, 3), 32, dtype=np.uint8)
    cv2.putText(panel, message, (18, 42), 0, .55, (235, 235, 235), 1)
    cv2.putText(panel, "Click a button or press its key IN THIS WINDOW. Esc: save & pause",
                (18, 72), 0, .43, (180, 220, 255), 1)
    buttons = []
    width = (680 // len(choices))
    for index, (value, label, _keys) in enumerate(choices):
        x = 12 + index * width
        box = (x, 108, x + width - 12, 167)
        cv2.rectangle(panel, box[:2], box[2:], (75, 100, 70), -1)
        cv2.putText(panel, label, (x + 8, 143), 0, .45, (255, 255, 255), 1)
        buttons.append((value, box))

    def click(event, x, y, _flags, _param):
        if event == cv2.EVENT_LBUTTONDOWN:
            for value, (left, top, right, bottom) in buttons:
                if left <= x <= right and top <= y <= bottom:
                    selected[:] = [value]

    cv2.namedWindow(title, cv2.WINDOW_AUTOSIZE)
    cv2.setMouseCallback(title, click)
    try:
        while not selected:
            cv2.imshow(title, panel)
            key = cv2.waitKey(20) & 0xFF
            if key == 27 or cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                raise ValueError("校准已暂停，已完成步骤已保存；重新运行 --calibrate 继续")
            for value, _label, keys in choices:
                if key in keys:
                    selected[:] = [value]
        return selected[0]
    finally:
        cv2.destroyWindow(title)
        cv2.waitKey(1)


def sample_choice(count: int) -> str:
    print(f"已保存 {count} 张怪物模板。请在新弹出的菜单里操作：Enter/Q=完成采样，N=新截图，S=同图继续。")
    return choose("Sampling / click Next to continue", f"{count} monster samples SAVED. Next action:", [
        ("finish", "Enter/Q: Next step", (10, 13, ord("q"), ord("Q"))),
        ("new", "N: New screenshot", (ord("n"), ord("N"))),
        ("same", "S: Same screenshot", (ord("s"), ord("S"))),
    ])


def fresh(window: GameWindow, instruction: str):
    cv2.destroyAllWindows()
    print(instruction)
    choose("Prepare screenshot", "Enter: start timer, then focus the game within 3 seconds.", [
        ("capture", "Enter: Capture in 3 seconds", (10, 13)),
    ])
    print("3 秒后截图，请切回游戏并松开所有按键。")
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
        if cv2.waitKey(20) & 0xFF == 27 or cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
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


def print_position(label: str, position, config: Config) -> None:
    print(f"{label}：X={position[0]:.4f}，Y={position[1]:.4f}；"
          f"底层设定 Y={config.bottom.y:.4f} ± {config.floor_tolerance:.4f}；"
          f"小地图范围={config.minimap_roi}")


def require_bottom(position, config: Config, label: str) -> None:
    low = config.bottom.y - config.floor_tolerance
    high = config.bottom.y + config.floor_tolerance
    if not low - 1e-9 <= position[1] <= high + 1e-9:
        raise ValueError(
            f"{label}未通过底层坐标校验：实测 X={position[0]:.4f}、Y={position[1]:.4f}，"
            f"配置要求 Y={config.bottom.y:.4f}（允许 {low:.4f}～{high:.4f}），"
            f"小地图范围={config.minimap_roi}。若已在底层站稳，请运行 "
            "python -m lionbot --check-position，确认红圈标记的是角色黄点，"
            "确认黄点正确后可运行 --calibrate-map 重测地图点位，无需重采模板。仅更换裁剪范围会同时改变 X、Y，"
            "不能只修改底层 Y 而继续沿用原梯子和边界坐标。")


def position_preview(frame, config: Config):
    """只放大显示原 ROI，坐标计算仍使用原图；红圈标示实际识别对象。"""
    if config.frame_size is not None and tuple(frame.shape[1::-1]) != config.frame_size:
        raise ValueError("客户区大小已改变，请恢复原尺寸再查看坐标")
    minimap = crop(frame, config.minimap_roi)
    position = minimap_position(frame, config.minimap_roi)
    h, w = minimap.shape[:2]
    zoom = min(4, 600 / w, 400 / h)
    enlarged = cv2.resize(minimap, (max(1, round(w * zoom)), max(1, round(h * zoom))), interpolation=cv2.INTER_NEAREST)
    if position is not None:
        center = (round(position[0] * enlarged.shape[1]), round(position[1] * enlarged.shape[0]))
        cv2.circle(enlarged, center, 8, (0, 0, 255), 2)
    panel = np.full((enlarged.shape[0] + 135, 620, 3), 32, dtype=np.uint8)
    panel[:enlarged.shape[0], :enlarged.shape[1]] = enlarged
    if position is None:
        status = "No UNIQUE yellow dot. Check minimap selection."
    else:
        floor = "bottom" if abs(position[1] - config.bottom.y) <= config.floor_tolerance else (
            "top" if abs(position[1] - config.top.y) <= config.floor_tolerance else "unclassified")
        status = f"X={position[0]:.4f}  Y={position[1]:.4f}  floor={floor}"
    lines = [status, f"Expected bottom Y={config.bottom.y:.4f} +/- {config.floor_tolerance:.4f}",
             f"ROI [x,y,w,h]={config.minimap_roi}",
             "Read-only. Keep beside game. Q / Esc: close."]
    for index, line in enumerate(lines):
        cv2.putText(panel, line, (10, enlarged.shape[0] + 25 + index * 28), 0, .46, (230, 230, 230), 1)
    return panel, position


def check_position(config: Config) -> None:
    """诊断不要求怪物模板或射程比例，不发送按键或修改配置。"""
    if config.minimap_roi is None:
        raise ValueError("尚未保存小地图范围，请先运行 --calibrate 框选小地图")
    window = GameWindow(config.window_title)
    title = "Minimap position / READ ONLY / Q to close"
    last_log = 0.0
    try:
        print("实时查看 X/Y，不发送按键。请把预览放到游戏旁边，避免遮挡原小地图。")
        cv2.namedWindow(title, cv2.WINDOW_AUTOSIZE)
        while True:
            frame = window.capture()
            panel, position = position_preview(frame, config)
            now = time.monotonic()
            if now - last_log >= .5:
                if position is not None:
                    print_position("实时黄点", position, config)
                else:
                    print("当前未识别到唯一黄点，请检查预览中的裁剪范围")
                last_log = now
            cv2.imshow(title, panel)
            if cv2.waitKey(20) & 0xFF in (ord("q"), ord("Q"), 27):
                break
            if cv2.getWindowProperty(title, cv2.WND_PROP_VISIBLE) < 1:
                break
            time.sleep(.08)
    finally:
        window.close()
        cv2.destroyAllWindows()


def measure_scale(window: GameWindow, config: Config) -> Config:
    first = fresh(window, "在底层平地站定，开始测量攻击距离的坐标比例。")
    first_pos = get_position(first, config)
    print_position("步行测量起点", first_pos, config)
    first_feet = select_point("Click YOUR character feet / Esc cancels", first)
    print("框选一块包含固定地形的宽区域，不含小地图、UI，尽量避开怪物和角色。")
    background_roi = select_roi("Static terrain for camera compensation", first)
    second = fresh(window, "手动沿一个方向走一小段（小地图 X 改变至少 0.025），站定；不要换层。")
    second_pos = get_position(second, config)
    print_position("步行测量终点", second_pos, config)
    print(f"步行实测 ΔX={second_pos[0] - first_pos[0]:.4f}，ΔY={second_pos[1] - first_pos[1]:.4f}")
    second_feet = select_point("Click YOUR character feet in SECOND frame", second)
    scale, quality = measure_world_scale(first, second, background_roi,
                                          first_pos, second_pos, first_feet, second_feet)
    print(f"每单位归一化 X = {scale:.1f} 主画面像素；0.086 射程约 {scale * .086:.0f} 像素；配准质量 {quality:.2f}")
    return replace(config, world_pixels_per_x=scale)


def legacy_directory(root: Path) -> Path | None:
    """旧版强退可能只留下 PNG；寻找最近一次采样，不删除或改写它。"""
    folders = [p.parent for p in (root / "calibration").glob("*/player.png")
               if any(p.parent.glob("lion-*.png"))]
    return max(folders, key=lambda p: p.stat().st_mtime_ns) if folders else None


def saved_samples(manifest: Path) -> list[dict]:
    if not manifest.exists():
        return []
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    for entry in entries:
        image = read_image(manifest.parent / entry["file"])
        make_template(image, entry["feet_offset"])
    return entries


def reuse_legacy_monsters(directory: Path, frame, config: Config, manifest: Path) -> list[dict]:
    entries = saved_samples(manifest)
    existing = {str((manifest.parent / e["file"]).resolve()) for e in entries}
    for source in sorted(directory.glob("lion-*.png")):
        if str(source.resolve()) in existing:
            continue
        image = read_image(source)
        found = detect_monsters(frame, config.combat_roi, [make_template(image, (0, 0))],
                                config.monster_threshold, config.edge_threshold, config.minimap_roi)
        if not found:
            print(f"暂未在新截图中匹配到 {source.name}，保留原图，跳过该姿态。可稍后补采。")
            continue
        preview = frame.copy()
        x, y = found[0].box[:2]
        cv2.rectangle(preview, (x, y), (x + image.shape[1], y + image.shape[0]), (0, 255, 255), 2)
        print(f"复用 {source.name}：点击黄色框中这只怪物的脚底，以恢复旧版未保存的落地偏移。")
        feet = select_point("Recovered monster / click its feet / Esc pauses", preview)
        offset = (feet[0] - x, feet[1] - y)
        make_template(image, offset)
        # 所有恢复清单与原图都在 calibration 下，用相对路径引用，不覆盖旧 PNG。
        entries.append(dict(file=os.path.relpath(source, manifest.parent), feet_offset=offset))
        save_json(manifest, entries)
    return entries


def prepare_calibration(path: Path, config: Config, restart: bool, reuse_templates: Path | None):
    root = path.parent
    legacy = reuse_templates.resolve() if reuse_templates is not None else None
    if legacy is not None and not legacy.is_dir():
        raise ValueError("--reuse-templates 必须指向旧 player.png 和 lion-*.png 所在文件夹")
    if not path.exists() or restart:
        if legacy is None and not restart:
            legacy = legacy_directory(root)
        if legacy is not None:
            print(f"发现旧采样图片：{legacy}。缺少位置数据的图片仍需在新截图上重新点脚底。")
            answer = choose("Existing samples found", "Existing images found. Reuse them?", [
                ("reuse", "Enter/R: Reuse", (10, 13, ord("r"), ord("R"))),
                ("new", "N: Start fresh", (ord("n"), ord("N"))),
            ])
            if answer == "new":
                legacy = None
        directory = root / "calibration" / str(time.time_ns())
        config = replace(config, minimap_roi=None, combat_roi=None, frame_size=None,
                         player_feet_offset=None, world_pixels_per_x=None, teleport_max_dx=None,
                         player_template=str((directory / "player.png").relative_to(root)),
                         template_manifest=str((directory / "monsters.json").relative_to(root)))
        save_config(path, config)
    return config, legacy


def calibrate(path: Path, restart: bool = False, reuse_templates: Path | None = None) -> None:
    config = load_config(path)
    # GameWindow 先启用 DPI 感知，再显示任何 OpenCV 菜单。
    window = GameWindow(config.window_title)
    try:
        config, legacy = prepare_calibration(path, config, restart, reuse_templates)
        root = path.parent
        manifest = root / config.template_manifest
        print("校准可中断续做；菜单内点击按钮或按快捷键，不需要切回终端输入 q/n。")
        frame = fresh(window, "请在底层平地站稳，保持原先测量坐标时的窗口大小和小地图展开方式。")
        if config.minimap_roi is None or config.frame_size is None:
            print("框选小地图内部坐标区域，必须与您提供 .029/.659/.759/.130 时的归一化范围一致。")
            minimap_roi = select_roi("Minimap coordinate region", frame)
            config = replace(config, minimap_roi=minimap_roi, frame_size=tuple(frame.shape[1::-1]))
            get_position(frame, config)
            save_config(path, config)
        print(f"当前角色小地图位置：{get_position(frame, config)}；底层配置 Y={config.bottom.y:.4f}，"
              "不一致时用 --calibrate-map 重测地图点位。")
        if config.combat_roi is None:
            print("框选主画面的游戏区域，排除底部 HP/MP、聊天栏；程序另行排除小地图。")
            config = replace(config, combat_roi=select_roi("Combat area / exclude HUD", frame))
            save_config(path, config)
        crop(frame, config.combat_roi)
        if config.player_feet_offset is None or not (root / config.player_template).exists():
            player_path = legacy / "player.png" if legacy is not None else root / config.player_template
            origin = None
            if player_path.exists():
                origin = locate_player(frame, config.combat_roi, read_image(player_path), (0, 0), config.player_threshold)
            if origin is None:
                print("框选自己固定的角色名牌文字，不要含会变化的伤害数字或大块背景。")
                player_roi = select_roi("YOUR unique nameplate", frame)
                player_image = crop(frame, player_roi)
                if cv2.cvtColor(player_image, cv2.COLOR_BGR2GRAY).std() < 5:
                    raise ValueError("角色名牌模板缺少纹理，请重新框选文字")
                player_path = manifest.parent / f"player-{time.time_ns()}.png"
                write_image(player_path, player_image)
                origin = player_roi[:2]
            else:
                print("已匹配到原角色名牌图片，只需重新单击自己的脚底。")
            feet = select_point("Click YOUR character feet", frame)
            config = replace(config, player_template=os.path.relpath(player_path, root),
                             player_feet_offset=(feet[0] - origin[0], feet[1] - origin[1]))
            save_config(path, config)
        entries = saved_samples(manifest)
        if legacy is not None:
            entries = reuse_legacy_monsters(legacy, frame, config, manifest)
        while True:
            if entries:
                answer = sample_choice(len(entries))
                if answer == "finish":
                    break
                if answer == "new":
                    frame = fresh(window, "等待另一个站立、行走或受击姿态后采集。")
                    get_position(frame, config)
            print("框选一只蓝独角狮的完整头部或身体，包含蓝白轮廓与双眼；排除名牌、HP 条和宠物。")
            monster_roi = select_roi("Blue lion HEAD or BODY / NOT pet", frame)
            monster_feet = select_point("Click THIS monster's ground contact point", frame)
            image = crop(frame, monster_roi)
            offset = (monster_feet[0] - monster_roi[0], monster_feet[1] - monster_roi[1])
            make_template(image, offset)
            filename = f"lion-{time.time_ns()}.png"
            write_image(manifest.parent / filename, image)
            entries.append(dict(file=filename, feet_offset=offset))
            save_json(manifest, entries)
            save_config(path, config)
            print(f"第 {len(entries)} 张怪物模板及脚底坐标已保存，随时可以暂停后继续。")
        if config.world_pixels_per_x is None:
            config = measure_scale(window, config)
            save_config(path, config)
        print("模板和射程比例校准完成。再运行 --measure-teleport 测量 F 的安全位移上限。")
    finally:
        window.close()
        cv2.destroyAllWindows()


def calibrate_height(path: Path) -> None:
    """只更新角色身高，保留现有区域、模板、地形参考和地图点位。"""
    config = load_config(path)
    window = GameWindow(config.window_title)
    try:
        frame = fresh(window, "请在地面站稳，角色完整露出；本次只测身高，不框选怪物或名牌。")
        if config.frame_size is not None and tuple(frame.shape[1::-1]) != config.frame_size:
            raise ValueError("客户区分辨率已改变，请恢复原尺寸再测身高")
        print("在同一张冻结截图中，先点自己的脚底接地处，再点自己的头顶（含帽子，不含特效）。")
        feet = select_point("1 / Click YOUR FEET on the ground", frame)
        head = select_point("2 / Click TOP of YOUR HEAD / include hat, not effects", frame)
        height = feet[1] - head[1]
        if (not 12 <= height <= min(512, frame.shape[0] / 2)
                or abs(feet[0] - head[0]) > max(32, height)
                or not all(0 <= point[0] < frame.shape[1] and 0 <= point[1] < frame.shape[0]
                           for point in (feet, head))):
            raise ValueError("身高点位不正确：先点自己脚底，再点同一角色头顶；未修改原配置")
        updated = replace(config, player_height_px=float(height))
        updated.validate()
        save_config(path, updated)
        print(f"已保存角色身高 {height:.0f}px；攻击带为每帧角色脚底 Y-{height:.0f} 到 Y。"
              "头像模板、怪物脚底偏移和地图点位保持原值。")
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
            print_position(f"瞬移测量 {trial + 1}/3 起点", before, config)
            require_bottom(before, config, "瞬移起点")
            second = fresh(window, "手动按住左或右方向，短按一次 F 后松开全部键；不要连续瞬移。")
            after = get_position(second, config)
            print_position(f"瞬移测量 {trial + 1}/3 终点", after, config)
            require_bottom(after, config, "瞬移终点")
            print(f"瞬移实测 ΔX={after[0] - before[0]:.4f}，ΔY={after[1] - before[1]:.4f}")
            if abs(after[1] - before[1]) > 0.02 or abs(after[0] - before[0]) < 0.005:
                raise ValueError("未测到有效的同层瞬移，请在无遮挡平地重新测量")
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
