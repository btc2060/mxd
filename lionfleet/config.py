from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, fields
from pathlib import Path


def number(value, name, low, high):
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} 必须在 {low}～{high} 之间")


def identity(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,32}", value):
        raise ValueError("machine_id 只允许 1～32 个英文字母、数字、下划线或减号")


def label(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 40 or any(ord(c) < 32 for c in value):
        raise ValueError(f"{name} 必须为 1～40 字的名称")


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


@dataclass(frozen=True)
class MachineConfig:
    machine_id: str = "M01"
    machine_name: str = "1号机"
    kvm_port: int = 1
    receiver_ip: str = "192.168.1.10"
    receiver_port: int = 27676
    heartbeat_seconds: float = 2.
    receiver_timeout_seconds: float = 15.
    startup_timeout_seconds: float = 20.
    loop_timeout_seconds: float = 8.
    local_sound: bool = True
    state_directory: str = "state"

    def validate(self):
        identity(self.machine_id)
        label(self.machine_name, "machine_name")
        if type(self.kvm_port) is not int or not 1 <= self.kvm_port <= 128:
            raise ValueError("kvm_port 必须为 1～128 的整数")
        if not isinstance(self.receiver_ip, str) or not self.receiver_ip.strip():
            raise ValueError("receiver_ip 请填写接收电脑的局域网 IPv4 地址")
        if type(self.receiver_port) is not int or not 1024 <= self.receiver_port <= 65535:
            raise ValueError("receiver_port 必须为 1024～65535 的整数")
        number(self.heartbeat_seconds, "heartbeat_seconds", .1, 10)
        number(self.receiver_timeout_seconds, "receiver_timeout_seconds", 1, 120)
        if self.receiver_timeout_seconds < 3 * self.heartbeat_seconds:
            raise ValueError("接收端失联时间须至少为心跳间隔的三倍")
        number(self.startup_timeout_seconds, "startup_timeout_seconds", 1, 120)
        number(self.loop_timeout_seconds, "loop_timeout_seconds", 1, 60)
        if type(self.local_sound) is not bool or not isinstance(self.state_directory, str) or not self.state_directory:
            raise ValueError("local_sound 或 state_directory 不合法")


@dataclass(frozen=True)
class RegisteredMachine:
    machine_id: str
    machine_name: str
    kvm_port: int
    alarm_wav: str = ""

    def validate(self):
        identity(self.machine_id)
        label(self.machine_name, "machine_name")
        if type(self.kvm_port) is not int or not 1 <= self.kvm_port <= 128 or not isinstance(self.alarm_wav, str):
            raise ValueError("机器登记的 kvm_port 或 alarm_wav 不合法")


@dataclass(frozen=True)
class ReceiverConfig:
    bind_ip: str = "0.0.0.0"
    port: int = 27676
    offline_seconds: float = 10.
    repeat_alarm_seconds: float = 8.
    speech_enabled: bool = True
    state_file: str = "state/receiver.json"
    log_file: str = "logs/receiver.jsonl"
    machines: tuple[RegisteredMachine, ...] = ()

    def validate(self):
        if not isinstance(self.bind_ip, str) or not self.bind_ip:
            raise ValueError("bind_ip 不合法")
        if type(self.port) is not int or not 1024 <= self.port <= 65535:
            raise ValueError("port 必须为 1024～65535 的整数")
        number(self.offline_seconds, "offline_seconds", 1, 120)
        number(self.repeat_alarm_seconds, "repeat_alarm_seconds", 2, 120)
        if (type(self.speech_enabled) is not bool or not isinstance(self.state_file, str) or not self.state_file
                or not isinstance(self.log_file, str) or not self.log_file):
            raise ValueError("接收端声音或日志配置不合法")
        if not self.machines or len(self.machines) > 128:
            raise ValueError("machines 至少登记一台，最多 128 台")
        for machine in self.machines:
            machine.validate()
        if len({m.machine_id for m in self.machines}) != len(self.machines):
            raise ValueError("machines 中 machine_id 重复")
        if len({m.kvm_port for m in self.machines}) != len(self.machines):
            raise ValueError("machines 中 KVM 口号重复")


def construct(cls, data):
    if not isinstance(data, dict):
        raise ValueError("配置必须为 JSON 对象")
    unknown = set(data) - {f.name for f in fields(cls)}
    if unknown:
        raise ValueError("未知配置字段：" + ", ".join(sorted(unknown)))
    try:
        result = cls(**data)
    except TypeError as error:
        raise ValueError(f"配置缺少字段：{error}") from error
    result.validate()
    return result


def load_machine(path):
    return construct(MachineConfig, read_json(path))


def load_receiver(path):
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("machines"), list):
        raise ValueError("接收端配置须包含 machines 数组")
    data["machines"] = tuple(construct(RegisteredMachine, item) for item in data["machines"])
    return construct(ReceiverConfig, data)
