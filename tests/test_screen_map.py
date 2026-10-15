"""用合成地形验证黄点/镜头映射，不代表真实游戏地形跟踪成功率。"""
import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from lionbot.__main__ import main
from lionbot.brain import target_status
from lionbot.config import Config
from lionbot.screen_map import ScreenMapper, calibrate_anchor
from lionbot.vision import Vision, minimap_position
from tests.test_vision import fixture


def textured_fixture(root):
    frame, config = fixture(root)
    rng = np.random.default_rng(104)
    frame[100:220] = rng.integers(10, 200, (120, 640, 3), dtype=np.uint8)
    frame[340:390] = rng.integers(10, 200, (50, 640, 3), dtype=np.uint8)
    return frame, config


class ScreenMappingTests(unittest.TestCase):
    def test_horizontal_and_vertical_camera_motion_compensates_minimap_displacement(self):
        with tempfile.TemporaryDirectory() as directory:
            frame, config = textured_fixture(Path(directory))
            mapper = ScreenMapper(config, Path(directory))
            mapper.seed(frame, (.3, .759), (312, 300))
            moved = cv2.warpAffine(frame, np.float32([[1, 0, -30], [0, 1, 25]]), (640, 480))
            feet = mapper.locate(moved, (.325, .659))
            self.assertIsNotNone(feet)
            # 横向 +50-30，纵向 -100+25，比例按小地图 ROI 宽高换算。
            self.assertAlmostEqual(feet[0], 332, delta=1)
            self.assertAlmostEqual(feet[1], 225, delta=1)

    def test_blank_or_scaled_terrain_is_not_used_as_a_camera_translation(self):
        with tempfile.TemporaryDirectory() as directory:
            frame, config = textured_fixture(Path(directory))
            mapper = ScreenMapper(config, Path(directory))
            mapper.seed(frame, (.3, .759), (312, 300))
            self.assertIsNone(mapper.locate(np.zeros_like(frame), (.3, .759)))
            scaled = cv2.warpAffine(frame, np.float32([[1.1, 0, 0], [0, 1.1, 0]]), (640, 480))
            self.assertIsNone(mapper.locate(scaled, (.3, .759)))

    def test_keyframes_allow_long_walk_and_relocalization_after_pause(self):
        with tempfile.TemporaryDirectory() as directory:
            config = replace(Config(), frame_size=(640, 480), minimap_roi=(0, 0, 120, 60),
                             combat_roi=(0, 80, 640, 360), world_pixels_per_x=2000)
            rng = np.random.default_rng(9)
            world = rng.integers(0, 230, (480, 2000, 3), dtype=np.uint8)
            mapper = ScreenMapper(config, Path(directory))
            mapper.seed(world[:, :640], (.1, .759), (200, 330))
            for index in range(1, 7):
                frame = world[:, index * 200:index * 200 + 640]
                feet = mapper.locate(frame, (.1 + index * .1, .759))
                self.assertIsNotNone(feet, f"walk step {index}")
                self.assertAlmostEqual(feet[0], 200, delta=1)
                self.assertAlmostEqual(feet[1], 330, delta=1)
            feet = mapper.locate(world[:, :640], (.1, .759))
            self.assertAlmostEqual(feet[0], 200, delta=1)
            self.assertLessEqual(len(mapper.references), 16)

    def save_reference(self, root):
        frame, config = textured_fixture(root)
        path = root / "config.local.json"
        path.write_text(json.dumps(asdict(config)), encoding="utf-8")
        original = path.read_bytes()
        with patch("lionbot.windows.GameWindow") as window, \
                patch("lionbot.calibrate.fresh", return_value=frame), \
                patch("lionbot.calibrate.select_point", return_value=(312, 300)), \
                patch("lionbot.screen_map.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
            calibrate_anchor(path)
        window.return_value.close.assert_called_once()
        self.assertEqual(path.read_bytes(), original)
        return frame, config, path

    def test_one_click_reference_persists_and_supports_detection_without_nameplate(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, config, _ = self.save_reference(root)
            (root / config.player_template).unlink()
            frame[310:324, 280:344] = (190, 50, 10)
            vision = Vision(config, root)
            for now in (0, .1):
                obs = vision.observe(frame, now=now)
            self.assertEqual(obs.location_source, "terrain")
            self.assertAlmostEqual(obs.player_feet[0], 312, delta=1)
            self.assertAlmostEqual(obs.player_feet[1], 300, delta=1)
            self.assertEqual(len(obs.mobs), 1)
            self.assertEqual(target_status(obs.mobs[0], obs, config, "right"), "ready")
            self.assertEqual(target_status(obs.mobs[0], obs, config, "left"), "behind")
            self.assertTrue((root / config.screen_anchor).exists())

    def test_existing_nameplate_can_seed_terrain_tracking_before_it_disappears(self):
        with tempfile.TemporaryDirectory() as directory:
            frame, config = textured_fixture(Path(directory))
            vision = Vision(config, Path(directory))
            self.assertEqual(vision.observe(frame, now=0).location_source, "nameplate")
            frame[310:324, 280:344] = (190, 50, 10)
            obs = vision.observe(frame, now=1)
            self.assertEqual(obs.location_source, "terrain")
            self.assertAlmostEqual(obs.player_feet[0], 312, delta=1)
            vision.reset()  # 暂停恢复可以重定位地形，不清除有效参考图。
            self.assertEqual(vision.observe(frame, now=2).location_source, "terrain")

    def test_without_any_anchor_detection_and_yellow_position_still_continue(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, config = fixture(root)
            (root / config.player_template).unlink()
            vision = Vision(config, root)
            vision.observe(frame, now=0)
            obs = vision.observe(frame, now=.1)
            self.assertIsNotNone(obs.position)
            self.assertIsNone(obs.player_feet)
            self.assertEqual(obs.location_source, "unmapped")
            self.assertTrue(obs.mobs[0].confirmed)

    def test_corrupt_or_stale_anchor_gives_notice_and_can_be_replaced(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, config, path = self.save_reference(root)
            source = root / config.screen_anchor
            data = json.loads(source.read_text())
            data["minimap_roi"] = [9, 9, 100, 100]
            source.write_text(json.dumps(data))
            mapper = ScreenMapper(config, root)
            self.assertEqual(mapper.references, [])
            self.assertIn("--calibrate-anchor", mapper.notice)
            source.write_text("{bad-json")
            self.assertIn("--calibrate-anchor", ScreenMapper(config, root).notice)
            with patch("lionbot.windows.GameWindow"), \
                    patch("lionbot.calibrate.fresh", return_value=frame), \
                    patch("lionbot.calibrate.select_point", return_value=(312, 300)), \
                    patch("lionbot.screen_map.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
                calibrate_anchor(path)
            self.assertTrue(ScreenMapper(config, root).references)

    def test_anchor_cli_dispatches_without_requiring_nameplate_calibration(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.local.json"
            with patch("lionbot.screen_map.calibrate_anchor") as function:
                self.assertEqual(main(["--config", str(path), "--calibrate-anchor"]), 0)
            function.assert_called_once_with(path)
