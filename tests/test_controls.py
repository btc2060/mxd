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

    def test_grab_is_up_and_space_and_exit_is_only_space(self):
        self.executor.execute(Action("grab", "up"))
        space = next(e for e in self.keyboard.events if e[:2] == ("press", "space"))
        self.assertEqual(space[2], {"up"})
        self.keyboard.events = []
        self.executor.execute(Action("climb_exit"))
        self.assertEqual([e[1] for e in self.keyboard.events if e[0] == "press"], ["space"])

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
