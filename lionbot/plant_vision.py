"""食人花的局部搜索范围与基于样本尺寸的棕色花头、绿叶补识别。"""
from __future__ import annotations

from dataclasses import dataclass
from math import ceil, floor

import cv2
import numpy as np

from .config import Config


def intersect_roi(a, b):
    left, top = max(a[0], b[0]), max(a[1], b[1])
    right, bottom = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    return (left, top, right - left, bottom - top) if right > left and bottom > top else None


def attack_search_roi(player, config: Config, templates):
    if player is None or config.attack_reach_px is None:
        return None
    x, y = player
    # 只为完整模板保留边缘余量；检测后仍按原攻击带筛选，不能让余量扩大射程。
    left = max((t.feet_offset[0] for t in templates), default=0)
    right = max((t.gray.shape[1] - t.feet_offset[0] for t in templates), default=0)
    above = max((max(t.gray.shape[0] - 1, t.feet_offset[1]) for t in templates), default=0)
    below = max((t.gray.shape[0] - min(0, t.feet_offset[1]) for t in templates), default=0)
    x0, x1 = floor(x - config.attack_reach_px - left), ceil(x + config.attack_reach_px + right + 1)
    y0, y1 = floor(y - config.player_height_px - above), ceil(y + below + 1)
    return intersect_roi((x0, y0, x1 - x0, y1 - y0), config.combat_roi)


def plant_masks(image):
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    brown = cv2.inRange(hsv, np.array([2, 65, 20]), np.array([30, 255, 180]))
    green = cv2.inRange(hsv, np.array([35, 75, 40]), np.array([90, 255, 255]))
    return brown, green


def head_contours(mask):
    # 保留花头形状，不用大核把树干、地砖和花头连在一起。
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = cv2.contourArea(contour)
        hull_fill = cv2.contourArea(cv2.convexHull(contour)) / (w * h)
        if w >= 7 and h >= 6 and area >= 35 and .18 <= area / (w * h) <= .94 and hull_fill <= .90:
            yield contour, (x, y, w, h)


def leaf_side(green, box):
    x, y, w, h = box
    x0, y0 = max(0, x - w), max(0, y - h)
    x1, y1 = min(green.shape[1], x + 2 * w), min(green.shape[0], y + max(1, h * 3 // 4))
    ys, xs = np.nonzero(green[y0:y1, x0:x1])
    side = (float(np.mean(xs)) + x0 - (x + w / 2)) / w if xs.size else 0.
    return int(xs.size), side


@dataclass
class HeadModel:
    contour: np.ndarray
    width: int
    height: int
    feet: tuple[float, float]
    leaf_side: float


@dataclass(frozen=True)
class ColorCandidate:
    box: tuple[int, int, int, int]
    feet: tuple[float, float]
    score: float


class PlantColorDetector:
    def __init__(self, templates):
        self.models = []
        for template in templates:
            brown, green = plant_masks(template.color)
            # 采样里较大的棕色轮廓才是花头；不从细小地砖/文字训练模型。
            choices = sorted(head_contours(brown), key=lambda item: cv2.contourArea(item[0]), reverse=True)
            for contour, (x, y, w, h) in choices[:1]:
                _, side = leaf_side(green, (x, y, w, h))
                self.models.append(HeadModel(cv2.convexHull(contour), w, h,
                                            (template.feet_offset[0] - x, template.feet_offset[1] - y), side))

    def detect(self, frame, roi, excluded_roi=None):
        if roi is None or not self.models:
            return []
        rx, ry, rw, rh = roi
        brown, green = plant_masks(frame[ry:ry + rh, rx:rx + rw])
        if excluded_roi is not None:
            excluded = intersect_roi(roi, excluded_roi)
            if excluded is not None:
                x, y, w, h = excluded
                brown[y - ry:y - ry + h, x - rx:x - rx + w] = 0
                green[y - ry:y - ry + h, x - rx:x - rx + w] = 0
        result = []
        for contour, box in head_contours(brown):
            x, y, w, h = box
            leaves, side = leaf_side(green, box)
            if leaves < max(10, .08 * w * h):
                continue
            best = None
            for model in self.models:
                sx, sy = w / model.width, h / model.height
                if not .65 <= sx <= 1.5 or not .65 <= sy <= 1.5 or abs(sx - sy) > .45:
                    continue
                if abs(model.leaf_side) > .2 and (abs(side) < .1 or side * model.leaf_side < 0):
                    continue
                # 外包轮廓不受嘴部开合和内部受击闪光切口影响；矩形地砖已在上面排除。
                shape = cv2.matchShapes(cv2.convexHull(contour), model.contour, cv2.CONTOURS_MATCH_I1, 0)
                if not np.isfinite(shape) or shape > .4:
                    continue
                score = .9 - min(.2, shape * .3 + abs(sx - sy) * .1)
                if best is None or score > best.score:
                    feet = (rx + x + model.feet[0] * sx, ry + y + model.feet[1] * sy)
                    best = ColorCandidate((rx + x, ry + y, w, h), feet, score)
            if best is not None:
                result.append(best)
        return result
