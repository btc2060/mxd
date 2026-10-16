"""用黄点位移与地形镜头位移推算主画面脚底，不要求名牌持续可见。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import Config, load_config


@dataclass
class ScreenReference:
    position: tuple[float, float]
    feet: tuple[float, float]
    points: np.ndarray
    descriptors: np.ndarray


class ScreenMapper:
    def __init__(self, config: Config, root: Path, load_saved: bool = True):
        self.c = config
        self.orb = cv2.ORB_create(nfeatures=800 if config.behavior == "stationary" else 1600,
                                 edgeThreshold=12, fastThreshold=14)
        self.matcher = cv2.BFMatcher(cv2.NORM_HAMMING)
        self.references: list[ScreenReference] = []
        self.last: ScreenReference | None = None
        self.notice = ""
        source = root / config.screen_anchor
        if load_saved and source.exists():
            from .vision import read_image

            try:
                data = json.loads(source.read_text(encoding="utf-8"))
                expected = {"frame_size": list(config.frame_size), "minimap_roi": list(config.minimap_roi),
                            "combat_roi": list(config.combat_roi), "world_pixels_per_x": config.world_pixels_per_x}
                if any(data.get(key) != value for key, value in expected.items()):
                    self.notice = "脚底参考与当前窗口/ROI/比例不一致，请重跑 --calibrate-anchor"
                else:
                    frame = read_image(source.parent / data["frame"])
                    position, feet = tuple(data["position"]), tuple(data["feet"])
                    if (tuple(frame.shape[1::-1]) != config.frame_size or len(position) != 2 or len(feet) != 2
                            or not all(np.isfinite(value) for value in position + feet)
                            or not all(0 <= v <= 1 for v in position)
                            or not 0 <= feet[0] < frame.shape[1] or not 0 <= feet[1] < frame.shape[0]):
                        raise ValueError("参考坐标不合法")
                    self.seed(frame, position, feet)
            except (ValueError, OSError, KeyError, TypeError, cv2.error):
                self.notice = "无法读取脚底参考，请重跑 --calibrate-anchor；黄点导航仍可运行"

    def features(self, frame: np.ndarray, excluded_boxes=()):
        mask = np.zeros(frame.shape[:2], np.uint8)
        x, y, w, h = self.c.combat_roi
        mask[y:y + h, x:x + w] = 255
        x, y, w, h = self.c.minimap_roi
        mask[y:y + h, x:x + w] = 0
        for x, y, w, h in excluded_boxes:
            mask[max(0, y - 8):y + h + 8, max(0, x - 8):x + w + 8] = 0
        image = frame
        if self.c.behavior == "stationary" and frame.shape[1] > 640:
            size = (640, round(frame.shape[0] * 640 / frame.shape[1]))
            image = cv2.resize(frame, size, interpolation=cv2.INTER_AREA)
            mask = cv2.resize(mask, size, interpolation=cv2.INTER_NEAREST)
        points, descriptors = self.orb.detectAndCompute(cv2.cvtColor(image, cv2.COLOR_BGR2GRAY), mask)
        # 参考文件和后续脚底换算仍使用原图像素，已有校准可以直接复用。
        scale = np.float32((frame.shape[1] / image.shape[1], frame.shape[0] / image.shape[0]))
        return np.float32([p.pt for p in points]).reshape(-1, 2) * scale, descriptors

    def remember(self, reference: ScreenReference) -> None:
        self.last = reference
        sx = self.c.world_pixels_per_x
        sy = sx * self.c.minimap_roi[3] / self.c.minimap_roi[2]
        if not self.references or all(
                abs(old.position[0] - reference.position[0]) * sx > self.c.frame_size[0] * .25
                or abs(old.position[1] - reference.position[1]) * sy > self.c.frame_size[1] * .25
                for old in self.references):
            self.references.append(reference)
            if len(self.references) > 16:
                self.references.pop(1)  # 保留最初手动/视觉参考，限制内存。

    def seed(self, frame: np.ndarray, position, feet, excluded_boxes=()) -> None:
        body = (round(feet[0] - 45), round(feet[1] - 90), 90, 135)
        points, descriptors = self.features(frame, [*excluded_boxes, body])
        if descriptors is not None and len(points) >= 12:
            self.notice = ""
            self.remember(ScreenReference(position, feet, points, descriptors))
        else:
            self.notice = "参考画面缺少固定地形特征，请在地形清晰处重跑 --calibrate-anchor"

    def translation(self, reference: ScreenReference, points, descriptors):
        pairs = self.matcher.knnMatch(reference.descriptors, descriptors, k=2)
        matches = [pair[0] for pair in pairs if len(pair) == 2
                   and pair[0].distance < .7 * pair[1].distance and pair[0].distance < 64]
        if len(matches) < 12:
            return None
        before = np.float32([reference.points[m.queryIdx] for m in matches])
        after = np.float32([points[m.trainIdx] for m in matches])
        matrix, inliers = cv2.estimateAffinePartial2D(before, after, method=cv2.RANSAC,
                                                    ransacReprojThreshold=2, maxIters=700)
        if matrix is None or inliers is None:
            return None
        good = inliers.ravel().astype(bool)
        # 地形只接受平移；怪物动画、重复式柱子、窗口缩放不能冒充镜头。
        if (good.sum() < 12 or good.mean() < .35 or abs(matrix[0, 0] - 1) > .015
                or abs(matrix[1, 1] - 1) > .015 or abs(matrix[0, 1]) > .01
                or abs(matrix[1, 0]) > .01
                or np.ptp(before[good, 0]) < self.c.combat_roi[2] * .18
                or np.ptp(before[good, 1]) < self.c.combat_roi[3] * .12):
            return None
        return np.median(after[good] - before[good], axis=0)

    def locate(self, frame: np.ndarray, position, excluded_boxes=()):
        if not self.references:
            return None
        points, descriptors = self.features(frame, excluded_boxes)
        if descriptors is None or len(points) < 12:
            return None
        sx = self.c.world_pixels_per_x
        sy = sx * self.c.minimap_roi[3] / self.c.minimap_roi[2]
        nearest = sorted(self.references, key=lambda r: np.hypot(
            (r.position[0] - position[0]) * sx, (r.position[1] - position[1]) * sy))[:3]
        references = ([self.last] if self.last is not None else []) + nearest
        for reference in references:
            shift = self.translation(reference, points, descriptors)
            if shift is None:
                continue
            feet = (reference.feet[0] + (position[0] - reference.position[0]) * sx + float(shift[0]),
                    reference.feet[1] + (position[1] - reference.position[1]) * sy + float(shift[1]))
            if 0 <= feet[0] < frame.shape[1] and 0 <= feet[1] < frame.shape[0]:
                self.remember(ScreenReference(position, feet, points, descriptors))
                return feet
        return None


def save_screen_anchor(config: Config, root: Path, frame, position, feet) -> None:
    from .calibrate import save_json
    from .vision import write_image

    mapper = ScreenMapper(config, root, load_saved=False)
    mapper.seed(frame, position, feet)
    if not mapper.references:
        raise ValueError(mapper.notice)
    target = root / config.screen_anchor
    image_path = target.with_name(target.stem + "-frame.png")
    write_image(image_path, frame)
    save_json(target, {"position": position, "feet": feet, "frame": image_path.name,
                       "frame_size": config.frame_size, "minimap_roi": config.minimap_roi,
                       "combat_roi": config.combat_roi, "world_pixels_per_x": config.world_pixels_per_x})


def calibrate_anchor(path: Path) -> None:
    from .calibrate import fresh, get_position, select_point
    from .windows import GameWindow

    config = load_config(path)
    config.require_calibration()
    window = GameWindow(config.window_title)
    try:
        print("只点一次角色脚底，不框选名牌或怪物；用于确定左右和攻击高度，导航使用黄点。")
        frame = fresh(window, "请让角色在底层或顶层地面站稳，截图时松开按键。")
        position = get_position(frame, config)
        if not any(abs(position[1] - floor.y) <= config.floor_tolerance for floor in (config.bottom, config.top)):
            raise ValueError("脚底参考需要在已配置的底层或顶层地面采集，不能在梯子上")
        feet = select_point("Click YOUR character feet ONCE / no nameplate needed", frame)
        save_screen_anchor(config, path.parent, frame, position, feet)
        print(f"已保存黄点位置 {position} 与主画面脚底 {feet}。运行时无需名牌持续出现。")
    finally:
        window.close()
        cv2.destroyAllWindows()
