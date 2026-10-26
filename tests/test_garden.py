"""暗影花园的底层巡逻、D/F 按键、紫红花识别与独立校准。"""
import io
import json
import tempfile
import time
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch

import cv2
import numpy as np

from lionbot.brain import Action, Mob, Observation, target_status
from lionbot.config import Config
from lionbot.controls import InputExecutor
from lionbot.garden import main, run_garden
from lionbot.garden_calibration import inherit_pixels, record_route, measure_teleport, sample_plant, setup_garden, select_area, record_anchor
from lionbot.garden_cycle import GardenConfig, GardenCycle, load_garden, save_garden
from lionbot.garden_vision import GardenReferenceError, GardenVision, combat_area_problem, save_garden_anchor
from lionbot.lie_alert import AlertState
from lionbot.plant_vision import PlantColorDetector, plant_masks
from lionbot.vision import make_template, write_image, detect_monsters
from tests.test_controls import Clock, Keyboard
from tests.test_plant_vision import brown_plant, SKY


def config(**changes):
    c = GardenConfig(frame_size=(640, 480), minimap_roi=(4, 4, 140, 70), combat_roi=(0, 90, 640, 300),
                     left_x=.1, right_x=.9, bottom_path=((.1, .75), (.9, .75)), world_pixels_per_x=2000,
                     attack_range_px=172, teleport_max_dx=.08)
    return replace(c, **changes)


def observation(x=.5, y=.75, mobs=(), feet=(312., 300.), focused=True):
    return Observation((x, y), feet, tuple(mobs), focused)


def plant(left=False, hurt=False):
    image = brown_plant(hurt=hurt)
    mask, _ = plant_masks(image)
    image[mask > 0] = (180, 20, 235)
    return cv2.flip(image, 1) if left else image


def scene(root, **changes):
    c = config(**changes)
    frame = np.full((480, 640, 3), SKY, np.uint8)
    # 地形纹理与角色/花分离，提供分布在画面内的固定特征。
    rng = np.random.default_rng(52)
    frame[90:390] = rng.integers(0, 200, (300, 640, 3), dtype=np.uint8)
    frame[235:325, 150:570] = SKY
    frame[4:74, 4:144] = 30
    cv2.rectangle(frame, (44, 55), (48, 59), (0, 255, 255), -1)
    name = rng.integers(0, 255, (14, 64, 3), dtype=np.uint8)
    frame[310:324, 280:344] = name
    write_image(root / "name.png", name)
    c = replace(c, player_template="name.png", player_feet_offset=(32, -10))
    manifest = root / c.template_manifest
    write_image(manifest.parent / "plant.png", plant())
    manifest.write_text(json.dumps([{"file": "plant.png", "feet_offset": [14, 46]}]), encoding="utf-8")
    save_garden_anchor(c, root, frame, (.3, 53 / 70), (312., 300.))
    return frame, c


class GardenCycleTests(unittest.TestCase):
    def test_round_trips_keep_continuous_direction_and_turn_at_both_edges(self):
        brain = GardenCycle(config(teleport_enabled=False), start_alt=False)
        directions = []
        for index, x in enumerate((.1, .3, .7, .9, .7, .4, .1, .4, .9, .1)):
            obs = observation(x)
            action = brain.decide(obs, index)
            directions.append(action.direction)
            brain.executed(action, index, obs)
        self.assertEqual(directions, ["right", "right", "right", "left", "left", "left", "right", "right", "left", "right"])
        self.assertEqual(brain.completed_round_trips, 2)

    def test_nearby_both_sides_are_cleared_with_d_only_then_original_route_resumes(self):
        c = config(teleport_enabled=False, clear_seconds=.3)
        brain = GardenCycle(c, start_alt=False)
        left, right = Mob((250, 300)), Mob((390, 300))
        now, actions, last_attack = 0., [], -100.
        for mobs in ((right, left),) * 12 + ((),) * 7:
            obs = observation(mobs=mobs)
            action = brain.decide(obs, now)
            actions.append(action)
            if action.kind == "attack":
                self.assertGreaterEqual(now - last_attack, .9 - 1e-9)
                last_attack = now
            brain.executed(action, now, obs)
            now += .91
        attacks = [a for a in actions if a.kind == "attack"]
        self.assertTrue(attacks)
        self.assertIn("left", [a.direction for a in attacks])
        self.assertIn("right", [a.direction for a in attacks])
        self.assertEqual(actions[-1].kind, "walk")
        self.assertEqual(actions[-1].direction, "right")
        self.assertTrue(all(a.kind not in ("attack_a", "grab", "step_jump", "climb", "top_teleport") for a in actions))

    def test_confirmed_burst_finishes_through_temporary_missing_animation(self):
        brain = GardenCycle(config(teleport_enabled=False), start_alt=False)
        brain.facing = "right"
        first = observation(mobs=[Mob((390, 300))])
        action = brain.decide(first, 0)
        self.assertEqual(action.kind, "attack")
        brain.executed(action, 0, first)
        for now in (.91, 1.82):
            obs = observation()
            action = brain.decide(obs, now)
            self.assertEqual(action.kind, "attack")
            brain.executed(action, now, obs)
        self.assertFalse(brain.burst_direction)
        self.assertEqual(brain.decide(observation(), 2.73).kind, "wait")
        self.assertEqual(brain.decide(observation(), 2.84).kind, "wait")
        self.assertEqual(brain.decide(observation(), 3.25).kind, "walk")

    def test_knockback_across_target_rechecks_side_before_attacking(self):
        brain = GardenCycle(config(), start_alt=False)
        first = observation(mobs=[Mob((350, 300))])
        brain.facing = "right"
        action = brain.decide(first, 0)
        brain.executed(action, 0, first)
        # 已超过原目标的世界 X，不能对着空方向完成旧组。
        action = brain.decide(observation(x=.55), 1)
        self.assertNotEqual(action.kind, "attack")

    def test_higher_or_lower_floor_cannot_walk_teleport_or_attack(self):
        brain = GardenCycle(config())
        for y in (.5, .9):
            action = brain.decide(observation(y=y, mobs=[Mob((390, 300))]), 0)
            self.assertEqual(action.kind, "wait")
            self.assertIn("不在已采集底层", action.reason)

    def test_minimap_loss_or_focus_loss_release_inputs_without_resetting_alt_clock(self):
        brain = GardenCycle(config())
        obs = observation()
        alt = brain.decide(obs, 0)
        brain.executed(alt, 0, obs)
        self.assertEqual(brain.decide(Observation(None, None), 1).kind, "wait")
        self.assertEqual(brain.decide(observation(focused=False), 1).kind, "wait")
        self.assertEqual(brain.next_alt, 190)
        self.assertNotEqual(brain.decide(obs, 189).kind, "alt")
        self.assertEqual(brain.decide(obs, 190).kind, "alt")

    def test_no_nameplate_or_screen_location_still_navigates_by_yellow_point_without_f(self):
        brain = GardenCycle(config(), start_alt=False)
        action = brain.decide(observation(feet=None), 0)
        self.assertEqual((action.kind, action.direction), ("walk", "right"))

    def test_f_in_middle_but_walk_near_edge_or_when_mob_pending(self):
        brain = GardenCycle(config(), start_alt=False)
        self.assertEqual(brain.decide(observation(), 0).kind, "teleport")
        self.assertEqual(brain.decide(observation(x=.85), 0).kind, "walk")
        pending = observation(mobs=[Mob((390, 300), confirmed=False)])
        self.assertEqual(brain.decide(pending, 0).kind, "wait")
        self.assertEqual(brain.decide(pending, .5).kind, "walk")

    def test_f_cooldown_settle_and_bad_distance_falls_back_to_walk(self):
        brain = GardenCycle(config(), start_alt=False)
        obs = observation()
        action = brain.decide(obs, 0)
        brain.executed(action, 0, obs)
        self.assertEqual(brain.decide(observation(x=.57), .1).kind, "wait")
        self.assertEqual(brain.decide(observation(x=.57), .31).kind, "walk")
        self.assertEqual(brain.decide(observation(x=.57), 1.51).kind, "teleport")
        brain.executed(Action("teleport", "right"), 2, observation(x=.57))
        action = brain.decide(observation(x=.73), 2.31)
        self.assertTrue(brain.teleport_fault)
        self.assertEqual(action.kind, "walk")

    def test_unmeasured_disabled_or_too_long_f_uses_walk(self):
        for changes in ({"teleport_max_dx": None}, {"teleport_enabled": False}, {"teleport_max_dx": .2}):
            brain = GardenCycle(config(**changes), start_alt=False)
            self.assertEqual(brain.decide(observation(), 0).kind, "walk")

    def test_sustained_walking_stuck_requires_pause_reset_not_combat_timeout(self):
        brain = GardenCycle(config(teleport_enabled=False), start_alt=False)
        obs = observation()
        brain.executed(brain.decide(obs, 0), 0, obs)
        self.assertEqual(brain.decide(obs, 6.1).kind, "wait")
        self.assertTrue(brain.blocked)
        brain.suspend()
        self.assertEqual(brain.decide(obs, 7).kind, "walk")

    def test_real_key_sequences_are_d_f_arrows_and_alt_without_a(self):
        c, clock = config(), Clock()
        brain = GardenCycle(c)
        keyboard = Keyboard(clock)
        executor = InputExecutor(keyboard, c.vision_config(), clock=clock.time, sleep=clock.sleep)
        first = observation()
        for action in (brain.decide(first, 0), Action("face", "right"), Action("attack", "right"), Action("teleport", "left")):
            brain.executed(action, executor.execute(action), first)
        keys = [e[1] for e in keyboard.events if e[0] == "press"]
        self.assertEqual(keys, ["alt", "right", "d", "left", "f"])
        self.assertFalse(keyboard.held)

    def test_long_patrol_simulation_kills_stationary_plants_and_completes_round_trips(self):
        c = config()
        brain = GardenCycle(c)
        x, now = .12, 0.
        health = {position: 3 for position in (.15, .25, .35, .45, .55, .65, .75, .85)}
        kinds, alt_times, teleports = [], [], []
        for _ in range(2400):
            mobs = [Mob((312 + (position - x) * c.world_pixels_per_x, 300), box=(0, 260, 40, 40))
                    for position, hp in health.items() if hp and abs(position - x) * c.world_pixels_per_x <= c.attack_range_px]
            obs = observation(x=x, mobs=mobs)
            action = brain.decide(obs, now)
            kinds.append(action.kind)
            brain.executed(action, now, obs)
            sign = 1 if action.direction == "right" else -1
            if action.kind == "attack":
                targets = [p for p, hp in health.items() if hp and (p - x) * sign >= 0
                           and abs(p - x) * c.world_pixels_per_x <= c.attack_range_px]
                if targets:
                    health[min(targets, key=lambda p: abs(p - x))] -= 1
            elif action.kind == "walk":
                x += sign * .006
            elif action.kind == "face":
                x += sign * .003
            elif action.kind == "teleport":
                before = x
                x += sign * .06
                teleports.append((before, x))
            elif action.kind == "alt":
                alt_times.append(now)
            self.assertGreaterEqual(x, c.left_x - .008)
            self.assertLessEqual(x, c.right_x + .008)
            now += .1
        self.assertTrue(all(hp == 0 for hp in health.values()), health)
        self.assertGreaterEqual(brain.completed_round_trips, 2)
        self.assertTrue(teleports)
        self.assertTrue(all(c.left_x < after < c.right_x for _, after in teleports))
        self.assertEqual(len(alt_times), 2)
        self.assertGreaterEqual(alt_times[1] - alt_times[0], c.alt_interval)
        self.assertTrue(set(kinds) <= {"wait", "alt", "walk", "face", "attack", "teleport"})


class GardenTestCase(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "config.garden.local.json"

    def tearDown(self):
        self.temp.cleanup()


class GardenFilesTests(GardenTestCase):
    def large_frame(self, textured=True):
        image = np.full((768, 1366, 3), SKY, np.uint8)
        if textured:
            rng = np.random.default_rng(135)
            image[170:680] = rng.integers(0, 200, (510, 1366, 3), dtype=np.uint8)
        image[73:158, 24:181] = 30
        cv2.rectangle(image, (101, 135), (105, 139), (0, 255, 255), -1)
        return image

    def large_config(self, bad=False):
        return config(frame_size=(1366, 768), minimap_roi=(24, 73, 157, 85),
                      combat_roi=(23, 72, 157, 86) if bad else (0, 0, 1366, 680))

    def test_reported_nearly_identical_rois_are_rejected_before_feature_extraction(self):
        c, frame = self.large_config(bad=True), self.large_frame()
        self.assertIn("小地图大面积重合", combat_area_problem(c, frame))
        with patch("lionbot.garden_vision.save_screen_anchor") as seed, self.assertRaises(GardenReferenceError) as error:
            save_garden_anchor(c, self.root, frame, (.5, .75), (680, 600))
        seed.assert_not_called()
        self.assertIn("--select-area", str(error.exception))
        diagnostics = list((self.root / "debug-output" / "garden").glob("*.json"))
        self.assertEqual(len(diagnostics), 1)
        data = json.loads(diagnostics[0].read_text(encoding="utf-8"))
        self.assertEqual(data["combat_roi"], [23, 72, 157, 86])
        self.assertEqual(data["minimap_roi"], [24, 73, 157, 85])

    def test_setup_repairs_reported_roi_and_continues_without_remeasuring_other_steps(self):
        c, frame = self.large_config(bad=True), self.large_frame()
        save_garden(self.path, c)
        manifest = self.root / c.template_manifest
        manifest.parent.mkdir(parents=True, exist_ok=True)
        write_image(manifest.parent / "plant.png", plant())
        manifest.write_text(json.dumps([{"file": "plant.png", "feet_offset": [14, 46]}]))
        before = manifest.read_bytes()
        with patch("lionbot.garden_calibration.GameWindow"), \
                patch("lionbot.garden_calibration.fresh", return_value=frame) as capture, \
                patch("lionbot.garden_calibration.select_roi", return_value=(0, 0, 1366, 680)) as roi, \
                patch("lionbot.garden_calibration.select_point", return_value=(680, 600)) as feet, \
                patch("lionbot.garden_calibration.measure_world_scale") as scale, \
                patch("lionbot.garden_calibration.sample_plant") as sample, \
                patch("lionbot.garden_calibration.record_route") as route, \
                patch("lionbot.garden_calibration.measure_teleport") as teleport, \
                patch("lionbot.garden_calibration.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
            setup_garden(self.path)
        saved = load_garden(self.path)
        self.assertEqual(saved, replace(c, combat_roi=(0, 0, 1366, 680)))
        self.assertEqual(capture.call_count, 2)
        roi.assert_called_once()
        feet.assert_called_once()
        scale.assert_not_called()
        sample.assert_not_called()
        route.assert_not_called()
        teleport.assert_not_called()
        self.assertEqual(manifest.read_bytes(), before)
        self.assertTrue(GardenVision(saved, self.root).mapper.references)

    def test_select_area_cli_preserves_minimap_route_range_and_samples(self):
        c, frame = self.large_config(bad=True), self.large_frame()
        save_garden(self.path, c)
        with patch("lionbot.garden_calibration.GameWindow"), \
                patch("lionbot.garden_calibration.fresh", return_value=frame), \
                patch("lionbot.garden_calibration.select_roi", return_value=(0, 0, 1366, 680)), \
                patch("lionbot.garden_calibration.select_point", return_value=(680, 600)), \
                patch("lionbot.windows.Keyboard") as keyboard, \
                patch("lionbot.garden_calibration.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "--select-area"]), 0)
        self.assertEqual(load_garden(self.path), replace(c, combat_roi=(0, 0, 1366, 680)))
        keyboard.assert_not_called()

    def test_bad_new_area_preserves_existing_configuration_and_anchor(self):
        frame, c = scene(self.root)
        save_garden(self.path, c)
        before, anchor_before = self.path.read_bytes(), (self.root / c.screen_anchor).read_bytes()
        with patch("lionbot.garden_calibration.GameWindow"), \
                patch("lionbot.garden_calibration.fresh", return_value=frame), \
                patch("lionbot.garden_calibration.select_roi", return_value=c.minimap_roi), \
                patch("lionbot.garden_calibration.select_point") as feet, \
                patch("lionbot.garden_calibration.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
            select_area(self.path)
        feet.assert_not_called()
        self.assertEqual(self.path.read_bytes(), before)
        self.assertEqual((self.root / c.screen_anchor).read_bytes(), anchor_before)

    def test_genuinely_featureless_frame_keeps_diagnostics_and_garden_recovery_command(self):
        c, frame = self.large_config(), self.large_frame(textured=False)
        with self.assertRaises(GardenReferenceError) as error:
            save_garden_anchor(c, self.root, frame, (.5, .75), (680, 600))
        self.assertIn("特征数=0/12", str(error.exception))
        self.assertNotIn("--calibrate-anchor", str(error.exception))
        data = json.loads(next((self.root / "debug-output" / "garden").glob("*.json")).read_text(encoding="utf-8"))
        self.assertGreater(data["search_pixels"], 100000)
        self.assertEqual(data["feature_count"], 0)

    def test_anchor_failure_can_reselect_area_on_same_frame_instead_of_repeating_capture(self):
        c, frame = self.large_config(), self.large_frame()
        frame[:350, :650] = SKY
        frame[73:158, 24:181] = 30
        cv2.rectangle(frame, (101, 135), (105, 139), (0, 255, 255), -1)
        c = replace(c, combat_roi=(0, 0, 600, 300))
        save_garden(self.path, c)
        with patch("lionbot.garden_calibration.GameWindow"), \
                patch("lionbot.garden_calibration.fresh", return_value=frame) as capture, \
                patch("lionbot.garden_calibration.select_point", return_value=(200, 220)), \
                patch("lionbot.garden_calibration.select_roi", return_value=(0, 0, 1366, 680)), \
                patch("lionbot.garden_calibration.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
            record_anchor(self.path)
        capture.assert_called_once()
        self.assertEqual(load_garden(self.path).combat_roi, (0, 0, 1366, 680))
        self.assertTrue((self.root / c.screen_anchor).exists())

    def test_config_roundtrip_and_incomplete_config_block_run(self):
        save_garden(self.path, config())
        self.assertEqual(load_garden(self.path), config())
        config().validate(ready=True)
        with self.assertRaisesRegex(ValueError, "--setup"):
            GardenConfig().validate(ready=True)
        for changes in ({"left_x": .95}, {"attack_burst_shots": True}, {"attack_interval": .1},
                        {"world_pixels_per_x": float("nan")}, {"minimap_roi": (0, 0, 900, 10)},
                        {"bottom_path": ((.9, .75), (.1, .75))}, {"teleport_cooldown": .1},
                        {"lie_alert_enabled": "true"}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config(**changes).validate()

    def test_different_config_paths_get_separate_map_assets_but_share_alert_template(self):
        first = load_garden(self.path)
        second = load_garden(self.root / "config.garden.other.local.json")
        self.assertNotEqual(first.template_manifest, second.template_manifest)
        self.assertNotEqual(first.screen_anchor, second.screen_anchor)
        self.assertEqual(first.lie_alert_template, second.lie_alert_template)

    def test_only_pixel_range_is_reused_and_old_files_are_never_modified(self):
        old = {"frame_size": [640, 480], "attack_range_px": 210, "station": [.2, .4],
               "world_pixels_per_x": 9000, "minimap_roi": [0, 0, 100, 100]}
        source = self.root / "config.plants.local.json"
        source.write_text(json.dumps(old))
        original = source.read_bytes()
        with redirect_stdout(io.StringIO()):
            copied = inherit_pixels(config(attack_range_px=None), self.root)
        self.assertEqual(copied.attack_range_px, 210)
        self.assertEqual(copied.world_pixels_per_x, 2000)
        self.assertEqual(copied.minimap_roi, (4, 4, 140, 70))
        self.assertEqual(source.read_bytes(), original)
        self.assertIsNone(inherit_pixels(config(frame_size=(800, 600), attack_range_px=None), self.root).attack_range_px)

    def test_record_endpoints_reads_actual_minimap_coordinates_and_keeps_d_parameters(self):
        c = config(attack_burst_shots=6)
        save_garden(self.path, c)
        frame = np.zeros((480, 640, 3), np.uint8)
        with patch("lionbot.garden_calibration.fresh", return_value=frame), \
                patch("lionbot.garden_calibration.get_position", side_effect=[(.12, .8), (.88, .8)]), \
                patch("lionbot.garden_calibration.select_point", return_value=(200, 300)), \
                patch("lionbot.garden_calibration.save_garden_anchor"), \
                patch("lionbot.garden_calibration.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
            record_route(self.path, Mock())
        updated = load_garden(self.path)
        self.assertEqual((updated.left_x, updated.right_x), (.12, .88))
        self.assertEqual(updated.bottom_path, ((.12, .8), (.88, .8)))
        self.assertEqual(updated.attack_burst_shots, 6)

    def test_invalid_boundary_capture_does_not_overwrite_original(self):
        save_garden(self.path, config())
        before = self.path.read_bytes()
        for points in ([(.8, .8), (.2, .8)], [(.1, .5), (.9, .8)]):
            with patch("lionbot.garden_calibration.fresh", return_value=np.zeros((480, 640, 3), np.uint8)), \
                    patch("lionbot.garden_calibration.get_position", side_effect=points), \
                    patch("lionbot.garden_calibration.select_point", return_value=(200, 300)), \
                    patch("lionbot.garden_calibration.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()), self.assertRaises(ValueError):
                record_route(self.path, Mock())
        self.assertEqual(self.path.read_bytes(), before)

    def test_teleport_measurement_uses_current_map_and_safety_margin(self):
        save_garden(self.path, config())
        positions = [(.3, .75), (.35, .75), (.3, .75), (.36, .75), (.3, .75), (.345, .75)]
        with patch("lionbot.garden_calibration.fresh", return_value=np.zeros((480, 640, 3), np.uint8)), \
                patch("lionbot.garden_calibration.get_position", side_effect=positions), \
                patch("lionbot.garden_calibration.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
            measure_teleport(self.path, Mock())
        self.assertAlmostEqual(load_garden(self.path).teleport_max_dx, .06 * 1.25 + 2 / 140)

    def test_cli_beep_test_has_no_game_or_keyboard_and_no_wave_dependency(self):
        with patch("lionbot.lie_alert.beep_alert") as beep, patch("lionbot.garden.time.sleep"), \
                patch("lionbot.windows.GameWindow") as window, patch("lionbot.windows.Keyboard") as keyboard, \
                redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--test-alert-sound"]), 0)
        self.assertEqual(beep.call_count, 3)
        window.assert_not_called()
        keyboard.assert_not_called()

    def test_missing_setup_never_enters_live_loop(self):
        with patch("lionbot.garden.run_garden") as live, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "--run"]), 1)
        live.assert_not_called()

    def test_alert_calibration_preserves_garden_settings_and_old_simple_config(self):
        from tests.test_lie_alert import scene as alert_scene

        c = config(attack_burst_shots=6)
        save_garden(self.path, c)
        old = self.root / "config.simple.local.json"
        old.write_text('{"station_x": 0.4}')
        image = self.root / "alert.png"
        write_image(image, alert_scene())
        with patch("lionbot.calibrate.select_roi", return_value=(250, 100, 160, 28)), \
                patch("lionbot.lie_alert.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "--calibrate-alert", "--image", str(image)]), 0)
        self.assertEqual(load_garden(self.path), c)
        self.assertEqual(old.read_text(), '{"station_x": 0.4}')
        self.assertTrue((self.root / c.lie_alert_template).exists())

    def test_sample_saves_full_plant_and_ground_offset_without_keyboard_input(self):
        frame, c = scene(self.root)
        save_garden(self.path, c)
        frame[254:302, 420:468] = plant(left=True)
        with patch("lionbot.garden_calibration.fresh", return_value=frame), \
                patch("lionbot.garden_calibration.select_roi", return_value=(420, 254, 48, 48)), \
                patch("lionbot.garden_calibration.select_point", return_value=(453, 300)), \
                patch("lionbot.windows.Keyboard") as keyboard, \
                patch("lionbot.garden_calibration.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
            sample_plant(self.path, Mock())
        entries = json.loads((self.root / c.template_manifest).read_text())
        self.assertEqual(entries[-1]["feet_offset"], [33, 46])
        self.assertEqual(len(GardenVision(c, self.root).templates), 4)
        keyboard.assert_not_called()

    def test_completed_setup_reuses_all_samples_without_repeating_measurements(self):
        frame, c = scene(self.root)
        save_garden(self.path, c)
        with patch("lionbot.garden_calibration.GameWindow") as window, \
                patch("lionbot.garden_calibration.fresh", return_value=frame), \
                patch("lionbot.garden_calibration.select_point") as select, \
                patch("lionbot.garden_calibration.record_route") as route, \
                patch("lionbot.garden_calibration.measure_teleport") as teleport, \
                patch("lionbot.garden_calibration.sample_plant") as sample, \
                patch("lionbot.garden_calibration.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
            setup_garden(self.path)
        self.assertEqual(load_garden(self.path), c)
        select.assert_not_called()
        route.assert_not_called()
        teleport.assert_not_called()
        sample.assert_not_called()
        window.return_value.close.assert_called_once()

    def test_first_setup_completes_all_steps_and_preserves_old_configuration(self):
        frame, c = scene(self.root)
        (self.root / c.template_manifest).unlink()
        old = self.root / "config.plants.local.json"
        old.write_text(json.dumps({"frame_size": [640, 480], "attack_range_px": 172}))
        before = old.read_bytes()
        def at(x, flower=False):
            image = frame.copy()
            image[4:74, 4:144] = 30
            mx = 4 + round(x * 140)
            cv2.rectangle(image, (mx - 2, 55), (mx + 2, 59), (0, 255, 255), -1)
            if flower:
                image[254:302, 420:468] = plant()
            return image
        frames = [at(.3), at(.3), at(.35), at(.3, True), at(.1), at(.9),
                  at(.4), at(.45), at(.4), at(.46), at(.4), at(.445)]
        points = [(312, 300), (312, 236), (412, 300), (434, 300), (312, 300), (312, 300)]
        with patch("lionbot.garden_calibration.GameWindow") as window, \
                patch("lionbot.garden_calibration.fresh", side_effect=frames), \
                patch("lionbot.garden_calibration.select_roi", side_effect=[(4, 4, 140, 70), (0, 90, 640, 300),
                                                                           (0, 90, 640, 140), (420, 254, 48, 48)]), \
                patch("lionbot.garden_calibration.select_point", side_effect=points), \
                patch("lionbot.windows.Keyboard") as keyboard, \
                patch("lionbot.garden_calibration.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
            setup_garden(self.path)
        saved = load_garden(self.path)
        saved.validate(ready=True)
        self.assertEqual((saved.left_x, saved.right_x), (.1, .9))
        self.assertAlmostEqual(saved.world_pixels_per_x, 2000, places=3)
        self.assertEqual(saved.player_height_px, 64)
        self.assertEqual(saved.attack_range_px, 172)
        self.assertGreater(saved.teleport_max_dx, .06)
        self.assertTrue((self.root / saved.template_manifest).exists())
        self.assertTrue(GardenVision(saved, self.root).mapper.references)
        self.assertEqual(old.read_bytes(), before)
        keyboard.assert_not_called()
        window.return_value.close.assert_called_once()


class GardenVisionTests(GardenTestCase):
    def test_mirrored_templates_local_roi_and_body_height_filter(self):
        frame, c = scene(self.root)
        for x, y, left in ((180, 254, True), (420, 254, False), (520, 254, False), (420, 140, False)):
            frame[y:y + 48, x:x + 48] = plant(left)
        vision = GardenVision(c, self.root)
        with patch("lionbot.vision.detect_monsters", wraps=detect_monsters) as detector:
            vision.observe(frame, now=0)
            obs = vision.observe(frame, now=.1)
        self.assertEqual(len(obs.mobs), 2)
        self.assertTrue(all(target_status(m, obs, vision.c) == "ready" for m in obs.mobs))
        self.assertTrue(all(m.confirmed for m in obs.mobs))
        for invocation in detector.call_args_list:
            roi = invocation.args[1]
            self.assertLess(roi[2] * roi[3], 640 * 300 * .5)

    def test_swinging_attacking_heads_can_be_color_confirmed_both_ways(self):
        for left in (False, True):
            frame, c = scene(self.root)
            frame[254:302, 420:468] = plant(left=left, hurt=True)
            vision = GardenVision(c, self.root)
            for index in range(3):
                obs = vision.observe(frame, now=index * .1)
            self.assertEqual(len(obs.mobs), 1)
            self.assertTrue(obs.mobs[0].confirmed)
            self.assertEqual(vision.detections[0].source, "color")

    def test_magenta_text_without_leaves_or_greenery_without_head_do_not_make_targets(self):
        detector = PlantColorDetector([make_template(plant(), (14, 46))], "magenta")
        image = np.full((100, 150, 3), SKY, np.uint8)
        cv2.putText(image, "HIT", (20, 40), 0, 1, (180, 20, 235), 2)
        cv2.ellipse(image, (110, 70), (15, 10), 0, 0, 360, (20, 190, 35), -1)
        self.assertEqual(detector.detect(image, (0, 0, 150, 100)), [])

    def test_terrain_locates_player_when_nameplate_is_missing(self):
        frame, c = scene(self.root)
        frame[310:324, 280:344] = SKY
        vision = GardenVision(c, self.root)
        obs = vision.observe(frame, now=0)
        self.assertIsNotNone(obs.position)
        self.assertIsNotNone(obs.player_feet)
        self.assertEqual(obs.location_source, "terrain")
        self.assertAlmostEqual(obs.player_feet[0], 312, delta=2)

    def test_saved_endpoint_anchors_allow_starting_with_different_camera_view(self):
        frame, c = scene(self.root)
        another = np.full_like(frame, SKY)
        rng = np.random.default_rng(1234)
        another[90:390] = rng.integers(0, 200, (300, 640, 3), dtype=np.uint8)
        another[4:74, 4:144] = 30
        cv2.rectangle(another, (114, 55), (118, 59), (0, 255, 255), -1)
        save_garden_anchor(c, self.root, another, (.8, 53 / 70), (312., 300.))
        vision = GardenVision(replace(c, player_feet_offset=None), self.root)
        obs = vision.observe(frame, now=0)
        self.assertIsNotNone(obs.player_feet)
        self.assertEqual(obs.location_source, "terrain")
        self.assertGreaterEqual(len(vision.mapper.references), 2)

    def test_world_vertical_motion_does_not_shift_stationary_flower_confirmation(self):
        frame, c = scene(self.root, floor_tolerance=.06)
        frame[254:302, 420:468] = plant()
        hurt = frame.copy()
        hurt[254:302, 420:468] = plant(hurt=True)
        vision = GardenVision(c, self.root)
        with patch("lionbot.vision.minimap_position", side_effect=[(.3, .75), (.3, .75), (.3, .79)]), \
                patch.object(vision, "locate_stationary", side_effect=[((312., 300.), "terrain"),
                                                                      ((312., 300.), "terrain"),
                                                                      ((312., 340.), "terrain")]):
            vision.observe(frame, now=0)
            self.assertTrue(vision.observe(frame, now=.1).mobs[0].confirmed)
            # 角色在世界里下降40px，镜头/固定花都没动。第三帧改为摆动姿态。
            obs = vision.observe(hurt, now=.2)
        self.assertTrue(obs.mobs[0].confirmed)
        self.assertEqual(vision.detections[0].source, "color")
        self.assertAlmostEqual(obs.mobs[0].feet[1], 300, delta=1)


class GardenRuntimeTests(GardenTestCase):
    def test_alert_stops_keys_and_manual_f8_is_required_before_startup_alt(self):
        window = Mock()
        window.hwnd = 123
        window.focused.return_value = True
        window.capture.return_value = np.zeros((480, 640, 3), np.uint8)
        vision = Mock()
        vision.observe.return_value = observation()
        keys = SimpleNamespace(f8="f8", f10="f10", f12="f12")
        module = ModuleType("pynput.keyboard")
        module.Key, module.Listener = keys, Mock(return_value=Mock())
        keyboard = Keyboard(SimpleNamespace(time=time.monotonic))
        instances = []
        class Monitor:
            def __init__(self, _detector, _size, factory, on_alarm, _interval, **kwargs):
                self.state, self.callback, self.factory = AlertState(max_age=10), on_alarm, factory
                instances.append(self)
            def start(self):
                self.factory()
                self.state.publish(hit=True)
                self.callback("检测到测谎")
            def stop(self):
                pass
            def status(self):
                return "test"
        ticks = []
        def drive(_):
            ticks.append(1)
            callbacks = module.Listener.call_args.kwargs
            if len(ticks) == 1:
                self.assertFalse(keyboard.events)
                callbacks["on_release"](keys.f8)
            elif len(ticks) == 2:
                self.assertFalse(keyboard.events)
                instances[0].state.publish()
                instances[0].state.publish()
            elif len(ticks) == 3:
                self.assertFalse(keyboard.events)
                callbacks["on_release"](keys.f8)
            else:
                callbacks["on_press"](keys.f12)
        with ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"pynput": ModuleType("pynput"), "pynput.keyboard": module}))
            stack.enter_context(patch("lionbot.windows.require_windows"))
            windows = stack.enter_context(patch("lionbot.windows.GameWindow", return_value=window))
            stack.enter_context(patch("lionbot.windows.Keyboard", return_value=keyboard))
            stack.enter_context(patch("lionbot.garden.GardenVision", return_value=vision))
            stack.enter_context(patch("lionbot.lie_alert.load_detector", return_value=Mock()))
            stack.enter_context(patch("lionbot.lie_alert.LieAlertMonitor", Monitor))
            stack.enter_context(patch("lionbot.garden.time.sleep", side_effect=drive))
            stack.enter_context(redirect_stdout(io.StringIO()))
            run_garden(config(), self.path, send_keys=True)
        self.assertEqual([e[1] for e in keyboard.events if e[0] == "press"], ["alt"])
        self.assertFalse(keyboard.held)
        self.assertEqual(windows.call_args_list, [call(config().window_title, frame_size=(640, 480)),
                                                   call(config().window_title, hwnd=123)])

    def test_observe_closing_preview_exits_without_inputs_and_restores_threads(self):
        window, vision = Mock(), Mock()
        window.focused.return_value = True
        window.capture.return_value = np.zeros((480, 640, 3), np.uint8)
        vision.observe.return_value = observation()
        module = ModuleType("pynput.keyboard")
        module.Key = SimpleNamespace(f8="f8", f10="f10", f12="f12")
        module.Listener = Mock(return_value=Mock())
        with ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"pynput": ModuleType("pynput"), "pynput.keyboard": module}))
            stack.enter_context(patch("lionbot.windows.require_windows"))
            stack.enter_context(patch("lionbot.windows.GameWindow", return_value=window))
            keyboard = stack.enter_context(patch("lionbot.windows.Keyboard"))
            stack.enter_context(patch("lionbot.garden.GardenVision", return_value=vision))
            stack.enter_context(patch("lionbot.garden.time.sleep"))
            stack.enter_context(patch("lionbot.garden.cv2.getNumThreads", return_value=4))
            threads = stack.enter_context(patch("lionbot.garden.cv2.setNumThreads"))
            for name in ("namedWindow", "resizeWindow", "imshow", "destroyAllWindows"):
                stack.enter_context(patch("lionbot.garden.cv2." + name))
            stack.enter_context(patch("lionbot.garden.cv2.waitKey", return_value=-1))
            stack.enter_context(patch("lionbot.garden.cv2.getWindowProperty", side_effect=[1, 0]))
            stack.enter_context(redirect_stdout(io.StringIO()))
            run_garden(config(lie_alert_enabled=False), self.path, show=True)
        keyboard.assert_not_called()
        window.capture.assert_called_once()
        window.close.assert_called_once()
        self.assertEqual(threads.call_args_list, [call(1), call(4)])


if __name__ == "__main__":
    unittest.main()
