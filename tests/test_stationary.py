import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import patch

import cv2
import numpy as np

from lionbot.__main__ import main
from lionbot.brain import Mob, Observation, target_status
from lionbot.config import Config, Floor, load_config
from lionbot.controls import InputExecutor
from lionbot.stationary import StationaryBrain
from lionbot.station_calibration import calibrate_stationary, record_station
from lionbot.vision import Vision, detect_monsters, make_template, write_image
from tests.test_screen_map import textured_fixture
from tests.test_controls import Clock, Keyboard
from tests.test_vision import fixture


def config():
    return replace(Config(), behavior="stationary", monster_name="食人花", auto_route=False,
                   minimap_roi=(0, 0, 137, 103), world_pixels_per_x=2000,
                   station=(.3, .5), bottom=Floor(.5, None, None), attack_range_px=172)


def obs(x=.3, y=.5, mobs=(), feet=(400, 500), focused=True):
    return Observation((x, y), feet, tuple(mobs), focused)


def prepared(start_alt=False):
    brain = StationaryBrain(config(), start_alt=start_alt)
    for now in (0, .1, .2):
        brain.decide(obs(), now)
    return brain


class StationaryTests(unittest.TestCase):
    def test_real_inputs_recover_after_knockback_and_clear_both_sides(self):
        class World(Clock, Keyboard):
            def __init__(self):
                Clock.__init__(self)
                Keyboard.__init__(self, self)
                self.x, self.facing = .3, "left"
                self.hp = {"left": 2, "right": 2}
                self.kicked = False

            def sleep(self, delay):
                self.x += .1 * delay * (int("right" in self.held) - int("left" in self.held))
                Clock.sleep(self, delay)

            def press(self, key):
                Keyboard.press(self, key)
                if key in ("left", "right"):
                    self.facing = key
                elif key == "d":
                    self.hp[self.facing] = max(0, self.hp[self.facing] - 1)
                    if not self.kicked:
                        self.x += .06
                        self.kicked = True

        world = World()
        brain = StationaryBrain(config(), start_alt=False)
        executor = InputExecutor(world, brain.c, clock=world.time, sleep=world.sleep)
        actions = []
        try:
            while world.now < 40:
                mobs = [Mob((400 + (x - world.x) * 2000, 500)) for side, x in (("left", .24), ("right", .36))
                        if world.hp[side] > 0]
                current = obs(x=round(world.x * 137) / 137, mobs=mobs)
                action = brain.decide(current, world.now)
                actions.append(action.kind)
                pressed = executor.execute(action)
                brain.executed(action, pressed, current)
                world.sleep(.03)
                if world.hp == {"left": 0, "right": 0} and brain.burst is None and not brain.clearing:
                    break
            else:
                self.fail(f"击退回位后无法继续：{brain.phase}, X={world.x}, HP={world.hp}, {actions[-20:]}")
        finally:
            executor.stop()
        self.assertIn("walk", actions)
        self.assertIn("nudge", actions)
        self.assertLess(abs(world.x - .3), .012)
        self.assertFalse(world.held)
        self.assertFalse({"f", "space", "up", "down"} & {event[1] for event in world.events})

    def test_three_attacks_continue_across_hurt_misses_then_wait_for_respawn(self):
        brain = prepared()
        brain.facing = "right"
        for shot, now in enumerate((1, 1.901, 2.802), 1):
            current = obs(mobs=[Mob((550, 500))]) if shot == 1 else obs()
            action = brain.decide(current, now)
            self.assertEqual((action.kind, action.direction), ("attack", "right"))
            self.assertTrue(action.reason.endswith(f"{shot}/3"))
            brain.executed(action, now, current)
            self.assertEqual(brain.decide(current, now + .899).kind, "wait")
        for now in (3.703, 4.003, 4.303, 4.603, 10, 100):
            self.assertEqual(brain.decide(obs(), now).kind, "wait")
        self.assertEqual(brain.route, "")
        self.assertIsNone(brain.burst)

    def test_both_sides_get_three_shot_groups_without_patrolling(self):
        brain = prepared()
        brain.facing = "right"
        current = obs(mobs=[Mob((550, 500)), Mob((250, 500))])
        for now in (1, 2, 3):
            action = brain.decide(current, now)
            self.assertEqual((action.kind, action.direction), ("attack", "right"))
            brain.executed(action, now, current)
        face = brain.decide(current, 4)
        self.assertEqual((face.kind, face.direction), ("face", "left"))
        brain.executed(face, 4, current)
        for now in (4.13, 5.13, 6.13):
            action = brain.decide(current, now)
            self.assertEqual((action.kind, action.direction), ("attack", "left"))
            brain.executed(action, now, current)
        self.assertEqual((brain.completed_round_trips, brain.route), (0, ""))

    def test_knockback_finishes_cast_then_walks_back_and_confirms_home(self):
        brain = prepared()
        brain.facing = "right"
        target = obs(mobs=[Mob((550, 500))])
        brain.executed(brain.decide(target, 1), 1, target)
        kicked = obs(x=.42, feet=None)
        self.assertEqual(brain.decide(kicked, 1.2).kind, "wait")
        action = brain.decide(kicked, 2)
        self.assertEqual((action.kind, action.direction), ("walk", "left"))
        self.assertIsNone(brain.burst)
        brain.executed(action, 2, kicked)
        near = obs(x=.315, feet=None)
        nudge = brain.decide(near, 2.1)
        self.assertEqual((nudge.kind, nudge.direction), ("nudge", "left"))
        brain.executed(nudge, 2.1, near)
        for now in (2.2, 2.3):
            self.assertEqual(brain.decide(target, now).kind, "wait")
        face = brain.decide(target, 2.4)
        self.assertEqual((face.kind, face.direction), ("face", "right"))
        self.assertEqual(brain.burst.shots, 0)

    def test_wrong_platform_never_sends_walk_or_attack(self):
        brain = prepared()
        wrong = obs(x=.6, y=.3, mobs=[Mob((450, 500))])
        for now in (1, 2, 10):
            self.assertEqual(brain.decide(wrong, now).kind, "wait")
        self.assertEqual(brain.home_frames, 0)

    def test_no_screen_location_blocks_attacks_but_not_minimap_return(self):
        brain = prepared()
        for now in (1, 2, 10):
            self.assertEqual(brain.decide(obs(feet=None, mobs=[Mob((450, 500))]), now).kind, "wait")
        self.assertEqual(brain.decide(obs(x=.5, feet=None), 11).kind, "walk")

    def test_recovery_timeout_stops_input_until_manual_return_or_pause_reset(self):
        brain = prepared()
        self.assertEqual(brain.decide(obs(x=.5), 1).kind, "walk")
        self.assertEqual(brain.decide(obs(x=.5), 7).kind, "wait")
        self.assertTrue(brain.return_failed)
        brain.reset_observation()
        self.assertEqual(brain.decide(obs(x=.5), 8).kind, "walk")
        for now in (9, 9.1, 9.2):
            brain.decide(obs(), now)
        self.assertFalse(brain.return_failed)

    def test_alt_starts_once_then_190_seconds_without_repeating_after_pause(self):
        brain = StationaryBrain(config())
        for now in (0, .1):
            self.assertEqual(brain.decide(obs(), now).kind, "wait")
        action = brain.decide(obs(), .2)
        self.assertEqual(action.kind, "alt")
        brain.executed(action, .2, obs())
        brain.reset_observation()
        for now in (1, 1.1, 1.2, 190.199):
            self.assertNotEqual(brain.decide(obs(), now).kind, "alt")
        self.assertEqual(brain.decide(obs(), 190.21).kind, "alt")

    def test_yellow_loss_cancels_burst_and_requires_home_reconfirmation(self):
        brain = prepared()
        brain.facing = "right"
        brain.decide(obs(mobs=[Mob((550, 500))]), 1)
        brain.decide(Observation(None, None), 2)
        self.assertIsNone(brain.burst)
        for now in (3, 3.1):
            self.assertEqual(brain.decide(obs(mobs=[Mob((550, 500))]), now).kind, "wait")

    def test_pixel_range_is_independent_of_new_map_scale_and_old_normalized_range(self):
        c = replace(config(), world_pixels_per_x=9000, attack_range=.9)
        self.assertEqual(target_status(Mob((572, 500)), obs(), c), "ready")
        self.assertEqual(target_status(Mob((573, 500)), obs(), c), "range")


def plant_sprite():
    image = np.full((60, 48, 3), (20, 50, 30), np.uint8)
    cv2.line(image, (24, 58), (24, 20), (30, 180, 40), 6)
    cv2.ellipse(image, (24, 25), (21, 20), 0, 0, 360, (60, 30, 220), -1)
    cv2.ellipse(image, (24, 29), (17, 10), 0, 0, 360, (10, 10, 10), -1)
    for x in (13, 23, 33):
        cv2.fillPoly(image, [np.array([[x - 3, 22], [x + 3, 22], [x, 30]])], (180, 230, 240))
    cv2.circle(image, (15, 14), 3, (0, 180, 255), -1)
    cv2.circle(image, (33, 14), 3, (0, 180, 255), -1)
    return image


class PlantVisionTests(unittest.TestCase):
    def test_nonblue_plant_template_matches_only_with_plant_color_mode(self):
        sprite = plant_sprite()
        frame = np.full((160, 240, 3), (20, 50, 30), np.uint8)
        frame[50:110, 100:148] = sprite
        templates = [make_template(sprite, (24, 58))]
        self.assertEqual(detect_monsters(frame, (0, 0, 240, 160), templates), [])
        found = detect_monsters(frame, (0, 0, 240, 160), templates, blue_lion_colors=False)
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0].feet, (124, 108))

    def test_live_vision_uses_plant_profile_and_confirms_two_frames(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, c = fixture(root)
            frame[240:300, 414:462] = plant_sprite()
            write_image(root / "plant.png", plant_sprite())
            (root / "plants.json").write_text(json.dumps([{"file": "plant.png", "feet_offset": [24, 58]}]))
            c = replace(c, behavior="stationary", auto_route=False, station=(.3, .5),
                        template_manifest="plants.json", attack_range_px=172)
            vision = Vision(c, root)
            first = vision.observe(frame, now=1)
            self.assertEqual(len(first.mobs), 1)
            self.assertFalse(first.mobs[0].confirmed)
            second = vision.observe(frame, now=1.1)
            self.assertTrue(second.mobs[0].confirmed)
            self.assertEqual(target_status(second.mobs[0], second, c), "ready")

    def test_offline_cli_loads_independent_plant_config_and_writes_detections(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            frame, c = fixture(root)
            frame[240:300, 414:462] = plant_sprite()
            write_image(root / "plant.png", plant_sprite())
            (root / "plants.json").write_text(json.dumps([{"file": "plant.png", "feet_offset": [24, 58]}]))
            c = replace(c, behavior="stationary", auto_route=False, station=(.3, .5),
                        template_manifest="plants.json", attack_range_px=172)
            source, output, path = root / "frame.png", root / "result.png", root / "config.plants.local.json"
            write_image(source, frame)
            path.write_text(json.dumps(asdict(c)), encoding="utf-8")
            log = io.StringIO()
            with redirect_stdout(log):
                self.assertEqual(main(["--config", str(path), "--image", str(source), "--output", str(output)]), 0)
            self.assertTrue(output.exists())
            self.assertIn("候选怪物=1", log.getvalue())


class StationCalibrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.frame, self.old = textured_fixture(self.root)
        self.old_path = self.root / "config.local.json"
        self.old_path.write_text(json.dumps(asdict(replace(self.old, attack_burst_shots=6))), encoding="utf-8")
        self.before = {p: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        self.path = self.root / "config.plants.local.json"
        self.stack = ExitStack()
        self.stack.enter_context(redirect_stdout(io.StringIO()))
        for module in ("lionbot.calibrate", "lionbot.station_calibration"):
            window = self.stack.enter_context(patch(module + ".GameWindow"))
            window.return_value.capture.return_value = self.frame
            window.return_value.focused.return_value = True
            self.stack.enter_context(patch(module + ".fresh", return_value=self.frame))
        self.stack.enter_context(patch("lionbot.station_calibration.time.sleep"))
        self.stack.enter_context(patch("lionbot.calibrate.cv2.destroyAllWindows"))
        roi = self.stack.enter_context(patch("lionbot.calibrate.select_roi"))
        roi.side_effect = [self.old.minimap_roi, self.old.combat_roi, (280, 310, 64, 14), (414, 260, 42, 44)]
        self.stack.enter_context(patch("lionbot.calibrate.select_point", side_effect=[(312, 300), (435, 300)]))
        self.menu = self.stack.enter_context(patch("lionbot.calibrate.sample_choice", return_value="finish"))
        self.stack.enter_context(patch("lionbot.calibrate.measure_scale", side_effect=lambda _w, c: replace(c, world_pixels_per_x=3000)))
        self.points = self.stack.enter_context(patch("lionbot.station_calibration.select_point", side_effect=[(312, 300), (312, 244)]))

    def tearDown(self):
        self.stack.close()
        self.temp.cleanup()

    def assert_old_untouched(self):
        for path, content in self.before.items():
            self.assertEqual(path.read_bytes(), content)

    def test_full_setup_records_new_map_and_reuses_only_pixel_range(self):
        calibrate_stationary(self.path)
        c = load_config(self.path)
        c.require_calibration()
        self.assertEqual((c.behavior, c.monster_name, c.attack_burst_shots), ("stationary", "食人花", 3))
        self.assertAlmostEqual(c.station[0], .3)
        self.assertAlmostEqual(c.station[1], 53 / 70)
        self.assertAlmostEqual(c.bottom.y, c.station[1])
        self.assertEqual(c.attack_range_px, self.old.attack_reach_px)
        self.assertEqual(c.world_pixels_per_x, 3000)
        self.assertEqual(c.player_height_px, 56)
        self.assertFalse(c.auto_route)
        self.assertIsNone(c.teleport_max_dx)
        self.assertTrue((self.root / c.screen_anchor).exists())
        entries = json.loads((self.root / c.template_manifest).read_text())
        self.assertTrue(entries[0]["file"].startswith("plant-"))
        self.assertEqual(self.points.call_count, 2)
        self.assert_old_untouched()

    def test_different_window_requires_manual_pixel_range_instead_of_import(self):
        self.old_path.write_text(json.dumps(asdict(replace(self.old, frame_size=(800, 600)))), encoding="utf-8")
        self.points.side_effect = [(312, 300), (312, 244), (500, 300)]
        calibrate_stationary(self.path)
        self.assertEqual(load_config(self.path).attack_range_px, 188)
        self.assertEqual(self.points.call_count, 3)

    def test_old_profile_path_is_rejected_before_any_capture_or_write(self):
        with self.assertRaisesRegex(ValueError, "不能覆盖"):
            calibrate_stationary(self.old_path)
        self.points.assert_not_called()
        self.assert_old_untouched()

    def test_interrupted_template_sampling_keeps_new_assets_and_old_profile(self):
        self.menu.side_effect = ValueError("采集暂停")
        with self.assertRaisesRegex(ValueError, "暂停"):
            calibrate_stationary(self.path)
        partial = load_config(self.path)
        self.assertIsNone(partial.station)
        self.assertTrue((self.root / partial.template_manifest).exists())
        self.assert_old_untouched()

    def test_knockback_during_station_recording_preserves_previous_station_and_templates(self):
        calibrate_stationary(self.path)
        c = load_config(self.path)
        before = {p: p.read_bytes() for p in (self.path, self.root / c.template_manifest,
                                             self.root / c.screen_anchor)}
        with patch("lionbot.station_calibration.get_position", side_effect=[(.3, .5), (.35, .5), (.35, .5), (.35, .5), (.35, .5)]):
            with self.assertRaisesRegex(ValueError, "击退"):
                record_station(self.path)
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assert_old_untouched()

    def test_cli_uses_independent_default_file_for_both_station_commands(self):
        for command, name in (("--calibrate-stationary", "calibrate_stationary"),
                              ("--record-station", "record_station")):
            with patch("lionbot.station_calibration." + name) as function:
                self.assertEqual(main([command]), 0)
            self.assertEqual(function.call_args.args[0].name, "config.plants.local.json")
