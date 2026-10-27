"""LAN reliability, durable alarms and the independent Garden input gate."""
import io
import hashlib
import json
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from contextlib import ExitStack, redirect_stdout
from dataclasses import replace
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock, patch

from lionfleet.config import (MachineConfig, ReceiverConfig, RegisteredMachine, atomic_json,
                              load_machine, load_receiver)
from lionfleet.protocol import decode, encode, receipt
from lionfleet.receiver import ReceiverServer
from lionfleet.speech import Announcer
from lionfleet.store import ReceiverStore
from lionfleet.transport import FleetClient


def packet(seq=1, boot=None, alarms=(), resolved=(), phase="running", machine="M01"):
    return {"v": 1, "type": "status", "machine_id": machine, "boot_id": boot or "a" * 32,
            "seq": seq, "phase": phase, "alarms": list(alarms), "resolved": list(resolved),
            "loop_age": .1, "scan_age": .1}


def alarm(kind="lie_detector"):
    return {"id": uuid.uuid4().hex, "kind": kind, "reason": "测试"}


def receiver_config(**changes):
    return replace(ReceiverConfig(machines=(RegisteredMachine("M01", "1号机", 1),
                                           RegisteredMachine("M02", "2号机", 2))), **changes)


def free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def wait_until(predicate, timeout=3):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        threading.Event().wait(.02)
    return False


class FleetTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.addCleanup(self.temporary.cleanup)

    def server(self, **changes):
        c = receiver_config(bind_ip="127.0.0.1", port=free_port(), **changes)
        result = ReceiverServer(c, self.root / "receiver")
        result.start()
        self.addCleanup(result.stop)
        return result

    def client(self, server, machine_id="M01", folder="agent", **changes):
        c = MachineConfig(machine_id=machine_id, receiver_ip="127.0.0.1", receiver_port=server.address[1],
                          heartbeat_seconds=.1, receiver_timeout_seconds=1., startup_timeout_seconds=1.)
        client = FleetClient(replace(c, **changes), self.root / folder)
        client.start()
        self.addCleanup(client.close)
        return client


class FleetProtocolTests(FleetTestCase):
    def test_config_examples_load_and_do_not_require_game_dependencies(self):
        self.assertEqual(load_machine("config.machine.example.json").machine_id, "M01")
        self.assertEqual(len(load_receiver("config.receiver.example.json").machines), 8)
        for changes in ({"receiver_timeout_seconds": 1}, {"machine_id": "../1"},
                        {"heartbeat_seconds": True}, {"kvm_port": True}, {"loop_timeout_seconds": float("nan")}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                replace(MachineConfig(), **changes).validate()

    def test_protocol_rejects_oversize_non_status_and_invalid_fields(self):
        self.assertEqual(decode(encode(packet())), packet())
        for value in (b"x" * 9000, b"not json", b"[]", encode({**packet(), "type": "keypress"}),
                      encode({**packet(), "seq": True}), encode({**packet(), "boot_id": "bad"}),
                      encode({**packet(), "alarms": [{"id": "a" * 32, "kind": "command", "reason": "d"}]})):
            with self.subTest(value=value[:80]), self.assertRaises(ValueError):
                decode(value)
        self.assertTrue(decode(encode(receipt(packet())))["ok"])

    def test_pending_alarms_survive_agent_restart_and_clear_only_expected_event(self):
        c = MachineConfig()
        client = FleetClient(c, self.root)
        old_id = client.raise_alarm("lie_detector", "检测到测谎窗口")
        restored = FleetClient(c, self.root)
        self.assertEqual(restored.pending_alarms()[0]["id"], old_id)
        restored.clear_alarms(expected_ids={"f" * 32})
        self.assertEqual(len(restored.pending_alarms()), 1)
        restored.clear_alarms(expected_ids={old_id})
        self.assertFalse(FleetClient(c, self.root).pending_alarms())
        self.assertIn(old_id, FleetClient(c, self.root).resolved)

    def test_main_loop_stall_is_latched_even_if_udp_thread_can_keep_running(self):
        now = [0.]
        callbacks = []
        client = FleetClient(MachineConfig(), self.root, clock=lambda: now[0],
                             on_emergency=lambda kind, reason: callbacks.append((kind, client.allows_input())))
        client.last_ack = 0
        client.arm_runtime()
        now[0] = 8.1
        client._health()
        self.assertEqual(callbacks, [("runtime_fault", False)])
        client.touch_runtime()
        self.assertFalse(client.allows_input())  # Progress does not silently clear the latch.

    def test_unknown_config_keys_and_duplicate_machine_ids_are_rejected(self):
        path = self.root / "machine.json"
        atomic_json(path, {"machine_id": "M01", "unexpected": 1})
        with self.assertRaises(ValueError):
            load_machine(path)
        with self.assertRaises(ValueError):
            receiver_config(machines=(RegisteredMachine("M01", "1", 1), RegisteredMachine("M01", "2", 2))).validate()

    def test_corrupt_pending_alarm_files_fail_closed_instead_of_being_discarded(self):
        c = MachineConfig()
        path = self.root / "state" / "agent-M01.json"
        atomic_json(path, {"machine_id": "M01", "alarms": [None], "resolved": []})
        with self.assertRaises(ValueError):
            FleetClient(c, self.root)
        atomic_json(self.root / "state" / "receiver.json", {"version": 1, "nodes": {"M01": {"alarms": {}, "retired": [], "resolved": []}}})
        with self.assertRaises(ValueError):
            ReceiverStore(receiver_config(), self.root)


class FleetStoreTests(FleetTestCase):
    def test_duplicates_and_old_packets_cannot_clear_alarm_or_refresh_last_seen(self):
        now = [0.]
        store = ReceiverStore(receiver_config(), self.root, clock=lambda: now[0])
        item = alarm()
        store.apply(packet(2, alarms=[item]), ("127.0.0.1", 2000))
        now[0] = 20
        self.assertTrue(store.apply(packet(1, resolved=[item["id"]]), ("127.0.0.1", 2000))[0])
        self.assertEqual(len(store.rows()[0]["alarms"]), 1)
        self.assertFalse(store.rows()[0]["online"])
        store.apply(packet(3, resolved=[item["id"]]), ("127.0.0.1", 2000))
        self.assertFalse(store.rows()[0]["alarms"])
        store.apply(packet(4, alarms=[item]), ("127.0.0.1", 2000))
        self.assertFalse(store.rows()[0]["alarms"])

    def test_empty_heartbeat_and_process_restart_do_not_resolve_old_alarm(self):
        store = ReceiverStore(receiver_config(), self.root)
        item = alarm()
        store.apply(packet(1, alarms=[item]), ("127.0.0.1", 2000))
        store.apply(packet(2, phase="stopped"), ("127.0.0.1", 2000))
        store.apply(packet(1, boot="b" * 32), ("127.0.0.1", 2001))
        self.assertEqual(len(store.rows()[0]["alarms"]), 1)
        self.assertFalse(store.apply(packet(99, resolved=[item["id"]]), ("127.0.0.1", 2000))[0])

    def test_receiver_restart_recovers_unresolved_alarm_and_resets_local_mute(self):
        c = receiver_config()
        store = ReceiverStore(c, self.root)
        store.apply(packet(alarms=[alarm()]), ("127.0.0.1", 2000))
        store.mute_machine("M01")
        self.assertFalse(store.notifications())
        restored = ReceiverStore(c, self.root)
        self.assertEqual(len(restored.rows()[0]["alarms"]), 1)
        self.assertTrue(restored.notifications())
        self.assertFalse(restored.rows()[0]["online"])

    def test_new_alarm_after_mute_is_audible_and_multiple_machines_are_fair(self):
        store = ReceiverStore(receiver_config(), self.root)
        first, second = alarm(), alarm("monitor_fault")
        store.apply(packet(alarms=[first]), ("127.0.0.1", 2000))
        store.mute_machine("M01")
        store.apply(packet(2, alarms=[first, second]), ("127.0.0.1", 2000))
        store.apply(packet(machine="M02", alarms=[alarm()]), ("127.0.0.1", 2001))
        announcer = Announcer(store, self.root)
        self.assertEqual(announcer.next_notice(0)["key"], ("M01", second["id"]))
        self.assertEqual(announcer.next_notice(.1)["key"][0], "M02")
        self.assertIsNone(announcer.next_notice(.2))

    def test_disk_failure_is_not_acknowledged_as_success(self):
        store = ReceiverStore(receiver_config(), self.root)
        with patch.object(store, "_save", side_effect=OSError("disk")):
            self.assertFalse(store.apply(packet(alarms=[alarm()]), ("127.0.0.1", 2000))[0])
        self.assertTrue(store.apply(packet(), ("127.0.0.1", 2000))[0])
        self.assertFalse(store.persistence_error)

    def test_muting_one_outage_does_not_silence_a_later_outage(self):
        now = [0.]
        store = ReceiverStore(receiver_config(), self.root, clock=lambda: now[0])
        store.apply(packet(), ("127.0.0.1", 2000))
        now[0] = 11
        first_key = store.notifications()[0]["key"]
        store.mute_machine("M01")
        self.assertFalse(store.notifications())
        now[0] = 12
        store.apply(packet(2), ("127.0.0.1", 2000))
        now[0] = 23
        second_key = store.notifications()[0]["key"]
        self.assertNotEqual(first_key, second_key)


class FleetUdpTests(FleetTestCase):
    def test_real_udp_alarm_ack_and_explicit_resolution(self):
        server = self.server()
        client = self.client(server)
        self.assertTrue(client.wait_ready())
        client.raise_alarm("lie_detector", "检测到测谎窗口")
        self.assertTrue(client.flush())
        self.assertEqual(server.store.rows()[0]["alarms"][0]["kind"], "lie_detector")
        self.assertFalse(client.allows_input())  # ACK is NOT permission to resume.
        client.clear_alarms()
        self.assertTrue(client.flush())
        self.assertFalse(server.store.rows()[0]["alarms"])
        self.assertTrue(client.allows_input())

    def test_lost_first_ack_is_retried_with_same_event(self):
        server = self.server()
        original = server.socket
        sends = []
        class DropFirstAck:
            def __getattr__(self, name):
                return getattr(original, name)
            def sendto(self, data, address):
                sends.append(decode(data)["seq"])
                if len(sends) == 1:
                    return len(data)
                return original.sendto(data, address)
        server.socket = DropFirstAck()
        client = self.client(server, heartbeat_seconds=1, receiver_timeout_seconds=3)
        self.assertTrue(client.wait_ready(2))
        self.assertGreaterEqual(len(sends), 2)
        self.assertEqual(sends[:2], [1, 1])

    def test_receiver_outage_latches_pause_then_recovers_alarm_after_restart(self):
        server = self.server()
        client = self.client(server)
        self.assertTrue(client.wait_ready())
        server.stop()
        self.assertTrue(wait_until(lambda: any(a["kind"] == "network_lost" for a in client.pending_alarms())))
        self.assertFalse(client.allows_input())
        replacement = ReceiverServer(server.c, self.root / "receiver")
        replacement.start()
        self.addCleanup(replacement.stop)
        self.assertTrue(wait_until(client.connected))
        self.assertTrue(client.flush())
        self.assertTrue(replacement.store.rows()[0]["alarms"])
        self.assertFalse(client.allows_input())  # Restored connection still requires local F8.
        client.clear_alarms()
        self.assertTrue(client.flush())
        self.assertTrue(client.allows_input())

    def test_unregistered_or_duplicate_live_machine_id_cannot_handshake(self):
        server = self.server()
        good = self.client(server)
        self.assertTrue(good.wait_ready())
        duplicate = self.client(server, folder="duplicate")
        self.assertFalse(duplicate.wait_ready(.3))
        self.assertIn("在线进程", duplicate.last_error)
        unknown = self.client(server, machine_id="M99", folder="unknown")
        self.assertFalse(unknown.wait_ready(.3))
        self.assertIn("未登记", unknown.last_error)

    def test_two_local_senders_same_id_are_rejected_before_start(self):
        server = self.server()
        first = self.client(server)
        second = FleetClient(first.c, self.root / "agent")
        with self.assertRaisesRegex(RuntimeError, "已经在运行"):
            second.start()

    def test_standard_library_only_check_command_works_in_separate_process(self):
        server = self.server()
        path = self.root / "config.machine.local.json"
        atomic_json(path, {"receiver_ip": "127.0.0.1", "receiver_port": server.address[1], "startup_timeout_seconds": 2})
        result = subprocess.run([sys.executable, "-S", "-m", "lionfleet.garden", "--machine-config", str(path), "--check-connection"],
                                capture_output=True, text=True, timeout=5)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("连接成功", result.stdout)


class FleetPackageTests(FleetTestCase):
    def test_role_archives_have_verified_sources_and_no_private_config_or_templates(self):
        from tools.package_fleet import build
        repo = Path(__file__).resolve().parent.parent
        for role in ("receiver", "agent"):
            with self.subTest(role=role), redirect_stdout(io.StringIO()):
                build(repo, self.root, role)
                with zipfile.ZipFile(self.root / f"mxd-fleet-{role}.zip") as archive:
                    prefix = f"mxd-{role}/"
                    manifest = json.loads(archive.read(prefix + "BUILD.json"))
                    for name, digest in manifest["files"].items():
                        self.assertEqual(hashlib.sha256(archive.read(prefix + name)).hexdigest(), digest)
                        self.assertNotIn(".local.json", name)
                        self.assertFalse(name.startswith(("calibration/", "state/", "logs/", ".venv/")))
                    self.assertEqual(any(name.startswith(prefix + "lionbot/") for name in archive.namelist()), role == "agent")
                    if role == "receiver":
                        archive.extractall(self.root / "unpacked")
                        result = subprocess.run([sys.executable, "-S", "-m", "lionfleet.receiver", "--help"],
                                                cwd=self.root / "unpacked" / "mxd-receiver", capture_output=True, text=True, timeout=5)
                        self.assertEqual(result.returncode, 0, result.stderr)


class FleetGardenRuntimeTests(FleetTestCase):
    def test_alarm_stops_before_sending_and_only_local_f8_clears_it(self):
        from lionbot.lie_alert import AlertState
        from lionfleet.garden import run_garden
        from tests.test_garden import config, observation

        events, ticks, clients, monitors = [], [], [], []
        window, vision = Mock(), Mock()
        window.focused.return_value = True
        window.hwnd = 123
        vision.observe.return_value = observation()
        keys = SimpleNamespace(f8="f8", f10="f10", f12="f12")
        module = ModuleType("pynput.keyboard")
        module.Key, module.Listener = keys, Mock(return_value=Mock())

        class Client:
            def __init__(self, *args, on_emergency=None, **kwargs):
                self.pending, self.emergency = [], on_emergency
                clients.append(self)
            def raise_alarm(self, kind, reason):
                events.append("send:" + kind)
                self.pending.append({"id": "a" * 32, "kind": kind})
                self.emergency(kind, reason)
            def clear_alarms(self, **kwargs):
                events.append("resolved")
                self.pending = []
            def pending_alarms(self):
                return tuple(self.pending)
            def allows_input(self):
                return not self.pending
            def connected(self):
                return True
            def wait_ready(self):
                return True
            def __getattr__(self, name):
                return lambda *args, **kwargs: None

        class Monitor:
            def __init__(self, _detector, _size, _factory, callback, *args, **kwargs):
                self.state, self.callback = AlertState(max_age=10), callback
                self.state.publish()
                self.state.publish()
                monitors.append(self)
            def start(self):
                pass
            def stop(self):
                pass

        executor = Mock()
        executor.execute.side_effect = lambda action: events.append("execute:" + action.kind) or time.monotonic()
        executor.stop.side_effect = lambda: events.append("stop")

        def drive(_):
            ticks.append(1)
            callbacks = module.Listener.call_args.kwargs
            if len(ticks) == 1:
                monitors[0].state.publish(hit=True)
                monitors[0].callback("检测到测谎窗口")
                self.assertEqual(events[events.index("send:lie_detector") - 1], "stop")
            elif len(ticks) == 2:
                callbacks["on_release"](keys.f8)
            elif len(ticks) == 3:
                self.assertTrue(clients[0].pending)
                monitors[0].state.publish()
                monitors[0].state.publish()
            elif len(ticks) == 4:
                self.assertTrue(clients[0].pending)
                callbacks["on_release"](keys.f8)
            else:
                callbacks["on_press"](keys.f12)

        with ExitStack() as stack:
            stack.enter_context(patch.dict("sys.modules", {"pynput": ModuleType("pynput"), "pynput.keyboard": module}))
            stack.enter_context(patch("lionbot.windows.require_windows"))
            stack.enter_context(patch("lionbot.windows.GameWindow", return_value=window))
            stack.enter_context(patch("lionbot.windows.Keyboard"))
            stack.enter_context(patch("lionbot.controls.InputExecutor", return_value=executor))
            stack.enter_context(patch("lionbot.garden_vision.GardenVision", return_value=vision))
            stack.enter_context(patch("lionbot.lie_alert.load_detector", return_value=Mock()))
            stack.enter_context(patch("lionbot.lie_alert.LieAlertMonitor", Monitor))
            stack.enter_context(patch("lionfleet.garden.FleetClient", Client))
            stack.enter_context(patch("lionfleet.garden.time.sleep", side_effect=drive))
            stack.enter_context(redirect_stdout(io.StringIO()))
            run_garden(config(), self.root / "config.garden.local.json", MachineConfig(), self.root / "config.machine.local.json", send_keys=True)
        self.assertIn("resolved", events)
        between = events[events.index("send:lie_detector"):events.index("resolved")]
        self.assertFalse(any(e.startswith("execute:") for e in between))


if __name__ == "__main__":
    unittest.main()
