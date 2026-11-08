"""Public web shell. Does not start agents or return secret values."""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from api import price_loop, router
from config import install_secret_redaction
from db import agents_enabled, db_status, heartbeat_ages, init_db

WEB_DIR = Path(__file__).resolve().parent / "web"
_INDEX_PATHS = {"/", "/index.html"}
_SHELL_ASSETS = {"/app.js", "/app.css", "/chart.js", "/sw.js", "/manifest.json"}

_SECURITY_HEADERS = (
    (b"x-content-type-options", b"nosniff"),
    (b"x-frame-options", b"DENY"),
    (b"referrer-policy", b"no-referrer"),
    (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
    (
        b"content-security-policy",
        b"default-src 'self'; script-src 'self'; style-src 'self'; "
        b"img-src 'self' data:; manifest-src 'self'; worker-src 'self'; "
        b"connect-src 'self'; base-uri 'self'; form-action 'self'",
    ),
)


def ensure_data_dir() -> None:
    """Create DESK_DATA_DIR when missing. Default is ./data. Write nothing else.

    The path and every other environment value stay out of logs and responses.
    """
    raw = os.environ.get("DESK_DATA_DIR") or "./data"
    try:
        Path(raw).mkdir(parents=True, exist_ok=True)
    except OSError:
        raise RuntimeError("DESK_DATA_DIR is not usable") from None


class SecurityHeaders:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        try:
            await self._http(scope, receive, send)
        except HTTPException:
            raise
        except Exception:
            response = JSONResponse(status_code=500, content={"detail": "error"})
            await response(scope, receive, send)

    async def _http(self, scope, receive, send):
        path = scope.get("path") or ""

        async def send_with_headers(message):
            if message["type"] == "http.response.start":
                headers = list(message.get("headers") or [])
                names = {name.lower() for name, _ in headers}
                for name, value in _SECURITY_HEADERS:
                    if name not in names:
                        headers.append((name, value))
                if path in _INDEX_PATHS and b"cache-control" not in names:
                    headers.append((b"cache-control", b"no-store"))
                elif path in _SHELL_ASSETS and b"cache-control" not in names:
                    headers.append((b"cache-control", b"no-cache"))
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_data_dir()
    init_db()
    install_secret_redaction()
    agent_task = None
    price_stop = asyncio.Event()
    price_task = None
    from health import PROCESS, process_heartbeat, supervisor
    beat_stop = asyncio.Event()
    beat_task = asyncio.create_task(process_heartbeat(beat_stop))
    supervisor.register(PROCESS, lambda: process_heartbeat(beat_stop))
    # Empty mint lists do not call DexScreener. Agents stay off unless enabled.
    # DESK_PRICE_LOOP=0 is for a local screenshot process only. Render does not set it.
    if os.environ.get("DESK_PRICE_LOOP") != "0":
        price_task = asyncio.create_task(price_loop(price_stop, fetch=_fetch_prices))
    import desk_loop
    if desk_loop.scheduler_status()["armed"] or desk_loop.scheduler_status()["started"]:
        raise RuntimeError("memecoin desk scheduler is not allowed to start")
    if agents_enabled():
        from main import run_desk
        agent_task = asyncio.create_task(run_desk())
    try:
        yield
    finally:
        price_stop.set()
        if price_task is not None:
            price_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await price_task
        beat_stop.set()
        beat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await beat_task
        if agent_task is not None:
            agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await agent_task


app = FastAPI(title="desk", lifespan=lifespan)
app.add_middleware(SecurityHeaders)
app.include_router(router)


@app.exception_handler(HTTPException)
async def http_error(_request, exc: HTTPException):
    detail = exc.detail if isinstance(exc.detail, str) else "error"
    return JSONResponse(status_code=exc.status_code, content={"detail": detail})


@app.exception_handler(Exception)
async def unexpected_error(_request, _exc: Exception):
    return JSONResponse(status_code=500, content={"detail": "error"})


async def _fetch_prices(url: str):
    import httpx
    async with httpx.AsyncClient() as client:
        response = await client.get(url, timeout=20)
        if response.status_code == 429:
            return []
        response.raise_for_status()
        return response.json()


@app.get("/health")
def health():
    return {
        "ok": True,
        "step": 12,
        "db": db_status(),
        "agents_enabled": agents_enabled(),
        "heartbeats": heartbeat_ages(),
    }


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
