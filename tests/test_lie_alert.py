"""合成弹窗与模拟窗口验证预警和停键，不识别/操作测谎题目。"""
import io
import json
import tempfile
import threading
import time
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
from lionbot.lie_alert import (AlertState, LieAlertMonitor, TitleDetector,
                              calibrate_alert, load_detector, sound_alert)
from lionbot.simple import main, run_simple
from lionbot.simple_cycle import load_simple
from lionbot.vision import write_image, read_image
from tests.test_controls import Clock, Keyboard
from tests.test_simple import config


def title(text="LIE DETECTOR"):
    image = np.full((28, 160, 3), 240, np.uint8)
    cv2.putText(image, text, (3, 20), cv2.FONT_HERSHEY_SIMPLEX, .55, (10, 10, 10), 1, cv2.LINE_AA)
    return image


def scene(at=(250, 100), header=None, phase=0):
    frame = np.full((600, 800, 3), (160, 75, 20), np.uint8)
    x, y = at
    cv2.rectangle(frame, (x - 5, y - 5), (x + 300, y + 350), (190, 195, 195), -1)
    frame[y:y + 28, x:x + 160] = title() if header is None else header
    cv2.putText(frame, str(phase), (x + 100, y + 80), 0, 1, (0, 0, 0), 2)
    cv2.circle(frame, (x + 80 + phase * 20, y + 180), 25, (20, 150, 200), -1)
    return frame


def wait_until(predicate, timeout=1):
    end = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > end:
            raise AssertionError("监控线程未及时完成预期动作")
        time.sleep(.005)


class TitleDetectionTests(unittest.TestCase):
    def test_fixed_title_survives_dialog_moving_countdown_and_animated_content(self):
        detector = TitleDetector(title())
        for at, phase in (((250, 100), 0), ((350, 150), 1), ((10, 50), 2)):
            with self.subTest(at=at, phase=phase):
                hit = detector.find(scene(at, phase=phase))
                self.assertIsNotNone(hit)
                self.assertGreater(hit[0], .99)
                self.assertEqual(hit[1], (*at, 160, 28))

    def test_generic_gray_dialog_different_title_and_blank_game_are_not_alerts(self):
        detector = TitleDetector(title())
        for image in (scene(header=title("INVENTORY")), scene(header=title("QUEST STATUS")),
                      np.full((600, 800, 3), 200, np.uint8)):
            self.assertIsNone(detector.find(image))

    def test_roi_can_limit_search_and_does_not_match_outside_it(self):
        detector = TitleDetector(title(), roi=(200, 80, 400, 150))
        self.assertIsNotNone(detector.find(scene()))
        self.assertIsNone(detector.find(scene((10, 350))))

    def test_blank_and_full_dialog_samples_are_rejected(self):
        for sample in (np.full((28, 160, 3), 240, np.uint8), scene(), title()[:, :20]):
            with self.assertRaises(ValueError):
                TitleDetector(sample)


class AlarmStateTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.state = AlertState(clock=self.clock.time)

    def test_no_keys_before_first_scan_or_when_scans_are_stale(self):
        self.assertFalse(self.state.allows_input())
        self.state.publish()
        self.assertTrue(self.state.allows_input())
        self.clock.sleep(.76)
        self.assertFalse(self.state.allows_input())

    def test_first_hit_latches_immediately_and_f8_only_mutes_while_present(self):
        self.assertTrue(self.state.publish(hit=True))
        self.assertTrue(self.state.should_ring())
        self.assertFalse(self.state.allows_input())
        self.assertFalse(self.state.acknowledge())
        self.assertFalse(self.state.should_ring())
        self.assertTrue(self.state.is_latched())

    def test_disappearance_does_not_auto_resume_and_two_clear_frames_need_manual_f8(self):
        self.state.publish(hit=True)
        self.state.publish()
        self.assertFalse(self.state.acknowledge())
        self.state.publish()
        self.assertFalse(self.state.allows_input())
        self.assertTrue(self.state.acknowledge())
        self.assertTrue(self.state.allows_input())
        self.assertTrue(self.state.publish(hit=True))
        self.assertTrue(self.state.should_ring())  # 新一次弹窗重新发声。

    def test_focus_loss_faults_and_stale_frames_never_authorize_resume(self):
        self.state.publish(hit=True)
        self.state.publish()
        self.state.publish()
        self.assertFalse(self.state.acknowledge(focused=False))
        self.state.unavailable()
        self.assertFalse(self.state.acknowledge())
        self.state.publish(error="截图错误")
        self.assertFalse(self.state.allows_input())
        self.assertFalse(self.state.acknowledge())
        self.state.publish()
        self.state.publish()
        self.clock.sleep(1)
        self.assertFalse(self.state.acknowledge())


class MonitorTests(unittest.TestCase):
    def test_focus_change_during_capture_never_matches_another_app_or_authorizes_keys(self):
        window, detector = Mock(), Mock()
        calls = []
        def focused():
            calls.append(1)
            return len(calls) == 1
        window.focused.side_effect = focused
        window.capture.return_value = scene()
        alarm = Mock()
        monitor = LieAlertMonitor(detector, (800, 600), lambda: window, alarm,
                                  interval=.02, sound=lambda: None)
        monitor.start()
        try:
            wait_until(lambda: len(calls) >= 3)
            self.assertFalse(monitor.state.allows_input())
            detector.find.assert_not_called()
            alarm.assert_not_called()
        finally:
            monitor.stop()

    def test_independent_monitor_releases_held_key_and_rings_then_waits_for_human(self):
        hit, rang, alarm = threading.Event(), threading.Event(), threading.Event()
        detector = Mock()
        detector.find.side_effect = lambda _frame: (.99, (1, 1, 160, 28)) if hit.is_set() else None
        window = Mock()
        window.focused.return_value = True
        window.capture.return_value = scene()
        owner_threads = []
        def factory():
            owner_threads.append(threading.get_ident())
            return window
        keyboard = Keyboard(SimpleNamespace(time=time.monotonic))
        executor = InputExecutor(keyboard, Config())
        def stop_input(_reason):
            executor.stop()
            alarm.set()
        monitor = LieAlertMonitor(detector, (800, 600), factory, stop_input, interval=.02, sound=rang.set)
        executor.active = monitor.state.allows_input
        monitor.start()
        try:
            wait_until(monitor.state.allows_input)
            executor.execute(Action("walk", "left"))
            self.assertEqual(keyboard.held, {"left"})
            hit.set()
            self.assertTrue(alarm.wait(1))
            self.assertTrue(rang.wait(1))
            self.assertFalse(keyboard.held)
            with self.assertRaises(InputCancelled):
                executor.execute(Action("attack"))
            hit.clear()
            wait_until(lambda: monitor.state.clear_frames >= 2)
            self.assertFalse(monitor.state.allows_input())
            self.assertTrue(monitor.state.acknowledge())
            self.assertTrue(monitor.state.allows_input())
        finally:
            monitor.stop()
            executor.stop()
        window.close.assert_called_once()
        self.assertNotEqual(owner_threads[0], threading.get_ident())
        self.assertTrue(all(not thread.is_alive() for thread in monitor.threads))

    def test_capture_error_and_changed_size_latch_fault_instead_of_allowing_keys(self):
        for fault in (RuntimeError("capture failed"), np.zeros((100, 100, 3), np.uint8)):
            with self.subTest(fault=type(fault).__name__):
                window, alarm = Mock(), threading.Event()
                window.focused.return_value = True
                if isinstance(fault, Exception):
                    window.capture.side_effect = fault
                else:
                    window.capture.return_value = fault
                monitor = LieAlertMonitor(Mock(), (800, 600), lambda: window, lambda _: alarm.set(),
                                          interval=.02, sound=lambda: None)
                monitor.start()
                try:
                    self.assertTrue(alarm.wait(1))
                    self.assertFalse(monitor.state.allows_input())
                    self.assertIn("异常", monitor.state.reason)
                finally:
                    monitor.stop()


class AlertCommandsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.path = self.root / "config.simple.local.json"
        self.path.write_text(json.dumps(asdict(config())), encoding="utf-8")
        self.image = self.root / "lie.png"
        write_image(self.image, scene())

    def tearDown(self):
        self.temp.cleanup()

    def calibrate(self, roi=(250, 100, 160, 28)):
        with patch("lionbot.calibrate.select_roi", return_value=roi), \
                patch("lionbot.lie_alert.cv2.destroyAllWindows"), redirect_stdout(io.StringIO()):
            calibrate_alert(self.path, self.image)

    def test_saved_screenshot_calibration_preserves_station_and_existing_settings(self):
        before = load_simple(self.path)
        with patch("lionbot.windows.GameWindow") as window:
            self.calibrate()
        window.assert_not_called()
        self.assertEqual(load_simple(self.path), before)
        saved = read_image(self.root / before.lie_alert_template)
        np.testing.assert_array_equal(saved, title())
        self.assertIsNotNone(load_detector(before, self.root).find(scene()))

    def test_bad_selection_keeps_previous_template_and_configuration(self):
        self.calibrate()
        config_before = self.path.read_bytes()
        image_path = self.root / config().lie_alert_template
        image_before = image_path.read_bytes()
        with self.assertRaises(ValueError):
            self.calibrate((700, 500, 80, 30))
        self.assertEqual(self.path.read_bytes(), config_before)
        self.assertEqual(image_path.read_bytes(), image_before)

    def test_missing_template_has_actionable_error(self):
        with self.assertRaisesRegex(ValueError, "--calibrate-alert"):
            load_detector(config(), self.root)

    def test_offline_cli_checks_title_without_starting_game_or_keyboard(self):
        self.calibrate()
        log = io.StringIO()
        with patch("lionbot.simple.run_simple") as live, redirect_stdout(log):
            result = main(["--config", str(self.path), "--check-alert", "--image", str(self.image)])
        self.assertEqual(result, 0)
        self.assertIn("检出测谎标题", log.getvalue())
        live.assert_not_called()

    def test_sound_test_does_not_open_game(self):
        with patch("lionbot.lie_alert.sound_alert") as sound, patch("lionbot.simple.time.sleep"), \
                patch("lionbot.windows.GameWindow") as window, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--test-alert-sound"]), 0)
        self.assertEqual(sound.call_count, 3)
        window.assert_not_called()

    def test_windows_sound_uses_two_beeps(self):
        sound = ModuleType("winsound")
        sound.Beep = Mock()
        with patch.dict("sys.modules", {"winsound": sound}):
            sound_alert()
        self.assertEqual(sound.Beep.call_count, 2)

    def test_bad_monitor_configuration_is_rejected(self):
        for changes in ({"lie_alert_enabled": "true"}, {"lie_alert_interval": 0},
                        {"lie_alert_threshold": 2}, {"lie_alert_template": ""},
                        {"lie_alert_roi": (0, 0, 801, 100)}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                config(**changes).validate()


class LiveAlertTests(unittest.TestCase):
    def test_missing_template_prevents_window_and_keyboard_startup(self):
        module = ModuleType("pynput.keyboard")
        module.Key, module.Listener = Mock(), Mock()
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"pynput": ModuleType("pynput"), "pynput.keyboard": module}))
            stack.enter_context(patch("lionbot.windows.require_windows"))
            window = stack.enter_context(patch("lionbot.windows.GameWindow"))
            keyboard = stack.enter_context(patch("lionbot.windows.Keyboard"))
            with self.assertRaisesRegex(ValueError, "--calibrate-alert"):
                run_simple(config(), Path(directory) / "config.simple.local.json", send_keys=True)
        window.assert_not_called()
        keyboard.assert_not_called()

    def test_alarm_blocks_keyboard_until_title_gone_and_user_presses_f8(self):
        window = Mock()
        window.focused.return_value = True
        image = np.zeros((100, 200, 3), np.uint8)
        cv2.rectangle(image, (98, 48), (102, 52), (0, 255, 255), -1)
        window.capture_region.return_value = image
        keyboard = Keyboard(SimpleNamespace(time=time.monotonic))
        keys = SimpleNamespace(f8="f8", f9="f9", f12="f12")
        module = ModuleType("pynput.keyboard")
        module.Key, module.Listener = keys, Mock(return_value=Mock())
        instances = []
        class Monitor:
            def __init__(self, _detector, _size, _factory, on_alarm, _interval):
                self.state, self.callback = AlertState(max_age=10), on_alarm
                instances.append(self)
            def start(self):
                self.state.publish(hit=True)
                self.callback("检测到测谎窗口")
            def stop(self):
                pass
        ticks = []
        def drive(_delay):
            ticks.append(1)
            callbacks = module.Listener.call_args.kwargs
            if len(ticks) == 1:
                self.assertFalse(keyboard.events)
                callbacks["on_release"](keys.f8)  # 弹窗仍在，仅静音。
            elif len(ticks) == 2:
                self.assertFalse(keyboard.events)
                self.assertTrue(instances[0].state.is_latched())
                instances[0].state.publish()
                instances[0].state.publish()
            elif len(ticks) == 3:
                self.assertFalse(keyboard.events)  # 消失后仍不自动恢复。
                callbacks["on_release"](keys.f8)
            else:
                callbacks["on_press"](keys.f12)
        with tempfile.TemporaryDirectory() as directory, ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"pynput": ModuleType("pynput"), "pynput.keyboard": module}))
            stack.enter_context(patch("lionbot.windows.require_windows"))
            stack.enter_context(patch("lionbot.windows.GameWindow", return_value=window))
            stack.enter_context(patch("lionbot.windows.Keyboard", return_value=keyboard))
            stack.enter_context(patch("lionbot.lie_alert.load_detector", return_value=Mock()))
            stack.enter_context(patch("lionbot.lie_alert.LieAlertMonitor", Monitor))
            stack.enter_context(patch("lionbot.simple.time.sleep", side_effect=drive))
            stack.enter_context(redirect_stdout(io.StringIO()))
            run_simple(config(), Path(directory) / "config.simple.local.json", send_keys=True)
        self.assertEqual([e[1] for e in keyboard.events if e[0] == "press"], ["alt"])
        window.capture_region.assert_called_once()
        self.assertFalse(keyboard.held)
