"""Produce a standalone source archive, excluding databases, passwords and browser artifacts."""
from pathlib import Path
import hashlib
import json
import zipfile

root = Path(__file__).resolve().parent.parent
destination = root.parent / "downloads"
destination.mkdir(exist_ok=True)
excluded = {"data", "artifacts", "backups", ".venv", "__pycache__", ".pytest_cache"}
payload = {}
for path in root.rglob("*"):
    relative = path.relative_to(root)
    if (not path.is_file() or any(part in excluded for part in relative.parts)
            or path.name == ".env" or path.suffix in (".pyc", ".db", ".sqlite")):
        continue
    payload[relative.as_posix()] = path.read_bytes()
manifest = {"project": "Maple Gold Watch", "files": {name: hashlib.sha256(data).hexdigest() for name, data in payload.items()}}
payload["BUILD.json"] = json.dumps(manifest, ensure_ascii=False, indent=2).encode()
target = destination / "maple-goldwatch.zip"
with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
    for name, data in sorted(payload.items()):
        info = zipfile.ZipInfo("maple-goldwatch/" + name, date_time=(2026, 10, 31, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        archive.writestr(info, data)
with zipfile.ZipFile(target) as archive:
    assert archive.testzip() is None
    assert all("/.env" not in name or name.endswith(".env.example") for name in archive.namelist())
print(f"Standalone package: {target} ({target.stat().st_size} bytes)")
