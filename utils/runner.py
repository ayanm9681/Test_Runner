import asyncio
import csv
import subprocess
import time
import os
import signal
import tempfile
import json
import logging
from typing import Optional

from models import TestConfig, TestMetrics, TestStatus, RequestStat
from utils.script_generator import generate_locust_script

logger = logging.getLogger(__name__)


class LocustRunner:
    """Manages a single Locust test run as a subprocess with CSV stats collection."""

    def __init__(self):
        self._process: Optional[subprocess.Popen] = None
        self._script_path: Optional[str] = None
        self._stats_dir: Optional[str] = None
        self._config: Optional[TestConfig] = None
        self._status: TestStatus = TestStatus.IDLE
        self._start_time: Optional[float] = None
        self._end_time: Optional[float] = None
        self._stats_file: Optional[str] = None
        self.timeseries: list[dict] = []
        self._ts_task: Optional[asyncio.Task] = None

    @property
    def status(self) -> TestStatus:
        if self._process is None:
            return self._status
        poll = self._process.poll()
        if poll is None:
            return TestStatus.RUNNING
        if self._status == TestStatus.STOPPING:
            return TestStatus.COMPLETED
        return TestStatus.COMPLETED if poll == 0 else TestStatus.FAILED

    def is_running(self) -> bool:
        return self.status == TestStatus.RUNNING

    async def start(self, config: TestConfig) -> None:
        if self.is_running():
            raise RuntimeError("A test is already running. Stop it first.")

        self._config = config
        self._status = TestStatus.RUNNING
        self._start_time = time.time()
        self.timeseries = []

        script_content = generate_locust_script(config)
        tmp = tempfile.NamedTemporaryFile(suffix=".py", delete=False, mode="w", prefix="locust_")
        tmp.write(script_content)
        tmp.flush()
        tmp.close()
        self._script_path = tmp.name

        self._stats_dir = tempfile.mkdtemp(prefix="locust_stats_")
        self._stats_file = os.path.join(self._stats_dir, "stats")

        cmd = [
            "locust", "-f", self._script_path,
            "--headless",
            "--users", str(config.users),
            "--spawn-rate", str(config.spawn_rate),
            "--run-time", f"{config.duration}s",
            "--csv", self._stats_file,
            "--csv-full-history",
            "--host", config.base_url,
            "--logfile", os.path.join(self._stats_dir, "locust.log"),
        ]

        logger.info(f"Starting locust: {' '.join(cmd)}")
        self._process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, preexec_fn=os.setsid)

        asyncio.create_task(self._watch_process())
        self._ts_task = asyncio.create_task(self._collect_timeseries())

    async def stop(self) -> None:
        if self._process and self._process.poll() is None:
            self._status = TestStatus.STOPPING
            try:
                os.killpg(os.getpgid(self._process.pid), signal.SIGTERM)
                await asyncio.sleep(2)
                if self._process.poll() is None:
                    os.killpg(os.getpgid(self._process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass
        self._status = TestStatus.COMPLETED

    async def _watch_process(self):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._process.wait)
        if self._status == TestStatus.RUNNING:
            self._status = TestStatus.COMPLETED
        self._end_time = time.time()
        logger.info(f"Locust process ended with returncode {self._process.returncode}")

    async def _collect_timeseries(self):
        while self.is_running():
            await asyncio.sleep(3)
            try:
                snap = self._parse_aggregate()
                if snap:
                    snap["t"] = round(time.time() - self._start_time, 1)
                    self.timeseries.append(snap)
            except Exception as e:
                logger.debug(f"Timeseries error: {e}")

    def _parse_aggregate(self) -> Optional[dict]:
        stats_csv = (self._stats_file or "") + "_stats.csv"
        if not os.path.exists(stats_csv):
            return None
        with open(stats_csv, newline="") as f:
            for row in csv.DictReader(f):
                if row.get("Name", "").strip() == "Aggregated":
                    def sf(v):
                        try: return float(v or 0)
                        except: return 0.0
                    def si(v):
                        try: return int(v or 0)
                        except: return 0
                    return {
                        "rps":       round(sf(row.get("Requests/s", 0)), 2),
                        "avg_rt":    round(sf(row.get("Average Response Time", 0)), 1),
                        "p95_rt":    round(sf(row.get("95%", 0)), 1),
                        "total_req": si(row.get("Request Count", 0)),
                        "failures":  si(row.get("Failure Count", 0)),
                        "users":     self._config.users if self._config else 0,
                    }
        return None

    def get_metrics(self) -> TestMetrics:
        elapsed = (time.time() - self._start_time) if self._start_time else 0.0
        stats_list: list[RequestStat] = []
        errors_list = []
        total_requests = 0
        total_failures = 0
        total_rps = 0.0
        avg_rt = 0.0
        p95_rt = 0.0

        stats_csv = (self._stats_file or "") + "_stats.csv"
        if os.path.exists(stats_csv):
            try:
                with open(stats_csv, newline="") as f:
                    rows = list(csv.DictReader(f))
                aggregate = None
                endpoint_rows = []
                for row in rows:
                    if row.get("Name", "").strip() == "Aggregated":
                        aggregate = row
                    else:
                        endpoint_rows.append(row)

                def sf(v):
                    try: return float(v or 0)
                    except: return 0.0
                def si(v):
                    try: return int(v or 0)
                    except: return 0

                for row in endpoint_rows:
                    nr = si(row.get("Request Count", 0))
                    nf = si(row.get("Failure Count", 0))
                    stats_list.append(RequestStat(
                        name=row.get("Name", ""),
                        method=row.get("Type", ""),
                        num_requests=nr,
                        num_failures=nf,
                        avg_response_time=sf(row.get("Average Response Time", 0)),
                        min_response_time=sf(row.get("Min Response Time", 0)),
                        max_response_time=sf(row.get("Max Response Time", 0)),
                        p50=sf(row.get("50%", 0)),
                        p95=sf(row.get("95%", 0)),
                        p99=sf(row.get("99%", 0)),
                        rps=sf(row.get("Requests/s", 0)),
                        failure_rate=(nf / nr * 100) if nr > 0 else 0.0,
                    ))
                if aggregate:
                    total_requests = si(aggregate.get("Request Count", 0))
                    total_failures = si(aggregate.get("Failure Count", 0))
                    total_rps = sf(aggregate.get("Requests/s", 0))
                    avg_rt = sf(aggregate.get("Average Response Time", 0))
                    p95_rt = sf(aggregate.get("95%", 0))
            except Exception as e:
                logger.warning(f"Could not parse stats CSV: {e}")

        errors_csv = (self._stats_file or "") + "_failures.csv"
        if os.path.exists(errors_csv):
            try:
                with open(errors_csv, newline="") as f:
                    for row in csv.DictReader(f):
                        errors_list.append(dict(row))
            except Exception as e:
                logger.warning(f"Could not parse errors CSV: {e}")

        return TestMetrics(
            status=self.status,
            elapsed=round(elapsed, 1),
            total_requests=total_requests,
            total_failures=total_failures,
            rps=round(total_rps, 2),
            avg_response_time=round(avg_rt, 1),
            p95_response_time=round(p95_rt, 1),
            user_count=self._config.users if self._config else 0,
            stats=stats_list,
            errors=errors_list,
        )

    def get_script(self) -> Optional[str]:
        if self._script_path and os.path.exists(self._script_path):
            with open(self._script_path) as f:
                return f.read()
        return None

    def reset(self):
        if self.is_running():
            raise RuntimeError("Cannot reset while test is running.")
        self._process = None
        self._config = None
        self._status = TestStatus.IDLE
        self._start_time = None
        self._end_time = None
        self.timeseries = []
