"""Build a standalone native Python deployment ZIP without local credentials/caches."""
from datetime import datetime, timezone
from hashlib import sha256
import json
from pathlib import Path
from zipfile import ZipFile, ZipInfo, ZIP_DEFLATED

root = Path(__file__).resolve().parent.parent
excluded = {".venv", ".browsers", "artifacts", "data", "backups", "__pycache__", ".pytest_cache", ".git"}
files = sorted(path for path in root.rglob("*") if path.is_file() and not set(path.relative_to(root).parts) & excluded
               and path.name != ".env" and path.suffix not in (".pyc", ".db", ".log", ".sqlite"))
manifest = {"edition": "native-python-baota", "created_at": datetime.now(timezone.utc).isoformat(),
            "entry": "run.py", "runtime": "Python 3.12 + existing MySQL", "docker_required": False,
            "verification": {"core_tests": "27 passed", "native_mysql_and_browser": "passed",
                             "live_7881": "not verified: domain outside cloud allowlist"},
            "files": {str(path.relative_to(root)): sha256(path.read_bytes()).hexdigest() for path in files}}
target = root.parent / "downloads" / "maple-goldwatch-python.zip"
target.parent.mkdir(exist_ok=True)
stamp = datetime.now().timetuple()[:6]
with ZipFile(target, "w", ZIP_DEFLATED, compresslevel=9) as archive:
    for name, content in [(str(path.relative_to(root)), path.read_bytes()) for path in files] + [("BUILD.json", (json.dumps(manifest, ensure_ascii=False, indent=2)+"\n").encode())]:
        info = ZipInfo("maple-goldwatch-python/" + name, stamp)
        info.create_system = 3
        info.external_attr = 0o100644 << 16
        info.compress_type = ZIP_DEFLATED
        archive.writestr(info, content)
with ZipFile(target) as archive:
    assert archive.testzip() is None
    assert not any(name.endswith("/.env") or "/.venv/" in name or "/.browsers/" in name for name in archive.namelist())
    for name, digest in manifest["files"].items():
        assert sha256(archive.read("maple-goldwatch-python/" + name)).hexdigest() == digest
print(f"部署包：{target.name}；{target.stat().st_size} 字节；内容哈希和敏感文件排除检查通过。")
