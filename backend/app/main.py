from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .api import accounts, downloads, events, health, lineage, settings as settings_api
from .aria2 import aria2
from .config import get_settings
from .db import dispose_engine, get_session_factory
from .migrations import upgrade_sync
from .security import sanitize
from .services.download import recover_interrupted
from .services.settings import load_overrides
from .services.scheduler import start_scheduler, stop_scheduler

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s %(message)s")


def _sanitize_processor(_logger, _method_name, event_dict):
    return sanitize(event_dict)


structlog.configure(
    processors=[
        structlog.contextvars.merge_contextvars,
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso"),
        _sanitize_processor,
        structlog.dev.ConsoleRenderer(),
    ],
    wrapper_class=structlog.make_filtering_bound_logger(logging.INFO),
    cache_logger_on_first_use=True,
)

log = structlog.get_logger(__name__)


def _frontend_dist() -> Path:
    configured = os.environ.get("FRONTEND_DIST")
    if configured:
        return Path(configured)
    return Path(__file__).resolve().parents[2] / "frontend" / "dist"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    settings.ensure_dirs()
    log.info("startup", data_dir=str(settings.data_dir))
    await asyncio.to_thread(upgrade_sync)
    factory = get_session_factory()
    async with factory() as session:
        await load_overrides(session)
        await recover_interrupted(session)
    await aria2.start()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()
        await aria2.stop()
        await dispose_engine()
        log.info("shutdown")


app = FastAPI(title="PikPak Share Suite", version="0.1.0", lifespan=lifespan)

app.include_router(health.router)
app.include_router(events.router)
app.include_router(downloads.router)
app.include_router(accounts.router)
app.include_router(lineage.router)
app.include_router(settings_api.router)


@app.exception_handler(StarletteHTTPException)
async def _http_exception_handler(request: Request, exc: StarletteHTTPException):
    if request.url.path.startswith("/api"):
        detail = exc.detail
        if isinstance(detail, dict) and "error" in detail:
            content = detail
        else:
            content = {
                "error": {
                    "code": f"HTTP_{exc.status_code}",
                    "message": str(detail),
                    "retryable": False,
                    "details": {},
                }
            }
        return JSONResponse(status_code=exc.status_code, content=content)
    if exc.status_code == 404:
        return await _spa_response("")
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={
            "error": {
                "code": "VALIDATION_ERROR",
                "message": "请求参数校验失败",
                "retryable": False,
                "details": {"errors": exc.errors()},
            }
        },
    )


dist = _frontend_dist()
if (dist / "assets").is_dir():
    app.mount("/assets", StaticFiles(directory=str(dist / "assets")), name="assets")


async def _spa_response(full_path: str):
    if full_path == "api" or full_path.startswith("api/"):
        return JSONResponse(
            status_code=404,
            content={
                "error": {
                    "code": "NOT_FOUND",
                    "message": "API 不存在",
                    "retryable": False,
                    "details": {},
                }
            },
        )
    if full_path:
        candidate = (dist / full_path).resolve()
        try:
            candidate.relative_to(dist.resolve())
        except ValueError:
            candidate = None
        if candidate is not None and candidate.is_file():
            return FileResponse(candidate)
    index = dist / "index.html"
    if index.is_file():
        return FileResponse(index)
    return JSONResponse(
        status_code=404,
        content={
            "error": {
                "code": "FRONTEND_NOT_BUILT",
                "message": "前端尚未构建，请运行 pnpm --dir frontend build",
                "retryable": False,
                "details": {},
            }
        },
    )


@app.get("/")
async def root():
    return await _spa_response("")


@app.get("/{full_path:path}")
async def spa_fallback(full_path: str):
    return await _spa_response(full_path)
