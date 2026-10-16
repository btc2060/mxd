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
from lionbot.controls import InputCancelled, InputExecutor
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
    def test_six_d_then_four_a_repeats_and_only_attacks_right(self):
        sim = Simulation()
        sim.until(lambda: sim.brain.d_groups == 70)
        attacks = [a for _, a in sim.history if a.kind in ("attack", "attack_a")]
        physical = [e[1] for e in sim.keyboard.events if e[0] == "press" and e[1] in ("d", "a")]
        self.assertEqual(physical, (["d"] * 6 + ["a"] * 4) * 34 + ["d"] * 6)
        self.assertTrue(all(a.direction == "right" for a in attacks))
        self.assertFalse({"enter", "esc", "left"} & {e[1] for e in sim.keyboard.events if e[0] == "press"})
        self.assertFalse(sim.keyboard.held)

    def test_group_pause_is_after_key_release_and_shots_keep_cast_interval(self):
        sim = Simulation()
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

    def test_counts_and_pauses_remain_configurable(self):
        sim = Simulation(config(right_attack_shots=4, group_pause=.5, attack_interval=1.1,
                                d_shots_before_a=5, a_attack_shots=2))
        sim.until(lambda: sim.brain.d_groups == 2)
        times = [at for at, a in sim.history if a.kind in ("attack", "attack_a")]
        self.assertEqual(len(times), 10)
        physical = [e[1] for e in sim.keyboard.events if e[0] == "press" and e[1] in ("d", "a")]
        self.assertEqual(physical, ["d"] * 5 + ["a"] * 2 + ["d"] * 3)
        self.assertTrue(all(b - a >= 1.1 - 1e-9 for a, b in zip(times, times[1:])))

    def test_a_finishes_four_shots_before_resuming_the_pending_d_group(self):
        sim = Simulation(config(d_shots_before_a=5))
        sim.until(lambda: sim.brain.a_shots == 1)
        self.assertEqual((sim.brain.d_since_a, sim.brain.shots), (5, 2))
        sim.until(lambda: sim.brain.d_groups == 2)
        physical = [e[1] for e in sim.keyboard.events if e[0] == "press" and e[1] in ("d", "a")]
        self.assertEqual(physical, ["d"] * 5 + ["a"] * 4 + ["d"])
        self.assertEqual(sim.brain.d_since_a, 1)

    def test_a_has_cast_interval_and_pauses_before_switching_back_to_d(self):
        sim = Simulation()
        sim.until(lambda: sum(a.kind == "attack" for _, a in sim.history) == 7)
        attacks = [(at, a.kind) for at, a in sim.history if a.kind in ("attack", "attack_a")]
        self.assertEqual([kind for _, kind in attacks], ["attack"] * 6 + ["attack_a"] * 4 + ["attack"])
        for i in range(6, 11):
            minimum = sim.c.attack_hold + sim.c.group_pause if i in (6, 10) else sim.c.attack_interval
            self.assertGreaterEqual(attacks[i][0] - attacks[i - 1][0] + 1e-9, minimum)

    def test_knockback_clears_right_then_walks_back_without_counting_recovery(self):
        sim = Simulation()
        sim.until(lambda: sim.brain.shots == 1)
        original_shots = sim.brain.shots
        start = len(sim.history)
        sim.until(lambda: sim.brain.recovery == "return", x=.56)
        recovery = [a for _, a in sim.history[start:] if a.kind == "attack"]
        self.assertEqual([a.direction for a in recovery], ["right"] * 3)
        self.assertEqual((sim.brain.d_groups, sim.brain.shots), (0, original_shots))
        while sim.tick(.56).kind != "walk":
            pass
        self.assertEqual(sim.keyboard.held, {"left"})
        sim.tick(.54)
        sim.tick(.52)
        self.assertEqual(sim.tick(.504).kind, "wait")
        self.assertFalse(sim.keyboard.held)
        sim.until(lambda: sim.brain.d_groups == 1, x=.504)
        self.assertFalse(sim.brain.recovery)

    def test_recovery_d_shots_count_toward_switch_and_a_does_not_finish_recovery_early(self):
        sim = Simulation()
        sim.until(lambda: sim.brain.d_since_a == 5)
        start = len(sim.history)
        sim.until(lambda: sim.brain.recovery == "return", x=.56)
        actions = [a.kind for _, a in sim.history[start:] if a.kind in ("attack", "attack_a")]
        self.assertEqual(actions, ["attack"] + ["attack_a"] * 4 + ["attack"] * 2)
        self.assertEqual((sim.brain.d_since_a, sim.brain.a_shots), (2, 0))
        self.assertEqual((sim.brain.d_groups, sim.brain.shots), (1, 2))

    def test_knockback_during_a_keeps_remaining_a_shots_before_recovery_d(self):
        sim = Simulation()
        sim.until(lambda: sim.brain.a_shots == 2)
        sim.until(lambda: sim.brain.recovery == "return", x=.56)
        physical = [e[1] for e in sim.keyboard.events if e[0] == "press" and e[1] in ("d", "a")]
        self.assertEqual(physical, ["d"] * 6 + ["a"] * 4 + ["d"] * 3)
        self.assertEqual(sim.brain.d_groups, 2)

    def test_switch_due_on_last_recovery_d_returns_home_then_a_before_any_more_d(self):
        sim = Simulation()
        sim.until(lambda: sim.brain.d_since_a == 3)
        sim.until(lambda: sim.brain.recovery == "return", x=.56)
        self.assertEqual((sim.brain.d_since_a, sim.brain.a_shots), (6, 0))
        while sim.tick(.56).kind != "walk":
            pass
        sim.tick(.5)
        sim.until(lambda: sim.brain.d_since_a == 0)
        physical = [e[1] for e in sim.keyboard.events if e[0] == "press" and e[1] in ("d", "a")]
        self.assertEqual(physical, ["d"] * 6 + ["a"] * 4)
        first_a = next(i for i, (_, a) in enumerate(sim.history) if a.kind == "attack_a")
        last_face = next(a for _, a in reversed(sim.history[:first_a]) if a.kind == "face")
        self.assertEqual(last_face.direction, "right")

    def test_pause_and_yellow_loss_preserve_partial_a_then_resume_remaining_shots(self):
        sim = Simulation()
        sim.until(lambda: sim.brain.a_shots == 2)
        sim.brain.suspend()
        sim.tick(None)
        sim.until(lambda: sim.brain.d_since_a == 0)
        physical = [e[1] for e in sim.keyboard.events if e[0] == "press" and e[1] in ("d", "a")]
        self.assertEqual(physical, ["d"] * 6 + ["a"] * 4)

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
        sim.until(lambda: sim.brain.a_shots == 2)
        next_alt, cooldown = sim.brain.next_alt, sim.brain.busy_until
        progress = (sim.brain.d_since_a, sim.brain.a_shots)
        sim.brain.set_station(.6)
        self.assertEqual((sim.brain.shots, sim.brain.d_groups), (0, 0))
        self.assertEqual((sim.brain.next_alt, sim.brain.busy_until), (next_alt, cooldown))
        self.assertEqual((sim.brain.d_since_a, sim.brain.a_shots), progress)
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

    def test_real_direction_inputs_clear_after_knockback_return_and_resume_cycle(self):
        class World(Clock, Keyboard):
            def __init__(self):
                Clock.__init__(self)
                Keyboard.__init__(self, self)
                self.x, self.kicked, self.facing = .5, False, "left"

            def sleep(self, delay):
                self.x += .06 * delay * (int("right" in self.held) - int("left" in self.held))
                Clock.sleep(self, delay)

            def press(self, key):
                Keyboard.press(self, key)
                if key in ("left", "right"):
                    self.facing = key
                if key == "d":
                    assert self.facing == "right", "回位后未先转向右侧"
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
        self.assertEqual(sum(e[:2] == ("press", "d") for e in world.events), 3 + 3)
        self.assertFalse(world.held)
        self.assertFalse({"a", "f", "space", "up", "down"} & {e[1] for e in world.events})


class SkillInputTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.keyboard = Keyboard(self.clock)
        self.executor = InputExecutor(self.keyboard, Config(), clock=self.clock.time, sleep=self.clock.sleep)

    def test_a_releases_movement_before_pressing_and_releases_afterwards(self):
        self.executor.execute(Action("walk", "left"))
        self.executor.execute(Action("attack_a", "right"))
        presses = [e for e in self.keyboard.events if e[0] == "press"]
        self.assertEqual([e[1] for e in presses], ["left", "a"])
        self.assertFalse(presses[-1][2])
        self.assertFalse(self.keyboard.held)

    def test_pause_after_keydown_still_counts_d_and_a_but_releases_keys(self):
        active = [True]
        self.executor.active = lambda: active[0]
        original = self.keyboard.press

        def interrupt(key):
            original(key)
            active[0] = False

        self.keyboard.press = interrupt
        brain = SimpleCycle(config())
        brain.facing, brain.next_alt, brain.d_since_a = "right", 190, 5
        for expected in ("attack", "attack_a"):
            active[0] = True
            action = brain.decide(.5, self.clock.now)
            self.assertEqual(action.kind, expected)
            brain.executed(action, self.executor.execute(action))
            self.assertFalse(self.keyboard.held)
            self.clock.sleep(1)
        self.assertEqual((brain.d_since_a, brain.a_shots), (6, 1))

    def test_pause_before_attack_never_presses_or_counts(self):
        self.executor.active = lambda: False
        with self.assertRaises(InputCancelled):
            self.executor.execute(Action("attack_a", "right"))
        self.assertFalse(self.keyboard.events)


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
        for change in ({"right_attack_shots": 0}, {"recovery_shots": True},
                       {"d_shots_before_a": 0}, {"d_shots_before_a": 2.5}, {"a_attack_shots": False},
                       {"attack_interval": .1}, {"group_pause": -1}, {"group_pause": float("inf")},
                       {"minimap_roi": (0, 0, 801, 100)}, {"station_x": 1.1}):
            with self.subTest(change=change), self.assertRaises(ValueError):
                config(**change).validate()

    def test_old_config_discards_chat_and_round_options_but_keeps_station_and_right_settings(self):
        raw = asdict(config(right_attack_shots=6))
        raw.update(left_attack_shots=99, groups_per_direction=5, alternate_every_rounds=6, alternate_shots=4)
        raw.update(pet_chat_min_seconds=10, pet_chat_max_seconds=30, pet_commands=["坐", "说话"])
        self.path.write_text(json.dumps(raw))
        loaded = load_simple(self.path)
        self.assertEqual(loaded, config(right_attack_shots=6))
        self.assertFalse(hasattr(loaded, "alternate_shots"))
        self.assertFalse(hasattr(loaded, "pet_commands"))
        self.assertEqual((loaded.d_shots_before_a, loaded.a_attack_shots), (6, 4))

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
