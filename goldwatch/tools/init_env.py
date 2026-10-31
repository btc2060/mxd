"""Create local deployment secrets without printing their values or overwriting a file."""
from pathlib import Path
import os
import secrets

root = Path(__file__).resolve().parent.parent
path = root / ".env"
if path.exists():
    print(".env 已存在，未覆盖。")
else:
    payload = (f"ADMIN_TOKEN={secrets.token_hex(24)}\nMYSQL_PASSWORD={secrets.token_hex(24)}\n"
               f"MYSQL_ROOT_PASSWORD={secrets.token_hex(24)}\nBIND_IP=0.0.0.0\nPORT=8080\n")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as file:
        file.write(payload)
    print("已生成 .env（仅文件拥有者可读写）。管理员口令为其中 ADMIN_TOKEN，请在你自己的服务器查看，不要提交或公开。")
