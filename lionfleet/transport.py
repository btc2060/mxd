from __future__ import annotations

import math
import socket
import threading
import time
import uuid
from pathlib import Path

from .config import atomic_json, read_json
from .protocol import KINDS, PHASES, VERSION, decode, encode, identifier


class InstanceLock:
    """One local sender per configured machine, including the test sender."""

    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.file = path.open("a+b")
        try:
            if self.file.tell() == 0:
                self.file.write(b"1")
                self.file.flush()
            self.file.seek(0)
            if __import__("os").name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            self.file.close()
            raise RuntimeError("该机的多机程序或测试通知已经在运行，请先退出旧进程") from error

    def close(self):
        self.file.close()  # OS releases the advisory lock; keep the lock file in place.


class FleetClient:
    def __init__(self, config, root, on_emergency=None, sound=None, clock=time.monotonic):
        config.validate()
        self.c, self.root, self.clock = config, Path(root), clock
        self.on_emergency, self.sound = on_emergency, sound
        self.lock = threading.RLock()
        self.delivered = threading.Condition(self.lock)
        self.wake, self.stopped = threading.Event(), threading.Event()
        self.boot_id = uuid.uuid4().hex
        self.phase, self.seq, self.revision = "starting", 0, 0
        self.acked_revision = -1
        self.last_ack = None
        self.last_error = "尚未收到接收端回执"
        self.loop_at = self.scan_at = None
        self.local_muted = False
        self.stopping = False
        self.alarms, self.resolved = {}, []
        self.threads, self.socket, self.instance = [], None, None
        self.journal = self.root / config.state_directory / f"agent-{config.machine_id}.json"
        self._load()

    def _load(self):
        if not self.journal.exists():
            return
        data = read_json(self.journal)
        if not isinstance(data, dict) or data.get("machine_id") != self.c.machine_id:
            raise ValueError(f"报警状态文件损坏或机号不符：{self.journal}")
        alarms, resolved = data.get("alarms"), data.get("resolved")
        if not isinstance(alarms, list) or not isinstance(resolved, list) or len(alarms) > len(KINDS) or len(resolved) > 16:
            raise ValueError(f"报警状态文件不合法：{self.journal}")
        for item in alarms:
            if not isinstance(item, dict):
                raise ValueError(f"报警状态文件不合法：{self.journal}")
            identifier(item.get("id"))
            if item.get("kind") not in KINDS or not isinstance(item.get("reason"), str) or len(item["reason"]) > 160:
                raise ValueError(f"报警状态文件不合法：{self.journal}")
            if item["kind"] in self.alarms:
                raise ValueError("报警状态文件存在重复类型")
            self.alarms[item["kind"]] = item
        for value in resolved:
            identifier(value)
        self.resolved = resolved

    def _save(self):
        atomic_json(self.journal, {"machine_id": self.c.machine_id,
                                  "alarms": list(self.alarms.values()), "resolved": self.resolved})

    def _changed(self):
        self.revision += 1
        self.wake.set()

    def set_phase(self, phase):
        if phase not in PHASES:
            raise ValueError("未知运行状态")
        with self.lock:
            if self.phase != phase:
                self.phase = phase
                self._changed()

    def raise_alarm(self, kind, reason):
        if kind not in KINDS:
            raise ValueError("未知报警类型")
        callback = None
        with self.lock:
            if kind in self.alarms:
                return self.alarms[kind]["id"]
            alarm = {"id": uuid.uuid4().hex, "kind": kind, "reason": str(reason)[:160]}
            self.alarms[kind] = alarm
            self.local_muted = False
            self._changed()
            # Input is already blocked by the in-memory latch before file/network operations.
            callback = self.on_emergency
        if callback:
            callback(kind, alarm["reason"])
        with self.lock:
            self._save()
        return alarm["id"]

    def clear_alarms(self, kinds=None, expected_ids=None):
        """Called locally after human F8, or for explicitly labelled synthetic tests."""
        with self.lock:
            kinds = set(self.alarms) if kinds is None else set(kinds)
            removed = [self.alarms.pop(kind)["id"] for kind in kinds if kind in self.alarms
                       and (expected_ids is None or self.alarms[kind]["id"] in expected_ids)]
            if removed:
                self.resolved = (self.resolved + removed)[-16:]
                self._changed()
                self._save()

    def pending_alarms(self):
        with self.lock:
            return tuple(dict(item) for item in self.alarms.values())

    def mute_local(self):
        with self.lock:
            self.local_muted = True

    def connected(self):
        with self.lock:
            return self.last_ack is not None and self.clock() - self.last_ack < self.c.receiver_timeout_seconds

    def allows_input(self):
        with self.lock:
            return self.connected() and not self.alarms and not self.stopping

    def arm_runtime(self):
        self.touch_runtime()

    def touch_runtime(self, scan_at=None):
        with self.lock:
            self.loop_at = self.clock()
            self.scan_at = scan_at if scan_at is not None and math.isfinite(scan_at) else None

    def _health(self):
        now = self.clock()
        with self.lock:
            network_lost = self.last_ack is not None and now - self.last_ack >= self.c.receiver_timeout_seconds
            loop_lost = self.loop_at is not None and now - self.loop_at >= self.c.loop_timeout_seconds
        if self.stopping:
            return
        if network_lost:
            self.raise_alarm("network_lost", "接收端失联，已停键；检查网络后在本机按 F8 恢复")
        if loop_lost:
            self.raise_alarm("runtime_fault", "挂机主循环长时间未更新，已停键；请检查后按 F8")

    def _packet(self):
        with self.lock:
            now = self.clock()
            self.seq += 1
            def age(stamp):
                return round(min(99999., max(0., now - stamp)), 2) if stamp is not None else None
            return ({"v": VERSION, "type": "status", "machine_id": self.c.machine_id,
                     "boot_id": self.boot_id, "seq": self.seq, "phase": self.phase,
                     "alarms": list(self.alarms.values()), "resolved": list(self.resolved),
                     "loop_age": age(self.loop_at), "scan_age": age(self.scan_at)}, self.revision)

    def start(self):
        if self.threads:
            raise RuntimeError("通讯已启动")
        self.instance = InstanceLock(self.journal.with_suffix(".lock"))
        try:
            self.socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.socket.connect((self.c.receiver_ip, self.c.receiver_port))
            self.socket.setblocking(False)
            self.threads = [threading.Thread(target=self._network, name="fleet-network", daemon=True)]
            if self.sound and self.c.local_sound:
                self.threads.append(threading.Thread(target=self._ring, name="fleet-local-sound", daemon=True))
            for thread in self.threads:
                thread.start()
        except Exception:
            if self.socket:
                self.socket.close()
            self.instance.close()
            raise

    def _network(self):
        pending = {}
        next_heartbeat = 0.
        try:
            while not self.stopped.is_set():
                self._health()
                now = self.clock()
                if self.wake.is_set() or now >= next_heartbeat:
                    self.wake.clear()
                    msg, revision = self._packet()
                    pending[msg["seq"]] = [encode(msg), revision, now, 0]
                    while len(pending) > 16:
                        del pending[min(pending)]
                    next_heartbeat = now + self.c.heartbeat_seconds
                for seq, item in list(pending.items()):
                    if now >= item[2]:
                        try:
                            self.socket.send(item[0])
                        except OSError as error:
                            self.last_error = str(error)
                        item[2] = now + (.5 if item[3] == 0 else 1 if item[3] == 1 else 2)
                        item[3] += 1
                for _ in range(64):
                    try:
                        ack = decode(self.socket.recv(8193))
                    except BlockingIOError:
                        break
                    except (ValueError, OSError):
                        break
                    if (ack["type"] != "ack" or ack["machine_id"] != self.c.machine_id
                            or ack["boot_id"] != self.boot_id or ack["seq"] not in pending):
                        continue
                    if not ack["ok"]:
                        self.last_error = ack.get("error", "接收端拒收")
                        continue
                    with self.delivered:
                        self.last_ack = self.clock()
                        self.last_error = ""
                        self.acked_revision = max(self.acked_revision, pending[ack["seq"]][1])
                        self.delivered.notify_all()
                    for seq in [s for s in pending if s <= ack["seq"]]:
                        del pending[seq]
                self.wake.wait(.03)
        except Exception as error:
            # Never leave the input gate open if the notification thread dies.
            self.last_error = f"通讯线程异常：{error}"
            with self.lock:
                self.last_ack = None
            try:
                self.raise_alarm("runtime_fault", self.last_error)
            except Exception:
                pass

    def _ring(self):
        while not self.stopped.is_set():
            with self.lock:
                ring = bool(self.alarms) and not self.local_muted
            if ring:
                try:
                    self.sound()
                except Exception as error:
                    print(f"本地声音失败：{error}", flush=True)
                self.stopped.wait(.65)
            else:
                self.stopped.wait(.05)

    def wait_ready(self, timeout=None):
        end = time.monotonic() + (self.c.startup_timeout_seconds if timeout is None else timeout)
        while time.monotonic() < end and not self.stopped.is_set():
            if self.connected():
                return True
            self.stopped.wait(.05)
        return False

    def flush(self, timeout=3):
        with self.delivered:
            revision = self.revision
            self.wake.set()
            return self.delivered.wait_for(lambda: self.acked_revision >= revision, timeout)

    def close(self):
        self.stopping = True
        if self.threads:
            self.set_phase("stopped")
            self.flush(.35)
        self.stopped.set()
        self.wake.set()
        for thread in self.threads:
            thread.join(timeout=1)
        if self.socket:
            self.socket.close()
        if self.instance:
            self.instance.close()
        self.threads = []
