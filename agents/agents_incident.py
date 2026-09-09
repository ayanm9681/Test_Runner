"""
Agent 4 — Incident submission (subagent).

Deliberately NOT an LLM step — deterministic HTTP only. Files one incident
PER escalated API endpoint (not one aggregate incident per test) using
Decisioning's per-endpoint result, then verifies each record round-trips via
a follow-up GET.

### MCP ###
Wrapped as an in-process MCP tool ("submit_incident") the same way as
agents_analysis.py's tool — see the docstring on build_analyze_failures_tool()
there for how create_sdk_mcp_server() wires it into the Orchestrator's
session in agents_orchestrator.py.

### Human-in-the-loop gate ###
When the test that's escalating was started with "require human approval"
enabled (models.TestConfig.require_incident_approval, threaded through
main.py's /ws/metrics -> monitor.py -> OrchestratorRunState.require_human_approval),
this tool pauses — after Decisioning has already said "escalate" for a given
endpoint but before anything is actually POSTed for it — and waits for a
human to approve/reject that one endpoint from the Sentinel dashboard. Since
filing is now per-endpoint, so is review: every escalated endpoint gets its
own asyncio.Event registered in _PENDING_REVIEWS (keyed by a per-endpoint
run_id) and its own review-and-file coroutine, all run concurrently via
asyncio.gather — so a test with three escalated endpoints still resolves in
at most ~10 minutes total (each endpoint's own timeout), not up to 30 minutes
stacked sequentially. monitor.py's /agent-ws WebSocket handler calls
resolve_human_review() for whichever run_id a browser responds to, which
wakes up that one endpoint's coroutine specifically — the others keep
waiting independently.
"""

import asyncio
import json
import logging
import time
import uuid

import httpx
from claude_agent_sdk import tool

from agents_common import log_jsonl

logger = logging.getLogger("agents.incident")

INCIDENT_API_BASE = "http://127.0.0.1:5006"

HUMAN_REVIEW_TIMEOUT_SECONDS = 600  # 10 minutes; auto-files (reviewed=false) if nobody responds

# run_id -> {"event": asyncio.Event, "approved": Optional[bool], "reason": Optional[str]}.
# A run_id is only ever registered here while its endpoint's review is
# actively pending, and is popped as soon as it resolves (response or
# timeout) — this never accumulates stale entries. Generic enough that
# monitor.py's overall-outcome review (a whole-test-level confirm/override,
# not tied to any one endpoint) reuses these same two functions rather than
# duplicating the wait/resolve plumbing — "approved" there just means
# "keep the computed verdict" vs. "override it".
_PENDING_REVIEWS: dict[str, dict] = {}


def register_pending_review(run_id: str) -> asyncio.Event:
    event = asyncio.Event()
    _PENDING_REVIEWS[run_id] = {"event": event, "approved": None, "reason": None}
    return event


def resolve_human_review(run_id: str, approved: bool, reason: str = None) -> bool:
    """Called by monitor.py's /agent-ws handler when a browser sends a
    human_review_response message — the same message type and handler serve
    both per-endpoint incident review and monitor.py's own overall-outcome
    review, since resolution here only cares about the run_id, not what kind
    of decision it's attached to. `reason` is an optional short free-text
    note — most useful when the human's choice deviates from what was
    recommended (rejecting an escalation, or overriding the computed overall
    verdict), but accepted either way. Returns False if run_id doesn't match
    a pending review (already resolved, timed out, or a stale/bogus id)."""
    entry = _PENDING_REVIEWS.get(run_id)
    if entry is None:
        return False
    entry["approved"] = approved
    entry["reason"] = (reason or "").strip()[:500] or None
    entry["event"].set()
    return True


def take_review_result(run_id: str) -> dict:
    """Pop and return {"approved": bool|None, "reason": str|None} for a
    run_id that was registered via register_pending_review() — call this
    once its event has fired (or timed out) to retrieve what
    resolve_human_review() set, and to remove it from the pending table.
    {} (both None) if it was never resolved or was already taken."""
    entry = _PENDING_REVIEWS.pop(run_id, {})
    return {"approved": entry.get("approved"), "reason": entry.get("reason")}


async def _file_one(client: httpx.AsyncClient, api: str, method: str, error: dict, reviewed: bool, test_time: str) -> tuple[dict, dict, bool]:
    """POST a single endpoint's incident and verify it round-trips. Returns
    (payload_sent, record_returned, verified)."""
    payload = {
        "test_time": test_time,
        "reviewed": reviewed,
        "apis": [{"api": api, "method": method, "error": error}],
    }
    resp = await client.post(f"{INCIDENT_API_BASE}/incidents", json=payload)
    resp.raise_for_status()
    record = resp.json()

    verified = False
    incident_number = record.get("incident_number")
    if incident_number is not None:
        try:
            get_resp = await client.get(f"{INCIDENT_API_BASE}/incidents/{incident_number}")
            verified = get_resp.status_code == 200
        except Exception as verify_exc:
            logger.warning(f"Incident verification GET failed for {api} [{method}]: {verify_exc}")
    return payload, record, verified


async def _review_and_file(state, client: httpx.AsyncClient, entry: dict, test_time: str) -> dict:
    """One escalated endpoint's full path: optional human gate, then file (or
    skip). Never raises — errors are captured in the returned dict so one
    endpoint's failure can't take down the others running alongside it."""
    api, method = entry.get("api"), entry.get("method")
    reasoning = entry.get("reasoning", "")

    reviewed = False
    review_status = "not_required"
    human_reason = None
    if state.require_human_approval:
        run_id = f"{state.run_id}-{uuid.uuid4().hex[:6]}"
        event = register_pending_review(run_id)
        await state.on_step("incident", "awaiting_review", {
            "note": f"Decisioning recommended filing an incident for {api} [{method}] — "
                    f"waiting for human confirmation...",
            "run_id": run_id,
            "api": api,
            "method": method,
            "reasoning": reasoning,
            "timeout_seconds": HUMAN_REVIEW_TIMEOUT_SECONDS,
        })
        try:
            await asyncio.wait_for(event.wait(), timeout=HUMAN_REVIEW_TIMEOUT_SECONDS)
            resolved = take_review_result(run_id)
            approved = resolved.get("approved")
            human_reason = resolved.get("reason")
            timed_out = False
        except asyncio.TimeoutError:
            take_review_result(run_id)
            approved, timed_out = None, True

        if timed_out:
            review_status = "timed_out"
        elif approved is False:
            await state.on_step("incident", "done", {
                "note": f"A human reviewer declined to file an incident for {api} [{method}].",
                "skipped": True, "api": api, "method": method, "reasoning": reasoning, "human_reason": human_reason,
            })
            return {
                "api": api, "method": method, "outcome": "rejected", "review_status": "rejected",
                "reasoning": reasoning, "human_reason": human_reason,
            }
        else:
            reviewed = True
            review_status = "approved"

    await state.on_step("incident", "active", {"note": f"Submitting incident for {api} [{method}]...", "api": api, "method": method})
    try:
        payload, record, verified = await _file_one(client, api, method, entry.get("error"), reviewed, test_time)
    except Exception as e:
        logger.error(f"submit_incident failed for {api} [{method}]: {e}")
        log_jsonl("incident", {"input": {"api": api, "method": method}, "output": {"error": str(e)}})
        await state.on_step("incident", "error", {"error": str(e), "api": api, "method": method})
        return {"api": api, "method": method, "outcome": "error", "review_status": review_status, "reasoning": reasoning, "error": str(e)}

    log_jsonl("incident", {"input": payload, "output": record})
    await state.on_step("incident", "done", {
        "record": record, "verified": verified, "review_status": review_status,
        "api": api, "method": method, "reasoning": reasoning, "human_reason": human_reason,
    })
    return {
        "api": api, "method": method, "outcome": "filed", "review_status": review_status,
        "reasoning": reasoning, "human_reason": human_reason, "record": record, "verified": verified,
    }


def build_submit_incident_tool(state):
    """### MCP TOOL DECLARATION ### — see agents_analysis.py's
    build_analyze_failures_tool() for the general mechanism. This tool
    additionally *enforces*, in code rather than only via the Orchestrator's
    system prompt, three invariants:
      1. It refuses to run unless analyze_failures and decide_escalation
         already populated state.analysis_result / state.decision_result
         earlier in this same run.
      2. It never files for an endpoint Analysis categorized as
         "business_logic" (the API correctly reporting an expected outcome,
         e.g. "No balance remaining" — not a fault), even if Decisioning
         somehow still marked it "escalate". If nothing survives this filter,
         it refuses outright rather than filing a vacuous incident.
      3. If this test opted into human approval (state.require_human_approval),
         each escalated endpoint pauses independently for a human's
         approve/reject before ever POSTing — see the module docstring's
         "Human-in-the-loop gate" section.
    """

    @tool(
        "submit_incident",
        "File one incident per API endpoint Decisioning marked 'escalate', using the most recent "
        "decide_escalation result. Requires analyze_failures and decide_escalation to have both "
        "been called first this run. Endpoints categorized as business_logic are never included, "
        "even if marked 'escalate'.",
        {"test_time": str},
    )
    async def submit_incident_tool(args):
        if state.analysis_result is None:
            return {
                "content": [{
                    "type": "text",
                    "text": "ERROR: analyze_failures has not been called yet this run — call it first.",
                }],
                "is_error": True,
            }
        if state.decision_result is None:
            return {
                "content": [{
                    "type": "text",
                    "text": "ERROR: decide_escalation has not been called yet this run — call it first.",
                }],
                "is_error": True,
            }

        escalated = [e for e in state.decision_result if e.get("decision") == "escalate"]
        # Code-level filter: business_logic findings are the system working as
        # designed, not a fault, and must never appear in a filed incident —
        # regardless of what Decisioning said.
        genuine = [e for e in escalated if e.get("category") != "business_logic"]
        if not genuine:
            text = (
                "Refusing to file: every endpoint Decisioning marked 'escalate' was categorized as "
                "an expected business-logic response, not a system fault."
                if escalated else
                "No endpoint was marked for escalation — nothing to file."
            )
            return {"content": [{"type": "text", "text": text}], "is_error": True}

        test_time = args.get("test_time") or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        async with httpx.AsyncClient(timeout=10) as client:
            results = await asyncio.gather(*[_review_and_file(state, client, e, test_time) for e in genuine])

        state.incident_records = [r for r in results if r["outcome"] == "filed"]
        state.human_review_result = {
            "required": state.require_human_approval,
            "entries": [
                {
                    "api": r["api"], "method": r["method"], "status": r["review_status"],
                    "recommended": "escalate", "reason": r.get("human_reason"),
                }
                for r in results
            ],
        }

        filed = len(state.incident_records)
        rejected = sum(1 for r in results if r["outcome"] == "rejected")
        errored = sum(1 for r in results if r["outcome"] == "error")
        summary_text = (
            f"{filed} incident(s) filed out of {len(genuine)} escalated endpoint(s)"
            f"{f', {rejected} rejected by a human reviewer' if rejected else ''}"
            f"{f', {errored} failed to submit' if errored else ''}."
        )
        return {"content": [{"type": "text", "text": summary_text + " " + json.dumps(results, default=str)}]}

    return submit_incident_tool
