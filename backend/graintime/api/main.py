"""Internal api for the management app, admin panel and setup wizard."""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

from ..common.db import get_engine
from ..common.logging import get_logger, setup_logging
from . import routes_auth_admin, routes_dashboards, routes_profiles, routes_setup, routes_sites, routes_stats

setup_logging("api")
log = get_logger("api")

app = FastAPI(title="GrainTime api", docs_url=None, redoc_url=None, openapi_url=None)

MUTATING = {"POST", "PUT", "PATCH", "DELETE"}


@app.middleware("http")
async def csrf_guard(request: Request, call_next):
    # Session cookies are SameSite=Strict; additionally every state-changing
    # call must carry a custom header, which cross-site forms cannot send.
    if request.method in MUTATING and request.url.path.startswith("/api/") \
            and request.headers.get("x-graintime") != "1":
        return JSONResponse({"detail": "Missing X-GrainTime header"}, status_code=403)
    return await call_next(request)


@app.get("/api/health")
def health():
    try:
        with get_engine().connect() as c:
            c.execute(text("SELECT 1"))
        return {"status": "ok"}
    except Exception as exc:
        log.warning("health check failed", extra={"error": str(exc)[:200]})
        return JSONResponse({"status": "unhealthy", "db": False}, status_code=503)


app.include_router(routes_setup.router)
app.include_router(routes_sites.router)
app.include_router(routes_profiles.router)
app.include_router(routes_stats.router)
app.include_router(routes_dashboards.router)
app.include_router(routes_auth_admin.router)
