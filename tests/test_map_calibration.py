import io
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import cv2

from lionbot.__main__ import main
from lionbot.brain import Brain
from lionbot.calibrate import require_bottom, save_json
from lionbot.config import load_config
from lionbot.map_calibration import apply_map_profile, calibrate_map, pick_minimap_x, standing_sample
from tests.test_vision import fixture


class MapCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.frame, config = fixture(self.root)
        # 使用用户截图的实际 ROI，底层黄点中心在该区域 y=83.5。
        self.config = replace(config, minimap_roi=(24, 73, 137, 103))
        self.path = self.root / "config.local.json"
        save_json(self.path, asdict(self.config))
        self.bottom = self.at(.4, .8107)
        self.top = self.at(.2, .29)
        self.stack = ExitStack()
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.window = self.stack.enter_context(patch("lionbot.map_calibration.GameWindow")).return_value
        self.window.focused.return_value = True
        self.stack.enter_context(patch("lionbot.map_calibration.time.sleep"))
        self.stack.enter_context(patch("lionbot.map_calibration.cv2.destroyAllWindows"))
        self.capture = self.stack.enter_context(patch("lionbot.map_calibration.fresh"))
        self.capture.side_effect = [self.bottom, self.top]
        self.window.capture.side_effect = [self.bottom] * 4 + [self.top] * 4
        self.points = self.stack.enter_context(patch("lionbot.map_calibration.pick_minimap_x"))
        self.points.side_effect = [.03, .97, .30, .05, .30]

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def at(self, x, y):
        frame = self.frame.copy()
        left, top, width, height = self.config.minimap_roi
        frame[top:top + height, left:left + width] = (30, 30, 30)
        cx, cy = left + round(x * width), top + round(y * height)
        cv2.rectangle(frame, (cx - 2, cy - 2), (cx + 1, cy + 1), (0, 255, 255), -1)
        return frame

    def test_full_map_records_observed_y_bounds_and_keeps_templates_and_scale(self):
        images = {p.name: p.read_bytes() for p in self.root.glob("*.png")}
        calibrate_map(self.path)
        updated = load_config(self.path)
        self.assertAlmostEqual(updated.bottom.y, 83.5 / 103)
        self.assertAlmostEqual(updated.bottom.y, .8107, places=4)
        self.assertAlmostEqual(updated.top.y, 29.5 / 103)
        self.assertEqual((updated.bottom.left, updated.bottom.right, updated.ladder_x), (.03, .97, .30))
        self.assertEqual((updated.top.left, updated.top.right, updated.top_drop_x), (.05, .30, .05))
        require_bottom((.792, .8107), updated, "当前底层")
        self.assertTrue(updated.auto_route)
        self.assertEqual(updated.player_template, self.config.player_template)
        self.assertEqual(updated.template_manifest, self.config.template_manifest)
        self.assertEqual(updated.world_pixels_per_x, self.config.world_pixels_per_x)
        self.assertEqual(updated.attack_range, self.config.attack_range)
        self.assertEqual(images, {p.name: p.read_bytes() for p in self.root.glob("*.png")})
        self.assertEqual(Brain(updated).classify_floor(.8107), "bottom")
        self.assertIsNone(Brain(updated).classify_floor(.6845))

    def test_bottom_only_can_unblock_teleport_measurement_without_guessing_top(self):
        self.capture.side_effect = [self.bottom]
        self.window.capture.side_effect = [self.bottom] * 4
        calibrate_map(self.path, bottom_only=True)
        updated = load_config(self.path)
        require_bottom((.1788, .8107), updated, "当前底层")
        self.assertFalse(updated.auto_route)
        self.assertEqual(updated.top, self.config.top)
        self.assertEqual(self.points.call_count, 3)
        self.assertEqual(self.capture.call_count, 1)

    def test_top_interruption_preserves_completed_bottom_and_disables_old_route(self):
        self.capture.side_effect = [self.bottom, ValueError("暂停")]
        with self.assertRaisesRegex(ValueError, "暂停"):
            calibrate_map(self.path)
        updated = load_config(self.path)
        self.assertAlmostEqual(updated.bottom.y, .8107, places=4)
        self.assertFalse(updated.auto_route)
        self.assertEqual(updated.top, self.config.top)
        self.window.close.assert_called_once()

    def test_rejects_recording_a_moving_y_as_standing_floor(self):
        self.window.capture.side_effect = [self.bottom, self.at(.4, .70), self.at(.4, .65), self.at(.4, .6)]
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "Y 仍在变化"):
            standing_sample(self.window, self.config, "请站稳")
        self.assertEqual(self.path.read_bytes(), original)

    def test_invalid_top_right_never_enables_an_unsafe_route(self):
        self.points.side_effect = [.03, .97, .30, .05, .50]
        with self.assertRaisesRegex(ValueError, "不能超过"):
            calibrate_map(self.path)
        self.assertFalse(load_config(self.path).auto_route)

    def test_zoomed_map_click_is_converted_back_to_original_normalized_x(self):
        # 单独测试真实点位转换，窗口点击用模拟输入替代。
        with patch("lionbot.map_calibration.select_point", return_value=(325, 100)) as click:
            x = pick_minimap_x("left", self.bottom, self.config)
        self.assertEqual(x, .5)
        self.assertEqual(click.call_args.args[1].shape[1], 650)

    def test_cli_map_calibration_does_not_require_finished_distance_measurement(self):
        save_json(self.path, asdict(replace(self.config, world_pixels_per_x=None)))
        with patch("lionbot.map_calibration.calibrate_map") as run:
            self.assertEqual(main(["--config", str(self.path), "--calibrate-map", "--bottom-only"]), 0)
        run.assert_called_once_with(self.path, bottom_only=True)

    def test_confirmed_profile_fixes_floor_and_converts_range_without_rescaling_runtime_measurements(self):
        apply_map_profile(self.path)
        updated = load_config(self.path)
        self.assertEqual((updated.bottom.left, updated.bottom.right, updated.bottom.y), (.0182, .9745, .8107))
        self.assertEqual(updated.top.y, .4806)
        scale = (.9745 - .0182) / (.659 - .029)
        self.assertAlmostEqual(updated.ladder_x, .0182 + (.212 - .029) * scale)
        self.assertAlmostEqual(updated.ladder_x, .2956, delta=.001)
        self.assertAlmostEqual(updated.top_drop_x, .0182 + (.045 - .029) * scale)
        self.assertAlmostEqual(updated.attack_range, .086 * scale)
        self.assertEqual(updated.world_pixels_per_x, self.config.world_pixels_per_x)
        self.assertEqual(updated.teleport_max_dx, self.config.teleport_max_dx)
        self.assertEqual(updated.player_template, self.config.player_template)
        self.assertEqual(updated.template_manifest, self.config.template_manifest)
        require_bottom((.792, .8107), updated, "用户实测")
        self.assertEqual(Brain(updated).classify_floor(.4806), "top")
        self.assertIsNone(Brain(updated).classify_floor(.6845))

    def test_applying_profile_twice_does_not_double_scale_any_value(self):
        apply_map_profile(self.path)
        once = load_config(self.path)
        apply_map_profile(self.path)
        self.assertEqual(load_config(self.path), once)

    def test_measured_profile_cannot_be_applied_to_another_roi(self):
        save_json(self.path, asdict(replace(self.config, minimap_roi=(4, 4, 140, 70))))
        original = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "仅适用于"):
            apply_map_profile(self.path)
        self.assertEqual(self.path.read_bytes(), original)


if __name__ == "__main__":
    unittest.main()
