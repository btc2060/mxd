"""不连接游戏，验证最简循环的实际按键、时序、X 回位与独立设置。"""
import io
import json
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

import cv2
import numpy as np

from lionbot.brain import Action
from lionbot.config import Config
from lionbot.controls import InputExecutor
from lionbot.simple import main, run_simple, setup
from lionbot.simple_cycle import SimpleConfig, SimpleCycle, load_simple
from lionbot.windows import GameWindow
from tests.test_controls import Clock, Keyboard


def config(**changes):
    base = SimpleConfig(minimap_roi=(0, 0, 200, 100), frame_size=(800, 600), station_x=.5)
    return replace(base, **changes)


class Simulation:
    def __init__(self, c=None):
        self.c = c or config()
        self.clock = Clock()
        self.keyboard = Keyboard(self.clock)
        self.brain = SimpleCycle(self.c)
        self.executor = InputExecutor(self.keyboard, Config(attack_hold=self.c.attack_hold,
                                                           face_hold=self.c.face_hold),
                                      clock=self.clock.time, sleep=self.clock.sleep)
        self.history = []

    def tick(self, x=.5, focused=True):
        action = self.brain.decide(x, self.clock.now, focused)
        at = self.executor.execute(action)
        self.brain.executed(action, at)
        self.history.append((at, action))
        self.clock.sleep(.05)
        return action

    def until(self, condition, x=.5, timeout=2000):
        limit = self.clock.now + timeout
        while not condition():
            if self.clock.now > limit:
                raise AssertionError("简单循环没有在模拟时限内完成")
            self.tick(x)


class SimpleCycleTests(unittest.TestCase):
    def test_defaults_run_six_full_rounds_before_four_a_and_then_resume_d(self):
        sim = Simulation()
        sim.until(lambda: any(e[:2] == ("press", "a") for e in sim.keyboard.events))
        before_a = [a for _, a in sim.history if a.kind == "attack"]
        self.assertEqual(len(before_a), 6 * 2 * 5 * 3)
        self.assertEqual([a.direction for a in before_a],
                         (["right"] * 15 + ["left"] * 15) * 6)
        self.assertEqual(sim.brain.completed_rounds, 6)
        sim.until(lambda: len([a for _, a in sim.history if a.kind == "attack"]) == 181)
        attacks = [a for _, a in sim.history if a.kind in ("attack", "attack_a")]
        self.assertEqual([a.kind for a in attacks[180:]], ["attack_a"] * 4 + ["attack"])
        self.assertEqual([a.direction for a in attacks[180:]], ["left"] * 4 + ["right"])
        self.assertFalse(sim.keyboard.held)

    def test_group_pause_is_after_key_release_and_shots_keep_cast_interval(self):
        sim = Simulation(config(groups_per_direction=2, alternate_every_rounds=1))
        sim.until(lambda: sim.brain.d_groups == 3)
        attacks = [at for at, a in sim.history if a.kind == "attack"]
        self.assertEqual(len(attacks), 9)
        for index, at in enumerate(attacks[1:], 1):
            minimum = sim.c.attack_hold + 3 if index % 3 == 0 else .9
            self.assertGreaterEqual(at - attacks[index - 1] + 1e-9, minimum)
        # 组间等待没有按住攻击或方向键。
        for _, key, held, _ in sim.keyboard.events:
            if key == "d":
                self.assertFalse({"left", "right"} & held)

    def test_counts_and_pauses_are_configurable_and_a_repeats_at_each_round_interval(self):
        sim = Simulation(config(right_attack_shots=2, left_attack_shots=2, group_pause=.5, attack_interval=1.1,
                                groups_per_direction=2, alternate_every_rounds=2, alternate_shots=1))
        sim.until(lambda: sum(a.kind == "attack_a" for _, a in sim.history) == 2)
        attacks = [a for _, a in sim.history if a.kind in ("attack", "attack_a")]
        self.assertEqual(len(attacks), 34)
        self.assertEqual([i for i, a in enumerate(attacks) if a.kind == "attack_a"], [16, 33])
        times = [at for at, a in sim.history if a.kind in ("attack", "attack_a")]
        self.assertTrue(all(b - a >= 1.1 - 1e-9 for a, b in zip(times, times[1:])))

    def test_right_and_left_shot_counts_can_be_set_independently(self):
        sim = Simulation(config(right_attack_shots=2, left_attack_shots=4,
                                groups_per_direction=1, alternate_every_rounds=1))
        sim.until(lambda: any(a.kind == "attack_a" for _, a in sim.history))
        attacks = [a.direction for _, a in sim.history if a.kind == "attack"]
        self.assertEqual(attacks, ["right"] * 2 + ["left"] * 4)

    def test_knockback_clears_each_side_then_walks_back_without_counting_recovery(self):
        sim = Simulation()
        sim.until(lambda: sim.brain.shots == 1)
        original_shots = sim.brain.shots
        start = len(sim.history)
        sim.until(lambda: sim.brain.recovery == "return", x=.56)
        recovery = [a for _, a in sim.history[start:] if a.kind == "attack"]
        self.assertEqual([a.direction for a in recovery], ["left"] * 3 + ["right"] * 3)
        self.assertEqual((sim.brain.d_groups, sim.brain.completed_rounds, sim.brain.shots), (0, 0, original_shots))
        while sim.tick(.56).kind != "walk":
            pass
        self.assertEqual(sim.keyboard.held, {"left"})
        sim.tick(.54)
        sim.tick(.52)
        self.assertEqual(sim.tick(.504).kind, "wait")
        self.assertFalse(sim.keyboard.held)
        sim.until(lambda: sim.brain.d_groups == 1, x=.504)
        self.assertEqual(sim.brain.completed_rounds, 0)
        self.assertFalse(sim.brain.recovery)

    def test_alternate_group_interrupted_by_knockback_resumes_remaining_shots(self):
        sim = Simulation(config(groups_per_direction=1, alternate_every_rounds=1))
        sim.until(lambda: sim.brain.alternate_due and sim.brain.shots == 1)
        self.assertEqual(sim.brain.completed_rounds, 1)
        sim.until(lambda: sim.brain.recovery == "return", x=.56)
        self.assertEqual(sim.brain.shots, 1)
        while sim.tick(.56).kind != "walk":
            pass
        sim.tick(.5)
        sim.until(lambda: not sim.brain.alternate_due)
        self.assertEqual(sum(a.kind == "attack_a" for _, a in sim.history), 4)
        self.assertEqual(sim.brain.completed_rounds, 1)

    def test_alt_is_first_key_then_every_190_seconds_and_pause_does_not_repeat_start(self):
        sim = Simulation()
        sim.until(lambda: sim.clock.now > 400)
        presses = [e for e in sim.keyboard.events if e[0] == "press"]
        self.assertEqual(presses[0][1], "alt")
        alts = [e[3] for e in presses if e[1] == "alt"]
        self.assertEqual(len(alts), 3)
        self.assertTrue(all(190 <= b - a < 191.1 for a, b in zip(alts, alts[1:])))
        sim.brain.suspend()
        next_alt = sim.brain.next_alt
        sim.tick()
        self.assertEqual(sim.brain.next_alt, next_alt)

    def test_new_station_resets_right_cycle_but_preserves_alt_schedule_and_cast_cooldown(self):
        sim = Simulation()
        sim.until(lambda: sim.brain.shots == 1)
        next_alt, cooldown = sim.brain.next_alt, sim.brain.busy_until
        sim.brain.set_station(.6)
        self.assertEqual((sim.brain.direction, sim.brain.shots, sim.brain.d_groups), ("right", 0, 0))
        self.assertEqual((sim.brain.next_alt, sim.brain.busy_until), (next_alt, cooldown))
        self.assertEqual(sim.brain.decide(.6, cooldown - .001).kind, "wait")

    def test_tiny_minimap_jitter_does_not_interrupt_normal_cycle(self):
        sim = Simulation()
        for x in (.5, .505, .495, .51, .49):
            sim.tick(x)
        self.assertFalse(sim.brain.recovery)

    def test_yellow_loss_stops_held_walk_and_never_sends_attack_or_alt(self):
        sim = Simulation()
        sim.until(lambda: sim.brain.recovery == "return", x=.56)
        while sim.tick(.56).kind != "walk":
            pass
        before = len(sim.keyboard.events)
        self.assertEqual(sim.tick(None).kind, "wait")
        self.assertFalse(sim.keyboard.held)
        self.assertFalse(any(e[0] == "press" for e in sim.keyboard.events[before:]))
        before = len(sim.keyboard.events)
        self.assertEqual(sim.tick(.56, focused=False).kind, "wait")
        self.assertEqual(len(sim.keyboard.events), before)

    def test_return_stops_after_crossing_target_instead_of_oscillating(self):
        sim = Simulation()
        sim.brain.next_alt = 190
        sim.brain.recovery = "return"
        self.assertEqual(sim.tick(.52).kind, "walk")
        self.assertEqual(sim.tick(.493).kind, "wait")
        self.assertFalse(sim.keyboard.held)
        self.assertFalse(sim.brain.recovery)

    def test_a_executor_releases_movement_and_uses_only_a(self):
        sim = Simulation()
        sim.executor.execute(Action("walk", "left"))
        sim.executor.execute(Action("attack_a", "right"))
        attack = next(e for e in sim.keyboard.events if e[:2] == ("press", "a"))
        self.assertEqual(attack[2], set())
        self.assertFalse(sim.keyboard.held)
        self.assertFalse(any(e[:2] == ("press", "d") for e in sim.keyboard.events))

    def test_real_direction_inputs_clear_after_knockback_return_and_resume_cycle(self):
        class World(Clock, Keyboard):
            def __init__(self):
                Clock.__init__(self)
                Keyboard.__init__(self, self)
                self.x, self.kicked = .5, False

            def sleep(self, delay):
                self.x += .06 * delay * (int("right" in self.held) - int("left" in self.held))
                Clock.sleep(self, delay)

            def press(self, key):
                Keyboard.press(self, key)
                if key == "d" and not self.kicked:
                    self.x += .06
                    self.kicked = True

        world = World()
        brain = SimpleCycle(config())
        executor = InputExecutor(world, Config(), clock=world.time, sleep=world.sleep)
        saw_recovery = saw_walk = False
        try:
            while world.now < 30:
                action = brain.decide(round(world.x * 200) / 200, world.now)
                brain.executed(action, executor.execute(action))
                saw_recovery |= bool(brain.recovery)
                saw_walk |= action.kind == "walk"
                world.sleep(.05)
                if saw_walk and not brain.recovery and brain.d_groups == 1:
                    break
            else:
                self.fail(f"未完成击退回位：X={world.x}, 状态={brain.recovery}, D组={brain.d_groups}")
        finally:
            executor.stop()
        self.assertTrue(saw_recovery)
        self.assertTrue(saw_walk)
        self.assertLessEqual(abs(world.x - .5), brain.outer_tolerance)
        self.assertEqual(sum(e[:2] == ("press", "d") for e in world.events), 3 + 6)
        self.assertFalse(world.held)
        self.assertFalse({"f", "space", "up", "down"} & {e[1] for e in world.events})


class SimpleSetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "config.simple.local.json"

    def tearDown(self):
        self.temp.cleanup()

    def frame(self):
        frame = np.zeros((600, 800, 3), np.uint8)
        cv2.rectangle(frame, (100, 50), (104, 54), (0, 255, 255), -1)
        return frame

    def test_setup_reuses_only_old_minimap_records_new_x_without_sampling(self):
        old = self.root / "config.plants.local.json"
        old.write_text(json.dumps({"window_title": "game", "minimap_roi": [0, 0, 200, 100],
                                   "frame_size": [800, 600], "station": [.1, .2],
                                   "template_manifest": "does-not-exist.json"}))
        before = old.read_bytes()
        with patch("lionbot.windows.GameWindow"), \
                patch("lionbot.calibrate.fresh", return_value=self.frame()), \
                patch("lionbot.calibrate.select_roi") as select, redirect_stdout(io.StringIO()):
            setup(self.path)
        select.assert_not_called()
        saved = load_simple(self.path)
        self.assertEqual(saved.station_x, .51)
        self.assertEqual(saved.right_attack_shots, 3)
        self.assertEqual(saved.left_attack_shots, 3)
        self.assertEqual(old.read_bytes(), before)
        self.assertNotIn("template_manifest", json.loads(self.path.read_text()))

    def test_first_setup_needs_only_one_roi_no_name_monster_or_feet_clicks(self):
        with patch("lionbot.windows.GameWindow"), \
                patch("lionbot.calibrate.fresh", return_value=self.frame()), \
                patch("lionbot.calibrate.select_roi", return_value=(0, 0, 200, 100)) as select, \
                patch("lionbot.calibrate.select_point") as point, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "--setup"]), 0)
        select.assert_called_once()
        point.assert_not_called()
        load_simple(self.path).validate(ready=True)

    def test_set_x_changes_only_x_and_rejects_invalid_value_without_overwriting(self):
        self.path.write_text(json.dumps(asdict(config(right_attack_shots=6))))
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "--set-x", "0.6778"]), 0)
        self.assertEqual(load_simple(self.path), replace(config(right_attack_shots=6), station_x=.6778))
        before = self.path.read_bytes()
        with redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "--set-x", "nan"]), 1)
        self.assertEqual(self.path.read_bytes(), before)

    def test_unset_config_fails_before_opening_game_or_loading_templates(self):
        with patch("lionbot.simple.run_simple") as live, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--config", str(self.path), "--run"]), 1)
        live.assert_not_called()

    def test_invalid_counts_intervals_and_rois_are_rejected(self):
        for change in ({"right_attack_shots": 0}, {"left_attack_shots": -1}, {"alternate_every_rounds": 0},
                       {"groups_per_direction": 1.5}, {"recovery_shots": True},
                       {"attack_interval": .1}, {"group_pause": -1}, {"group_pause": float("inf")},
                       {"minimap_roi": (0, 0, 801, 100)}, {"station_x": 1.1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                config(**change).validate()

    def test_region_capture_grabs_only_minimap_and_rejects_resized_window(self):
        window = object.__new__(GameWindow)
        window.hwnd, window.gui, window.screen = 1, Mock(), Mock()
        window.gui.IsWindow.return_value = True
        window.gui.IsIconic.return_value = False
        window.gui.GetClientRect.return_value = (0, 0, 800, 600)
        window.gui.ClientToScreen.return_value = (50, 80)
        window.screen.grab.return_value = np.zeros((100, 200, 4), np.uint8)
        image = window.capture_region((10, 20, 200, 100), (800, 600))
        self.assertEqual(image.shape, (100, 200, 3))
        window.screen.grab.assert_called_once_with(dict(left=60, top=100, width=200, height=100))
        with self.assertRaisesRegex(ValueError, "大小已改变"):
            window.capture_region((10, 20, 200, 100), (900, 600))

    def test_live_observe_f9_records_while_paused_and_never_loads_game_vision(self):
        window = Mock()
        window.focused.return_value = True
        window.capture_region.return_value = self.frame()[:100, :200]
        keys = SimpleNamespace(f8="f8", f9="f9", f12="f12")
        listener = Mock()
        module = ModuleType("pynput.keyboard")
        module.Key, module.Listener = keys, Mock(return_value=listener)
        def start_listener():
            callbacks = module.Listener.call_args.kwargs
            callbacks["on_release"](keys.f8)
            callbacks["on_release"](keys.f9)

        def stop_after_record(_):
            module.Listener.call_args.kwargs["on_press"](keys.f12)

        listener.start.side_effect = start_listener
        with ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"pynput": ModuleType("pynput"), "pynput.keyboard": module}))
            stack.enter_context(patch("lionbot.windows.require_windows"))
            stack.enter_context(patch("lionbot.windows.GameWindow", return_value=window))
            keyboard = stack.enter_context(patch("lionbot.windows.Keyboard"))
            vision = stack.enter_context(patch("lionbot.vision.Vision"))
            stack.enter_context(patch("lionbot.simple.time.sleep", side_effect=stop_after_record))
            stack.enter_context(redirect_stdout(io.StringIO()))
            run_simple(config(), self.path, send_keys=False)
        self.assertEqual(load_simple(self.path).station_x, .51)
        keyboard.assert_not_called()
        vision.assert_not_called()
        window.capture.assert_not_called()
        window.close.assert_called_once()
        listener.stop.assert_called_once()
