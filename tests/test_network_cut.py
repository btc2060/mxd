"""Network operations are ALWAYS mocked; tests never disable a real adapter."""
import base64
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
from types import ModuleType, SimpleNamespace
import unittest
from contextlib import ExitStack, redirect_stdout, redirect_stderr
from unittest.mock import Mock, patch

from lionbot.brain import Action
from lionbot.garden import main, run_garden
from lionbot.lie_alert import AlertState
from lionbot.network_cut import NetworkSwitch, normalize_adapters, powershell, require_admin, save_record
from tests.test_garden import config, observation


def adapter(number, name="Ethernet", status="Up", admin="Up"):
    return {"guid": f"00000000-0000-0000-0000-{number:012d}", "name": name, "status": status, "admin": admin}


class NetworkSwitchTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.path = self.root / "network-cut.json"
        self.rows = [adapter(1, "以太网"), adapter(2, "Wi-Fi"), adapter(3, "原本禁用", "Disabled", "Down")]
        self.commands = []

    def command(self, script):
        self.commands.append(script)
        if "Disable-NetAdapter" in script:
            record = json.loads(self.path.read_text(encoding="utf-8"))
            self.assertEqual(record["state"], "pending")
            self.assertEqual({a["guid"] for a in record["adapters"]}, {a["guid"] for a in self.rows[:2]})
            return {"adapters": [dict(a, admin="Down", status="Disabled") for a in self.rows[:2]], "errors": []}
        if "Enable-NetAdapter" in script:
            return {"adapters": [dict(a, status="Disconnected") for a in self.rows[:2]], "errors": []}
        self.assertIn("Get-NetAdapter -Physical", script)
        return self.rows

    def switch(self, command=None, **kwargs):
        return NetworkSwitch(self.path, command=command or self.command, check_admin=lambda: None,
                             computer="TEST-PC", **kwargs)

    def test_prepare_is_read_only_and_selects_all_connected_physical_adapters(self):
        selected = self.switch().prepare()
        self.assertEqual([a["name"] for a in selected], ["以太网", "Wi-Fi"])
        self.assertFalse(self.path.exists())
        self.assertTrue(all("Disable-NetAdapter" not in s for s in self.commands))

    def test_disable_saves_recovery_before_changing_and_restore_only_enables_recorded_adapters(self):
        switch = self.switch()
        with redirect_stdout(io.StringIO()):
            switch.disconnect()
            self.assertEqual(json.loads(self.path.read_text())["state"], "disconnected")
            switch.restore()
        self.assertEqual(json.loads(self.path.read_text())["state"], "restored")
        for script in self.commands:
            if "-NetAdapter -Confirm" in script:
                self.assertNotIn(self.rows[2]["guid"], script)
        count = len(self.commands)
        with redirect_stdout(io.StringIO()):
            switch.restore()
        self.assertEqual(len(self.commands), count)

    def test_pending_record_cannot_be_overwritten_by_another_disconnect(self):
        with redirect_stdout(io.StringIO()):
            self.switch().disconnect()
        before = self.path.read_bytes()
        with self.assertRaisesRegex(ValueError, "尚未恢复"):
            self.switch().disconnect()
        self.assertEqual(self.path.read_bytes(), before)

    def test_snapshot_write_failure_never_disables_any_adapter(self):
        with patch("lionbot.network_cut.save_record", side_effect=OSError("disk")), self.assertRaises(OSError):
            self.switch().disconnect()
        self.assertFalse(any("Disable-NetAdapter" in s for s in self.commands))

    def test_partial_disable_is_reported_and_keeps_all_recovery_guids(self):
        def partial(script):
            if "Disable-NetAdapter" in script:
                return {"adapters": [dict(self.rows[0], admin="Down", status="Disabled"), self.rows[1]],
                        "errors": ["Wi-Fi: denied"]}
            return self.command(script)
        with self.assertRaisesRegex(RuntimeError, "部分网卡"), redirect_stdout(io.StringIO()):
            self.switch(partial).disconnect()
        record = json.loads(self.path.read_text())
        self.assertEqual(record["state"], "failed")
        self.assertEqual(len(record["adapters"]), 2)
        with redirect_stdout(io.StringIO()):
            self.switch().restore()
        self.assertEqual(json.loads(self.path.read_text())["state"], "restored")

    def test_command_crash_after_write_leaves_pending_record_for_manual_restore(self):
        def crash(script):
            if "Disable-NetAdapter" in script:
                raise RuntimeError("PowerShell timeout")
            return self.command(script)
        with self.assertRaises(RuntimeError), redirect_stdout(io.StringIO()):
            self.switch(crash).disconnect()
        self.assertEqual(json.loads(self.path.read_text())["state"], "pending")

    def test_post_disable_write_failure_keeps_initial_record_recoverable(self):
        writes = []
        def write(path, value):
            writes.append(value["state"])
            if len(writes) == 2:
                raise OSError("disk full")
            save_record(path, value)
        with patch("lionbot.network_cut.save_record", side_effect=write), \
                self.assertRaises(OSError), redirect_stdout(io.StringIO()):
            self.switch().disconnect()
        self.assertEqual(json.loads(self.path.read_text())["state"], "pending")
        with redirect_stdout(io.StringIO()):
            self.switch().restore()
        self.assertEqual(json.loads(self.path.read_text())["state"], "restored")

    def test_no_connected_adapter_does_not_replace_existing_recovery_data(self):
        self.rows = [adapter(3, "原本禁用", "Disabled", "Down")]
        with self.assertRaisesRegex(ValueError, "没有已连接"):
            self.switch().disconnect()
        self.assertFalse(self.path.exists())

    def test_restore_rejects_foreign_computer_or_malformed_guid_before_any_change(self):
        with redirect_stdout(io.StringIO()):
            self.switch().disconnect()
        record = json.loads(self.path.read_text())
        for change in ({"computer": "ANOTHER-PC"}, {"adapters": [dict(adapter(1), guid="';Remove-Item*")]}):
            self.path.write_text(json.dumps({**record, **change}), encoding="utf-8")
            count = len(self.commands)
            with self.assertRaises(ValueError):
                self.switch().restore()
            self.assertEqual(len(self.commands), count)

    def test_adapter_names_cannot_be_evaluated_as_powershell(self):
        self.rows[0]["name"] = "NIC';Write-Output SECRET;#"
        with redirect_stdout(io.StringIO()):
            self.switch().disconnect()
        self.assertFalse(any("SECRET" in script for script in self.commands))
        with self.assertRaises(ValueError):
            normalize_adapters([adapter(1), adapter(1)])

    def test_privilege_check_precedes_inventory_and_changes(self):
        command = Mock()
        switch = NetworkSwitch(self.path, command=command, check_admin=Mock(side_effect=RuntimeError("admin")))
        with self.assertRaisesRegex(RuntimeError, "admin"):
            switch.prepare()
        command.assert_not_called()
        with patch("lionbot.network_cut.sys.platform", "linux"), self.assertRaisesRegex(RuntimeError, "仅支持 Windows"):
            require_admin()

    def test_powershell_uses_encoded_script_and_utf8_without_shell_evaluation(self):
        result = SimpleNamespace(returncode=0, stdout='[{"name":"以太网"}]'.encode(), stderr=b"")
        with patch("lionbot.network_cut.subprocess.run", return_value=result) as process:
            self.assertEqual(powershell("Write-Output 'test'"), [{"name": "以太网"}])
        call = process.call_args
        self.assertNotIn("shell", call.kwargs)
        script = base64.b64decode(call.args[0][-1]).decode("utf-16le")
        self.assertIn("Write-Output 'test'", script)
        self.assertEqual(call.kwargs["timeout"], 30)
        with patch("lionbot.network_cut.subprocess.run", side_effect=subprocess.TimeoutExpired("PS", 30)):
            with self.assertRaisesRegex(RuntimeError, "restorenet"):
                powershell("test")


class CutNetCliTests(unittest.TestCase):
    def test_testcutnet_does_not_load_config_or_create_game_keyboard(self):
        with ExitStack() as stack:
            switch = stack.enter_context(patch("lionbot.network_cut.NetworkSwitch")).return_value
            switch.prepare.return_value = [adapter(1)]
            stack.enter_context(patch("lionbot.garden.time.sleep"))
            loader = stack.enter_context(patch("lionbot.garden.load_garden"))
            window = stack.enter_context(patch("lionbot.windows.GameWindow"))
            keyboard = stack.enter_context(patch("lionbot.windows.Keyboard"))
            stack.enter_context(redirect_stdout(io.StringIO()))
            self.assertEqual(main(["--testcutnet"]), 0)
        switch.prepare.assert_called_once()
        switch.disconnect.assert_called_once()
        switch.restore.assert_not_called()
        loader.assert_not_called()
        window.assert_not_called()
        keyboard.assert_not_called()

    def test_manual_restore_does_not_run_game_or_disable_network(self):
        with patch("lionbot.network_cut.NetworkSwitch") as factory, \
                patch("lionbot.garden.load_garden") as loader, redirect_stdout(io.StringIO()):
            self.assertEqual(main(["--restorenet"]), 0)
        factory.return_value.restore.assert_called_once()
        factory.return_value.disconnect.assert_not_called()
        loader.assert_not_called()

    def test_cutnet_requires_explicit_run(self):
        for args in (["--cutnet"], ["--observe", "--cutnet"], ["--setup", "--cutnet"]):
            with self.subTest(args=args), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit) as error:
                main(args)
            self.assertEqual(error.exception.code, 2)


class CutNetRuntimeTests(unittest.TestCase):
    def run_case(self, mode="hit", enabled=True, disconnect_error=False):
        events, held, monitors, ticks = [], set(), [], []
        window, vision, brain = Mock(), Mock(), Mock()
        window.focused.return_value, window.hwnd = True, 123
        vision.observe.return_value = observation()
        brain.recovering, brain.completed_round_trips = False, 0
        brain.direction, brain.facing = "right", "right"
        brain.decide.return_value = Action("walk", "right", "test")
        executor = Mock()
        def execute(action):
            events.append("execute:" + action.kind)
            if action.kind == "walk":
                held.add(action.direction)
            return time.monotonic()
        def stop():
            events.append("stop")
            held.clear()
        executor.execute.side_effect, executor.stop.side_effect = execute, stop
        keys = SimpleNamespace(f8="f8", f10="f10", f12="f12")
        module = ModuleType("pynput.keyboard")
        module.Key, module.Listener = keys, Mock(return_value=Mock())
        switch = Mock()
        switch.prepare.return_value = [adapter(1)]
        def disconnect():
            self.assertFalse(held)
            events.append("disconnect")
            if disconnect_error:
                raise RuntimeError("network failure")
        switch.disconnect.side_effect = disconnect

        class Monitor:
            def __init__(self, _detector, _size, _factory, callback, *args, **kwargs):
                self.state, self.callback = AlertState(max_age=10), callback
                self.sound = kwargs["sound"]
                monitors.append(self)
            def start(self):
                self.state.publish()
                self.state.publish()
                if mode in ("fault", "fault_then_hit"):
                    self.state.publish(error="截图失败")
                    self.sound()
                    self.callback("截图失败")
            def stop(self):
                events.append("monitor-stop")
            def status(self):
                return "test"

        def drive(_):
            ticks.append(1)
            monitor = monitors[0]
            if len(ticks) == 1 and mode in ("hit", "hit_then_fault", "fault_then_hit"):
                if mode.startswith("hit"):
                    self.assertTrue(held)
                new = monitor.state.publish(hit=True)
                monitor.sound()  # Before callback dispatch: hit must already be silent.
                if new:
                    monitor.callback("检测到测谎窗口")
                if mode == "hit_then_fault":
                    monitor.state.publish(error="随后截图失败")
                    monitor.callback("随后截图失败")
                    monitor.sound()
            else:
                module.Listener.call_args.kwargs["on_press"](keys.f12)

        with ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"pynput": ModuleType("pynput"), "pynput.keyboard": module}))
            stack.enter_context(patch("lionbot.windows.require_windows"))
            stack.enter_context(patch("lionbot.windows.GameWindow", return_value=window))
            stack.enter_context(patch("lionbot.windows.Keyboard"))
            stack.enter_context(patch("lionbot.garden.InputExecutor", return_value=executor))
            stack.enter_context(patch("lionbot.garden.GardenVision", return_value=vision))
            stack.enter_context(patch("lionbot.garden.GardenCycle", return_value=brain))
            stack.enter_context(patch("lionbot.lie_alert.load_detector", return_value=Mock()))
            stack.enter_context(patch("lionbot.lie_alert.LieAlertMonitor", Monitor))
            factory = stack.enter_context(patch("lionbot.network_cut.NetworkSwitch", return_value=switch))
            beep = stack.enter_context(patch("lionbot.lie_alert.beep_alert"))
            stack.enter_context(patch("lionbot.garden.time.sleep", side_effect=drive))
            stack.enter_context(redirect_stdout(io.StringIO()))
            if disconnect_error:
                with self.assertRaisesRegex(RuntimeError, "network failure"):
                    run_garden(config(), Path("config.garden.local.json").resolve(), True, cutnet=enabled)
            else:
                run_garden(config(), Path("config.garden.local.json").resolve(), True, cutnet=enabled)
        self.assertFalse(held)
        window.close.assert_called_once()
        return events, switch, beep, factory

    def test_hit_releases_held_arrow_before_single_disconnect_and_never_beeps(self):
        events, switch, beep, _ = self.run_case()
        switch.disconnect.assert_called_once()
        switch.restore.assert_not_called()
        beep.assert_not_called()
        self.assertEqual(events[events.index("disconnect") - 1], "stop")
        self.assertFalse(any(e.startswith("execute:") for e in events[events.index("disconnect"):]))

    def test_plain_capture_fault_does_not_disconnect_and_remains_audible(self):
        _, switch, beep, _ = self.run_case("fault")
        switch.disconnect.assert_not_called()
        beep.assert_called()

    def test_no_beep_is_added_if_capture_errors_after_disconnect_is_requested(self):
        _, switch, beep, _ = self.run_case("hit_then_fault")
        switch.disconnect.assert_called_once()
        beep.assert_not_called()

    def test_hit_after_existing_fault_still_disconnects_without_a_second_alarm_callback(self):
        _, switch, beep, _ = self.run_case("fault_then_hit")
        switch.disconnect.assert_called_once()
        self.assertEqual(beep.call_count, 1)  # Only the earlier fault, not the later detected title.

    def test_without_flag_original_sound_pause_is_preserved_and_no_network_switch_is_created(self):
        _, switch, beep, factory = self.run_case(enabled=False)
        factory.assert_not_called()
        switch.disconnect.assert_not_called()
        beep.assert_called_once()

    def test_failed_disconnect_still_closes_game_capture_and_stops_monitor(self):
        events, switch, _, _ = self.run_case(disconnect_error=True)
        switch.disconnect.assert_called_once()
        self.assertIn("monitor-stop", events)


if __name__ == "__main__":
    unittest.main()
