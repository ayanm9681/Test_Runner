import asyncio
import logging
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse

from models import TestConfig, TestMetrics, TestStatus, SaveConfigRequest
from utils.runner import LocustRunner
from utils.script_generator import generate_locust_script
from utils.mongo import close_mongo
from utils import history as hist
from utils import config_store

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
runner = LocustRunner()


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("LocustForge API starting up")
    yield
    if runner.is_running():
        await runner.stop()
    close_mongo()
    logger.info("MongoDB connection closed")
    logger.info("LocustForge API shut down")


app = FastAPI(
    title="LocustForge",
    description="API load testing tool powered by Locust",
    version="1.1.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ── UI ────────────────────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def root():
    path = BASE_DIR / "templates" / "index.html"
    with open(path, encoding="utf-8") as f:
        return f.read()


# ── Test control ──────────────────────────────────────────────────────────────

@app.post("/api/test/start")
async def start_test(config: TestConfig):
    if runner.is_running():
        raise HTTPException(status_code=409, detail="A test is already running. Stop it first.")
    try:
        runner.history_target = config.history_target
        await runner.start(config)
        return {"message": "Test started", "status": TestStatus.RUNNING}
    except Exception as e:
        logger.error(f"Failed to start test: {e}")
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/test/stop")
async def stop_test():
    if not runner.is_running():
        raise HTTPException(status_code=409, detail="No test is currently running.")
    await runner.stop()
    _auto_save(runner.history_target if hasattr(runner, "history_target") else "local")
    return {"message": "Test stopped", "status": TestStatus.COMPLETED}


@app.get("/api/test/status")
async def get_status() -> TestMetrics:
    return runner.get_metrics()


@app.get("/api/test/timeseries")
async def get_timeseries():
    return {"timeseries": runner.timeseries}


@app.get("/api/test/script")
async def get_script():
    script = runner.get_script()
    if not script:
        raise HTTPException(status_code=404, detail="No script generated yet.")
    return {"script": script}


@app.post("/api/script/preview")
async def preview_script(config: TestConfig):
    script = generate_locust_script(config)
    return {"script": script}


@app.post("/api/test/reset")
async def reset_test():
    try:
        runner.reset()
        return {"message": "Runner reset", "status": TestStatus.IDLE}
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))


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
    was_running = False
    try:
        while True:
            metrics = runner.get_metrics()
            payload = metrics.model_dump()
            payload["timeseries"] = runner.timeseries
            await ws.send_json(payload)

            # Auto-save once when test finishes
            if was_running and metrics.status in (TestStatus.COMPLETED, TestStatus.FAILED):
                _auto_save(runner.history_target if hasattr(runner, "history_target") else "local")

            was_running = runner.is_running()
            await asyncio.sleep(2)
    except WebSocketDisconnect:
        logger.info("WebSocket client disconnected")
    except Exception as e:
        logger.warning(f"WebSocket error: {e}")


def _auto_save(source: str = "local"):
    """Save the last completed run to history (exactly once per run)."""
    try:
        if runner._config is None or runner._run_saved:
            return
        metrics = runner.get_metrics()
        run_id = hist.save_run(
            config=runner._config.model_dump(),
            metrics=metrics.model_dump(),
            timeseries=runner.timeseries,
            script=runner.get_script(),
            source=source,
        )
        runner._run_saved = True
        logger.info(f"Auto-saved run {run_id} to {source} history")
    except Exception as e:
        logger.warning(f"Auto-save failed: {e}")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="127.0.0.1", port=6002, reload=True)
