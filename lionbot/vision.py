from __future__ import annotations

import json
import time
from dataclasses import dataclass, replace
from pathlib import Path

import cv2
import numpy as np

from .brain import Mob, Observation, target_status
from .config import Config


TARGET_COLORS = {"ready": (0, 255, 0), "height": (160, 160, 160),
                 "range": (255, 160, 0), "pending": (0, 180, 255),
                 "unlocalized": (130, 130, 130), "uncalibrated": (180, 80, 180)}


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
    recovered: bool = False


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
    """只输出当前帧命中；短暂漏检保留确认历史，不生成幽灵检测。"""

    def __init__(self, frames: int = 2, gap: float = .35):
        self.frames = frames
        self.gap = gap
        self.previous: list[Detection] = []
        self.history: list[tuple[Detection, float]] = []

    def update(self, found: list[Detection], now: float | None = None, shift=(0, 0)) -> list[Detection]:
        now = time.monotonic() if now is None else now
        result = []
        available = [(shift_detection(d, *shift), at) for d, at in self.history if now - at <= self.gap]
        for item in found:
            nearest = min(available, key=lambda p: np.hypot(p[0].feet[0] - item.feet[0], p[0].feet[1] - item.feet[1]), default=None)
            hits = 1
            if nearest is not None and np.hypot(nearest[0].feet[0] - item.feet[0], nearest[0].feet[1] - item.feet[1]) <= 35:
                hits = nearest[0].hits + 1
                available.remove(nearest)
            result.append(replace(item, hits=hits, confirmed=item.confirmed or hits >= self.frames))
        self.previous = result
        self.history = available + [(d, now) for d in result]
        return result

    def reset(self) -> None:
        self.previous = []
        self.history = []


def shift_detection(d: Detection, dx: float, dy: float) -> Detection:
    x, y, w, h = d.box
    return replace(d, box=(round(x + dx), round(y + dy), w, h),
                   feet=(d.feet[0] + dx, d.feet[1] + dy))


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


class PlayerLocator:
    """完整名牌优先；短时遮挡时要求当前画面中两段文字一致命中。"""

    def __init__(self, config: Config, template: np.ndarray):
        self.c, self.template = config, template
        self.feet = None
        self.full_at = float("-inf")

    def locate(self, frame: np.ndarray, now: float):
        feet = locate_player(frame, self.c.combat_roi, self.template,
                             self.c.player_feet_offset, self.c.player_threshold)
        if feet is not None:
            self.feet, self.full_at = feet, now
            return feet
        if self.feet is None or now - self.full_at > self.c.player_partial_seconds:
            return None
        rx, ry, rw, rh = self.c.combat_roi
        ox, oy = self.c.player_feet_offset
        h, w = self.template.shape[:2]
        last_x, last_y = self.feet[0] - ox, self.feet[1] - oy
        x, y = max(rx, round(last_x - 40)), max(ry, round(last_y - 25))
        right, bottom = min(rx + rw, round(last_x + w + 40)), min(ry + rh, round(last_y + h + 25))
        gray = cv2.cvtColor(frame[y:bottom, x:right], cv2.COLOR_BGR2GRAY)
        target = cv2.cvtColor(self.template, cv2.COLOR_BGR2GRAY)
        origins = []
        for index in range(3):
            a, b = index * w // 3, (index + 1) * w // 3
            part = target[:, a:b]
            if part.size == 0 or part.std() < 5 or part.shape[0] > gray.shape[0] or part.shape[1] > gray.shape[1]:
                continue
            _, score, _, (px, py) = cv2.minMaxLoc(cv2.matchTemplate(gray, part, cv2.TM_CCOEFF_NORMED))
            if score >= self.c.player_threshold:
                origins.append((x + px - a, y + py))
        for a in origins:
            agrees = [b for b in origins if abs(a[0] - b[0]) <= 3 and abs(a[1] - b[1]) <= 3]
            if len(agrees) >= 2:
                feet = (float(np.mean([b[0] for b in agrees])) + ox,
                        float(np.mean([b[1] for b in agrees])) + oy)
                if 0 <= feet[0] < frame.shape[1] and 0 <= feet[1] < frame.shape[0]:
                    self.feet = feet
                    return feet
        return None


class Vision:
    def __init__(self, config: Config, root: Path):
        self.c = config
        self.player_template = read_image(root / config.player_template)
        self.templates = load_templates(root / config.template_manifest)
        self.locator = PlayerLocator(config, self.player_template)
        self.confirmer = Confirmer(config.confirmation_frames, config.confirmation_gap)
        self.detections: list[Detection] = []
        self.strong: list[tuple[Detection, float]] = []
        self.previous: Observation | None = None

    def reset(self) -> None:
        self.confirmer.reset()
        self.detections = []
        self.strong = []
        self.previous = None
        self.locator.feet = None
        self.locator.full_at = float("-inf")

    def observe(self, frame: np.ndarray, focused: bool = True, now: float | None = None) -> Observation:
        now = time.monotonic() if now is None else now
        if tuple(frame.shape[1::-1]) != self.c.frame_size:
            raise ValueError("客户区分辨率已改变，请恢复原尺寸或重新校准")
        position = minimap_position(frame, self.c.minimap_roi)
        player = self.locator.locate(frame, now)
        if not focused or position is None or player is None:
            self.reset()
            return Observation(position, player, focused=focused)
        shift = (0.0, 0.0)
        if self.previous is not None:
            if abs(position[1] - self.previous.position[1]) > self.c.floor_tolerance:
                self.confirmer.reset()
                self.strong = []
            else:
                # 世界坐标来自小地图，剩余的主画面位移是镜头滚动。
                shift = (player[0] - self.previous.player_feet[0]
                         - (position[0] - self.previous.position[0]) * self.c.world_pixels_per_x,
                         player[1] - self.previous.player_feet[1])
        self.strong = [(shift_detection(d, *shift), at) for d, at in self.strong
                       if now - at <= self.c.hurt_recovery_seconds]
        found = detect_monsters(frame, self.c.combat_roi, self.templates,
                                self.c.monster_threshold, self.c.edge_threshold,
                                excluded_roi=self.c.minimap_roi)
        # 低阈值只在已确认怪物附近使用；受击匹配不延长正常姿态的有效期。
        rx, ry, rw, rh = self.c.combat_roi
        for seed, _ in self.strong:
            if any(np.hypot(seed.feet[0] - d.feet[0], seed.feet[1] - d.feet[1]) <= 35 for d in found):
                continue
            sx, sy, sw, sh = seed.box
            x, y = max(rx, sx - 35), max(ry, sy - 25)
            right, bottom = min(rx + rw, sx + sw + 35), min(ry + rh, sy + sh + 25)
            if right <= x or bottom <= y:
                continue
            local = detect_monsters(frame, (x, y, right - x, bottom - y), self.templates,
                                    self.c.hurt_threshold, min(.30, self.c.edge_threshold),
                                    excluded_roi=self.c.minimap_roi)
            local = [d for d in local if np.hypot(seed.feet[0] - d.feet[0], seed.feet[1] - d.feet[1]) <= 45]
            if local:
                best = max(local, key=lambda d: d.score)
                found.append(replace(best, recovered=True, confirmed=True))
        self.detections = self.confirmer.update(suppress_duplicates(found), now, shift)
        for d in self.detections:
            if d.confirmed and not d.recovered:
                self.strong = [(old, at) for old, at in self.strong
                               if np.hypot(old.feet[0] - d.feet[0], old.feet[1] - d.feet[1]) > 35]
                self.strong.append((d, now))
        mobs = tuple(Mob(d.feet, d.score, d.confirmed, d.recovered) for d in self.detections)
        obs = Observation(position, player, mobs, focused)
        self.previous = obs
        return obs

    def draw(self, frame: np.ndarray, obs: Observation, status: str) -> np.ndarray:
        output = frame.copy()
        for d in self.detections:
            x, y, w, h = d.box
            state = target_status(Mob(d.feet, d.score, d.confirmed), obs, self.c)
            color = TARGET_COLORS[state]
            cv2.rectangle(output, (x, y), (x + w, y + h), color, 1)
            cv2.circle(output, tuple(map(int, d.feet)), 3, color, -1)
            dy = f" dY={d.feet[1] - obs.player_feet[1]:+.0f}" if obs.player_feet else ""
            dx = f" dX={d.feet[0] - obs.player_feet[0]:+.0f}" if obs.player_feet else ""
            cv2.putText(output, f"{state.upper()}{'~' if d.recovered else ''}{dx}{dy} {d.score:.2f}",
                        (x, y - 4), 0, .4, color, 1)
        if obs.player_feet:
            x, y = map(int, obs.player_feet)
            reach = round(self.c.attack_range * self.c.world_pixels_per_x)
            tolerance = round(self.c.feet_tolerance_px)
            cv2.rectangle(output, (x - reach, y - tolerance), (x + reach, y + tolerance), (255, 0, 255), 1)
            cv2.line(output, (x - reach, y), (x + reach, y), (255, 0, 255), 2)
            cv2.circle(output, (x, y), 5, (0, 255, 255), 2)
        cv2.putText(output, status, (12, output.shape[0] - 15), 0, .5, (0, 255, 255), 1)
        cv2.putText(output, "GREEN: in range | GRAY: height | BLUE: far | ORANGE: pending",
                    (12, output.shape[0] - 38), 0, .45, (230, 230, 230), 1)
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
