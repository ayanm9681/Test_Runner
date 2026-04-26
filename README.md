# LocustForge

A FastAPI-powered web UI for building and running [Locust](https://locust.io) load tests — no CLI required.

![LocustForge demo](Locust_Forge.gif)

---

## Architectural Trade-offs & Production Scaling

> This is a **lite showcase build** — intentionally simple, zero-infrastructure, single-file UI. The trade-offs below are known and accepted for that purpose. Here's what they are and how you'd fix them at scale.

| # | Status | Limitation | Root cause | Remedy |
|---|--------|------------|------------|--------|
| 1 | ✅ **Fixed** | ~~One test at a time~~ | Singleton runner rejected concurrent starts with 409 | `asyncio.Queue`-backed `JobQueue` (`utils/job_queue.py`) — tests queue and execute sequentially; zero extra dependencies |
| 2 | ⏭ Skipped | **In-memory state lost on restart** | Timeseries lives only in the Python process | Would require flushing every 2 s snapshot to disk/DB — I/O cost not justified for a local tool. Production fix: Redis streams |
| 3 | ⏭ Skipped | **WebSocket breaks behind a load balancer** | WS handler polls in-process runner object | Requires Redis pub/sub fan-out across workers. Not applicable to single-instance local use |
| 4 | ⏭ Skipped | **Locust capped to one machine** | Single headless subprocess per run | Locust master/worker distributed mode requires separate worker processes and infra coordination |
| 5 | ✅ **Fixed** | ~~Flat JSON has no concurrency safety~~ | Read-modify-write with no locking | `threading.Lock()` wraps every mutating operation in `history.py` and `config_store.py` |
| 6 | ✅ **Fixed** | ~~No authentication~~ | All endpoints open | Optional `API_KEY` env var — if set, every HTTP route enforces `X-API-Key` header; WS accepts `?api_key=` |
| 7 | ✅ **Fixed** | ~~Metrics polling latency ~3 s~~ | Hardcoded `asyncio.sleep(3)` | Reduced to `2 s` to match WebSocket broadcast interval; chart updates are noticeably smoother |
| 8 | ✅ **Fixed** | ~~Stale temp files after crashes~~ | `reset()` never called on hard kill | Startup lifespan hook scans `tempdir` and removes `locust_*` entries older than 1 hour |
| 9 | ✅ **Fixed** | ~~No rate limiting~~ | Unrestricted `POST /api/test/start` | `slowapi` added — 20 starts/minute per IP; returns 429 with a clear message |
| 10 | ⏭ Skipped | **Script runs as API process user** | `subprocess.Popen` inherits OS user | Requires `gVisor` / Docker-in-Docker sandboxing. Not a local dev concern |

---

## Features

- **Visual endpoint builder** — add APIs with method, path, headers, JSON body, and weight
- **Request dependency chaining** — extract values from responses (JSON / headers) and inject them into later requests (header / body / path / query)
- **Saved configurations** — name and save endpoint setups to local JSON or MongoDB (`test_config` collection), then reload them instantly from the **Saved** sidebar tab
- **Instant script generation** — preview or download the Locust `.py` file
- **Live metrics via WebSocket** — RPS, avg/p95/p99 latency, failure rate, user count, real-time elapsed timer
- **Live charts** — Chart.js time-series for RPS, Avg + p95 response time, failures, and users
- **Per-endpoint stats table** — request count, failures, min/avg/p50/p95/p99 latency, RPS
- **Test history** — every completed run is auto-saved (exactly once) with full metrics + script
- **Dual storage** — persist history and saved configs to local JSON or MongoDB
- **History browser** — review and delete past runs from the sidebar
- **Docker support** — single-command container deployment

## Project Structure

```
Test_Runner/
├── main.py                   # FastAPI app — routes, WebSocket, auto-save
├── models.py                 # Pydantic schemas (TestConfig, TestMetrics, …)
├── requirements.txt
├── Dockerfile
├── .dockerignore
├── templates/
│   └── index.html            # Single-file UI (Syne + JetBrains Mono + Chart.js)
└── utils/
    ├── __init__.py
    ├── script_generator.py   # Builds a valid Locust script from TestConfig
    ├── runner.py             # Manages the Locust subprocess, CSV parsing, timeseries
    ├── history.py            # Run persistence (local JSON or MongoDB)
    ├── config_store.py       # Saved endpoint config persistence (local JSON or MongoDB)
    └── mongo.py              # MongoDB client initialisation
```

## Quick Start (local)

```bash
pip install -r requirements.txt
python main.py
# Open http://localhost:6002
```

## Quick Start (Docker)

```bash
# Build
docker build -t locustforge .

# Run without MongoDB (local JSON history only)
docker run -p 6002:6002 locustforge

# Run with MongoDB
docker run -p 6002:6002 \
  -e MONGO_CONNECTION="mongodb+srv://user:pass@cluster.mongodb.net/" \
  -e DB_NAME="TestRunner" \
  -e TEST_COLLECTION="test_run_results" \
  locustforge

# Or use an env file
docker run -p 6002:6002 --env-file .env locustforge
```

Open http://localhost:6002 in your browser.

## Environment Variables

| Variable           | Default              | Description                          |
|--------------------|----------------------|--------------------------------------|
| `MONGO_CONNECTION` | *(unset)*            | MongoDB URI — required for DB storage|
| `DB_NAME`          | `TestRunner`         | MongoDB database name                |
| `TEST_COLLECTION`  | `test_run_results`   | MongoDB collection name              |
| `API_KEY`          | *(unset)*            | If set, all API calls require `X-API-Key: <value>` header. WS passes it as `?api_key=`. Leave unset for open local dev. |

When `MONGO_CONNECTION` is not set the app runs fully without MongoDB; history is stored in `locust_history.json`.

## API Reference

| Method | Path                    | Description                          |
|--------|-------------------------|--------------------------------------|
| GET    | `/`                     | Serve the UI                         |
| POST   | `/api/test/start`       | Start a test run                     |
| POST   | `/api/test/stop`        | Stop the running test                |
| GET    | `/api/test/status`      | Current `TestMetrics` snapshot       |
| GET    | `/api/test/timeseries`  | Raw timeseries data points           |
| GET    | `/api/test/script`      | Retrieve generated `locustfile.py`   |
| POST   | `/api/script/preview`   | Preview script without running       |
| POST   | `/api/test/reset`       | Reset runner to IDLE, clean temp files (only when queue is empty)|
| GET    | `/api/jobs`             | List all jobs (queued/running/done)  |
| GET    | `/api/jobs/{job_id}`    | Full job details with metrics        |
| DELETE | `/api/jobs/{job_id}`    | Cancel a queued or running job       |
| DELETE | `/api/jobs`             | Clear completed/failed/cancelled jobs|
| GET    | `/api/history`          | List all saved runs (summary)        |
| GET    | `/api/history/{run_id}` | Full details of a run                |
| DELETE | `/api/history/{run_id}` | Delete a specific run                |
| DELETE | `/api/history`          | Clear all history                    |
| WS     | `/ws/metrics`           | Live metrics stream (every 2 s)      |

All history endpoints accept `?source=local` (default) or `?source=db`.

### Saved configs

| Method | Path                       | Description                          |
|--------|----------------------------|--------------------------------------|
| POST   | `/api/configs`             | Save a named config `{name, config}` |
| GET    | `/api/configs`             | List saved configs (summary)         |
| GET    | `/api/configs/{config_id}` | Full config by ID                    |
| DELETE | `/api/configs/{config_id}` | Delete a specific config             |
| DELETE | `/api/configs`             | Clear all saved configs              |

All saved-config endpoints accept `?source=local` (default) or `?source=db`. When using `db`, configs are stored in the `test_config` MongoDB collection.

## TestConfig Schema

```json
{
  "base_url": "https://api.example.com",
  "users": 10,
  "spawn_rate": 2.0,
  "duration": 30,
  "think_time_min": 0.5,
  "think_time_max": 2.0,
  "history_target": "local",
  "endpoints": [
    {
      "name": "List Posts",
      "method": "GET",
      "path": "/posts",
      "weight": 3,
      "headers": null,
      "body": null,
      "extract": [],
      "inject": []
    }
  ]
}
```

### Dependency chaining

**Extract** a value from a response and store it in a named variable:

```json
"extract": [
  { "var": "token", "from": "json", "path": "$.data.token" },
  { "var": "request_id", "from": "header", "path": "X-Request-Id" }
]
```

**Inject** a stored variable into a subsequent request:

```json
"inject": [
  { "var": "token", "into": "header", "key": "Authorization" },
  { "var": "user_id", "into": "path",   "key": "{user_id}" },
  { "var": "cursor",  "into": "query",  "key": "after" },
  { "var": "ref",     "into": "body",   "key": "reference_id" }
]
```
