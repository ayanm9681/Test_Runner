# LocustForge 🔥

A FastAPI-powered web UI for building and running [Locust](https://locust.io) load tests — no CLI required.

## Features
- **Visual endpoint builder** — add APIs with method, path, headers, JSON body, and weight
- **Instant script generation** — preview or download the Locust `.py` file
- **Live metrics via WebSocket** — RPS, avg/p95/p99 latency, failure rate, user count
- **Live charts** — Chart.js powered time-series for RPS, response time, failures, users
- **Per-endpoint stats table** — request count, failures, latency percentiles
- **Test history** — every completed run is auto-saved with full metrics + script
- **History browser** — review, compare, and delete past runs from the sidebar

## Project Structure

```
locust_tool/
├── main.py                   # FastAPI app — routes + WebSocket + auto-save
├── models.py                 # Pydantic schemas
├── requirements.txt
├── templates/
│   └── index.html            # Single-file UI (Syne + JetBrains Mono + Chart.js)
└── utils/
    ├── __init__.py
    ├── script_generator.py   # Builds valid Locust Python from TestConfig
    ├── runner.py             # Manages Locust subprocess + CSV parsing + timeseries
    └── history.py            # JSON-file based run persistence
```

## Quick Start

```bash
pip install -r requirements.txt
python main.py
# Open http://localhost:8000
```

## API Reference

| Method   | Path                      | Description                        |
|----------|---------------------------|------------------------------------|
| GET      | `/`                       | Serve the UI                       |
| POST     | `/api/test/start`         | Start a test run                   |
| POST     | `/api/test/stop`          | Stop the running test              |
| GET      | `/api/test/status`        | Current TestMetrics snapshot       |
| GET      | `/api/test/timeseries`    | Raw timeseries data points         |
| GET      | `/api/test/script`        | Retrieve generated locustfile.py   |
| POST     | `/api/script/preview`     | Preview script without running     |
| POST     | `/api/test/reset`         | Reset runner to IDLE               |
| GET      | `/api/history`            | List all saved runs (summary)      |
| GET      | `/api/history/{run_id}`   | Full details of a run              |
| DELETE   | `/api/history/{run_id}`   | Delete a specific run              |
| DELETE   | `/api/history`            | Clear all history                  |
| WS       | `/ws/metrics`             | Live metrics stream (every 2s)     |

## TestConfig Schema

```json
{
  "base_url": "https://api.example.com",
  "users": 10,
  "spawn_rate": 2.0,
  "duration": 30,
  "think_time_min": 0.5,
  "think_time_max": 2.0,
  "endpoints": [
    {
      "name": "List Posts",
      "method": "GET",
      "path": "/posts",
      "weight": 3,
      "headers": null,
      "body": null
    }
  ]
}
```
