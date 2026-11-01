from __future__ import annotations

import argparse
import asyncio

from sqlalchemy import select

from .analytics import run_json
from .config import Environment
from .db import CrawlRun, database
from .service import Worker


def main():
    parser = argparse.ArgumentParser(description="宝塔原生版：数据库检查、只读诊断、手动采集")
    parser.add_argument("action", choices=("collect", "check-db", "doctor"))
    parser.add_argument("--source", action="store_true", help="诊断源站公开报价，不入库")
    parser.add_argument("--http-only", action="store_true", help="诊断时只用 HTTP，不启动浏览器")
    parser.add_argument("--save-page", action="store_true", help="诊断时保存公开 HTML／浏览器画面")
    arguments = parser.parse_args()
    env = Environment.load()
    if arguments.action == "doctor":
        from .diagnostic import doctor
        raise SystemExit(asyncio.run(doctor(env, arguments.source, arguments.http_only, arguments.save_page)))
    if arguments.action == "check-db":
        engine, _ = database(env.database_url)
        engine.dispose()
        print("MySQL／应用表检查通过；不会修改数据库用户密码。")
        return
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
