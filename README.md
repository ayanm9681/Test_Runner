# LocustForge

A FastAPI-powered web UI for building and running [Locust](https://locust.io) load tests — no CLI required.

## Features

- **Visual endpoint builder** — add APIs with method, path, headers, JSON body, and weight
- **Request dependency chaining** — extract values from responses (JSON / headers) and inject them into later requests (header / body / path / query)
- **Instant script generation** — preview or download the Locust `.py` file
- **Live metrics via WebSocket** — RPS, avg/p95/p99 latency, failure rate, user count
- **Live charts** — Chart.js time-series for RPS, avg + p95 response time, failures, and users
- **Per-endpoint stats table** — request count, failures, min/avg/p50/p95/p99 latency, RPS
- **Test history** — every completed run is auto-saved (exactly once) with full metrics + script
- **Dual storage** — persist history to local JSON or MongoDB
- **History browser** — review and delete past runs from the sidebar

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
