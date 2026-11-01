from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv
from sqlalchemy.engine import URL

SOURCE_URL = "https://search.7881.com/G6344-100001-G6344P002-G6344P002005-0.html?pageNum=1"
BASE_DIR = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Environment:
    database_url: str
    admin_token: str = ""
    mode: str = "live"
    worker_enabled: bool = True
    source_url: str = SOURCE_URL
    market_name: str = "冒险岛怀旧服 · 国服/小白兔"

    host: str = "127.0.0.1"
    port: int = 8080

    @classmethod
    def load(cls):
        if os.getenv("GOLDWATCH_IGNORE_ENV_FILE") != "1":
            load_dotenv(BASE_DIR / ".env", override=False)
        mode = os.getenv("MODE", "live").lower()
        if mode not in ("live", "demo"):
            raise ValueError("MODE 只能为 live 或 demo")
        url = os.getenv("DATABASE_URL", "")
        if not url:
            password = os.getenv("MYSQL_PASSWORD", "")
            if not password or "请填写" in password:
                raise ValueError("请在项目 .env 填写已有 MySQL 的 MYSQL_PASSWORD；不会自动创建或重设密码")
            url = URL.create("mysql+pymysql", username=os.getenv("MYSQL_USER", "goldwatch"), password=password,
                             host=os.getenv("MYSQL_HOST", "127.0.0.1"), port=int(os.getenv("MYSQL_PORT", "3306")),
                             database=os.getenv("MYSQL_DATABASE", "goldwatch"), query={"charset": "utf8mb4"}).render_as_string(hide_password=False)
        elif url.startswith("mysql://"):
            url = "mysql+pymysql://" + url[len("mysql://"):]
        if not url.startswith(("mysql+pymysql://", "sqlite:")):
            raise ValueError("此宝塔版支持 MySQL；SQLite 仅供本机验证")
        source = os.getenv("SOURCE_URL", SOURCE_URL)
        parsed = urlsplit(source)
        if parsed.scheme != "https" or parsed.hostname != "search.7881.com" or parsed.username or parsed.password:
            raise ValueError("SOURCE_URL 仅支持 https://search.7881.com 下的公开搜索页")
        token = os.getenv("ADMIN_TOKEN", "")
        if token and (len(token) < 16 or token == "GENERATE_WITH_SETUP"):
            raise ValueError("ADMIN_TOKEN 至少 16 个字符")
        port = int(os.getenv("PORT", "8080"))
        if not 1024 <= port <= 65535:
            raise ValueError("PORT 必须为 1024～65535")
        return cls(url, token, mode, os.getenv("COLLECTOR_ENABLED", "true").lower() == "true", source,
                   os.getenv("MARKET_NAME", "冒险岛怀旧服 · 国服/小白兔"), os.getenv("HOST", "127.0.0.1"), port)
