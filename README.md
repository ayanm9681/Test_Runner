# LocustForge

A FastAPI-powered web UI for building and running [Locust](https://locust.io) load tests — no CLI required.

![LocustForge demo](Locust_Forge.gif)

---

## Architectural Trade-offs & Production Scaling

> This is a **lite showcase build** — intentionally simple, zero-infrastructure, single-file UI. The trade-offs below are known and accepted for that purpose. Here's what they are and how you'd fix them at scale.

| # | Limitation in this build | Root cause | Production remedy |
|---|--------------------------|------------|-------------------|
| 1 | **One test at a time, globally** | `LocustRunner` is a module-level singleton; a second `POST /api/test/start` is rejected with 409 | Replace the singleton with a job queue (Celery + Redis / RQ). Each submitted test becomes a task; results are keyed by job ID, enabling concurrent multi-tenant runs |
| 2 | **In-memory state lost on restart** | Timeseries, current metrics, and runner status live only in the Python process | Stream intermediate snapshots to Redis or MongoDB during the run so a restarted server can reconstruct live state |
| 3 | **WebSocket breaks behind a load balancer** | The `/ws/metrics` handler polls the in-process runner object; with multiple uvicorn workers or replicas, a client may connect to a worker that has no runner state | Add sticky sessions at the LB layer, or push metric events through a Redis pub/sub channel that every worker subscribes to |
| 4 | **Locust is capped to one machine** | Locust subprocess runs in headless single-process mode on the API host; max practical users is a few thousand on a small VM | Use Locust's native **master/worker** distributed mode — spin up worker pods (e.g. K8s Jobs) that the master coordinates; the API only drives the master |
| 5 | **Flat-file storage has no concurrency safety** | `locust_history.json` and `test_configs.json` are read-parse-write with no locking; two simultaneous saves can corrupt the file | Already partially solved by the MongoDB path. For local-only deployments, replace with SQLite (via `aiosqlite`) which provides proper file locking |
| 6 | **No authentication or authorization** | All API endpoints and the UI are open with `allow_origins=["*"]` CORS | Add an auth layer (OAuth2 / JWT via `fastapi-users`, or a simple API-key header check). Tighten CORS to known origins |
| 7 | **Metrics polling latency is ~3 s** | `_collect_timeseries` sleeps 3 s between CSV reads; Locust writes stats periodically to CSV | Integrate with Locust's internal `stats` object via its REST API (`/stats/requests`) at sub-second intervals, or use `locust-plugins` event hooks for push-based streaming |
| 8 | **Temp files can accumulate on crashes** | If the API process is killed mid-test, `reset()` never runs so `/tmp/locust_*` directories are orphaned | Add a startup hook that scans for and removes stale temp dirs older than N hours, or configure Docker with `tmpfs` mounts that are wiped on container restart |
| 9 | **No rate limiting** | Any client can `POST /api/test/start` in a loop | Add `slowapi` (or an Nginx/Kong gateway rule) to rate-limit test-start calls per IP / API key |
| 10 | **Script execution is fully trusted** | Generated Locust scripts are written to disk and executed as a subprocess with the same OS user as the API | Run the subprocess inside a container sandbox (e.g. `gVisor`, Docker-in-Docker, or an ephemeral K8s pod) with no network access beyond the target host |

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
| POST   | `/api/test/reset`       | Reset runner to IDLE, clean temp files|
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
