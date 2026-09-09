"""
Decisioning agent — the actual judgment call, made per API endpoint.

For every endpoint in the test, weighs three signals: failure category (from
Analysis — business_logic / system_fault / unclear), failure percentage, and
response-time degradation. Deliberately separated from the Orchestrator
(agents_orchestrator.py): the Orchestrator's job is routing — which agent to
invoke, in what order — while this agent's job is the one substantive
judgment call in the whole pipeline. Splitting them keeps each narrow and
single-purpose, matching every other agent here (Monitor classifies, Analysis
categorizes, Incident submits — none of them also decide something else on
the side).

Two of the three signals resolve most endpoints deterministically, in plain
Python, with no LLM call at all — matching agents_monitor.py's check_gate()
pattern of a cheap rule-based gate deciding what's even worth a model's
attention:
  - failure_rate >= ESCALATE_THRESHOLD (70%): escalate outright. At that
    volume there's nothing to weigh — something is very wrong regardless of
    category.
  - 0 < failure_rate < PASS_THRESHOLD (5%): pass outright. Isolated,
    low-frequency failures aren't worth an automated escalation call either
    way; a human reviewing the Test Summary can still see them.
  - 0 failures and avg_response_time <= this endpoint's own p50: pass
    outright — nothing to look at, the distribution is normal.
That leaves exactly two situations that reach the LLM: a failure rate
between 5-70% (where category — business_logic vs. genuine fault — actually
matters), and a 0-failure endpoint whose average has drifted above its own
p50 (where "is this close enough to p99 to be a real problem" needs judgment,
not a hardcoded ratio).

business_logic is still never escalated even inside that 5-70% band — that
invariant predates this file (see agents_incident.py's own hard filter,
which stays as a second, code-level backstop regardless of what this agent
outputs) and nothing here overrides it; a very high failure rate on its own
is not treated as evidence against a business_logic categorization.

Only used in "final" mode. Mid-run interim checks (agents_orchestrator.py,
is_final=False) skip Decisioning entirely — there is nothing for a decision
to gate mid-run, since submit_incident isn't even a registered tool there.

### MCP ###
Wrapped as an in-process MCP tool ("decide_escalation") the same way as
agents_analysis.py's tool — see the docstring on build_analyze_failures_tool()
there for how create_sdk_mcp_server() wires it into the Orchestrator's
session in agents_orchestrator.py.
"""

import json
import logging
from typing import Optional

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, query, tool

from agents_common import extract_usage, log_jsonl, strip_json_fences

logger = logging.getLogger("agents.decisioning")

DECISION_MODEL = "claude-sonnet-5"

# Deterministic failure-rate thresholds (percent) — see module docstring.
ESCALATE_RATE_THRESHOLD = 70.0
PASS_RATE_THRESHOLD = 5.0

DECISION_SYSTEM_PROMPT = """You are the decisioning agent for a load-testing pipeline, judging
individual API endpoints that a cheap deterministic pre-check could not resolve on its own. You will
be given a list of such endpoints, each in one of two situations:

1. A "failure_rate" between 5% and 70% (inclusive of 5, exclusive of 70) — endpoints outside that
   band are already resolved without you. You're given "category" (business_logic / system_fault /
   unclear) and "likely_cause" from an upstream Analysis step, plus the raw failure_rate,
   num_requests and num_failures.
2. Zero failures, but "avg_response_time" is above this endpoint's own "p50" — a possible sign that
   response times have degraded for a meaningful slice of traffic even though nothing outright
   failed. You're given avg/p50/p95/p99 response times (all in ms) to judge how close the average
   is sitting to the tail.

For situation 1, weigh failure_rate together with category:
- "business_logic" is the system correctly reporting an expected outcome (e.g. "no records
  available") — it should essentially never escalate, no matter how high the rate climbs within this
  band. A high rate of an expected, well-formed business outcome is not itself evidence of a fault.
- "system_fault" or "unclear" in this band deserves real judgment, weighing both how high the rate
  already is within 5-70% and how severe the failure mode looks from likely_cause: a 60% system_fault
  is a stronger case than a 6% one, but a 6% system_fault describing something like exhausted DB
  connections or a NullPointerException can still be serious enough to escalate on its own.

For situation 2, decide whether avg_response_time is genuinely close to p99 (the tail) rather than
just marginally above p50 (the middle) — sitting near the tail means a large fraction of requests are
experiencing something close to the worst-case latency, not just a few rare outliers, which is a real
degradation worth flagging even with zero outright failures. Marginally above p50 with p95/p99 still
far away is normal request-time variance, not something to escalate.

For each endpoint given, decide "escalate" or "pass" and give a one-sentence reasoning that cites the
concrete numbers (rate, or the response-time figures) that drove it.

Respond with ONLY a compact JSON array, no other text, no markdown fences, one entry per endpoint
given, in the same order:
[{"api": "<path>", "method": "<HTTP method>", "decision": "escalate"|"pass", "reasoning": "<one sentence>"}]
"""


def _find_analysis_entry(analysis_result: list[dict], api: str, method: str) -> Optional[dict]:
    for a in analysis_result or []:
        if a.get("api") == api and a.get("method") == method:
            return a
    return None


def _endpoint_precheck(stat: dict, category: Optional[str]) -> Optional[str]:
    """Pure, deterministic — returns 'escalate'/'pass' when the numbers alone
    already settle it, or None when this endpoint needs the LLM's judgment."""
    num_failures = stat.get("num_failures", 0)
    if num_failures > 0:
        rate = stat.get("failure_rate", 0)
        if rate < PASS_RATE_THRESHOLD:
            return "pass"
        if rate >= ESCALATE_RATE_THRESHOLD:
            # Overwhelming on its own — UNLESS Analysis already identified
            # this as the system correctly reporting an expected outcome.
            # business_logic happening 90% of the time is still just the
            # system working as designed, not a fault made worse by volume.
            return "pass" if category == "business_logic" else "escalate"
        return None
    avg = stat.get("avg_response_time", 0)
    p50 = stat.get("p50", 0)
    if avg > 0 and p50 > 0 and avg > p50:
        return None
    return "pass"


async def _call_decision_llm(endpoints: list[dict]) -> tuple[list[dict], dict]:
    """The actual nested LLM call — a single-turn query with no tools of its
    own (tools=[]); it only judges the ambiguous endpoints it's handed.
    Returns (decisions, usage) — see agents_analysis.run_analysis for why."""
    payload = {"endpoints": endpoints}

    options = ClaudeAgentOptions(
        model=DECISION_MODEL,
        system_prompt=DECISION_SYSTEM_PROMPT,
        permission_mode="dontAsk",
        tools=[],
        allowed_tools=[],
        max_turns=1,
    )

    result_text = ""
    usage = {}
    async for message in query(prompt=json.dumps(payload), options=options):
        if isinstance(message, ResultMessage):
            result_text = message.result or ""
            usage = extract_usage(message, "decisioning")

    cleaned = strip_json_fences(result_text)
    data = json.loads(cleaned)
    if not isinstance(data, list):
        raise ValueError(f"expected a JSON array from decisioning, got: {result_text[:200]!r}")
    for entry in data:
        if entry.get("decision") not in ("escalate", "pass"):
            raise ValueError(f"unexpected decision value: {entry.get('decision')!r}")
    return data, usage


async def run_decisioning(final_metrics: dict, analysis_result: list[dict]) -> tuple[list[dict], dict]:
    """Per-endpoint escalate/pass across every endpoint in the test. Endpoints
    the deterministic pre-check resolves never reach the LLM at all. Returns
    (decisions, usage) — usage is {} when nothing needed the LLM this call."""
    stats = (final_metrics or {}).get("stats") or []

    resolved: list[dict] = []
    needs_judgment: list[dict] = []
    for stat in stats:
        api, method = stat.get("name"), stat.get("method")
        entry = {
            "api": api,
            "method": method,
            "failure_rate": round(stat.get("failure_rate", 0), 2),
            "num_failures": stat.get("num_failures", 0),
            "num_requests": stat.get("num_requests", 0),
            "avg_response_time": round(stat.get("avg_response_time", 0), 1),
            "p50": round(stat.get("p50", 0), 1),
            "p95": round(stat.get("p95", 0), 1),
            "p99": round(stat.get("p99", 0), 1),
        }
        analysis_entry = _find_analysis_entry(analysis_result, api, method)
        category = analysis_entry.get("category") if analysis_entry else None
        if analysis_entry:
            entry["category"] = category
            entry["likely_cause"] = analysis_entry.get("likely_cause")
            entry["error"] = analysis_entry.get("error")

        pre = _endpoint_precheck(stat, category)
        if pre is not None:
            if not entry["num_failures"]:
                reasoning = "No failures, and average response time is at or below p50 — nothing to flag."
            elif entry["failure_rate"] < PASS_RATE_THRESHOLD:
                reasoning = f"Deterministic rule: failure rate {entry['failure_rate']}% is below {PASS_RATE_THRESHOLD}%."
            elif category == "business_logic":
                reasoning = (
                    f"Failure rate {entry['failure_rate']}% is very high, but every failure is "
                    f"business_logic — the system correctly reporting an expected outcome, not a fault."
                )
            else:
                reasoning = f"Deterministic rule: failure rate {entry['failure_rate']}% is at or above {ESCALATE_RATE_THRESHOLD}%."
            resolved.append({**entry, "decision": pre, "reasoning": reasoning})
        else:
            needs_judgment.append(entry)

    llm_results: list[dict] = []
    usage: dict = {}
    if needs_judgment:
        judged, usage = await _call_decision_llm(needs_judgment)
        # Merge the LLM's decision/reasoning back onto our own copy of each
        # endpoint's data (category, error, response times, ...) — the model
        # is only asked to return the fields needed to make its call, not to
        # faithfully echo everything back.
        by_key = {(j.get("api"), j.get("method")): j for j in judged}
        for entry in needs_judgment:
            j = by_key.get((entry["api"], entry["method"]), {})
            llm_results.append({**entry, "decision": j.get("decision", "pass"), "reasoning": j.get("reasoning", "")})

    all_results = resolved + llm_results
    log_jsonl("decisioning", {
        "input": {"endpoints_considered": len(stats), "sent_to_llm": len(needs_judgment)},
        "output": all_results,
        "usage": usage,
    })
    return all_results, usage


def build_decide_escalation_tool(state):
    """### MCP TOOL DECLARATION ### — see agents_analysis.py's
    build_analyze_failures_tool() for the general mechanism. Requires
    analyze_failures to have already run this session (reads
    state.analysis_result) — refuses otherwise, the same enforcement pattern
    used by submit_incident.
    """

    @tool(
        "decide_escalation",
        "Given this test's full per-endpoint stats and the most recent analyze_failures result, "
        "decide per-endpoint whether each API's failures (or response-time degradation) warrant "
        "filing an incident ('escalate') or not ('pass'). Call this after analyze_failures and "
        "before submit_incident.",
        {},
    )
    async def decide_escalation_tool(args):
        if state.analysis_result is None:
            return {
                "content": [{
                    "type": "text",
                    "text": "ERROR: analyze_failures has not been called yet this run — call it first.",
                }],
                "is_error": True,
            }
        await state.on_step("decisioning", "active", {"note": "Deciding per-endpoint whether to escalate..."})
        try:
            result, usage = await run_decisioning(state.final_metrics, state.analysis_result)
        except Exception as e:
            logger.error(f"decide_escalation failed: {e}")
            await state.on_step("decisioning", "error", {"error": str(e)})
            return {"content": [{"type": "text", "text": f"Decisioning failed: {e}"}], "is_error": True}
        state.decision_result = result
        if usage:
            state.llm_calls.append(usage)
        escalated = [r for r in result if r.get("decision") == "escalate"]
        await state.on_step("decisioning", "done", {"result": result, "escalated_count": len(escalated)})
        return {"content": [{"type": "text", "text": json.dumps(result)}]}

    return decide_escalation_tool
