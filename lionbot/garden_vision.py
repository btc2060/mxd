"""复用局部识别/镜像/受击追踪，用紫红花头及绿叶补识别。"""
from __future__ import annotations

import json
import time
from pathlib import Path

import cv2

from .plant_vision import PlantColorDetector, intersect_roi
from .screen_map import AnchorFeatureError, save_screen_anchor
from .vision import Vision, read_image, write_image


def anchor_signature(config):
    return {"frame_size": list(config.frame_size), "minimap_roi": list(config.minimap_roi),
            "combat_roi": list(config.combat_roi), "world_pixels_per_x": config.world_pixels_per_x}


def combat_area_problem(config, frame=None, feet=None):
    if config.combat_roi is None:
        return "尚未框选主画面"
    x, y, w, h = config.combat_roi
    overlap = intersect_roi(config.combat_roi, config.minimap_roi) if config.minimap_roi else None
    if overlap and overlap[2] * overlap[3] >= .5 * w * h:
        return "主画面框与小地图大面积重合，排除小地图后没有足够地形；请框选完整游戏场景"
    width, height = frame.shape[1::-1] if frame is not None else config.frame_size
    if w < max(160, .4 * width) or h < max(96, .18 * height):
        return f"主画面框只有 {w}×{h}，范围过小；请包含角色、平台和树木/岩壁，不要只框小地图或角色"
    if feet is not None and not (x <= feet[0] < x + w and y <= feet[1] < y + h):
        return "角色脚底不在主画面框内，请扩大主画面范围"
    return ""


def anchor_metadata_valid(config, root):
    path = root / config.screen_anchor
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return (all(data.get(key) == value for key, value in anchor_signature(config).items())
                and (path.parent / data["frame"]).is_file())
    except (ValueError, OSError, KeyError, TypeError):
        return False


class GardenReferenceError(ValueError):
    pass


def reference_error(config, root, frame, position, feet, reason, diagnostics=None):
    from .calibrate import save_json

    prefix = root / "debug-output" / "garden" / f"anchor-{time.time_ns()}"
    try:
        marked = frame.copy()
        for roi, color in ((config.combat_roi, (0, 165, 255)), (config.minimap_roi, (0, 0, 255))):
            if roi is not None:
                x, y, w, h = roi
                cv2.rectangle(marked, (x, y), (x + w - 1, y + h - 1), color, 2)
        cv2.circle(marked, tuple(map(round, feet)), 5, (0, 255, 255), 2)
        write_image(Path(f"{prefix}-screen.png"), frame)
        write_image(Path(f"{prefix}-regions.png"), marked)
        save_json(Path(f"{prefix}.json"), {"reason": reason, "combat_roi": config.combat_roi,
                                          "minimap_roi": config.minimap_roi, "frame_size": config.frame_size,
                                          "position": position, "feet": feet, **(diagnostics or {})})
        detail = f"；诊断已保存 {prefix}-regions.png 和 .json"
    except (OSError, ValueError) as error:
        detail = f"；诊断写盘失败：{error}"
    count = f"，特征数={diagnostics['feature_count']}/12" if diagnostics else ""
    return GardenReferenceError(f"脚底/地形参考未保存：{reason}{count}；主画面={config.combat_roi}，"
                                f"小地图={config.minimap_roi}{detail}。请运行 python -m lionbot.garden --select-area "
                                "重新框选主画面，不要重复重采小地图。")


def read_anchor_bank(config, root):
    path = root / config.anchor_bank
    if not path.exists():
        return []
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("signature") != anchor_signature(config):
        return []
    return data["anchors"]


def save_garden_anchor(config, root, frame, position, feet):
    from .calibrate import save_json

    problem = combat_area_problem(config, frame, feet)
    if problem:
        raise reference_error(config, root, frame, position, feet, problem)
    try:
        save_screen_anchor(config.vision_config(), root, frame, position, feet)
    except AnchorFeatureError as error:
        raise reference_error(config, root, frame, position, feet,
                              "当前主画面中可用于配准的地形特征不足；请扩大到包含平台和树木/岩壁的游戏场景",
                              error.diagnostics) from error
    path = root / config.anchor_bank
    try:
        anchors = read_anchor_bank(config, root)
    except (ValueError, KeyError, TypeError, OSError):
        anchors = []
    filename = f"anchor-{time.time_ns()}.png"
    write_image(path.parent / filename, frame)
    # 替换同一区域的旧参考，保留左右端及少量中间参考。
    anchors = [a for a in anchors if abs(a["position"][0] - position[0]) * config.world_pixels_per_x > 100]
    anchors.append({"frame": filename, "position": position, "feet": feet})
    save_json(path, {"signature": anchor_signature(config), "anchors": anchors[-12:]})


class GardenVision(Vision):
    def __init__(self, config, root: Path):
        problem = combat_area_problem(config)
        if problem:
            raise ValueError(problem + "；请运行 python -m lionbot.garden --select-area 重选主画面")
        super().__init__(config.vision_config(), root)
        self.color_detector = PlantColorDetector(self.templates, head_color="magenta")
        if not self.color_detector.models:
            raise ValueError("样本中没有可用的紫红花头，请运行 python -m lionbot.garden --sample，框选完整食人花含绿叶")
        for anchor in read_anchor_bank(config, root):
            frame = read_image((root / config.anchor_bank).parent / anchor["frame"])
            if tuple(frame.shape[1::-1]) != config.frame_size:
                raise ValueError("地形参考尺寸改变，请重新 --setup 或 --anchor")
            self.mapper.seed(frame, tuple(anchor["position"]), tuple(anchor["feet"]))
        if not self.mapper.references:
            raise ValueError("缺少当前地图的脚底/地形参考，请运行 python -m lionbot.garden --anchor")
