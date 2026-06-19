"""FastAPI application — entry point for the benchmark web panel.

Run with:
    uvicorn web.main:app --reload --port 8765
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles

from web.api.routers import benchmarks, runs
from web.db.database import close_db, init_db
from web.engine.orchestrator import orchestrator
from web.engine.patch_microbench import patch_microbench

# Apply patch for microbench tasks path
patch_microbench()


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup
    await init_db()
    orchestrator.set_loop(asyncio.get_running_loop())
    yield
    # Shutdown
    await close_db()


app = FastAPI(
    title="Harness Bench Panel",
    description="Web panel for harness-bench-fast benchmark",
    version="0.1.0",
    lifespan=lifespan,
)

# CORS — allow frontend dev server
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API routers
app.include_router(runs.router)
app.include_router(benchmarks.router)


# Health check
@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "harness-bench-panel"}


# Serve frontend static files if built
_frontend_dist = Path(__file__).parent.parent / "frontend" / "out"
if _frontend_dist.is_dir():
    app.mount("/", StaticFiles(directory=str(_frontend_dist), html=True), name="frontend")
