"""
History manager – persists completed test runs to a local JSON file.
Each record stores the config, final aggregate metrics, per-endpoint stats,
and the full time-series so the UI can replay charts.
"""

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

HISTORY_FILE = Path("locust_history.json")
MAX_RECORDS = 50   # keep the last N runs


def _load() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text())
        except Exception as e:
            logger.warning(f"Could not load history: {e}")
    return []


def _save(records: list[dict]) -> None:
    try:
        HISTORY_FILE.write_text(json.dumps(records, indent=2))
    except Exception as e:
        logger.warning(f"Could not save history: {e}")


def save_run(
    config: dict,
    metrics: dict,
    timeseries: list[dict],
    script: Optional[str] = None,
) -> str:
    """Persist a finished test run. Returns the generated run_id."""
    run_id = str(uuid.uuid4())[:8]
    record = {
        "run_id": run_id,
        "ts": time.time(),
        "label": _make_label(config),
        "config": config,
        "metrics": metrics,
        "timeseries": timeseries,
        "script": script,
    }
    records = _load()
    records.insert(0, record)
    records = records[:MAX_RECORDS]
    _save(records)
    logger.info(f"Saved run {run_id} to history ({len(records)} total)")
    return run_id


def list_runs() -> list[dict]:
    """Return summary list (no per-endpoint stats, no script, no timeseries)."""
    out = []
    for r in _load():
        m = r.get("metrics", {})
        out.append({
            "run_id": r["run_id"],
            "ts": r["ts"],
            "label": r.get("label", ""),
            "base_url": r.get("config", {}).get("base_url", ""),
            "users": r.get("config", {}).get("users", 0),
            "duration": r.get("config", {}).get("duration", 0),
            "total_requests": m.get("total_requests", 0),
            "total_failures": m.get("total_failures", 0),
            "rps": m.get("rps", 0),
            "avg_response_time": m.get("avg_response_time", 0),
            "p95_response_time": m.get("p95_response_time", 0),
            "status": m.get("status", ""),
        })
    return out


def get_run(run_id: str) -> Optional[dict]:
    for r in _load():
        if r["run_id"] == run_id:
            return r
    return None


def delete_run(run_id: str) -> bool:
    records = _load()
    filtered = [r for r in records if r["run_id"] != run_id]
    if len(filtered) == len(records):
        return False
    _save(filtered)
    return True


def clear_all() -> int:
    records = _load()
    _save([])
    return len(records)


def _make_label(config: dict) -> str:
    base = config.get("base_url", "?")
    eps = config.get("endpoints", [])
    names = [e.get("name", "") for e in eps[:3]]
    suffix = ", ".join(names)
    if len(eps) > 3:
        suffix += f" +{len(eps)-3}"
    return f"{base} [{suffix}]"
