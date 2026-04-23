"""Saved test configuration manager.

Persists named endpoint configurations to local JSON or a MongoDB
'test_config' collection so they can be reloaded without re-entering
everything from scratch.
"""

import json
import logging
import threading
import time
import uuid
from pathlib import Path
from typing import Optional

from utils.mongo import client, DB_NAME

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIGS_FILE = BASE_DIR / "test_configs.json"
CONFIG_COLLECTION = "test_config"
MAX_CONFIGS = 50

_local_lock = threading.Lock()


# ── internal helpers ──────────────────────────────────────────────────────────

def _get_collection():
    if client is None:
        return None
    return client[DB_NAME][CONFIG_COLLECTION]


def _require_collection():
    col = _get_collection()
    if col is None:
        raise ValueError("MongoDB storage requires MONGO_CONNECTION configured in .env")
    return col


def _load_json() -> list[dict]:
    if CONFIGS_FILE.exists():
        try:
            return json.loads(CONFIGS_FILE.read_text(encoding="utf-8"))
        except Exception as e:
            logger.warning(f"Could not load local configs: {e}")
    return []


def _save_json(records: list[dict]) -> None:
    try:
        CONFIGS_FILE.write_text(json.dumps(records, indent=2), encoding="utf-8")
    except Exception as e:
        logger.warning(f"Could not save local configs: {e}")


def _validate_source(source: str) -> str:
    if source not in ("local", "db"):
        raise ValueError("source must be 'local' or 'db'")
    return source


def _summary(record: dict) -> dict:
    cfg = record.get("config", {})
    return {
        "config_id": record["config_id"],
        "name": record["name"],
        "ts": record["ts"],
        "base_url": cfg.get("base_url", ""),
        "endpoint_count": len(cfg.get("endpoints", [])),
        "users": cfg.get("users", 0),
        "duration": cfg.get("duration", 0),
    }


# ── public API ────────────────────────────────────────────────────────────────

def save_config(name: str, config: dict, source: str = "local") -> str:
    source = _validate_source(source)
    config_id = str(uuid.uuid4())[:8]
    record = {
        "config_id": config_id,
        "name": name.strip(),
        "ts": time.time(),
        "config": config,
    }
    if source == "db":
        col = _require_collection()
        col.insert_one(record)
        # Trim oldest beyond MAX_CONFIGS
        stale = col.find({}, {"_id": 1}).sort("ts", -1).skip(MAX_CONFIGS)
        stale_ids = [doc["_id"] for doc in stale]
        if stale_ids:
            col.delete_many({"_id": {"$in": stale_ids}})
        logger.info(f"Saved config '{name}' ({config_id}) to MongoDB")
    else:
        with _local_lock:
            records = _load_json()
            records.insert(0, record)
            _save_json(records[:MAX_CONFIGS])
        logger.info(f"Saved config '{name}' ({config_id}) to local JSON")
    return config_id


def list_configs(source: str = "local") -> list[dict]:
    source = _validate_source(source)
    if source == "db":
        col = _require_collection()
        return [_summary(r) for r in col.find({}, {"_id": 0}).sort("ts", -1)]
    return [_summary(r) for r in _load_json()]


def get_config(config_id: str, source: str = "local") -> Optional[dict]:
    source = _validate_source(source)
    if source == "db":
        col = _require_collection()
        return col.find_one({"config_id": config_id}, {"_id": 0})
    for r in _load_json():
        if r["config_id"] == config_id:
            return r
    return None


def delete_config(config_id: str, source: str = "local") -> bool:
    source = _validate_source(source)
    if source == "db":
        col = _require_collection()
        return col.delete_one({"config_id": config_id}).deleted_count > 0
    with _local_lock:
        records = _load_json()
        filtered = [r for r in records if r["config_id"] != config_id]
        if len(filtered) == len(records):
            return False
        _save_json(filtered)
    return True


def clear_all(source: str = "local") -> int:
    source = _validate_source(source)
    if source == "db":
        col = _require_collection()
        count = col.count_documents({})
        col.delete_many({})
        return count
    with _local_lock:
        records = _load_json()
        _save_json([])
    return len(records)
