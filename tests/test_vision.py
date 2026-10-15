"""合成图只验证匹配、排重与坐标链路，不代表真实游戏识别准确率。"""
import json
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

from lionbot.brain import Brain, Mob
from lionbot.config import Config
from lionbot.vision import (Confirmer, Detection, Vision, detect_monsters,
                            make_template, measure_world_scale,
                            minimap_position, write_image)


def sprite(golden=False):
    patch = np.full((44, 42, 3), (190, 50, 10), dtype=np.uint8)
    outline = (20, 180, 230) if golden else (240, 130, 40)
    cv2.ellipse(patch, (21, 25), (17, 16), 0, 0, 360, outline, -1)
    cv2.ellipse(patch, (21, 25), (13, 12), 0, 0, 360, (240, 245, 248), -1)
    cv2.fillPoly(patch, [np.array([[7, 16], [5, 2], [18, 14]])], outline)
    cv2.fillPoly(patch, [np.array([[25, 13], [37, 3], [34, 21]])], outline)
    cv2.line(patch, (21, 15), (23, 5), (0, 140, 230), 3)
    for x in (15, 28):
        cv2.ellipse(patch, (x, 23), (5, 4), 0, 0, 360, (15, 20, 10), -1)
        cv2.circle(patch, (x, 23), 2, (50, 200, 100), -1)
    cv2.line(patch, (18, 33), (26, 32), (0, 0, 0), 2)
    return patch


def fixture(root):
    frame = np.full((480, 640, 3), (190, 50, 10), dtype=np.uint8)
    frame[4:74, 4:144] = (30, 30, 30)
    cv2.rectangle(frame, (44, 55), (48, 59), (0, 255, 255), -1)
    cv2.rectangle(frame, (100, 40), (104, 44), (0, 0, 255), -1)
    rng = np.random.default_rng(52)
    name = rng.integers(0, 255, (14, 64, 3), dtype=np.uint8)
    frame[310:324, 280:344] = name
    lion = sprite()
    frame[260:304, 414:456] = lion
    frame[260:304, 330:372] = sprite(golden=True)
    write_image(root / "player.png", name)
    write_image(root / "lion.png", lion)
    (root / "monsters.json").write_text(json.dumps([
        {"file": "lion.png", "feet_offset": [21, 40]}]), encoding="utf-8")
    config = replace(Config(), minimap_roi=(4, 4, 140, 70), combat_roi=(0, 90, 640, 300),
                     frame_size=(640, 480), player_template="player.png", player_feet_offset=(32, -10),
                     template_manifest="monsters.json", world_pixels_per_x=2000,
                     teleport_max_dx=.045)
    return frame, config


class VisionTests(unittest.TestCase):
    def test_yellow_point_and_red_other_player_are_distinct(self):
        frame = np.zeros((100, 160, 3), np.uint8)
        cv2.rectangle(frame, (28, 73), (32, 77), (0, 255, 255), -1)
        cv2.rectangle(frame, (80, 40), (84, 44), (0, 0, 255), -1)
        pos = minimap_position(frame, (0, 0, 150, 100))
        self.assertAlmostEqual(pos[0], .2)
        self.assertAlmostEqual(pos[1], .75)

    def test_ambiguous_yellow_points_do_not_return_stale_position(self):
        frame = np.zeros((100, 150, 3), np.uint8)
        for x in (30, 80):
            cv2.rectangle(frame, (x, 73), (x + 4, 77), (0, 255, 255), -1)
        self.assertIsNone(minimap_position(frame, (0, 0, 150, 100)))

    def test_multiple_poses_and_mirror_are_deduplicated(self):
        template = make_template(sprite(), (21, 40))
        frame = np.full((110, 180, 3), (190, 50, 10), np.uint8)
        frame[30:74, 60:102] = sprite()
        found = detect_monsters(frame, (0, 0, 180, 110), [template, template])
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].feet, (81, 70))

    def test_blue_sky_and_golden_pet_do_not_match_blue_sprite(self):
        frame = np.full((120, 220, 3), (190, 50, 10), np.uint8)
        frame[30:74, 60:102] = sprite(golden=True)
        found = detect_monsters(frame, (0, 0, 220, 120), [make_template(sprite(), (21, 40))])
        self.assertEqual(found, [])

    def test_minimap_sprite_is_not_a_combat_target(self):
        frame = sprite()
        found = detect_monsters(frame, (0, 0, 42, 44), [make_template(frame, (21, 40))],
                                excluded_roi=(0, 0, 42, 44))
        self.assertEqual(found, [])

    def test_confirmation_survives_short_gap_but_never_emits_missing_target(self):
        confirmer = Confirmer(2)
        detection = Detection((20, 20, 40, 40), (40, 60), .95)
        self.assertFalse(confirmer.update([detection], 0)[0].confirmed)
        self.assertTrue(confirmer.update([detection], .1)[0].confirmed)
        self.assertEqual(confirmer.update([], .2), [])
        self.assertTrue(confirmer.update([detection], .3)[0].confirmed)
        self.assertFalse(confirmer.update([detection], 1)[0].confirmed)

    def test_confirmation_compensates_camera_motion(self):
        confirmer = Confirmer(2)
        detection = Detection((100, 20, 40, 40), (120, 60), .95)
        confirmer.update([detection], 0)
        shifted = replace(detection, box=(40, 20, 40, 40), feet=(60, 60))
        self.assertTrue(confirmer.update([shifted], .1, shift=(-60, 0))[0].confirmed)

    def test_map_scale_compensates_camera_scroll(self):
        rng = np.random.default_rng(12)
        first = rng.integers(0, 255, (160, 320, 3), dtype=np.uint8)
        second = cv2.warpAffine(first, np.float32([[1, 0, -14], [0, 1, 0]]), (320, 160))
        scale, quality = measure_world_scale(first, second, (20, 10, 270, 140),
                                             (.3, .759), (.34, .759), (40, 120), (126, 120))
        self.assertAlmostEqual(scale, 2500, delta=50)
        self.assertGreater(quality, .25)

    def test_map_scale_rejects_insufficient_motion(self):
        frame = np.zeros((100, 200, 3), np.uint8)
        with self.assertRaises(ValueError):
            measure_world_scale(frame, frame, (0, 0, 200, 100), (.3, .75), (.301, .75), (0, 0), (1, 0))

    def test_real_pipeline_from_pixels_to_attack_and_missing_nameplate(self):
        with tempfile.TemporaryDirectory() as directory:
            frame, config = fixture(Path(directory))
            vision, brain = Vision(config, Path(directory)), Brain(config, start_alt=False)
            for now in (0, .1, .2):
                obs = vision.observe(frame)
                action = brain.decide(obs, now)
            self.assertEqual(obs.player_feet, (312, 300))
            self.assertEqual(len(obs.mobs), 1)
            self.assertTrue(obs.mobs[0].confirmed)
            # 已走到左侧内缩点后，向右巡逻时遇到前方目标。
            brain.direction = "right"
            action = brain.decide(obs, .2)
            self.assertEqual(action.kind, "face")
            brain.executed(action, .2, obs)
            action = brain.decide(vision.observe(frame), .4)
            self.assertEqual(action.kind, "attack")
            brain.executed(action, .4, obs)
            frame[310:324, 280:344] = (190, 50, 10)
            obs = vision.observe(frame)
            self.assertIsNone(obs.player_feet)
            self.assertEqual(brain.decide(obs, 1).kind, "wait")
            self.assertEqual(brain.decide(obs, 1.31).kind, "attack")
            self.assertTrue(vision.confirmer.previous[0].confirmed)

    def test_hurt_pixels_recovered_locally_without_extending_strong_match_lifetime(self):
        with tempfile.TemporaryDirectory() as directory:
            frame, config = fixture(Path(directory))
            vision = Vision(config, Path(directory))
            vision.observe(frame, now=0)
            self.assertTrue(vision.observe(frame, now=.1).mobs[0].confirmed)
            hurt = frame.copy()
            cv2.line(hurt, (414, 276), (455, 290), (255, 220, 180), 5)
            self.assertEqual(detect_monsters(hurt, config.combat_roi, vision.templates,
                                            excluded_roi=config.minimap_roi), [])
            recovered = vision.observe(hurt, now=.5)
            self.assertEqual(len(recovered.mobs), 1)
            self.assertTrue(recovered.mobs[0].recovered)
            self.assertTrue(recovered.mobs[0].confirmed)
            self.assertTrue(vision.observe(hurt, now=1.1).mobs[0].recovered)
            self.assertEqual(vision.observe(hurt, now=1.4).mobs, ())
            self.assertEqual(Vision(config, Path(directory)).observe(hurt, now=0).mobs, ())

    def test_recovery_does_not_substitute_golden_pet_for_missing_lion(self):
        with tempfile.TemporaryDirectory() as directory:
            frame, config = fixture(Path(directory))
            vision = Vision(config, Path(directory))
            vision.observe(frame, now=0)
            vision.observe(frame, now=.1)
            frame[260:304, 414:456] = sprite(golden=True)
            self.assertEqual(vision.observe(frame, now=.5).mobs, ())

    def test_camera_scroll_moves_local_hurt_search_with_world(self):
        with tempfile.TemporaryDirectory() as directory:
            frame, config = fixture(Path(directory))
            vision = Vision(config, Path(directory))
            vision.observe(frame, now=0)
            vision.observe(frame, now=.1)
            frame[260:304, 330:456] = (190, 50, 10)
            hurt = sprite()
            cv2.line(hurt, (0, 16), (41, 30), (255, 220, 180), 5)
            frame[260:304, 357:399] = hurt
            frame[55:60, 44:49] = (30, 30, 30)
            cv2.rectangle(frame, (48, 55), (52, 59), (0, 255, 255), -1)
            obs = vision.observe(frame, now=.3)
            self.assertEqual(len(obs.mobs), 1)
            self.assertEqual(obs.mobs[0].feet, (378, 300))
            self.assertTrue(obs.mobs[0].recovered)

    def test_partial_nameplate_requires_current_text_and_expires_from_full_match(self):
        with tempfile.TemporaryDirectory() as directory:
            frame, config = fixture(Path(directory))
            vision = Vision(config, Path(directory))
            self.assertEqual(vision.observe(frame, now=0).player_feet, (312, 300))
            name = frame[310:324, 280:344].copy()
            name[:, 42:] = (190, 50, 10)
            frame[310:324, 280:350] = (190, 50, 10)
            frame[310:324, 286:350] = name
            self.assertEqual(vision.observe(frame, now=.2).player_feet, (318, 300))
            obs = vision.observe(frame, now=.36)
            self.assertIsNone(obs.player_feet)
            self.assertTrue(obs.mobs[0].confirmed)  # 名牌丢失不再清空怪物识别。

    def test_resizing_requires_recalibration(self):
        with tempfile.TemporaryDirectory() as directory:
            frame, config = fixture(Path(directory))
            vision = Vision(config, Path(directory))
            with self.assertRaises(ValueError):
                vision.observe(frame[:400])

    def test_overlay_distinguishes_high_platform_from_targets_and_draws_height_band(self):
        with tempfile.TemporaryDirectory() as directory:
            frame, config = fixture(Path(directory))
            vision = Vision(config, Path(directory))
            vision.observe(frame)
            obs = vision.observe(frame)
            vision.detections += [Detection((450, 190, 40, 44), (470, 230), .99, True),
                                  Detection((540, 260, 40, 44), (561, 300), .99, True)]
            obs = replace(obs, mobs=tuple(Mob(d.feet, d.score, d.confirmed) for d in vision.detections))
            output = vision.draw(frame, obs, "test")
            self.assertEqual(tuple(output[260, 414]), (0, 255, 0))  # 同高、射程内。
            self.assertEqual(tuple(output[190, 450]), (160, 160, 160))  # 高台，不能攻击。
            self.assertEqual(tuple(output[260, 540]), (255, 160, 0))  # 同高，但太远。
            self.assertEqual(tuple(output[278, 312]), (255, 0, 255))  # 脚底 Y=300 上方 22px。

    def test_offline_cli_produces_detection_artifact_without_windows(self):
        from dataclasses import asdict

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, config = fixture(root)
            config_path = root / "config.json"
            config_path.write_text(json.dumps(asdict(config)), encoding="utf-8")
            write_image(root / "frame.png", frame)
            result = subprocess.run([sys.executable, "-m", "lionbot", "--config", str(config_path),
                                     "--image", str(root / "frame.png"), "--output", str(root / "out.png")],
                                    capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue((root / "out.png").exists())
            self.assertIn("候选怪物=1", result.stdout)


if __name__ == "__main__":
    unittest.main()
