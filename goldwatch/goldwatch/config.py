from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

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

    @classmethod
    def load(cls):
        mode = os.getenv("MODE", "live").lower()
        if mode not in ("live", "demo"):
            raise ValueError("MODE 只能为 live 或 demo")
        data = Path(os.getenv("DATA_DIR", "data")).resolve()
        data.mkdir(parents=True, exist_ok=True)
        url = os.getenv("DATABASE_URL", f"sqlite:///{data / ('demo.db' if mode == 'demo' else 'goldwatch.db')}")
        if url.startswith("postgres://"):
            url = "postgresql+psycopg://" + url[len("postgres://"):]
        elif url.startswith("postgresql://"):
            url = "postgresql+psycopg://" + url[len("postgresql://"):]
        elif url.startswith("mysql://"):
            url = "mysql+pymysql://" + url[len("mysql://"):]
        source = os.getenv("SOURCE_URL", SOURCE_URL)
        parsed = urlsplit(source)
        if parsed.scheme != "https" or parsed.hostname != "search.7881.com" or parsed.username or parsed.password:
            raise ValueError("SOURCE_URL 仅支持 https://search.7881.com 下的公开搜索页")
        token = os.getenv("ADMIN_TOKEN", "")
        if token and len(token) < 16:
            raise ValueError("ADMIN_TOKEN 至少 16 个字符")
        return cls(url, token, mode, os.getenv("COLLECTOR_ENABLED", "true").lower() == "true", source,
                   os.getenv("MARKET_NAME", "冒险岛怀旧服 · 国服/小白兔"))
