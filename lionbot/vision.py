from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np

from .brain import Mob, Observation
from .config import Config


def read_image(path: Path) -> np.ndarray:
    image = cv2.imdecode(np.fromfile(path, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError(f"无法读取图像：{path}")
    return image


def write_image(path: Path, image: np.ndarray) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, encoded = cv2.imencode(".png", image)
    if not ok:
        raise ValueError(f"无法保存图像：{path}")
    encoded.tofile(path)


def crop(frame: np.ndarray, roi) -> np.ndarray:
    x, y, w, h = roi
    if x < 0 or y < 0 or x + w > frame.shape[1] or y + h > frame.shape[0]:
        raise ValueError("ROI 超出客户区；窗口尺寸或 DPI 已改变，请重新校准")
    return frame[y:y + h, x:x + w]


def minimap_position(frame: np.ndarray, roi) -> tuple[float, float] | None:
    image = crop(frame, roi)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    mask = cv2.inRange(hsv, np.array([22, 130, 160]), np.array([32, 255, 255]))
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    candidates = [c for c in contours if 3 <= cv2.contourArea(c) <= 20]
    # 不拿标题、红点或上一次位置代替角色；多个黄点也必须先排除歧义。
    if len(candidates) != 1:
        return None
    moments = cv2.moments(candidates[0])
    if moments["m00"] == 0:
        return None
    return (moments["m10"] / moments["m00"] / image.shape[1],
            moments["m01"] / moments["m00"] / image.shape[0])


@dataclass(frozen=True)
class Detection:
    box: tuple[int, int, int, int]
    feet: tuple[float, float]
    score: float
    confirmed: bool = False
    hits: int = 1


@dataclass(frozen=True)
class Template:
    gray: np.ndarray
    edges: np.ndarray
    feet_offset: tuple[float, float]
    color: np.ndarray


def make_template(image: np.ndarray, feet_offset) -> Template:
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 60, 140)
    if min(gray.shape) < 12 or gray.std() < 8 or np.count_nonzero(edges) < 12:
        raise ValueError("怪物模板太小或没有轮廓，请框选完整头部/身体，不要只选眼睛")
    return Template(gray, edges, tuple(feet_offset), image.copy())


def load_templates(manifest: Path) -> list[Template]:
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    result = []
    for entry in entries:
        image = read_image(manifest.parent / entry["file"])
        offset = entry["feet_offset"]
        result.append(make_template(image, offset))
        result.append(make_template(cv2.flip(image, 1), (image.shape[1] - 1 - offset[0], offset[1])))
    if not result:
        raise ValueError("至少采集一张蓝独角狮模板；建议站立、行走、受击各采集")
    return result


def overlap(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    intersection = max(0, min(ax + aw, bx + bw) - max(ax, bx)) * max(0, min(ay + ah, by + bh) - max(ay, by))
    return intersection / max(1, aw * ah + bw * bh - intersection)


def suppress_duplicates(found: list[Detection]) -> list[Detection]:
    kept = []
    for candidate in sorted(found, key=lambda d: d.score, reverse=True):
        if all(overlap(candidate.box, other.box) < 0.25 and
               np.hypot(candidate.feet[0] - other.feet[0], candidate.feet[1] - other.feet[1]) > 20
               for other in kept):
            kept.append(candidate)
    return kept


def detect_monsters(frame: np.ndarray, roi, templates: list[Template],
                    threshold: float = 0.82, edge_threshold: float = 0.40,
                    excluded_roi=None) -> list[Detection]:
    image = crop(frame, roi)
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 60, 140)
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    found = []
    for template in templates:
        h, w = template.gray.shape
        if h > gray.shape[0] or w > gray.shape[1]:
            continue
        scores = cv2.matchTemplate(gray, template.gray, cv2.TM_CCOEFF_NORMED)
        peaks = cv2.dilate(scores, np.ones((7, 7), np.uint8))
        ys, xs = np.where((scores >= threshold) & (scores >= peaks - 1e-6))
        # 每个姿态最多处理 100 个峰值；先选分数最高的，而非屏幕左上角的。
        ranked = sorted(zip(ys, xs), key=lambda p: float(scores[p]), reverse=True)[:100]
        template_norm = np.linalg.norm(template.edges.astype(np.float32))
        for y, x in ranked:
            box = (int(x + roi[0]), int(y + roi[1]), w, h)
            if excluded_roi is not None and overlap(box, excluded_roi) > 0:
                continue
            # 蓝天背景也含大量蓝色；必须核对对应位置的颜色，不能只统计蓝色占比。
            color_score = float(cv2.matchTemplate(image[y:y + h, x:x + w], template.color,
                                                   cv2.TM_CCOEFF_NORMED)[0, 0])
            if color_score < threshold:
                continue
            patch_edges = edges[y:y + h, x:x + w].astype(np.float32)
            similarity = float(np.sum(patch_edges * template.edges)) / max(
                1.0, float(np.linalg.norm(patch_edges) * template_norm))
            if similarity < edge_threshold:
                continue
            colors = hsv[y:y + h, x:x + w]
            white = (colors[:, :, 1] < 100) & (colors[:, :, 2] > 160)
            blue = ((colors[:, :, 0] >= 90) & (colors[:, :, 0] <= 135)
                    & (colors[:, :, 1] >= 60))
            if np.mean(white) < 0.08 or np.mean(blue) < 0.02:
                continue
            feet = (box[0] + template.feet_offset[0], box[1] + template.feet_offset[1])
            found.append(Detection(box, feet, min(float(scores[y, x]), color_score)))
    return suppress_duplicates(found)


class Confirmer:
    """只输出当前帧命中的检测；不会拿暂时丢失的旧怪物触发攻击。"""

    def __init__(self, frames: int = 2):
        self.frames = frames
        self.previous: list[Detection] = []

    def update(self, found: list[Detection]) -> list[Detection]:
        result = []
        available = list(self.previous)
        for item in found:
            nearest = min(available, key=lambda p: np.hypot(p.feet[0] - item.feet[0], p.feet[1] - item.feet[1]), default=None)
            hits = 1
            if nearest is not None and np.hypot(nearest.feet[0] - item.feet[0], nearest.feet[1] - item.feet[1]) <= 35:
                hits = nearest.hits + 1
                available.remove(nearest)
            result.append(replace(item, hits=hits, confirmed=hits >= self.frames))
        self.previous = result
        return result

    def reset(self) -> None:
        self.previous = []


def locate_player(frame: np.ndarray, roi, template: np.ndarray, feet_offset,
                  threshold: float) -> tuple[float, float] | None:
    gray = cv2.cvtColor(crop(frame, roi), cv2.COLOR_BGR2GRAY)
    target = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    if target.shape[0] > gray.shape[0] or target.shape[1] > gray.shape[1] or target.std() < 5:
        return None
    scores = cv2.matchTemplate(gray, target, cv2.TM_CCOEFF_NORMED)
    _, score, _, (x, y) = cv2.minMaxLoc(scores)
    if score < threshold:
        return None
    feet = (x + roi[0] + feet_offset[0], y + roi[1] + feet_offset[1])
    if not 0 <= feet[0] < frame.shape[1] or not 0 <= feet[1] < frame.shape[0]:
        return None
    return feet


class Vision:
    def __init__(self, config: Config, root: Path):
        self.c = config
        self.player_template = read_image(root / config.player_template)
        self.templates = load_templates(root / config.template_manifest)
        self.confirmer = Confirmer(config.confirmation_frames)
        self.detections: list[Detection] = []

    def observe(self, frame: np.ndarray, focused: bool = True) -> Observation:
        if tuple(frame.shape[1::-1]) != self.c.frame_size:
            raise ValueError("客户区分辨率已改变，请恢复原尺寸或重新校准")
        position = minimap_position(frame, self.c.minimap_roi)
        player = locate_player(frame, self.c.combat_roi, self.player_template,
                               self.c.player_feet_offset, self.c.player_threshold)
        if not focused or position is None or player is None:
            self.confirmer.reset()
            self.detections = []
            return Observation(position, player, focused=focused)
        found = detect_monsters(frame, self.c.combat_roi, self.templates,
                                self.c.monster_threshold, self.c.edge_threshold,
                                excluded_roi=self.c.minimap_roi)
        self.detections = self.confirmer.update(found)
        mobs = tuple(Mob(d.feet, d.score, d.confirmed) for d in self.detections)
        return Observation(position, player, mobs, focused)

    def draw(self, frame: np.ndarray, obs: Observation, status: str) -> np.ndarray:
        output = frame.copy()
        for d in self.detections:
            x, y, w, h = d.box
            color = (0, 255, 0) if d.confirmed else (0, 180, 255)
            cv2.rectangle(output, (x, y), (x + w, y + h), color, 1)
            cv2.circle(output, tuple(map(int, d.feet)), 3, color, -1)
            cv2.putText(output, f"{d.score:.2f} [{d.hits}]", (x, y - 4), 0, .4, color, 1)
        if obs.player_feet:
            x, y = map(int, obs.player_feet)
            reach = round(self.c.attack_range * self.c.world_pixels_per_x)
            cv2.line(output, (x - reach, y), (x + reach, y), (255, 0, 255), 2)
            cv2.circle(output, (x, y), 5, (0, 255, 255), 2)
        cv2.putText(output, status, (12, output.shape[0] - 15), 0, .5, (0, 255, 255), 1)
        return output


def measure_world_scale(before: np.ndarray, after: np.ndarray, roi,
                        before_pos, after_pos, before_feet, after_feet) -> tuple[float, float]:
    """世界位移 = 角色屏幕位移 - 背景位移，补偿横向镜头滚动。"""
    if before.shape != after.shape:
        raise ValueError("测量过程中客户区大小已改变，请恢复原尺寸")
    dx = after_pos[0] - before_pos[0]
    if abs(dx) < 0.025 or abs(after_pos[1] - before_pos[1]) > 0.02:
        raise ValueError("请在同一平地沿一个方向移动至少小地图宽度的 2.5%")
    a = cv2.cvtColor(crop(before, roi), cv2.COLOR_BGR2GRAY).astype(np.float32)
    b = cv2.cvtColor(crop(after, roi), cv2.COLOR_BGR2GRAY).astype(np.float32)
    window = cv2.createHanningWindow((a.shape[1], a.shape[0]), cv2.CV_32F)
    (background_dx, background_dy), response = cv2.phaseCorrelate(a, b, window)
    if response < 0.25 or abs(background_dy) > 5:
        raise ValueError("背景配准不可靠，请选择有固定地形的区域重新测量")
    world_dx = after_feet[0] - before_feet[0] - background_dx
    scale = world_dx / dx
    if not 100 < scale < 100000:
        raise ValueError("测得的地图比例异常，请检查黄点、脚底和固定背景区域")
    return scale, response
