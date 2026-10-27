"""Small status snapshots and receipts, never remote input commands."""
from __future__ import annotations

import json
import math
import re

from .config import identity

VERSION = 1
MAX_PACKET = 8192
KINDS = frozenset(("lie_detector", "monitor_fault", "network_lost", "runtime_fault", "test"))
PHASES = frozenset(("starting", "running", "observing", "paused", "unfocused", "monitor_wait", "stopped"))


def identifier(value):
    if not isinstance(value, str) or re.fullmatch(r"[0-9a-f]{32}", value) is None:
        raise ValueError("启动或事件标识不合法")


def encode(message):
    data = json.dumps(message, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > MAX_PACKET:
        raise ValueError("通知消息过大")
    return data


def decode(data):
    if len(data) > MAX_PACKET:
        raise ValueError("通知消息过大")
    try:
        msg = json.loads(data)
        if not isinstance(msg, dict) or msg.get("v") != VERSION or msg.get("type") not in ("status", "ack"):
            raise ValueError("未知协议")
        identity(msg.get("machine_id"))
        identifier(msg.get("boot_id"))
        if type(msg.get("seq")) is not int or not 1 <= msg["seq"] <= 2**63 - 1:
            raise ValueError("消息序号不合法")
        if msg["type"] == "ack":
            if type(msg.get("ok")) is not bool or not isinstance(msg.get("error", ""), str):
                raise ValueError("回执不合法")
        else:
            if msg.get("phase") not in PHASES or not isinstance(msg.get("alarms"), list) or len(msg["alarms"]) > 8:
                raise ValueError("状态不合法")
            if not isinstance(msg.get("resolved"), list) or len(msg["resolved"]) > 16:
                raise ValueError("恢复事件不合法")
            for value in msg["resolved"]:
                identifier(value)
            for alarm in msg["alarms"]:
                if not isinstance(alarm, dict):
                    raise ValueError("报警不合法")
                identifier(alarm.get("id"))
                if alarm.get("kind") not in KINDS or not isinstance(alarm.get("reason"), str) or len(alarm["reason"]) > 160:
                    raise ValueError("报警类型或内容不合法")
            if len({a["id"] for a in msg["alarms"]}) != len(msg["alarms"]):
                raise ValueError("重复报警标识")
            for key in ("loop_age", "scan_age"):
                value = msg.get(key)
                if value is not None and (type(value) not in (int, float) or not math.isfinite(value) or value < 0):
                    raise ValueError("健康时间不合法")
        return msg
    except (UnicodeError, KeyError, TypeError, json.JSONDecodeError) as error:
        raise ValueError("通知不是有效状态消息") from error


def receipt(msg, ok=True, error=""):
    return {"v": VERSION, "type": "ack", "machine_id": msg["machine_id"],
            "boot_id": msg["boot_id"], "seq": msg["seq"], "ok": ok, "error": error[:160]}
