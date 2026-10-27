from __future__ import annotations

import json
import math
import threading
import time
import uuid
from pathlib import Path

from .config import atomic_json, read_json
from .protocol import KINDS, PHASES, identifier

PHASE_LABELS = {"starting": "启动中", "running": "挂机运行", "observing": "仅观察",
                "paused": "人工暂停", "unfocused": "游戏失焦", "monitor_wait": "等待有效测谎画面", "stopped": "已停止"}
ALARM_LABELS = {"lie_detector": "出现测谎，请处理", "monitor_fault": "测谎截图异常，请检查",
                "network_lost": "接收端曾失联，等待本机恢复", "runtime_fault": "程序异常，请检查",
                "test": "测试报警"}


class ReceiverStore:
    def __init__(self, config, root, clock=time.monotonic, wall=time.time):
        config.validate()
        self.c, self.root, self.clock, self.wall = config, Path(root), clock, wall
        self.lock = threading.RLock()
        self.registered = {m.machine_id: m for m in config.machines}
        self.nodes = {}
        self.muted = set()
        self.offline_keys = {}
        self.persistence_error = ""
        path = self.root / config.state_file
        if path.exists():
            data = read_json(path)
            if not isinstance(data, dict) or data.get("version") != 1 or not isinstance(data.get("nodes"), dict):
                raise ValueError(f"接收端状态文件损坏：{path}；请保留文件并检查，不要直接清空未处理报警")
            for machine_id, node in data["nodes"].items():
                if machine_id not in self.registered:
                    continue
                if (not isinstance(node, dict) or not isinstance(node.get("alarms"), dict)
                        or not isinstance(node.get("retired"), list) or not isinstance(node.get("resolved"), list)):
                    raise ValueError(f"接收端状态记录损坏：{machine_id}")
                identifier(node.get("boot_id"))
                if (type(node.get("seq")) is not int or node["seq"] < 0 or node.get("phase") not in PHASES
                        or type(node.get("last_wall")) not in (int, float) or not math.isfinite(node["last_wall"])):
                    raise ValueError(f"接收端状态记录损坏：{machine_id}")
                for value in node["retired"] + node["resolved"]:
                    identifier(value)
                for alarm_id, item in node["alarms"].items():
                    identifier(alarm_id)
                    if (not isinstance(item, dict) or item.get("id") != alarm_id or item.get("kind") not in KINDS
                            or not isinstance(item.get("reason"), str)
                            or type(item.get("first_wall")) not in (int, float) or not math.isfinite(item["first_wall"])):
                        raise ValueError(f"接收端报警记录损坏：{machine_id}")
                node["seen"] = None  # Monotonic timestamps cannot survive a process restart.
                self.nodes[machine_id] = node

    def _save(self):
        atomic_json(self.root / self.c.state_file, {"version": 1, "nodes": self.nodes})

    def _log(self, event, machine_id, **details):
        path = self.root / self.c.log_file
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps({"time": time.strftime("%Y-%m-%d %H:%M:%S"), "event": event,
                                     "machine_id": machine_id, **details}, ensure_ascii=False) + "\n")

    def apply(self, msg, address):
        machine_id = msg["machine_id"]
        if machine_id not in self.registered:
            return False, "机号未登记，请检查接收端 machines 配置"
        now = self.clock()
        with self.lock:
            node = self.nodes.get(machine_id)
            if node is None:
                node = {"boot_id": msg["boot_id"], "retired": [], "seq": 0, "alarms": {},
                        "resolved": [], "seen": None, "phase": "starting", "last_wall": 0}
                self.nodes[machine_id] = node
            changed = node["seq"] == 0
            if msg["boot_id"] in node["retired"]:
                return False, "这是已经退出的旧进程"
            if msg["boot_id"] != node["boot_id"]:
                if node["seen"] is not None and now - node["seen"] < self.c.offline_seconds and node["phase"] != "stopped":
                    return False, "同一机号已有在线进程；请退出旧进程，或给另一台设置不同机号"
                node["retired"] = (node["retired"] + [node["boot_id"]])[-32:]
                node["boot_id"], node["seq"] = msg["boot_id"], 0
                changed = True
            if msg["seq"] <= node["seq"]:
                # ACK duplicates, without letting them revert state or refresh online time.
                if self.persistence_error:
                    try:
                        self._save()
                        self.persistence_error = ""
                    except OSError as error:
                        self.persistence_error = str(error)
                        return False, "接收端保存状态失败，请检查磁盘"
                return True, ""
            node["seq"], node["seen"], node["last_wall"] = msg["seq"], now, self.wall()
            self.offline_keys.pop(machine_id, None)
            node["address"] = address[0]
            node["loop_age"], node["scan_age"] = msg.get("loop_age"), msg.get("scan_age")
            if node["phase"] != msg["phase"]:
                changed = True
                node["phase"] = msg["phase"]
            for alarm_id in msg["resolved"]:
                if alarm_id not in node["resolved"]:
                    node["resolved"] = (node["resolved"] + [alarm_id])[-128:]
                    changed = True
                if node["alarms"].pop(alarm_id, None) is not None:
                    changed = True
            for alarm in msg["alarms"]:
                if alarm["id"] in node["resolved"]:
                    continue
                old = node["alarms"].get(alarm["id"])
                if old is None:
                    node["alarms"][alarm["id"]] = {**alarm, "first_wall": self.wall()}
                    changed = True
            if changed or self.persistence_error:
                try:
                    self._save()
                    self.persistence_error = ""
                    self._log("status", machine_id, phase=node["phase"], alarms=list(node["alarms"].values()))
                except OSError as error:
                    self.persistence_error = str(error)
                    return False, "接收端保存状态或日志失败，请检查磁盘"
            return True, ""

    def rows(self):
        now = self.clock()
        with self.lock:
            result = []
            for machine in self.c.machines:
                node = self.nodes.get(machine.machine_id)
                online = bool(node and node["seen"] is not None and now - node["seen"] < self.c.offline_seconds)
                alarms = list(node["alarms"].values()) if node else []
                if not node:
                    status = "尚未连接"
                elif not online:
                    status = "已停止" if node["phase"] == "stopped" else "失联"
                else:
                    status = PHASE_LABELS.get(node["phase"], node["phase"])
                    if node.get("loop_age") is not None and node["loop_age"] >= 8 and node["phase"] not in ("stopped", "starting"):
                        status = "主循环未更新"
                    elif node["phase"] == "running" and (node.get("scan_age") is None or node["scan_age"] > 2):
                        status = "测谎扫描未更新"
                if alarms:
                    if online:
                        status = "测试通知" if all(a["kind"] == "test" for a in alarms) else "报警暂停"
                    status += " · " + "；".join(dict.fromkeys(ALARM_LABELS[a["kind"]] for a in alarms))
                last = time.strftime("%H:%M:%S", time.localtime(node["last_wall"])) if node and node["last_wall"] else "—"
                age = max(0, int(self.wall() - min(a["first_wall"] for a in alarms))) if alarms else 0
                result.append({"id": machine.machine_id, "name": machine.machine_name, "port": machine.kvm_port,
                               "online": online, "status": status, "last": last, "alarm_seconds": age,
                               "alarms": [dict(a) for a in alarms], "address": node.get("address", "") if node else ""})
            return result

    def notifications(self):
        result = []
        with self.lock:
            active_keys = set()
            for row in self.rows():
                machine = self.registered[row["id"]]
                for alarm in row["alarms"]:
                    key = (row["id"], alarm["id"])
                    active_keys.add(key)
                    if key not in self.muted:
                        result.append({"key": key, "machine": machine,
                                       "text": machine.machine_name + ALARM_LABELS[alarm["kind"]]})
                node = self.nodes.get(row["id"])
                if node and not row["online"] and node["phase"] != "stopped":
                    if row["id"] not in self.offline_keys:
                        self.offline_keys[row["id"]] = "offline:" + uuid.uuid4().hex
                        try:
                            self._log("offline", row["id"])
                        except OSError as error:
                            self.persistence_error = str(error)
                    key = (row["id"], self.offline_keys[row["id"]])
                    active_keys.add(key)
                    if key not in self.muted:
                        result.append({"key": key, "machine": machine, "text": machine.machine_name + "已失联，请检查"})
                else:
                    self.offline_keys.pop(row["id"], None)
            self.muted.intersection_update(active_keys)
        return result

    def mute_machine(self, machine_id):
        with self.lock:
            for item in self.notifications():
                if item["key"][0] == machine_id:
                    self.muted.add(item["key"])

    def unmute_machine(self, machine_id):
        with self.lock:
            self.muted = {key for key in self.muted if key[0] != machine_id}
