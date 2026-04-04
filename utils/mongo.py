import os
from pathlib import Path

from dotenv import load_dotenv
from pymongo import MongoClient
from pymongo.collection import Collection

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    if isinstance(value, str):
        return value.strip().strip("'\" ")
    return value


MONGO_URI = _env("MONGO_CONNECTION") or _env("MONGO_URI")
if not MONGO_URI:
    raise RuntimeError("Missing MONGO_CONNECTION or MONGO_URI in .env")

DB_NAME = _env("DB_NAME") or "TestRunner"
COLLECTION_NAME = _env("TEST_COLLECTION") or "test_run_results"

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[DB_NAME]
collection: Collection = db[COLLECTION_NAME]


def connect_mongo() -> None:
    try:
        client.admin.command("ping")
    except Exception as exc:
        raise RuntimeError(f"Unable to connect to MongoDB at {MONGO_URI}: {exc}") from exc


def close_mongo() -> None:
    client.close()
