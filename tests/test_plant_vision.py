"""食人花局部匹配、颜色补识别与降采样定位；合成图不代表实机准确率。"""
import json
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from lionbot.brain import target_status
from lionbot.config import Config
from lionbot.plant_vision import PlantColorDetector, attack_search_roi
from lionbot.vision import (Confirmer, Detection, Vision, detect_monsters,
                            locate_player, make_template, write_image)
from tests.test_vision import fixture


SKY = (190, 50, 10)


def brown_plant(left=False, hurt=False):
    image = np.full((48, 48, 3), SKY, np.uint8)
    cv2.line(image, (14, 46), (14, 12), (15, 155, 35), 4)
    for x, y in ((10, 11), (10, 25), (8, 38)):
        cv2.ellipse(image, (x, y), (8, 5), -30, 0, 360, (20, 190, 35), -1)
    cv2.ellipse(image, (30, 22), (15, 10), 0, 0, 360, (20, 45, 72), -1)
    cv2.line(image, (31, 21), (44, 23), (3, 9, 15), 3)
    if hurt:
        # 保留花头外轮廓、改变内部图案，模拟朝向/攻击动画导致模板失配。
        cv2.rectangle(image, (22, 16), (36, 27), (220, 230, 235), -1)
    return cv2.flip(image, 1) if left else image


def scene(root, **kwargs):
    frame, c = fixture(root)
    frame[240:310] = SKY
    write_image(root / "plant.png", brown_plant())
    (root / "plants.json").write_text(json.dumps([{"file": "plant.png", "feet_offset": [14, 46]}]))
    c = replace(c, behavior="stationary", auto_route=False, station=(.3, .5),
                template_manifest="plants.json", attack_range_px=172, **kwargs)
    return frame, c


class PlantVisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_only_nearby_height_band_is_matched_and_both_directions_survive(self):
        frame, c = scene(self.root)
        for x, y, left in ((420, 254, False), (180, 254, True),
                           (520, 254, False), (420, 140, False)):
            frame[y:y + 48, x:x + 48] = brown_plant(left)
        vision = Vision(c, self.root)
        with patch("lionbot.vision.detect_monsters", wraps=detect_monsters) as detector:
            vision.observe(frame, now=0)
            obs = vision.observe(frame, now=.1)
        self.assertEqual(len(obs.mobs), 2)
        self.assertTrue(all(target_status(m, obs, c) == "ready" for m in obs.mobs))
        self.assertLess(min(m.feet[0] for m in obs.mobs), obs.player_feet[0])
        self.assertGreater(max(m.feet[0] for m in obs.mobs), obs.player_feet[0])
        for call in detector.call_args_list:
            roi = call.args[1]
            self.assertLess(roi[2] * roi[3], c.combat_roi[2] * c.combat_roi[3] * .5)

    def test_template_is_not_cut_off_at_horizontal_range_boundary(self):
        frame, c = scene(self.root, plant_color_fallback=False)
        # 根部恰好在射程边缘，花头和背景越过边缘仍须完整匹配。
        x, y = 312 + 172 - 14, 254
        frame[y:y + 48, x:x + 48] = brown_plant()
        vision = Vision(c, self.root)
        vision.observe(frame, now=0)
        obs = vision.observe(frame, now=.1)
        self.assertEqual(len(obs.mobs), 1)
        self.assertEqual(obs.mobs[0].feet, (484, 300))
        self.assertEqual(target_status(obs.mobs[0], obs, c), "ready")
        frame[y:y + 48, x:x + 48] = SKY
        frame[y:y + 48, x + 1:x + 49] = brown_plant()
        self.assertEqual(vision.observe(frame, now=.2).mobs, ())

    def test_head_above_band_is_found_when_calibrated_body_reaches_band(self):
        frame, c = scene(self.root, plant_color_fallback=False)
        write_image(self.root / "plant.png", brown_plant()[:34])
        # 模板仍以根部偏移 46 计算身体；根部与角色头顶 Y=236 相交。
        frame[190:238, 420:468] = brown_plant()
        vision = Vision(c, self.root)
        vision.observe(frame, now=0)
        obs = vision.observe(frame, now=.1)
        self.assertEqual(len(obs.mobs), 1)
        self.assertEqual(obs.mobs[0].feet, (434, 236))
        self.assertEqual(target_status(obs.mobs[0], obs, c), "ready")

    def test_missing_player_skips_matching_without_reusing_old_target(self):
        frame, c = scene(self.root)
        frame[254:302, 420:468] = brown_plant()
        vision = Vision(c, self.root)
        vision.observe(frame, now=0)
        self.assertTrue(vision.observe(frame, now=.1).mobs)
        # 无地形参考的空白场景，删除名牌和缓存脚底，保留黄点。
        vision.mapper.references = []
        vision.mapper.last = None
        vision.locator = None
        with patch("lionbot.vision.detect_monsters") as detector:
            obs = vision.observe(frame, now=1)
        detector.assert_not_called()
        self.assertIsNotNone(obs.position)
        self.assertIsNone(obs.player_feet)
        self.assertIsNone(vision.search_roi)
        self.assertEqual(obs.mobs, ())
        self.assertEqual(vision.strong, [])

    def test_color_fallback_confirms_changed_head_both_ways_after_three_frames(self):
        for left in (False, True):
            with self.subTest(left=left):
                frame, c = scene(self.root)
                frame[254:302, 420:468] = brown_plant(left=left, hurt=True)
                vision = Vision(c, self.root)
                self.assertEqual(detect_monsters(frame, c.combat_roi, vision.templates,
                                                blue_lion_colors=False), [])
                for index in range(3):
                    obs = vision.observe(frame, now=index * .1)
                    self.assertEqual(len(obs.mobs), 1)
                    self.assertEqual(obs.mobs[0].confirmed, index == 2)
                    self.assertEqual(vision.detections[0].source, "color")
                self.assertEqual(target_status(obs.mobs[0], obs, c), "ready")
                self.assertAlmostEqual(obs.mobs[0].feet[0], 420 + (33 if left else 14), delta=1)
                self.assertAlmostEqual(obs.mobs[0].feet[1], 300, delta=1)
                self.assertEqual(vision.strong, [])  # 颜色命中不扩大低阈值模板追踪范围。

    def test_color_does_not_confirm_a_disappeared_head_or_extend_attack_range(self):
        frame, c = scene(self.root)
        frame[254:302, 420:468] = brown_plant(hurt=True)
        vision = Vision(c, self.root)
        vision.observe(frame, now=0)
        frame[254:302, 420:468] = SKY
        self.assertEqual(vision.observe(frame, now=.1).mobs, ())
        frame[254:302, 520:568] = brown_plant(hurt=True)
        frame[120:168, 420:468] = brown_plant(hurt=True)
        for now in (.2, .3, .4):
            self.assertEqual(vision.observe(frame, now=now).mobs, ())

    def test_intermittent_colors_do_not_accumulate_confirmation_hits(self):
        confirmer = Confirmer()
        candidate = Detection((100, 100, 31, 21), (100, 134), .9, source="color")
        self.assertFalse(confirmer.update([candidate], 0)[0].confirmed)
        self.assertFalse(confirmer.update([candidate], .1)[0].confirmed)
        self.assertEqual(confirmer.update([], .2), [])
        self.assertEqual(confirmer.update([candidate], .3)[0].hits, 1)

    def test_color_fallback_can_be_disabled_and_templates_still_work(self):
        frame, c = scene(self.root, plant_color_fallback=False)
        frame[254:302, 420:468] = brown_plant(hurt=True)
        vision = Vision(c, self.root)
        self.assertIsNone(vision.color_detector)
        for now in (0, .1, .2):
            self.assertEqual(vision.observe(frame, now=now).mobs, ())
        frame[254:302, 420:468] = brown_plant(left=True)
        vision.observe(frame, now=.3)
        self.assertTrue(vision.observe(frame, now=.4).mobs[0].confirmed)

    def test_greenery_wood_sky_and_golden_pet_do_not_make_color_targets(self):
        frame, c = scene(self.root)
        cv2.ellipse(frame, (425, 266), (20, 16), 0, 0, 360, (20, 190, 35), -1)
        cv2.rectangle(frame, (160, 245), (175, 300), (20, 45, 72), -1)
        # 相邻绿叶也不能把与花头尺寸相异的长树干当作花头。
        cv2.ellipse(frame, (155, 248), (9, 8), 0, 0, 360, (20, 190, 35), -1)
        cv2.rectangle(frame, (230, 274), (257, 296), (20, 45, 72), -1)
        cv2.ellipse(frame, (225, 270), (9, 8), 0, 0, 360, (20, 190, 35), -1)
        cv2.ellipse(frame, (350, 282), (14, 12), 0, 0, 360, (10, 200, 240), -1)
        vision = Vision(c, self.root)
        for now in (0, .1, .2):
            self.assertEqual(vision.observe(frame, now=now).mobs, ())

    def test_template_and_color_of_same_plant_are_not_double_counted(self):
        frame, c = scene(self.root)
        frame[254:302, 420:468] = brown_plant()
        vision = Vision(c, self.root)
        vision.observe(frame, now=0)
        obs = vision.observe(frame, now=.1)
        self.assertEqual(len(obs.mobs), 1)
        self.assertTrue(obs.mobs[0].confirmed)
        self.assertEqual(vision.detections[0].source, "template")

    def test_minimap_cannot_supply_color_target(self):
        template = make_template(brown_plant(), (14, 46))
        detector = PlantColorDetector([template])
        frame = np.full((70, 80, 3), SKY, np.uint8)
        frame[5:53, 10:58] = brown_plant()
        self.assertEqual(len(detector.detect(frame, (0, 0, 80, 70))), 1)
        self.assertEqual(detector.detect(frame, (0, 0, 80, 70), (0, 0, 80, 70)), [])

    def test_nameplate_is_searched_locally_and_terrain_not_rebuilt_every_frame(self):
        frame, c = scene(self.root)
        vision = Vision(c, self.root)
        with patch("lionbot.vision.locate_player", wraps=locate_player) as locate, \
                patch.object(vision.mapper, "seed") as seed:
            for now in (0, .1, .2, .8):
                self.assertEqual(vision.observe(frame, now=now).location_source, "nameplate")
        self.assertEqual(locate.call_args_list[0].args[1], c.combat_roi)
        self.assertTrue(all(call.args[1] != c.combat_roi for call in locate.call_args_list[1:]))
        self.assertEqual(seed.call_count, 2)

    def test_hurt_recovery_search_never_leaves_current_local_roi(self):
        frame, c = scene(self.root, plant_color_fallback=False)
        frame[254:302, 420:468] = brown_plant()
        vision = Vision(c, self.root)
        for now in (0, .1):
            vision.observe(frame, now=now)
        frame[254:302, 420:468] = SKY
        with patch("lionbot.vision.detect_monsters", wraps=detect_monsters) as detector:
            vision.observe(frame, now=.2)
        self.assertGreaterEqual(detector.call_count, 2)
        rx, ry, rw, rh = vision.search_roi
        for call in detector.call_args_list:
            x, y, w, h = call.args[1]
            self.assertTrue(rx <= x and ry <= y and x + w <= rx + rw and y + h <= ry + rh)

    def test_local_search_without_player_or_pixel_scale_is_disabled(self):
        c = replace(Config(), combat_roi=(0, 0, 640, 480))
        self.assertIsNone(attack_search_roi(None, c, []))
        self.assertIsNone(attack_search_roi((300, 300), c, []))

    def test_invalid_performance_and_color_options_are_rejected(self):
        for changes in ({"stationary_fps": 0}, {"stationary_fps": 60},
                        {"plant_color_frames": 1}, {"plant_color_frames": 2.5},
                        {"plant_color_fallback": "yes"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(Config(), **changes).validate()


class StationaryMappingTests(unittest.TestCase):
    def test_saved_high_resolution_anchor_survives_missing_nameplate_and_camera_motion(self):
        from lionbot.screen_map import save_screen_anchor

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, c = scene(root)
            rng = np.random.default_rng(7)
            frame[100:225] = rng.integers(0, 230, (125, 640, 3), np.uint8)
            frame[340:390] = rng.integers(0, 230, (50, 640, 3), np.uint8)
            frame = cv2.resize(frame, (1280, 960), interpolation=cv2.INTER_NEAREST)
            c = replace(c, frame_size=(1280, 960), combat_roi=(0, 180, 1280, 600),
                        minimap_roi=(8, 8, 280, 140), player_feet_offset=None,
                        attack_range_px=344, world_pixels_per_x=4000)
            save_screen_anchor(c, root, frame, (.3, 53 / 70), (624, 600))
            vision = Vision(c, root)
            self.assertEqual(vision.mapper.orb.getMaxFeatures(), 800)
            frame[620:648, 560:688] = SKY
            moved = cv2.warpAffine(frame, np.float32([[1, 0, -40], [0, 1, 16]]), (1280, 960))
            # 小地图不随主画面平移，保留符合黄点尺寸的 5×5 标记。
            moved[8:148, 8:288] = (30, 30, 30)
            cv2.rectangle(moved, (97, 98), (101, 102), (0, 255, 255), -1)
            obs = vision.observe(moved, now=0)
            self.assertEqual(obs.location_source, "terrain")
            expected = (624 + (obs.position[0] - .3) * 4000 - 40,
                        600 + (obs.position[1] - 53 / 70) * 2000 + 16)
            np.testing.assert_allclose(obs.player_feet, expected, atol=2)
            # 空白画面不沿用旧脚底、不能把无新观察当成当前定位。
            blank = np.full_like(moved, SKY)
            blank[8:148, 8:288] = moved[8:148, 8:288]
            self.assertIsNone(vision.observe(blank, now=.1).player_feet)
