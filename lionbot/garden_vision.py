"""复用局部识别/镜像/受击追踪，用紫红花头及绿叶补识别。"""
from __future__ import annotations

import json
import time
from pathlib import Path

from .plant_vision import PlantColorDetector
from .screen_map import save_screen_anchor
from .vision import Vision, read_image, write_image


def anchor_signature(config):
    return {"frame_size": list(config.frame_size), "minimap_roi": list(config.minimap_roi),
            "combat_roi": list(config.combat_roi), "world_pixels_per_x": config.world_pixels_per_x}


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

    save_screen_anchor(config.vision_config(), root, frame, position, feet)
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
