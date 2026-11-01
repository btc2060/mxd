"""Create application settings only; never create or change MySQL users/passwords."""
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parent.parent
target = root / ".env"
if target.exists():
    print(".env 已存在，未覆盖。请核对 MYSQL_* 和 PORT。")
else:
    content = (root / ".env.example").read_text(encoding="utf-8").replace("GENERATE_WITH_SETUP", secrets.token_hex(24))
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        stream.write(content)
    print("已生成完整 .env 和随机管理员口令。请填写已有 MySQL 的实际密码，不需要 DATABASE_URL。")
