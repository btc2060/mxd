import threading
import unittest

from lionbot.brain import Action
from lionbot.config import Config
from lionbot.controls import InputCancelled, InputExecutor


class Clock:
    def __init__(self):
        self.now = 0.

    def time(self):
        return self.now

    def sleep(self, delay):
        self.now += delay


class Keyboard:
    def __init__(self, clock):
        self.clock = clock
        self.events = []
        self.held = set()

    def press(self, key):
        self.events.append(("press", key, set(self.held), self.clock.time()))
        self.held.add(key)

    def release(self, key):
        self.events.append(("release", key, set(self.held), self.clock.time()))
        self.held.discard(key)


class ControlTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.keyboard = Keyboard(self.clock)
        self.executor = InputExecutor(self.keyboard, Config(), clock=self.clock.time, sleep=self.clock.sleep)

    def test_teleport_holds_direction_when_f_is_pressed(self):
        self.executor.execute(Action("teleport", "right"))
        press_f = next(e for e in self.keyboard.events if e[:2] == ("press", "f"))
        self.assertIn("right", press_f[2])
        self.assertAlmostEqual(press_f[3], .04)
        self.assertEqual(self.keyboard.held, set())

    def test_attack_d_is_not_sent_with_movement(self):
        self.executor.press("left")
        self.executor.execute(Action("attack", "right"))
        press_d = next(e for e in self.keyboard.events if e[:2] == ("press", "d"))
        self.assertEqual(press_d[2], set())
        self.assertEqual(self.keyboard.held, set())

    def test_consecutive_walks_hold_direction_and_scan_releases_it(self):
        for _ in range(3):
            self.executor.execute(Action("walk", "right"))
        self.assertEqual(self.keyboard.held, {"right"})
        self.assertEqual([e[:2] for e in self.keyboard.events], [("press", "right")])
        self.executor.execute(Action("scan"))
        self.assertEqual(self.keyboard.held, set())

    def test_turn_releases_old_direction_before_pressing_new_one(self):
        self.executor.execute(Action("walk", "right"))
        self.executor.execute(Action("walk", "left"))
        presses = [e for e in self.keyboard.events if e[0] == "press"]
        self.assertEqual(presses[-1][2], set())
        self.assertEqual(self.keyboard.held, {"left"})
        self.executor.stop()

    def test_guard_stops_continuous_walk_on_focus_loss_or_slow_frame(self):
        self.executor.execute(Action("walk", "left"))
        self.executor.active = lambda: False
        self.executor.guard_once()
        self.assertEqual(self.keyboard.held, set())
        self.executor.active = lambda: True
        self.executor.execute(Action("walk", "right"))
        self.clock.sleep(.251)
        self.executor.guard_once()
        self.assertEqual(self.keyboard.held, set())

    def test_failed_walk_does_not_leave_held_direction(self):
        self.executor.active = lambda: self.clock.now < .03
        with self.assertRaises(InputCancelled):
            self.executor.execute(Action("walk", "right"))
        self.assertEqual(self.keyboard.held, set())

    def test_background_guard_releases_walk_between_frames(self):
        active, stopped, released = threading.Event(), threading.Event(), threading.Event()
        active.set()
        self.executor.active = active.is_set
        real_release = self.keyboard.release

        def release(key):
            real_release(key)
            released.set()

        self.keyboard.release = release
        guard = self.executor.start_guard(stopped)
        try:
            self.executor.execute(Action("walk", "left"))
            active.clear()
            self.assertTrue(released.wait(1), "守卫未在帧处理间隙释放方向键")
            self.assertEqual(self.keyboard.held, set())
        finally:
            stopped.set()
            guard.join(timeout=1)
            self.executor.stop()
        self.assertFalse(guard.is_alive())

    def test_grab_is_up_and_space_and_exit_is_only_space(self):
        self.executor.execute(Action("grab", "up"))
        space = next(e for e in self.keyboard.events if e[:2] == ("press", "space"))
        self.assertEqual(space[2], {"up"})
        self.keyboard.events = []
        self.executor.execute(Action("climb_exit"))
        self.assertEqual([e[1] for e in self.keyboard.events if e[0] == "press"], ["space"])

    def test_nudge_is_short_direction_pulse_without_f_or_jump_and_releases(self):
        self.executor.execute(Action("nudge", "left"))
        self.assertEqual([e[:2] for e in self.keyboard.events], [("press", "left"), ("release", "left")])
        self.assertAlmostEqual(self.clock.now, self.executor.c.ladder_nudge_seconds)
        self.assertLess(self.clock.now, .06)
        self.assertEqual(self.keyboard.held, set())

    def test_climb_holds_up_between_frames_then_releases_before_exit_jump(self):
        for _ in range(3):
            self.executor.execute(Action("climb", "up"))
        self.assertEqual(self.keyboard.held, {"up"})
        self.assertEqual([e[:2] for e in self.keyboard.events], [("press", "up")])
        self.executor.execute(Action("climb_exit"))
        space = next(e for e in self.keyboard.events if e[:2] == ("press", "space"))
        self.assertEqual(space[2], set())
        self.assertEqual(self.keyboard.held, set())

    def test_climb_focus_loss_releases_up(self):
        self.executor.execute(Action("climb", "up"))
        self.executor.active = lambda: False
        self.executor.guard_once()
        self.assertEqual(self.keyboard.held, set())

    def test_drop_does_not_press_space(self):
        self.executor.execute(Action("drop_walk", "left"))
        self.assertEqual([e[1] for e in self.keyboard.events if e[0] == "press"], ["left"])

    def test_focus_loss_during_chord_releases_everything(self):
        self.executor.active = lambda: self.clock.now < .05
        with self.assertRaises(InputCancelled):
            self.executor.execute(Action("teleport", "left"))
        self.assertEqual(self.keyboard.held, set())
        self.assertEqual(self.executor.held, set())

    def test_paused_before_action_never_presses(self):
        self.executor.active = lambda: False
        with self.assertRaises(InputCancelled):
            self.executor.execute(Action("attack"))
        self.assertFalse(self.keyboard.events)

    def test_backend_failure_still_releases_direction(self):
        real_press = self.keyboard.press

        def fail(key):
            if key == "f":
                raise RuntimeError("backend failed")
            real_press(key)

        self.keyboard.press = fail
        with self.assertRaises(RuntimeError):
            self.executor.execute(Action("teleport", "right"))
        self.assertEqual(self.keyboard.held, set())


if __name__ == "__main__":
    unittest.main()
