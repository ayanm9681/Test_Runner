"""History manager – persists completed test runs to MongoDB."""

import logging
import time
import uuid
from typing import Optional

from utils.mongo import collection

logger = logging.getLogger(__name__)

MAX_RECORDS = 50  # keep the last N runs


def save_run(
    config: dict,
    metrics: dict,
    timeseries: list[dict],
    script: Optional[str] = None,
) -> str:
    """Persist a finished test run to MongoDB. Returns the generated run_id."""
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

    collection.insert_one(record)

    # Keep only the latest MAX_RECORDS records
    stale = collection.find({}, {"_id": 1}).sort("ts", -1).skip(MAX_RECORDS)
    stale_ids = [doc["_id"] for doc in stale]
    if stale_ids:
        collection.delete_many({"_id": {"$in": stale_ids}})

    logger.info(f"Saved run {run_id} to MongoDB history")
    return run_id


def list_runs() -> list[dict]:
    """Return summary list (no per-endpoint stats, no script, no timeseries)."""
    out = []
    cursor = collection.find({}, {"_id": 0}).sort("ts", -1)
    for r in cursor:
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
    return collection.find_one({"run_id": run_id}, {"_id": 0})


def delete_run(run_id: str) -> bool:
    result = collection.delete_one({"run_id": run_id})
    return result.deleted_count > 0


def clear_all() -> int:
    count = collection.count_documents({})
    collection.delete_many({})
    return count


def _make_label(config: dict) -> str:
    base = config.get("base_url", "?")
    eps = config.get("endpoints", [])
    names = [e.get("name", "") for e in eps[:3]]
    suffix = ", ".join(names)
    if len(eps) > 3:
        suffix += f" +{len(eps)-3}"
    return f"{base} [{suffix}]"
