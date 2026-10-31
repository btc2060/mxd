from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, suppress
import csv
from datetime import timedelta
import io
import secrets
from typing import Annotated, Literal

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StrictBool
from sqlalchemy import select

from .analytics import history, latest_summary, quote_rows, run_json
from .config import BASE_DIR, Environment
from .db import CrawlRun, Lease, Preferences, aware, database, utcnow
from .parser import parse_html
from .collector import CollectionResult
from .service import Worker, options, store_result


class SettingsInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    interval_seconds: int = Field(ge=60, le=86400, strict=True)
    enabled: StrictBool
    max_pages: int = Field(ge=1, le=3, strict=True)
    engine: Literal["auto", "http", "browser"]
    merchant_selector: str = Field(default="", max_length=400)
    player_selector: str = Field(default="", max_length=400)


class HtmlInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    html: str = Field(min_length=10, max_length=2_000_000)


def create_app(environment=None, sessions=None, collector=None):
    env = environment or Environment.load()
    owned_engine = None
    if sessions is None:
        owned_engine, sessions = database(env.database_url)
    worker = Worker(sessions, env, collector)

    @asynccontextmanager
    async def lifespan(app):
        task = None
        if env.mode == "demo":
            from .demo import seed
            await asyncio.to_thread(seed, sessions, env.source_url)
        elif env.worker_enabled:
            task = asyncio.create_task(worker.loop())
        yield
        if task:
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task
        if owned_engine:
            owned_engine.dispose()

    app = FastAPI(title="枫币行情", lifespan=lifespan, docs_url="/api/docs", redoc_url=None)
    app.state.sessions, app.state.worker, app.state.environment = sessions, worker, env

    def admin(authorization: Annotated[str | None, Header()] = None):
        if not env.admin_token:
            raise HTTPException(503, "服务器尚未配置 ADMIN_TOKEN，管理操作不可用")
        supplied = authorization[7:] if authorization and authorization.startswith("Bearer ") else ""
        if not secrets.compare_digest(supplied.encode(), env.admin_token.encode()):
            raise HTTPException(401, "管理员口令不正确")

    @app.middleware("http")
    async def headers(request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        response.headers["X-Frame-Options"] = "DENY"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/health")
    def health():
        with sessions() as session:
            session.execute(select(Preferences.id).limit(1))
        return {"ok": True, "mode": env.mode}

    @app.get("/api/summary")
    def summary():
        with sessions() as session:
            return latest_summary(session, env)

    @app.get("/api/quotes")
    def quotes(side: Literal["merchant", "player"], limit: int = Query(30, ge=1, le=100)):
        with sessions() as session:
            return quote_rows(session, env.mode, side, limit)

    @app.get("/api/history")
    def chart(period: Literal["5m", "30m", "1h", "1d", "1w"] = "30m",
              span: Literal["1d", "7d", "30d", "90d"] = "7d", metric: Literal["best", "median"] = "best",
              unit: Literal["yuan_per_wan", "wan_per_yuan"] = "yuan_per_wan"):
        with sessions() as session:
            return history(session, env.mode, period, span, metric, unit)

    @app.get("/api/runs")
    def runs(limit: int = Query(20, ge=1, le=100)):
        with sessions() as session:
            values = session.scalars(select(CrawlRun).where(CrawlRun.dataset == env.mode).order_by(CrawlRun.id.desc()).limit(limit))
            return {"rows": [run_json(value) for value in values]}

    @app.get("/api/export.csv")
    def export(span: Literal["1d", "7d", "30d", "90d"] = "7d", period: Literal["5m", "30m", "1h", "1d", "1w"] = "30m"):
        with sessions() as session:
            data = history(session, env.mode, period, span)
        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["模式", "时间（UTC）", "价格区域", "开盘(元/万金)", "最高", "最低", "收盘", "采样次数", "采样报价条数(非成交量)"])
        for side, series in data["series"].items():
            for at, point in zip(data["times"], series):
                if point:
                    writer.writerow([env.mode, at, "商家回收" if side == "merchant" else "玩家挂售", point["open"], point["high"],
                                     point["low"], point["close"], point["samples"], point["count"]])
        return Response("\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="goldwatch-{span}-{env.mode}.csv"'})

    @app.post("/api/admin/check", dependencies=[Depends(admin)])
    def check():
        return {"ok": True}

    @app.put("/api/settings", dependencies=[Depends(admin)])
    def settings(value: SettingsInput):
        import soupsieve
        try:
            for selector in (value.merchant_selector, value.player_selector):
                if selector.strip():
                    soupsieve.compile(selector)
        except Exception:
            raise HTTPException(422, "CSS 选择器语法无效")
        with sessions.begin() as session:
            prefs = session.get(Preferences, 1)
            for key, item in value.model_dump().items():
                setattr(prefs, key, item.strip() if isinstance(item, str) else item)
            prefs.next_due_at = utcnow() + timedelta(seconds=value.interval_seconds)
        return {"ok": True}

    @app.post("/api/collect", status_code=202, dependencies=[Depends(admin)])
    def collect():
        if env.mode == "demo":
            raise HTTPException(409, "演示模式不抓取真实页面；请部署 MODE=live")
        if not env.worker_enabled:
            raise HTTPException(503, "采集工作进程未启用，请配置 COLLECTOR_ENABLED=true")
        with sessions.begin() as session:
            prefs = session.get(Preferences, 1, with_for_update=True)
            lease = session.get(Lease, 1)
            if lease.expires_at and aware(lease.expires_at) > utcnow():
                return {"ok": True, "message": "当前已有采集任务运行，完成后刷新看板即可"}
            if prefs.last_manual_at and utcnow() - aware(prefs.last_manual_at) < timedelta(seconds=60):
                raise HTTPException(429, "手动采集至少间隔 60 秒")
            prefs.last_manual_at, prefs.requested = utcnow(), True
        return {"ok": True, "message": "已加入采集队列，通常 2 秒内开始"}

    @app.post("/api/import-html", dependencies=[Depends(admin)])
    def import_html(value: HtmlInput):
        if env.mode != "live":
            raise HTTPException(409, "演示模式不可导入真实报价")
        with sessions.begin() as session:
            prefs = session.get(Preferences, 1)
            try:
                parsed = parse_html(value.html, env.source_url, prefs.merchant_selector, prefs.player_selector)
            except Exception:
                raise HTTPException(422, "页面解析失败，请检查选择器和 HTML")
            if parsed.blocked or not parsed.quotes:
                raise HTTPException(422, "未识别到公开报价，验证或登录页面不能作为报价导入")
            run = CrawlRun(dataset="live", origin="manual-html", engine="html-import")
            session.add(run)
            session.flush()
            store_result(session, run, CollectionResult(parsed.quotes, 1, "html-import", parsed.warnings))
            return {"ok": True, "merchant_count": run.merchant_count, "player_count": run.player_count}

    app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")

    @app.get("/")
    def index():
        return FileResponse(BASE_DIR / "static/index.html", headers={"Cache-Control": "no-cache"})

    return app
