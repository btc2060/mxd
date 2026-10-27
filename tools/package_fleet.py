"""Build separate distributable ZIPs without local configs, images, journals or venvs."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def build(root, output, role):
    selected = list((root / "lionfleet").rglob("*.py"))
    selected += [root / "FLEET_SETUP.md"]
    if role == "receiver":
        selected += [root / "config.receiver.example.json"]
        scripts = ("_python.cmd", "start_receiver.cmd", "test_receiver_sound.cmd", "allow_receiver_firewall.ps1")
    else:
        selected += list((root / "lionbot").rglob("*.py"))
        selected += [root / "requirements.txt", root / "config.machine.example.json", root / "config.garden.example.json"]
        scripts = ("_python.cmd", "_machine_config.cmd", "check_connection.cmd", "test_notification.cmd",
                   "start_garden_multi.cmd", "observe_garden_multi.cmd")
    selected += [root / "scripts" / name for name in scripts]
    payload = {str(path.relative_to(root)).replace("\\", "/"): path.read_bytes() for path in selected}
    manifest = {"format": 1, "role": role,
                "files": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(payload.items())}}
    payload["BUILD.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8")
    output.mkdir(parents=True, exist_ok=True)
    path = output / f"mxd-fleet-{role}.zip"
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in sorted(payload.items()):
            info = zipfile.ZipInfo(f"mxd-{role}/" + name, date_time=(2026, 10, 27, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    with zipfile.ZipFile(path) as archive:
        assert archive.testzip() is None
    print(f"{role}: {path} ({path.stat().st_size} bytes)")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("downloads"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent
    for role in ("receiver", "agent"):
        build(root, args.output.resolve(), role)


if __name__ == "__main__":
    main()
