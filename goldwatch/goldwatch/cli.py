from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from .analytics import run_json
from .config import Environment
from .db import CrawlRun, database
from .service import Worker


def main():
    parser = argparse.ArgumentParser(description="在部署服务器手动运行一次公开报价采集，沿用数据库锁")
    parser.add_argument("action", choices=("collect",))
    parser.parse_args()
    env = Environment.load()
    if env.mode != "live":
        raise SystemExit("演示模式不会访问真实来源，请用 MODE=live")
    engine, sessions = database(env.database_url)
    try:
        completed = asyncio.run(Worker(sessions, env).once(force=True))
        if not completed:
            print("当前已有采集任务运行，未重复启动。请在网页采集记录中查看结果。")
            return
        with sessions() as session:
            run = session.scalar(select(CrawlRun).where(CrawlRun.dataset == "live").order_by(CrawlRun.id.desc()).limit(1))
            data = run_json(run)
        print(f"结果：{data['status']}；商家 {data['merchant_count']} 条；玩家 {data['player_count']} 条；{data['message'] or '报价已入库'}")
        if data["status"] in ("failed", "blocked"):
            raise SystemExit(1)
    finally:
        engine.dispose()


if __name__ == "__main__":
    main()
