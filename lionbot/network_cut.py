"""Local Windows network stop switch. No challenge response or automatic reconnect."""
from __future__ import annotations

import argparse
import base64
import ctypes
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
import uuid


def require_admin():
    if sys.platform != "win32":
        raise RuntimeError("断网／恢复网卡仅支持 Windows；未执行任何网络修改")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        raise RuntimeError("断网／恢复网卡需要管理员权限，请以管理员身份打开终端后重试")


def powershell(script):
    prefix = ("$ErrorActionPreference='Stop';$WarningPreference='SilentlyContinue';$ProgressPreference='SilentlyContinue';"
              "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
              "$OutputEncoding=[Console]::OutputEncoding;")
    encoded = base64.b64encode((prefix + script).encode("utf-16le")).decode("ascii")
    executable = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32/WindowsPowerShell/v1.0/powershell.exe"
    try:
        result = subprocess.run([str(executable), "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                                capture_output=True, timeout=30,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except subprocess.TimeoutExpired as error:
        raise RuntimeError("网卡操作超时，可能已修改部分网卡；请用本地键鼠／KVM 检查，并运行 --restorenet") from error
    output = result.stdout.decode("utf-8-sig", errors="replace").strip()
    if result.returncode:
        detail = result.stderr.decode("utf-8-sig", errors="replace").strip()
        raise RuntimeError(f"Windows 网卡操作失败：{detail[:1000]}")
    try:
        return json.loads(output)
    except ValueError as error:
        raise RuntimeError("Windows 未返回可验证的网卡状态；如已断网，请在本机运行 --restorenet") from error


PROPERTIES = ("Select-Object @{n='guid';e={$_.InterfaceGuid.ToString()}},"
              "@{n='name';e={$_.Name}},@{n='status';e={$_.Status.ToString()}},"
              "@{n='admin';e={$_.AdminStatus.ToString()}}")


def normalize_adapters(items):
    if not isinstance(items, list) or len(items) > 128:
        raise ValueError("网卡清单不合法，未继续修改")
    result = []
    for item in items:
        if not isinstance(item, dict):
            raise ValueError("网卡清单不合法")
        try:
            guid = str(uuid.UUID(item["guid"]))
        except (KeyError, ValueError, TypeError, AttributeError) as error:
            raise ValueError("网卡 GUID 不合法") from error
        if any(not isinstance(item.get(key), str) for key in ("name", "status", "admin")):
            raise ValueError("网卡状态不完整")
        result.append({"guid": guid, **{key: item[key] for key in ("name", "status", "admin")}})
    if len({item["guid"] for item in result}) != len(result):
        raise ValueError("网卡 GUID 重复")
    return result


def save_record(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


class NetworkSwitch:
    def __init__(self, record, *, command=powershell, check_admin=require_admin, computer=None):
        self.record = Path(record).resolve()
        self.command, self.check_admin = command, check_admin
        self.computer = computer if computer is not None else socket.gethostname()

    def _read_record(self):
        try:
            value = json.loads(self.record.read_text(encoding="utf-8-sig"))
        except (OSError, ValueError) as error:
            raise ValueError(f"无法读取网卡恢复记录：{self.record}；可通过 Win+R → ncpa.cpl 手动启用网卡") from error
        if (not isinstance(value, dict) or value.get("version") != 1
                or value.get("state") not in ("pending", "disconnected", "failed", "restored")
                or value.get("computer") != self.computer):
            raise ValueError("恢复记录损坏或不属于本机，未修改网卡；请在本机检查恢复记录")
        value["adapters"] = normalize_adapters(value.get("adapters"))
        if not value["adapters"]:
            raise ValueError("恢复记录没有网卡")
        return value

    def prepare(self):
        """Read-only network preflight; the only file action is a writable-directory probe."""
        self.check_admin()
        if self.record.exists() and self._read_record()["state"] != "restored":
            raise ValueError("上次断网记录尚未恢复，请先运行 --restorenet；不会覆盖已有恢复信息")
        items = normalize_adapters(self.command("$items=@(Get-NetAdapter -Physical -ErrorAction Stop | "
                                                + PROPERTIES + ");ConvertTo-Json -InputObject $items -Compress"))
        targets = [item for item in items if item["status"].casefold() == "up" and item["admin"].casefold() == "up"]
        if not targets:
            raise ValueError("没有已连接且已启用的物理网卡，未执行断网；如之前已断网，请先 --restorenet")
        self.record.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryFile(dir=self.record.parent):
            pass
        return targets

    def _change(self, targets, enable):
        # Only canonical GUIDs enter PowerShell source. Adapter names are never evaluated.
        ids = ",".join("'" + item["guid"] + "'" for item in targets)
        verb = "Enable-NetAdapter" if enable else "Disable-NetAdapter"
        script = (f"$ids=@({ids});$issues=@();foreach($id in $ids){{try{{"
                  "$a=@(Get-NetAdapter -IncludeHidden -ErrorAction Stop | Where-Object {"
                  " $_.InterfaceGuid.ToString().Trim([char[]]'{}') -eq $id });"
                  "if($a.Count -ne 1){throw ('Adapter missing or ambiguous: '+$id)};"
                  f"$a | {verb} -Confirm:$false -ErrorAction Stop"
                  "}catch{$issues+=($_.Exception.Message)}};"
                  "$after=@(Get-NetAdapter -IncludeHidden -ErrorAction Stop | Where-Object {"
                  "$ids -contains $_.InterfaceGuid.ToString().Trim([char[]]'{}')} | " + PROPERTIES + ");"
                  "@{adapters=$after;errors=@($issues)} | ConvertTo-Json -Depth 5 -Compress")
        value = self.command(script)
        if not isinstance(value, dict) or not isinstance(value.get("errors"), list):
            raise ValueError("未获得完整网卡操作结果")
        after = normalize_adapters(value.get("adapters"))
        by_id = {item["guid"]: item for item in after}
        problems = [str(item)[:500] for item in value["errors"]]
        for target in targets:
            current = by_id.get(target["guid"])
            if current is None or (current["admin"].casefold() != ("up" if enable else "down")):
                problems.append(f"未确认{'启用' if enable else '禁用'}：{target['name']}")
        return after, problems

    def disconnect(self):
        targets = self.prepare()  # Refresh adapters at the actual stop time, not hours earlier.
        value = {"version": 1, "computer": self.computer, "state": "pending", "adapters": targets,
                 "created": datetime.now(timezone.utc).isoformat()}
        save_record(self.record, value)  # Must succeed BEFORE the first adapter is disabled.
        try:
            after, problems = self._change(targets, enable=False)
            value.update(state="failed" if problems else "disconnected", after=after, errors=problems)
            save_record(self.record, value)
        except Exception:
            # The durable pending record still identifies every adapter we might have changed.
            print(f"无法确认全部网卡操作，挂机已停止；恢复记录保留在 {self.record}", flush=True)
            raise
        if problems:
            raise RuntimeError("部分网卡未确认禁用，挂机已停止：" + "；".join(problems) + "；请检查并用 --restorenet 恢复")
        print("已禁用本机已连接的物理网卡：" + "、".join(item["name"] for item in targets), flush=True)
        print(f"挂机已结束，不会自动重连。恢复记录：{self.record}；本机管理员终端执行 --restorenet。", flush=True)

    def restore(self):
        self.check_admin()
        value = self._read_record()
        if value["state"] == "restored":
            print("记录中的网卡已经恢复，没有执行额外操作。", flush=True)
            return
        after, problems = self._change(value["adapters"], enable=True)
        value.update(state="failed" if problems else "restored", after=after, errors=problems)
        save_record(self.record, value)
        if problems:
            raise RuntimeError("部分网卡未确认恢复：" + "；".join(problems) + "；可重试 --restorenet 或在 ncpa.cpl 中启用")
        print("记录中的网卡已重新启用；网络获取地址可能需稍候。没有启动或恢复挂机。", flush=True)


def main(argv=None):
    parser = argparse.ArgumentParser(description="手动恢复 Garden 记录的网卡；仅标准库，不启动游戏或自动重连")
    parser.add_argument("--restore", action="store_true", required=True)
    parser.add_argument("--record", type=Path, default=Path("debug-output/garden/network-cut.json"))
    args = parser.parse_args(argv)
    try:
        NetworkSwitch(args.record).restore()
        return 0
    except (OSError, ValueError, RuntimeError) as error:
        print(f"恢复网卡失败：{error}", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
