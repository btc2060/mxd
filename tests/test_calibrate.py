import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import cv2

from lionbot.calibrate import calibrate, sample_choice, save_json
from lionbot.config import load_config
from tests.test_vision import fixture


class MenuTests(unittest.TestCase):
    def gui(self, key, click=None):
        stack = ExitStack()
        for name in ("namedWindow", "imshow", "destroyWindow"):
            stack.enter_context(patch(f"lionbot.calibrate.cv2.{name}"))
        stack.enter_context(patch("lionbot.calibrate.cv2.getWindowProperty", return_value=1))
        stack.enter_context(patch("lionbot.calibrate.cv2.waitKey", return_value=key))
        callback = stack.enter_context(patch("lionbot.calibrate.cv2.setMouseCallback"))
        if click is not None:
            callback.side_effect = lambda _title, handler: handler(cv2.EVENT_LBUTTONDOWN, *click, 0, None)
        stack.enter_context(redirect_stdout(io.StringIO()))
        return stack

    def test_menu_accepts_q_n_s_and_enter_in_image_window(self):
        for key, expected in ((ord("q"), "finish"), (ord("Q"), "finish"),
                              (13, "finish"), (10, "finish"),
                              (ord("n"), "new"), (ord("N"), "new"),
                              (ord("s"), "same")):
            with self.subTest(key=key), self.gui(key):
                self.assertEqual(sample_choice(4), expected)

    def test_menu_buttons_work_without_keyboard_input(self):
        for point, expected in (((50, 130), "finish"), ((280, 130), "new"), ((510, 130), "same")):
            with self.subTest(point=point), self.gui(255, click=point):
                self.assertEqual(sample_choice(4), expected)

    def test_escape_pauses_with_resume_instruction(self):
        with self.gui(27), self.assertRaisesRegex(ValueError, "--calibrate"):
            sample_choice(4)


class CalibrationPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.frame, self.seed = fixture(self.root)
        self.path = self.root / "config.local.json"
        self.patches = ExitStack()
        self.patches.enter_context(redirect_stdout(io.StringIO()))
        self.window = self.patches.enter_context(patch("lionbot.calibrate.GameWindow"))
        self.patches.enter_context(patch("lionbot.calibrate.fresh", return_value=self.frame))
        self.patches.enter_context(patch("lionbot.calibrate.cv2.destroyAllWindows"))
        self.roi = self.patches.enter_context(patch("lionbot.calibrate.select_roi"))
        self.point = self.patches.enter_context(patch("lionbot.calibrate.select_point"))
        self.menu = self.patches.enter_context(patch("lionbot.calibrate.sample_choice"))
        self.scale = self.patches.enter_context(patch("lionbot.calibrate.measure_scale"))
        self.scale.side_effect = lambda _window, config: replace(config, world_pixels_per_x=2000)
        self.roi.side_effect = [self.seed.minimap_roi, self.seed.combat_roi,
                                (280, 310, 64, 14), (414, 260, 42, 44)]
        self.point.side_effect = [(312, 300), (435, 300)]

    def tearDown(self):
        self.patches.close()
        self.temp.cleanup()

    def test_interrupt_after_one_monster_keeps_metadata_and_resume_skips_recropping(self):
        self.menu.side_effect = ValueError("校准已暂停")
        with self.assertRaisesRegex(ValueError, "暂停"):
            calibrate(self.path)
        partial = load_config(self.path)
        self.assertEqual(partial.minimap_roi, self.seed.minimap_roi)
        self.assertEqual(partial.player_feet_offset, (32, -10))
        manifest = self.root / partial.template_manifest
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["feet_offset"], [21, 40])
        image = manifest.parent / entries[0]["file"]
        original_bytes = image.read_bytes()
        self.assertIsNone(partial.world_pixels_per_x)
        self.scale.assert_not_called()

        self.roi.side_effect = AssertionError("恢复时不应重新框选已完成的区域")
        self.point.side_effect = AssertionError("恢复时不应重新点已保存的脚底")
        self.menu.side_effect = None
        self.menu.return_value = "finish"
        calibrate(self.path)
        complete = load_config(self.path)
        complete.require_calibration()
        self.assertEqual(complete.world_pixels_per_x, 2000)
        self.assertEqual(image.read_bytes(), original_bytes)
        self.assertEqual(complete.template_manifest, partial.template_manifest)
        self.assertEqual(self.window.return_value.close.call_count, 2)

    def test_new_screenshot_option_captures_again_and_persists_both_samples(self):
        self.roi.side_effect = [self.seed.minimap_roi, self.seed.combat_roi,
                                (280, 310, 64, 14), (414, 260, 42, 44), (414, 260, 42, 44)]
        self.point.side_effect = [(312, 300), (435, 300), (435, 300)]
        self.menu.side_effect = ["new", "finish"]
        with patch("lionbot.calibrate.fresh", return_value=self.frame) as capture:
            calibrate(self.path)
        self.assertEqual(capture.call_count, 2)
        manifest = self.root / load_config(self.path).template_manifest
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 2)
        self.assertNotEqual(entries[0]["file"], entries[1]["file"])
        self.assertTrue(all((manifest.parent / entry["file"]).exists() for entry in entries))

    def test_scale_interruption_keeps_samples_and_gives_specific_next_command(self):
        self.menu.return_value = "finish"
        self.scale.side_effect = KeyboardInterrupt
        with self.assertRaises(KeyboardInterrupt):
            calibrate(self.path)
        partial = load_config(self.path)
        self.assertTrue((self.root / partial.template_manifest).exists())
        with self.assertRaisesRegex(ValueError, "--measure-scale"):
            partial.require_calibration()

    def test_old_png_only_session_reuses_images_but_recollects_missing_offsets(self):
        legacy = self.root / "calibration" / "old-session"
        legacy.mkdir(parents=True)
        for old_name, new_name in (("player.png", "player.png"), ("lion.png", "lion-0.png")):
            (legacy / new_name).write_bytes((self.root / old_name).read_bytes())
        originals = {p.name: p.read_bytes() for p in legacy.iterdir()}
        # 旧版未保存小地图、区域及脚底元数据，重选两个区域；不再框角色和怪物图片。
        self.roi.side_effect = [self.seed.minimap_roi, self.seed.combat_roi]
        self.point.side_effect = [(312, 300), (435, 300)]
        self.menu.return_value = "finish"
        def choose_reuse(*_args):
            self.window.assert_called_once()  # DPI 初始化必须先于第一个菜单。
            return "reuse"

        with patch("lionbot.calibrate.choose", side_effect=choose_reuse):
            calibrate(self.path)
        complete = load_config(self.path)
        complete.require_calibration()
        self.assertEqual((self.root / complete.player_template).resolve(), legacy / "player.png")
        manifest = self.root / complete.template_manifest
        entries = json.loads(manifest.read_text(encoding="utf-8"))
        self.assertEqual(len(entries), 1)
        self.assertEqual((manifest.parent / entries[0]["file"]).resolve(), legacy / "lion-0.png")
        self.assertEqual(entries[0]["feet_offset"], [21, 40])
        self.assertEqual({p.name: p.read_bytes() for p in legacy.iterdir()}, originals)
        self.assertEqual(self.roi.call_count, 2)

    def test_interrupted_json_replace_preserves_previous_file(self):
        original = {"step": "already-saved"}
        target = self.root / "progress.json"
        save_json(target, original)
        with patch.object(Path, "replace", side_effect=OSError("write interrupted")):
            with self.assertRaises(OSError):
                save_json(target, {"step": "new"})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), original)
        self.assertEqual(list(self.root.glob("*.tmp")), [])


if __name__ == "__main__":
    unittest.main()
