"""Public web shell. Does not start agents or return secret values."""

from __future__ import annotations

import asyncio
import contextlib
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from config import install_secret_redaction
from db import agents_enabled, db_status, heartbeat_ages, init_db

WEB_DIR = Path(__file__).resolve().parent / "web"
_INDEX_PATHS = {"/", "/index.html"}

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
                message["headers"] = headers
            await send(message)

        await self.app(scope, receive, send_with_headers)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_data_dir()
    init_db()
    install_secret_redaction()
    agent_task = None
    # Default deploy keeps the five agents off so nothing calls Jev or RPC.
    if agents_enabled():
        from main import run_desk
        agent_task = asyncio.create_task(run_desk())
    try:
        yield
    finally:
        if agent_task is not None:
            agent_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await agent_task


app = FastAPI(title="desk", lifespan=lifespan)
app.add_middleware(SecurityHeaders)


@app.get("/health")
def health():
    return {
        "ok": True,
        "step": 5,
        "db": db_status(),
        "agents_enabled": agents_enabled(),
        "heartbeats": heartbeat_ages(),
    }


app.mount("/", StaticFiles(directory=str(WEB_DIR), html=True), name="web")
