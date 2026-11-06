"""HTTP API and static web/ for the home-screen app. Skeleton: not implemented yet."""

from __future__ import annotations

from fastapi import FastAPI

app = FastAPI(title="desk")


@app.get("/api/health")
def health():
    return {"ok": True, "service": "desk-skeleton"}
