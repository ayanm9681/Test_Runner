import asyncio
import logging
import os
import shutil
import tempfile
import time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.util import get_remote_address
from starlette.requests import HTTPConnection

from models import TestConfig, TestMetrics, TestStatus, SaveConfigRequest
from utils.job_queue import JobQueue
from utils.script_generator import generate_locust_script
from utils.mongo import close_mongo
from utils import history as hist
from utils import config_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

# ── Optional API-key auth ──────────────────────────────────────────────────────
# Set API_KEY in .env to enable. Leave unset to run open (local dev default).
_API_KEY = os.getenv("API_KEY", "").strip()


async def _check_api_key(conn: HTTPConnection) -> None:
    """Works for both HTTP (header) and WebSocket (header or query param)."""
    if not _API_KEY:
        return
    key = conn.headers.get("x-api-key", "") or conn.query_params.get("api_key", "")
    if key != _API_KEY:
        raise HTTPException(status_code=403, detail="Invalid or missing X-API-Key header")


# ── Rate limiter ───────────────────────────────────────────────────────────────
limiter = Limiter(key_func=get_remote_address, default_limits=[])

# ── Job queue (singleton) ──────────────────────────────────────────────────────
job_queue = JobQueue()


# ── Startup / shutdown ────────────────────────────────────────────────────────

def _cleanup_stale_temp_files() -> None:
    """Remove locust_* temp dirs/files older than 1 h left by previous crashes."""
    tmp = Path(tempfile.gettempdir())
    cutoff = time.time() - 3600
    cleaned = 0
    for p in tmp.glob("locust_*"):
        try:
            if p.stat().st_mtime < cutoff:
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                cleaned += 1
        except OSError:
            pass
    if cleaned:
        logger.info(f"Startup: removed {cleaned} stale locust temp file(s)")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _cleanup_stale_temp_files()
    job_queue.start()
    logger.info("LocustForge API starting up")
    yield
    await job_queue.shutdown()
    close_mongo()
    logger.info("LocustForge API shut down")


# ── App ────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="LocustForge",
    description="API load testing tool powered by Locust",
    version="1.2.0",
    lifespan=lifespan,
    dependencies=[Depends(_check_api_key)],
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse, dependencies=[])   # UI is always open
async def root():
    path = BASE_DIR / "templates" / "index.html"
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── Test control ──────────────────────────────────────────────────────────────

@app.post("/api/test/start")
@limiter.limit("20/minute")
async def start_test(request: Request, config: TestConfig):
    try:
        job_id, position = await job_queue.submit(config)
        if position == 0:
            msg = "Test started"
            status = TestStatus.RUNNING
        else:
            msg = f"Test queued at position {position + 1}"
            status = "queued"
        return {"message": msg, "status": status, "job_id": job_id, "queue_position": position}
    except RuntimeError as e:
        raise HTTPException(status_code=429, detail=str(e))
    except Exception as e:
        logger.error(f"Failed to queue test: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/test/stop")
async def stop_test():
    if not job_queue.is_running():
        raise HTTPException(status_code=409, detail="No test is currently running.")
    await job_queue.stop_current()
    return {"message": "Current test stopped; queue continues", "status": TestStatus.COMPLETED}


@app.get("/api/test/status")
async def get_status() -> TestMetrics:
    return job_queue.runner.get_metrics()


@app.get("/api/test/timeseries")
async def get_timeseries():
    return {"timeseries": job_queue.runner.timeseries}


@app.get("/api/test/script")
async def get_script():
    script = job_queue.runner.get_script()
    if not script:
        raise HTTPException(status_code=404, detail="No script generated yet.")
    return {"script": script}


@app.post("/api/script/preview")
async def preview_script(config: TestConfig):
    return {"script": generate_locust_script(config)}


@app.post("/api/test/reset")
async def reset_test():
    if job_queue.is_running() or job_queue.queue_depth() > 0:
        raise HTTPException(
            status_code=409,
            detail="Cannot reset while a test is running or jobs are queued.",
        )
    try:
        job_queue.runner.reset()
        return {"message": "Runner reset", "status": TestStatus.IDLE}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


# ── Job queue ─────────────────────────────────────────────────────────────────

@app.get("/api/jobs")
async def list_jobs():
    return {"jobs": job_queue.list_jobs()}


@app.get("/api/jobs/{job_id}")
async def get_job(job_id: str):
    job = job_queue.get_job(job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job.to_dict()


@app.delete("/api/jobs/{job_id}")
async def cancel_job(job_id: str):
    cancelled = await job_queue.cancel_job(job_id)
    if not cancelled:
        raise HTTPException(status_code=404, detail="Job not found or already finished")
    return {"cancelled": job_id}


@app.delete("/api/jobs")
async def clear_finished_jobs():
    n = job_queue.clear_inactive_jobs()
    return {"cleared": n}


# ── History ───────────────────────────────────────────────────────────────────

@app.get("/api/history")
async def list_history(source: str = "local"):
    try:
        return {"runs": hist.list_runs(source)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/history/{run_id}")
async def get_history_run(run_id: str, source: str = "local"):
    try:
        run = hist.get_run(run_id, source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not run:
        raise HTTPException(status_code=404, detail="Run not found")
    return run


@app.delete("/api/history/{run_id}")
async def delete_history_run(run_id: str, source: str = "local"):
    try:
        deleted = hist.delete_run(run_id, source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Run not found")
    return {"deleted": run_id}


@app.delete("/api/history")
async def clear_history(source: str = "local"):
    try:
        n = hist.clear_all(source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"cleared": n}


# ── Saved configs ─────────────────────────────────────────────────────────────

@app.post("/api/configs")
async def save_config(req: SaveConfigRequest, source: str = "local"):
    try:
        config_id = config_store.save_config(
            name=req.name,
            config=req.config.model_dump(),
            source=source,
        )
        return {"config_id": config_id, "name": req.name}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/configs")
async def list_configs(source: str = "local"):
    try:
        return {"configs": config_store.list_configs(source)}
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/configs/{config_id}")
async def get_config(config_id: str, source: str = "local"):
    try:
        cfg = config_store.get_config(config_id, source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not cfg:
        raise HTTPException(status_code=404, detail="Config not found")
    return cfg


@app.delete("/api/configs/{config_id}")
async def delete_config(config_id: str, source: str = "local"):
    try:
        deleted = config_store.delete_config(config_id, source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    if not deleted:
        raise HTTPException(status_code=404, detail="Config not found")
    return {"deleted": config_id}


@app.delete("/api/configs")
async def clear_configs(source: str = "local"):
    try:
        n = config_store.clear_all(source)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    return {"cleared": n}


# ── WebSocket ─────────────────────────────────────────────────────────────────

@app.websocket("/ws/metrics")
async def metrics_websocket(ws: WebSocket):
    await ws.accept()
    logger.info("WebSocket client connected")
    try:
        while True:
            metrics = job_queue.runner.get_metrics()
            payload = metrics.model_dump()
            payload["timeseries"]        = job_queue.runner.timeseries
            payload["queue_depth"]       = job_queue.queue_depth()
            payload["current_job_id"]    = job_queue._current_job_id
            payload["current_duration"]  = job_queue.current_duration()
            await ws.send_json(payload)
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.warning(f"WebSocket error: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=6002, reload=True)
