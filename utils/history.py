"""History manager – persists completed test runs to local JSON or MongoDB."""

import json
import logging
import time
import uuid
from pathlib import Path
from typing import Optional

from utils.mongo import collection

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
HISTORY_FILE = BASE_DIR / "locust_history.json"
MAX_RECORDS = 50  # keep the last N runs


def _load_json() -> list[dict]:
    if HISTORY_FILE.exists():
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not load local history: {e}")
    return []


def _save_json(records: list[dict]) -> None:
    try:
        HISTORY_FILE.write_text(json.dumps(records, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not save local history: {e}")


def _make_label(config: dict) -> str:
    base = config.get("base_url", "?")
    eps = config.get("endpoints", [])
    names = [e.get("name", "") for e in eps[:3]]
    suffix = ", ".join(names)
    if len(eps) > 3:
        suffix += f" +{len(eps)-3}"
    return f"{base} [{suffix}]"


def _require_db_collection() -> None:
    if collection is None:
        raise ValueError("MongoDB storage requires MONGO_CONNECTION configured in .env")


def _save_run_local(record: dict) -> str:
    records = _load_json()
    records.insert(0, record)
    records = records[:MAX_RECORDS]
    _save_json(records)
    logger.info(f"Saved run {record['run_id']} to local JSON history")
    return record["run_id"]


def _save_run_db(record: dict) -> str:
    _require_db_collection()
    collection.insert_one(record)
    stale = collection.find({}, {"_id": 1}).sort("ts", -1).skip(MAX_RECORDS)
    stale_ids = [doc["_id"] for doc in stale]
    if stale_ids:
        collection.delete_many({"_id": {"$in": stale_ids}})
    logger.info(f"Saved run {record['run_id']} to MongoDB history")
    return record["run_id"]


def _list_runs_local() -> list[dict]:
    out = []
    for r in _load_json():
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


def _list_runs_db() -> list[dict]:
    _require_db_collection()
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


def _get_run_local(run_id: str) -> Optional[dict]:
    for r in _load_json():
        if r["run_id"] == run_id:
            return r
    return None


def _get_run_db(run_id: str) -> Optional[dict]:
    _require_db_collection()
    return collection.find_one({"run_id": run_id}, {"_id": 0})


def _delete_run_local(run_id: str) -> bool:
    records = _load_json()
    filtered = [r for r in records if r["run_id"] != run_id]
    if len(filtered) == len(records):
        return False
    _save_json(filtered)
    return True


def _delete_run_db(run_id: str) -> bool:
    _require_db_collection()
    result = collection.delete_one({"run_id": run_id})
    return result.deleted_count > 0


def _clear_all_local() -> int:
    records = _load_json()
    _save_json([])
    return len(records)


def _clear_all_db() -> int:
    _require_db_collection()
    count = collection.count_documents({})
    collection.delete_many({})
    return count


def _validate_source(source: str) -> str:
    if source not in ("local", "db"):
        raise ValueError("source must be 'local' or 'db'")
    return source


def save_run(
    config: dict,
    metrics: dict,
    timeseries: list[dict],
    script: Optional[str] = None,
    source: str = "local",
) -> str:
    source = _validate_source(source)
    run_id = str(uuid.uuid4())[:8]
    record = {
        "run_id": run_id,
        "ts": time.time(),
        "label": _make_label(config),
        "config": config,
        "metrics": metrics,
        "timeseries": timeseries,
        "script": script,
        "storage": source,
    }
    if source == "db":
        return _save_run_db(record)
    return _save_run_local(record)


def list_runs(source: str = "local") -> list[dict]:
    source = _validate_source(source)
    return _list_runs_db() if source == "db" else _list_runs_local()


def get_run(run_id: str, source: str = "local") -> Optional[dict]:
    source = _validate_source(source)
    return _get_run_db(run_id) if source == "db" else _get_run_local(run_id)


def delete_run(run_id: str, source: str = "local") -> bool:
    source = _validate_source(source)
    return _delete_run_db(run_id) if source == "db" else _delete_run_local(run_id)


def clear_all(source: str = "local") -> int:
    source = _validate_source(source)
    return _clear_all_db() if source == "db" else _clear_all_local()
