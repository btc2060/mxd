import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

from lionbot.__main__ import main
from lionbot.calibrate import calibrate_height
from lionbot.config import Config, load_config
from tests.test_vision import fixture


class HeightCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.frame, self.config = fixture(self.root)
        self.path = self.root / "config.local.json"
        self.path.write_text(json.dumps(asdict(self.config)), encoding="utf-8")
        self.original = self.path.read_bytes()
        self.stack = ExitStack()
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        self.window = self.stack.enter_context(patch("lionbot.calibrate.GameWindow"))
        self.capture = self.stack.enter_context(patch("lionbot.calibrate.fresh", return_value=self.frame))
        self.points = self.stack.enter_context(patch("lionbot.calibrate.select_point"))
        self.stack.enter_context(patch("lionbot.calibrate.cv2.destroyAllWindows"))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def test_two_clicks_only_change_height_keep_assets_and_backup_previous_config(self):
        assets = {name: (self.root / name).read_bytes() for name in ("player.png", "lion.png", "monsters.json")}
        anchor = self.root / self.config.screen_anchor
        anchor.parent.mkdir(parents=True)
        anchor.write_bytes(b"unchanged ground reference")
        self.points.side_effect = [(312, 300), (315, 240)]
        calibrate_height(self.path)
        self.assertEqual(load_config(self.path), replace(self.config, player_height_px=60))
        self.capture.assert_called_once()
        self.assertEqual(self.points.call_count, 2)
        self.window.return_value.close.assert_called_once()
        self.assertEqual(anchor.read_bytes(), b"unchanged ground reference")
        for name, data in assets.items():
            self.assertEqual((self.root / name).read_bytes(), data)
        backup, = self.root.glob("config.local.json.*.bak")
        self.assertEqual(backup.read_bytes(), self.original)

    def test_canceling_second_point_preserves_configuration(self):
        self.points.side_effect = [(312, 300), ValueError("点选已取消")]
        with self.assertRaisesRegex(ValueError, "取消"):
            calibrate_height(self.path)
        self.assertEqual(self.path.read_bytes(), self.original)
        self.window.return_value.close.assert_called_once()

    def test_wrong_point_order_or_different_character_is_rejected_before_save(self):
        for feet, head in (((312, 300), (312, 320)), ((312, 300), (100, 240)),
                           ((312, 300), (312, 295)), ((312, 300), (312, -10))):
            with self.subTest(feet=feet, head=head):
                self.points.side_effect = [feet, head]
                with self.assertRaisesRegex(ValueError, "点位不正确"):
                    calibrate_height(self.path)
                self.assertEqual(self.path.read_bytes(), self.original)

    def test_resized_window_is_rejected_before_clicking(self):
        self.capture.return_value = self.frame[:400]
        with self.assertRaisesRegex(ValueError, "分辨率"):
            calibrate_height(self.path)
        self.points.assert_not_called()
        self.assertEqual(self.path.read_bytes(), self.original)

    def test_height_cli_does_not_repeat_full_template_calibration(self):
        with patch("lionbot.calibrate.calibrate_height") as function:
            self.assertEqual(main(["--config", str(self.path), "--calibrate-height"]), 0)
        function.assert_called_once_with(self.path)

    def test_old_feet_tolerance_does_not_override_new_height_default(self):
        raw = json.loads(self.original)
        raw.pop("player_height_px")
        raw["feet_tolerance_px"] = 22
        self.path.write_text(json.dumps(raw))
        self.assertEqual(load_config(self.path).player_height_px, 64)
        for height in (0, 11, 513, float("inf"), float("nan")):
            with self.subTest(height=height), self.assertRaises(ValueError):
                replace(Config(), player_height_px=height).validate()
