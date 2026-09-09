"""
Small shared helpers used by every agent module in this package.

Not itself an agent — just avoids duplicating the same JSONL-logging and
JSON-fence-stripping snippets in agents_monitor.py / agents_orchestrator.py /
agents_analysis.py / agents_incident.py.
"""

import json
import logging
import time
from pathlib import Path

logger = logging.getLogger("agents.common")

BASE_DIR = Path(__file__).resolve().parent
LOG_DIR = BASE_DIR / "logs"
LOG_DIR.mkdir(exist_ok=True)


def strip_json_fences(text: str) -> str:
    """Claude sometimes wraps a JSON response in ```json ... ``` even when
    told not to — strip that before json.loads()."""
    return text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()


def extract_usage(message, agent: str) -> dict:
    """Every nested LLM call in this pipeline gets back a ResultMessage
    carrying real cost/token figures (message.total_cost_usd, message.usage)
    — this just standardizes pulling them into one small dict so every
    caller (agents_monitor/analysis/decisioning/orchestrator) can append the
    same shape onto a per-test tally. cost_usd is the SDK's own computed
    figure at list token pricing — under Claude Code subscription auth (as
    opposed to a raw ANTHROPIC_API_KEY) that's an estimate, not necessarily
    what you're literally billed, which the UI should say plainly."""
    usage = getattr(message, "usage", None) or {}
    return {
        "agent": agent,
        "cost_usd": getattr(message, "total_cost_usd", None) or 0.0,
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
    }


def log_jsonl(name: str, entry: dict) -> None:
    """Append one entry to logs/<name>_<date>.jsonl, so every agent's calls
    are inspectable after the fact. `name` is the agent ('monitor',
    'orchestrator', 'analysis', 'incident')."""
    log_file = LOG_DIR / f"{name}_{time.strftime('%Y%m%d')}.jsonl"
    try:
        with log_file.open("a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": time.time(), **entry}, default=str) + "\n")
    except Exception as e:
        logger.warning(f"Could not write {name} call log: {e}")
