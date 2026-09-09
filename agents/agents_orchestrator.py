"""
Orchestrator — pure routing/control, no judgment of its own.

Runs in two distinct modes:

  - **Mid-run (investigate-only)**: triggered from monitor.py whenever Agent 1
    (Monitor) flags a live snapshot as CONCERNING, throttled by
    MIN_MIDRUN_INTERVAL_SECONDS. Only the Analysis tool is available in this
    mode — submit_incident and decide_escalation are not even registered, so
    it is *structurally* impossible to file an incident mid-run, not just
    discouraged by prompt. This mode exists to surface an early root-cause
    read while the test is still running; it never decides anything or acts.
  - **Final**: triggered once, always, when a test ends — regardless of
    whether it passed or failed. This is the only mode with decide_escalation
    and submit_incident available. On a clean pass it still runs and reports
    a real "no incident needed" outcome rather than staying silent.

The Orchestrator's own job is deliberately mechanical: call Analysis, then
(final mode only) hand its result to Decisioning, then act on whatever
Decisioning decided. The actual judgment — is this escalation-worthy — lives
in agents_decisioning.py, not here. See that module's docstring for why this
is split out rather than folded into the Orchestrator's own reasoning.

### MCP ###
This is the one place an in-process MCP *server* gets created
(claude_agent_sdk.create_sdk_mcp_server) and wired into a ClaudeAgentOptions
session (mcp_servers=...). The tools it serves are DECLARED in
agents_analysis.py, agents_decisioning.py, and agents_incident.py (via the
@tool decorator in each) — this file only assembles them into one server (a
*different* tool set depending on is_final — see run_orchestrator_for_test)
and grants this session permission to call them.

Gotcha found by live testing (not obvious from the SDK's own docstring
example, which uses bare tool names): allowed_tools entries for an in-process
SDK MCP server must use the "mcp__<server_name>__<tool_name>" form. A bare
tool name is silently denied under permission_mode="dontAsk" — the
orchestrator just reports it can't proceed instead of erroring loudly.

Every step is reported through on_step(agent, status, detail) so the caller
(monitor.py) can broadcast it to the dashboard in real time.
"""

import json
import logging
import time
import uuid
from typing import Awaitable, Callable, Optional

from claude_agent_sdk import ClaudeAgentOptions, ResultMessage, create_sdk_mcp_server, query

import agents_analysis
import agents_decisioning
import agents_incident
from agents_common import extract_usage, log_jsonl

logger = logging.getLogger("agents.orchestrator")

ORCHESTRATOR_MODEL = "claude-sonnet-5"

# Throttle for mid-run investigate calls — these are triggered by Agent 1
# flagging CONCERNING, which can happen frequently; each mid-run call is a
# real multi-turn Sonnet + Haiku round trip, so this keeps cost/latency sane.
MIN_MIDRUN_INTERVAL_SECONDS = 60

OnStep = Callable[[str, str, dict], Awaitable[None]]

ORCHESTRATOR_FINAL_SYSTEM_PROMPT = """You are the orchestrator for a load-testing pipeline. Your
job is routing — deciding which agent to invoke and in what order — not judgment. A test run has
just finished. You are given a summary of per-endpoint failure rates and response times for the
whole run — this may show zero failures (a clean pass) or some failures.

Decisioning now makes a PER-ENDPOINT call, not one call for the whole test — it independently
checks every endpoint, including ones with zero failures (a slow-but-not-failing endpoint can still
be escalated on response-time grounds alone). Because of that, you always run the full chain, even
on a totally clean run — a clean run just means decide_escalation will most likely resolve every
endpoint to "pass" on its own, which is a real answer, not something to skip finding out.

Follow this procedure exactly, every time:
1. Call the analyze_failures tool (no arguments) to get a categorized, root-cause read on whichever
   endpoints failed (if none did, it simply returns an empty list — that's expected, not an error).
2. Call the decide_escalation tool (no arguments) to have the decisioning step judge, per endpoint,
   whether it warrants an incident. You must call analyze_failures before this — it will refuse
   otherwise. Do not make any escalate/pass judgment yourself; that decision belongs to
   decide_escalation, not you.
3. If decide_escalation marked at least one endpoint "escalate", call submit_incident once, passing
   test_time as an ISO-8601 UTC timestamp for when this test ran. It files one incident per
   escalated endpoint internally and returns a per-endpoint outcome — some endpoints may still end
   up not filed (e.g. a human reviewer rejects one, or Analysis had already categorized it
   business_logic) even when others are. That is a valid, expected outcome, not an error.
4. If decide_escalation marked every endpoint "pass", do not call submit_incident.
5. Report back in one or two sentences: how many endpoints were escalated and why (citing the
   driving numbers — failure rate, or response-time drift — and category where relevant), and how
   many incidents actually ended up filed vs. declined and why.
"""

ORCHESTRATOR_MIDRUN_SYSTEM_PROMPT = """You are the central decisioning agent for a load-testing
pipeline, checking in on a test that is STILL RUNNING (it has not finished yet). Agent 1 (the
Monitor) just flagged the current moment as CONCERNING, based on raw failure-rate/response-time
thresholds. You are given a live, partial summary of per-endpoint failure rates and response times
collected so far — not the final picture.

Important: those raw thresholds cannot tell a genuine system fault from the API correctly
reporting an expected business-logic outcome (e.g. "No records available", "No balance
remaining") — the system working exactly as designed. Do not assume this is a real problem yet.

Your job right now is to investigate only, not to act: call the analyze_failures tool (no
arguments) to get a categorized, root-cause read on what is happening right now, then report it in
one or two sentences. If the categorized results show only "business_logic" findings, say so
plainly — that's a completely normal outcome, not a failure of this check, not something to sound
alarmed about. You do NOT have a submit_incident tool available on purpose — no incident is ever
filed mid-run regardless of what you find. A single, final decision (and incident, if warranted) is
made once, after the test completes, using the complete picture.
"""


class OrchestratorRunState:
    """Per-invocation state the Analysis, Decisioning, and Incident-submission
    tools close over: this run's current metrics (final, or a partial mid-run
    snapshot), the analysis result once produced (also the enforcement point
    stopping decide_escalation/submit_incident from firing without it), the
    per-endpoint decisioning result once produced, the incidents actually
    filed (one entry per escalated endpoint, not one per test), and (final
    mode only) whether this test opted into a human-approval gate before
    filing — each escalated endpoint gets its own run_id derived from this
    run's own run_id, see agents_incident.py's module docstring."""

    def __init__(self, final_metrics: dict, on_step: OnStep):
        self.final_metrics = final_metrics
        self.on_step = on_step
        self.run_id = uuid.uuid4().hex[:8]
        self.analysis_result: Optional[list[dict]] = None
        self.decision_result: Optional[list[dict]] = None
        self.incident_records: list[dict] = []
        self.require_human_approval: bool = False
        self.human_review_result: Optional[dict] = None
        # Every LLM call's cost/token usage this invocation makes — this
        # session's own top-level query below, plus whatever the Analysis and
        # Decisioning tool wrappers append onto it as they run. monitor.py
        # folds this into the whole test's running total (see MonitorAgentState.llm_calls).
        self.llm_calls: list[dict] = []


class OrchestratorHostState:
    """Host-side throttle state for scheduling mid-run Orchestrator
    invocations — analogous to agents_monitor.MonitorAgentState, but for
    deciding *when* the host (monitor.py) should trigger an interim check.
    The final (end-of-test) invocation ignores this entirely — it always
    runs, unthrottled."""

    def __init__(self):
        self.last_midrun_call_ts: float = 0.0
        self.midrun_in_flight: bool = False


def should_invoke_midrun(state: OrchestratorHostState) -> bool:
    if state.midrun_in_flight:
        return False
    return (time.time() - state.last_midrun_call_ts) >= MIN_MIDRUN_INTERVAL_SECONDS


async def run_orchestrator_for_test(
    metrics: dict, on_step: OnStep, is_final: bool = True, require_human_approval: bool = False,
) -> dict:
    """Run the central decisioning agent once.

    is_final=True (test just ended, always called regardless of pass/fail):
        full decision, both tools available, can file an incident.
    is_final=False (mid-run, triggered by a CONCERNING flag from Monitor):
        investigate-only — only analyze_failures is registered, so
        submit_incident cannot be called even if the model tried.
    require_human_approval: this test's TestConfig.require_incident_approval
        flag, passed through unconditionally — it's only actually consulted
        by submit_incident, which mid-run mode can never reach anyway.

    Returns {"incident_filed": bool, "final_text": str, "incidents": list[dict],
    "analysis": list | None, "decision": list[dict] | None, "human_review": dict | None,
    "llm_calls": list[dict]}.
    """
    state = OrchestratorRunState(metrics, on_step)
    state.require_human_approval = require_human_approval

    # ### MCP: assemble this run's in-process server ###
    # A fresh server (and fresh tool closures over a fresh `state`) per
    # invocation — cheap, and keeps one run's state from ever leaking into
    # another's. The tool set itself differs by mode: decide_escalation and
    # submit_incident are simply not built/registered for a mid-run call, so
    # it's not just prompted against — they structurally do not exist as
    # callable tools.
    if is_final:
        tools = [
            agents_analysis.build_analyze_failures_tool(state),
            agents_decisioning.build_decide_escalation_tool(state),
            agents_incident.build_submit_incident_tool(state),
        ]
        allowed_tools = [
            "mcp__incident_pipeline__analyze_failures",
            "mcp__incident_pipeline__decide_escalation",
            "mcp__incident_pipeline__submit_incident",
        ]
        system_prompt = ORCHESTRATOR_FINAL_SYSTEM_PROMPT
    else:
        tools = [agents_analysis.build_analyze_failures_tool(state)]
        allowed_tools = ["mcp__incident_pipeline__analyze_failures"]
        system_prompt = ORCHESTRATOR_MIDRUN_SYSTEM_PROMPT

    server = create_sdk_mcp_server(name="incident_pipeline", tools=tools)

    stats = metrics.get("stats") or []
    summary = {
        "total_requests": metrics.get("total_requests"),
        "total_failures": metrics.get("total_failures"),
        "elapsed": metrics.get("elapsed"),
        "endpoints": [
            {
                "name": s.get("name"),
                "method": s.get("method"),
                "num_requests": s.get("num_requests"),
                "num_failures": s.get("num_failures"),
                "failure_rate": round(s.get("failure_rate", 0), 2),
                "avg_response_time": round(s.get("avg_response_time", 0), 1),
                "p50": round(s.get("p50", 0), 1),
                "p95": round(s.get("p95", 0), 1),
                "p99": round(s.get("p99", 0), 1),
            }
            for s in stats
        ],
    }

    options = ClaudeAgentOptions(
        model=ORCHESTRATOR_MODEL,
        system_prompt=system_prompt,
        permission_mode="dontAsk",
        # ### MCP: register the server, and explicitly allow only its
        # applicable tool(s) for this mode ###
        # See the module docstring for the mcp__<server>__<tool> naming gotcha.
        mcp_servers={"incident_pipeline": server},
        allowed_tools=allowed_tools,
        max_turns=6,
    )

    note = "Reviewing test outcome..." if is_final else "Checking in on a CONCERNING flag from Monitor..."
    await state.on_step("orchestrator", "active", {"note": note, "summary": summary, "final": is_final})

    final_text = ""
    try:
        async for message in query(prompt=json.dumps(summary), options=options):
            if isinstance(message, ResultMessage):
                final_text = message.result or ""
                # This ResultMessage covers the Orchestrator's OWN multi-turn
                # session total (its reasoning across every turn) — it does
                # NOT include the nested Analysis/Decisioning calls, which are
                # separate query() sessions the tool wrappers record onto
                # state.llm_calls themselves as they run.
                state.llm_calls.append(extract_usage(message, "orchestrator"))
    except Exception as e:
        logger.error(f"orchestrator query failed (is_final={is_final}): {e}")
        await state.on_step("orchestrator", "error", {"error": str(e), "final": is_final})
        return {
            "incident_filed": False, "final_text": f"orchestrator failed: {e}",
            "incidents": [], "analysis": None, "decision": None, "human_review": None, "llm_calls": state.llm_calls,
        }

    # incident_filed reflects whether submit_incident actually filed at least
    # one incident — NOT whether analyze_failures/decide_escalation ran.
    # Those are different things: a call can trigger analysis, conclude
    # everything was business_logic or below threshold, and file nothing.
    # Conflating the two previously mislabeled the UI ("ESCALATED") on
    # exactly that case.
    incident_filed = len(state.incident_records) > 0
    log_jsonl("orchestrator", {
        "is_final": is_final, "input": summary,
        "output": {"final_text": final_text, "incident_filed": incident_filed, "incidents_filed": len(state.incident_records)},
    })
    await state.on_step("orchestrator", "done", {"final_text": final_text, "incident_filed": incident_filed, "final": is_final})

    # analysis/decision are surfaced here (not just via on_step) so the host
    # (monitor.py) can compose an end-of-test summary from this one return
    # value, without re-deriving it from the individual step broadcasts.
    return {
        "incident_filed": incident_filed,
        "final_text": final_text,
        "incidents": state.incident_records,
        "analysis": state.analysis_result,
        "decision": state.decision_result,
        "human_review": state.human_review_result,
        "llm_calls": state.llm_calls,
    }
