"""合成弹窗与模拟窗口验证预警和停键，不识别/操作测谎题目。"""
import io
import json
import tempfile
import threading
import time
import unittest
import wave
from contextlib import ExitStack, redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, call, patch

import cv2
import numpy as np

from lionbot.brain import Action
from lionbot.config import Config
from lionbot.controls import InputCancelled, InputExecutor
from lionbot.lie_alert import (AlertState, LieAlertMonitor, TitleDetector,
                              alert_wave, calibrate_alert, load_detector, sound_alert, watch_alert)
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
    def test_resized_titles_include_windows_dpi_sizes_and_non_integer_scale(self):
        detector = TitleDetector(title())
        for scale in (.75, .8, 1.25, 1.333, 1.5, 1.75, 2.):
            with self.subTest(scale=scale):
                actual = cv2.resize(title(), None, fx=scale, fy=scale, interpolation=cv2.INTER_LINEAR)
                frame = np.full((600, 800, 3), (170, 80, 20), np.uint8)
                h, w = actual.shape[:2]
                frame[221:221 + h, 333:333 + w] = actual
                hit = detector.find(frame)
                self.assertIsNotNone(hit, detector.last_report.describe())
                self.assertLessEqual(abs(hit[1][0] - 333), 2)
                self.assertLessEqual(abs(hit[1][1] - 221), 2)
                self.assertAlmostEqual(detector.last_report.scale, scale, delta=.03)

    def test_short_title_survives_tint_compression_and_antialiasing_changes(self):
        sample = cv2.resize(title(), (128, 23), interpolation=cv2.INTER_AREA)
        detector = TitleDetector(sample)
        tinted = cv2.add(sample, np.full_like(sample, (50, 5, 0)))
        encoded = cv2.imencode(".jpg", sample, [cv2.IMWRITE_JPEG_QUALITY, 55])[1]
        for actual in (tinted, cv2.GaussianBlur(sample, (3, 3), .8),
                       cv2.imdecode(encoded, cv2.IMREAD_COLOR)):
            frame = np.full((300, 500, 3), (170, 80, 20), np.uint8)
            frame[101:124, 137:265] = actual
            self.assertIsNotNone(detector.find(frame), detector.last_report.describe())

    def test_miss_reports_scores_and_does_not_reuse_previous_hit(self):
        detector = TitleDetector(title())
        self.assertIsNotNone(detector.find(scene()))
        self.assertIsNone(detector.find(scene(header=title("INVENTORY"))))
        self.assertFalse(detector.last_report.found)
        self.assertIn("未检出", detector.last_report.describe())
        self.assertGreater(detector.last_report.score, 0)
        self.assertLess(detector.last_report.score, detector.threshold)

    def test_random_game_and_occluded_title_are_not_alerts(self):
        detector = TitleDetector(title())
        occluded = title()
        occluded[:, :60] = 240
        rng = np.random.default_rng(123)
        for frame in (scene(header=occluded), rng.integers(0, 256, (600, 800, 3), dtype=np.uint8)):
            self.assertIsNone(detector.find(frame), detector.last_report.describe())

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
    def test_f10_saves_actual_capture_and_scores_without_triggering_alarm(self):
        window, alarm = Mock(), Mock()
        window.focused.return_value = True
        frame = scene(header=title("INVENTORY"))
        window.capture.return_value = frame
        with tempfile.TemporaryDirectory() as directory:
            monitor = LieAlertMonitor(TitleDetector(title()), (800, 600), lambda: window, alarm,
                                      interval=.02, sound=lambda: None, diagnostic_dir=Path(directory))
            monitor.request_snapshot()
            with redirect_stdout(io.StringIO()):
                monitor.start()
                try:
                    wait_until(lambda: bool(list(Path(directory).glob("*.json"))))
                    monitor.stop()
                    self.assertTrue(monitor.state.allows_input())
                finally:
                    monitor.stop()
            alarm.assert_not_called()
            metadata = json.loads(next(Path(directory).glob("*.json")).read_text(encoding="utf-8"))
            self.assertFalse(metadata["found"])
            self.assertEqual(metadata["reason"], "F10")
            self.assertIn("分数=", monitor.status())
            np.testing.assert_array_equal(read_image(next(Path(directory).glob("*-screen.png"))), frame)

    def test_failed_diagnostic_write_does_not_disable_monitor_or_latch_false_alarm(self):
        window, alarm = Mock(), Mock()
        window.focused.return_value = True
        window.capture.return_value = scene(header=title("INVENTORY"))
        monitor = LieAlertMonitor(TitleDetector(title()), (800, 600), lambda: window, alarm,
                                  interval=.02, sound=lambda: None, diagnostic_dir=Path("unused"))
        monitor.request_snapshot()
        with patch("lionbot.lie_alert.save_diagnostic", side_effect=OSError("disk full")), \
                redirect_stdout(io.StringIO()) as log:
            monitor.start()
            try:
                wait_until(lambda: "保存失败" in log.getvalue())
            finally:
                monitor.stop()
        self.assertFalse(monitor.state.is_latched())
        alarm.assert_not_called()

    def test_scaled_dialog_latches_and_rings_and_preserves_evidence(self):
        frame = np.full((600, 800, 3), (170, 80, 20), np.uint8)
        resized = cv2.resize(title(), (200, 35), interpolation=cv2.INTER_LINEAR)
        frame[101:136, 251:451] = resized
        window, alarm, sound = Mock(), threading.Event(), threading.Event()
        window.focused.return_value = True
        window.capture.return_value = frame
        with tempfile.TemporaryDirectory() as directory, redirect_stdout(io.StringIO()):
            monitor = LieAlertMonitor(TitleDetector(title()), (800, 600), lambda: window, lambda _: alarm.set(),
                                      interval=.02, sound=sound.set, diagnostic_dir=Path(directory))
            monitor.start()
            try:
                self.assertTrue(alarm.wait(1))
                self.assertTrue(sound.wait(1))
                wait_until(lambda: bool(list(Path(directory).glob("*.json"))))
            finally:
                monitor.stop()
            self.assertFalse(monitor.state.allows_input())
            metadata = json.loads(next(Path(directory).glob("*.json")).read_text(encoding="utf-8"))
            self.assertTrue(metadata["found"])
            self.assertEqual(metadata["reason"], "alarm")
            self.assertEqual(len(list(Path(directory).glob("*-screen.png"))), 1)

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

    def test_offline_miss_shows_scores_and_saves_best_candidate_not_just_a_boolean(self):
        self.calibrate()
        write_image(self.image, scene(header=title("INVENTORY")))
        with redirect_stdout(io.StringIO()) as log:
            result = main(["--config", str(self.path), "--check-alert", "--image", str(self.image)])
        self.assertEqual(result, 1)
        self.assertIn("未检出测谎标题：分数=", log.getvalue())
        self.assertIn("文字边缘=", log.getvalue())
        directory = self.root / "debug-output" / "lie-alert"
        metadata = json.loads(next(directory.glob("*.json")).read_text(encoding="utf-8"))
        self.assertFalse(metadata["found"])
        self.assertEqual(metadata["reason"], "offline")
        np.testing.assert_array_equal(read_image(next(directory.glob("*-template.png"))), title())

    def test_watch_command_runs_without_ready_station_or_game_inputs(self):
        self.path.write_text(json.dumps({"frame_size": [800, 600]}), encoding="utf-8")
        with patch("lionbot.lie_alert.watch_alert") as watch, patch("lionbot.simple.run_simple") as live:
            self.assertEqual(main(["--config", str(self.path), "--watch-alert"]), 0)
        watch.assert_called_once()
        live.assert_not_called()

    def test_live_watch_never_constructs_keyboard_controller(self):
        self.calibrate()
        module = ModuleType("pynput.keyboard")
        module.Key = SimpleNamespace(f8="f8", f10="f10", f12="f12")
        def listener_factory(**callbacks):
            listener = Mock()
            listener.start.side_effect = lambda: callbacks["on_release"](module.Key.f12)
            return listener
        module.Listener = Mock(side_effect=listener_factory)
        with patch.dict("sys.modules", {"pynput": ModuleType("pynput"), "pynput.keyboard": module}), \
                patch("lionbot.windows.require_windows"), patch("lionbot.windows.Keyboard") as keyboard, \
                patch("lionbot.lie_alert.LieAlertMonitor") as monitor, redirect_stdout(io.StringIO()):
            watch_alert(load_simple(self.path), self.path)
        keyboard.assert_not_called()
        monitor.return_value.start.assert_called_once()
        monitor.return_value.stop.assert_called_once()

    def test_sound_test_does_not_open_game(self):
        with patch("lionbot.lie_alert.sound_alert") as sound, patch("lionbot.simple.time.sleep"), \
                patch("lionbot.windows.GameWindow") as window, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--test-alert-sound"]), 0)
        self.assertEqual(sound.call_count, 3)
        window.assert_not_called()

    def test_windows_sound_uses_normal_wave_playback_for_remote_audio_capture(self):
        sound = ModuleType("winsound")
        sound.PlaySound, sound.Beep = Mock(), Mock()
        sound.SND_MEMORY, sound.SND_NODEFAULT = 4, 2
        with patch.dict("sys.modules", {"winsound": sound}):
            sound_alert()
        sound.PlaySound.assert_called_once_with(alert_wave(), 6)
        sound.Beep.assert_not_called()
        with wave.open(io.BytesIO(sound.PlaySound.call_args.args[0]), "rb") as audio:
            self.assertEqual(audio.getnchannels(), 1)
            self.assertEqual(audio.getsampwidth(), 2)
            self.assertEqual(audio.getframerate(), 22050)
            self.assertAlmostEqual(audio.getnframes() / audio.getframerate(), .4)
            samples = np.frombuffer(audio.readframes(audio.getnframes()), dtype="<i2")
        first, second = samples[:3969], samples[-3969:]
        for segment, expected in ((first, 1400), (second, 900)):
            frequencies = np.fft.rfftfreq(len(segment), 1 / 22050)
            actual = frequencies[np.argmax(np.abs(np.fft.rfft(segment)))]
            self.assertAlmostEqual(actual, expected, delta=6)

    def test_wave_playback_failure_falls_back_to_local_beeps(self):
        sound = ModuleType("winsound")
        sound.PlaySound, sound.Beep = Mock(side_effect=RuntimeError("audio device unavailable")), Mock()
        sound.SND_MEMORY, sound.SND_NODEFAULT = 4, 2
        with patch.dict("sys.modules", {"winsound": sound}):
            sound_alert()
        self.assertEqual(sound.Beep.call_count, 2)

    def test_all_windows_audio_failures_keep_terminal_bell_and_do_not_crash_alarm_thread(self):
        sound = ModuleType("winsound")
        sound.PlaySound = Mock(side_effect=RuntimeError("audio unavailable"))
        sound.Beep = Mock(side_effect=OSError("beep unavailable"))
        sound.SND_MEMORY, sound.SND_NODEFAULT = 4, 2
        with patch.dict("sys.modules", {"winsound": sound}), redirect_stdout(io.StringIO()) as log:
            sound_alert()
        self.assertIn("\a", log.getvalue())

    def test_sound_test_cli_supports_winsound_without_snd_sync(self):
        # 兼容支持范围内的旧版 Python，不给模拟模块补造可选的新常量。
        sound = ModuleType("winsound")
        sound.PlaySound, sound.Beep = Mock(), Mock()
        sound.SND_MEMORY, sound.SND_NODEFAULT = 4, 2
        self.assertFalse(hasattr(sound, "SND_SYNC"))
        with patch.dict("sys.modules", {"winsound": sound}), patch("lionbot.simple.time.sleep"), \
                patch("lionbot.windows.GameWindow") as window, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--test-alert-sound"]), 0)
        self.assertEqual(sound.PlaySound.call_count, 3)
        sound.Beep.assert_not_called()
        window.assert_not_called()

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
        window.hwnd = 123
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
            def __init__(self, _detector, _size, _factory, on_alarm, _interval, diagnostic_dir=None):
                self.state, self.callback = AlertState(max_age=10), on_alarm
                self.factory = _factory
                instances.append(self)
            def start(self):
                self.factory()
                self.state.publish(hit=True)
                self.callback("检测到测谎窗口")
            def stop(self):
                pass
            def status(self):
                return "测谎监控测试"
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
            windows = stack.enter_context(patch("lionbot.windows.GameWindow", return_value=window))
            stack.enter_context(patch("lionbot.windows.Keyboard", return_value=keyboard))
            stack.enter_context(patch("lionbot.lie_alert.load_detector", return_value=Mock()))
            stack.enter_context(patch("lionbot.lie_alert.LieAlertMonitor", Monitor))
            stack.enter_context(patch("lionbot.simple.time.sleep", side_effect=drive))
            stack.enter_context(redirect_stdout(io.StringIO()))
            run_simple(config(), Path(directory) / "config.simple.local.json", send_keys=True)
        self.assertEqual([e[1] for e in keyboard.events if e[0] == "press"], ["alt"])
        window.capture_region.assert_called_once()
        self.assertFalse(keyboard.held)
        self.assertEqual(windows.call_args_list, [call(config().window_title, frame_size=(800, 600)),
                                                   call(config().window_title, hwnd=123)])
