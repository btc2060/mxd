"""Read-only diagnostics. --source makes public requests, never writes prices or settings."""
import importlib.metadata
import os
from datetime import datetime, timedelta, timezone

from .browser import browser_path, error_details, launch_options
from .collector import Collector
from .config import BASE_DIR


async def doctor(env, source=False, http_only=False, save_page=False):
    stage = "检查环境"
    try:
        print(f"Python：{os.sys.version.split()[0]}；Playwright：{importlib.metadata.version('playwright')}")
        print(f"项目：{BASE_DIR}；UID：{getattr(os, 'getuid', lambda: 'Windows')()}")
        print(f"浏览器：{browser_path() or 'Playwright 默认缓存（可能尚未安装）'}")
        if not http_only:
            from playwright.async_api import async_playwright
            stage = "启动 Chromium"
            async with async_playwright() as p:
                browser = await p.chromium.launch(**launch_options())
                try:
                    page = await browser.new_page()
                    await page.set_content('<!doctype html><title>GoldWatch check</title><h1>OK</h1>')
                    if await page.title() != "GoldWatch check":
                        raise RuntimeError("本地测试页渲染异常")
                    print(f"本地浏览器测试：PASS；Chromium {browser.version}")
                finally:
                    await browser.close()
        if source:
            stage = "检查源站公开报价"
            directory = BASE_DIR / "artifacts" / datetime.now(timezone(timedelta(hours=8))).strftime("diagnostic-%Y%m%d-%H%M%S-%f") if save_page else None
            result = await Collector(artifact_dir=directory).collect(env.source_url, {"engine": "http" if http_only else "auto", "max_pages": 1})
            count = lambda side: sum(q.side == side for q in result.quotes)
            print(f"源站结果：商家 {count('merchant')} 条；玩家 {count('player')} 条；方式 {result.engine}")
            for warning in result.warnings:
                print(f"提示：{warning}")
            if directory:
                print(f"诊断画面目录：{directory}")
            return 0 if count("merchant") and count("player") else 1
        print("未访问源站、未连接数据库。本地 PASS 不代表已采集到真实报价；加 --source 可检查来源。")
        return 0
    except Exception as error:
        print(f"诊断失败（{stage}）：{error_details(error)}")
        if source and save_page and 'directory' in locals():
            print(f"已生成的诊断页面请在此目录查看：{directory}")
        return 1
