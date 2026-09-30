"""
ISDO Lab C6/C7/C8 - LangGraph Orchestrator with an extended HITL gate and A2A
Wires the Triage, Resolution, SLA, HITL and Communication agents (Labs C3-C5)
into a single StateGraph.

Flow:
    triage -> resolution -> sla -> [hitl if hitl_required] -> communication

hitl_required (and hitl_reason) is set in sla_node - by the time it runs,
triage and resolution have already populated everything the gate needs to
check. Three independent triggers, checked in this order:
    1. SLA      - P1 ticket at CRITICAL/BREACHED risk (Lab C5's rule)
    2. ACCESS_GRANT - category 'Access' + request_type 'Access Grant',
                       regardless of priority (security-sensitive)
    3. LOW_CONFIDENCE - Resolution Agent's confidence is LOW, regardless
                       of priority (no clear KB fix to send un-reviewed)
determine_hitl() is the single source of truth for these three checks, used
by both sla_node (to decide whether to route through hitl) and
communication_node (to decide which message to draft).

Lab C8: when resolution_node gets LOW confidence from ChromaDB, it calls the
A2A Knowledge Specialist (a2a/knowledge_specialist.py, run separately with
`uvicorn knowledge_specialist:app --port 8001`) for a deeper look before
falling through to the LOW_CONFIDENCE HITL trigger. If the specialist raises
confidence above LOW, that trigger no longer fires for this ticket - but
auto_resolve is NOT retroactively set: it stays whatever the Resolution
Agent's own ChromaDB-based guardrail already decided, since A2A confidence
hasn't gone through that guardrail's priority rules. If the A2A server isn't
running, the code catches the connection error and falls back to the
original ChromaDB result untouched, so LOW confidence and its HITL gate
still apply exactly as in Lab C7.

Run from the project root:  python orchestrator/supervisor.py
Before running: start the A2A server in a separate terminal --
    uvicorn a2a.knowledge_specialist:app --port 8001
(the orchestrator still works without it - it just won't get the A2A upgrade)
"""

import operator
import os
import sys
from datetime import datetime, timezone
from typing import Annotated, List, Optional, TypedDict

import requests
from langgraph.graph import END, START, StateGraph

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agents"))

import resolution_agent   # noqa: E402  (path set above)
import sla_agent          # noqa: E402
import triage_agent       # noqa: E402

A2A_BASE_URL = os.environ.get("A2A_KNOWLEDGE_SPECIALIST_URL", "http://localhost:8001")
A2A_TIMEOUT_SECONDS = 15   # the specialist makes its own LLM call, so give it real time

# -- Shared state ---------------------------------------------------------------

class TicketState(TypedDict, total=False):
    # Input
    ticket_number: str
    short_description: str
    description: str
    category: str
    priority: str
    sla_due: str
    request_type: Optional[str]   # e.g. 'Access Grant' - only present on REQ- style tickets
    # Triage agent
    triage_category: str
    triage_priority: str
    triage_assignment_group: str
    pii_detected: bool
    # Resolution agent
    kb_article: str
    resolution_text: str
    auto_resolve: bool
    confidence: str
    # SLA agent
    sla_breach_risk: str
    escalation_required: bool
    hitl_required: bool
    hitl_reason: Optional[str]
    # HITL node
    hitl_approved: Optional[bool]
    escalation_team: Optional[str]
    # Communication agent
    user_message: str
    final_status: str
    # Every node appends here; the reducer concatenates instead of overwriting.
    audit_log: Annotated[List[dict], operator.add]


def log(agent: str, action: str, detail: str) -> list:
    """One audit_log entry, in the list form nodes return for the reducer to append."""
    return [{"timestamp": datetime.now(timezone.utc).isoformat(), "agent": agent, "action": action, "detail": detail}]


def determine_hitl(state: dict):
    """Single source of truth for the three HITL triggers (Lab C7).
    Returns (required, trigger_type, reason). Checked in this order:
    a P1 SLA breach is the most time-critical, then a security-sensitive
    access grant, then a low-confidence resolution."""
    if state.get("triage_priority") == "P1" and state.get("sla_breach_risk") in ("CRITICAL", "BREACHED"):
        return True, "SLA", f"P1 SLA {state.get('sla_breach_risk')} - escalation requires approval"

    if state.get("triage_category") == "Access" and state.get("request_type") == "Access Grant":
        return True, "ACCESS_GRANT", "ACCESS GRANT -- request requires security approval"

    if state.get("confidence") == "LOW":
        return True, "LOW_CONFIDENCE", "LOW confidence KB match -- resolution requires human review"

    return False, None, None


def call_a2a_specialist(ticket_number: str, query: str, context: str = ""):
    """POST /tasks then GET /tasks/{task_id} against the A2A Knowledge Specialist.
    Returns the result dict on success, or None if the server can't be reached
    or returns anything unexpected - callers should treat None as 'keep the
    original ChromaDB result and carry on', not as a fatal error.

    Note: requests.exceptions.ConnectionError (what's actually raised when the
    server isn't running) is a subclass of the builtin ConnectionError, but it's
    caught explicitly here rather than relying on that fact, since a Timeout is
    just as likely on a slow LLM call and isn't a ConnectionError at all."""
    try:
        post_resp = requests.post(
            f"{A2A_BASE_URL}/tasks",
            json={"query": query, "ticket_number": ticket_number, "context": context},
            timeout=A2A_TIMEOUT_SECONDS,
        )
        post_resp.raise_for_status()
        task_id = post_resp.json()["task_id"]

        get_resp = requests.get(f"{A2A_BASE_URL}/tasks/{task_id}", timeout=A2A_TIMEOUT_SECONDS)
        get_resp.raise_for_status()
        return get_resp.json()["result"]

    except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as exc:
        print(f"  ! A2A Knowledge Specialist unreachable ({exc.__class__.__name__}) "
              f"- is `uvicorn a2a.knowledge_specialist:app --port 8001` running?")
        return None
    except requests.exceptions.RequestException as exc:
        print(f"  ! A2A call failed: {exc}")
        return None
    except (KeyError, ValueError) as exc:
        print(f"  ! A2A response was malformed: {exc}")
        return None

# -- Nodes ------------------------------------------------------------------------

def triage_node(state: TicketState) -> dict:
    print(f"\n{'#' * 60}")
    print(f"PROCESSING TICKET: {state['ticket_number']}")
    print(f"{'#' * 60}")
    print(f"\n\u25b6 TRIAGE AGENT \u2014 {state['ticket_number']}")

    result = triage_agent.triage_ticket(state["ticket_number"], state["short_description"], state["description"])
    if result is None:
        # Model never called classify_ticket - fail safe rather than crash the graph.
        print("  ! Triage did not return a classification - defaulting to P3/Service-Desk.")
        result = {"category": "Software", "priority": "P3", "assignment_group": "Service-Desk",
                  "pii_detected": False, "reasoning": "Fallback: classification unavailable."}

    return {
        "triage_category": result["category"],
        "triage_priority": result["priority"],
        "triage_assignment_group": result["assignment_group"],
        "pii_detected": result["pii_detected"],
        "audit_log": log("TriageAgent", "classify_ticket",
                          f"{result['category']} / {result['priority']} -> {result['assignment_group']}"),
    }


def resolution_node(state: TicketState) -> dict:
    print(f"\n\u25b6 RESOLUTION AGENT \u2014 searching KB")

    result = resolution_agent.resolve_ticket(
        state["ticket_number"], state["short_description"], state["description"],
        state["triage_category"], state["triage_priority"],
    )

    kb_article = result.get("kb_article_used", "None")
    resolution_text = result.get("resolution_text", "")
    confidence = result.get("confidence", "LOW")
    auto_resolve = bool(result.get("auto_resolve"))   # not re-derived from A2A - see module docstring

    audit_entries = log("ResolutionAgent", "search_kb",
                         f"{kb_article} - {confidence} ({result.get('top_score', 0):.0%})")

    if confidence == "LOW":
        print("  -> ChromaDB confidence LOW - calling A2A Knowledge Specialist...")
        a2a_context = f"category={state['triage_category']}, priority={state['triage_priority']}"
        a2a_result = call_a2a_specialist(state["ticket_number"], state["short_description"], a2a_context)

        if a2a_result:
            confidence = a2a_result.get("confidence", confidence)
            resolution_text = a2a_result.get("resolution", resolution_text)
            kb_article = a2a_result.get("best_match", kb_article)
            print(f"  -> A2A Knowledge Specialist: {confidence} "
                  f"({a2a_result.get('confidence_score', 0):.0%}) via {kb_article}")
            audit_entries += log("KnowledgeSpecialist", "a2a_task",
                                  f"{kb_article} - {confidence} (ChromaDB was LOW)")
        else:
            audit_entries += log("KnowledgeSpecialist", "a2a_unavailable",
                                  "A2A server unreachable - keeping ChromaDB LOW result")

    return {
        "kb_article": kb_article,
        "resolution_text": resolution_text,
        "auto_resolve": auto_resolve,
        "confidence": confidence,
        "audit_log": audit_entries,
    }


def sla_node(state: TicketState) -> dict:
    print(f"\n\u25b6 SLA AGENT \u2014 checking deadline")

    status = sla_agent.get_sla_status(state["ticket_number"], state["sla_due"], state["triage_priority"])
    breach_risk = status.get("breach_risk", "ON_TRACK")
    escalation_required = bool(status.get("requires_escalation"))

    # determine_hitl needs sla_breach_risk, which isn't in `state` yet at this point
    # in the node (it's part of THIS node's own return value) - check against a
    # merged view instead of mutating `state` itself.
    hitl_required, _, hitl_reason = determine_hitl({**state, "sla_breach_risk": breach_risk})

    print(f"  SLA Risk: {breach_risk}  |  Minutes remaining: {status.get('minutes_remaining')}")
    if hitl_required:
        print(f"  HITL trigger: {hitl_reason}")

    return {
        "sla_breach_risk": breach_risk,
        "escalation_required": escalation_required,
        "hitl_required": hitl_required,
        "hitl_reason": hitl_reason,
        "escalation_team": sla_agent.ESCALATION_TEAMS.get(state["triage_category"], "L2-Service-Desk"),
        "audit_log": log("SLAAgent", "get_sla_status", f"{breach_risk} ({status.get('minutes_remaining')} min remaining)"),
    }


def hitl_node(state: TicketState) -> dict:
    """Pause for human approval. The banner always shows hitl_reason (Lab C7),
    whichever of the three triggers fired. Only the SLA trigger performs a real
    mock-ServiceNow escalation on approval - the other two have no corresponding
    update_ticket action defined in the labs, so they just record the decision."""
    print("\n> HITL GATE -- human approval required")
    print("  " + "WARNING " * 8)
    print(f"  Ticket:  {state['ticket_number']}  |  Priority: {state.get('triage_priority')}")
    print(f"  Reason:  {state.get('hitl_reason')}")
    print("  " + "WARNING " * 8)
    decision = input("  Approve action? [y/n]: ").strip().lower()
    approved = decision == "y"
    print(f"  Decision: {'APPROVED' if approved else 'REJECTED'}")

    _, trigger_type, _ = determine_hitl(state)
    if approved and trigger_type == "SLA":
        sla_agent.update_ticket(state["ticket_number"], "escalate", escalation_team=state.get("escalation_team"))

    detail = f"{state.get('hitl_reason')} -> {'APPROVED' if approved else 'REJECTED'}"
    return {"hitl_approved": approved, "audit_log": log("HITLGate", "approval_decision", detail)}


def communication_node(state: TicketState) -> dict:
    print(f"\n\u25b6 COMMUNICATION AGENT")

    ticket = state["ticket_number"]
    approved = state.get("hitl_approved")
    _, trigger_type, _ = determine_hitl(state)

    if state.get("auto_resolve"):
        message = (f"Dear User, regarding {ticket}: we found a known fix for this issue "
                    f"({state.get('kb_article')}) and applied it automatically.\n\n{state.get('resolution_text')}")
        final_status = "RESOLVED"

    elif approved is True and trigger_type == "ACCESS_GRANT":
        message = (f"Dear Requester, your access grant request {ticket} has been approved "
                    f"and will be provisioned shortly.")
        final_status = "ACCESS GRANTED"
    elif approved is False and trigger_type == "ACCESS_GRANT":
        message = (f"Dear Requester, your access grant request {ticket} needs further review "
                    f"before it can be approved. You will be contacted.")
        final_status = "ACCESS REQUEST PENDING"

    elif approved is True and trigger_type == "SLA":
        message = (f"Dear User, regarding {ticket}: this ticket has been escalated to "
                    f"{state.get('escalation_team')} following approval. You will be contacted shortly.")
        final_status = "ESCALATED"
    elif approved is False and trigger_type == "SLA":
        message = (f"Dear User, regarding {ticket}: escalation was reviewed and held for manual handling "
                    f"by {state.get('triage_assignment_group')}.")
        final_status = "ESCALATION REJECTED - MANUAL REVIEW"

    elif approved is True and trigger_type == "LOW_CONFIDENCE":
        message = (f"Dear User, regarding {ticket}: our support team manually reviewed your issue and "
                    f"confirmed the following resolution:\n\n{state.get('resolution_text')}")
        final_status = "RESOLVED - MANUAL REVIEW"
    elif approved is False and trigger_type == "LOW_CONFIDENCE":
        message = (f"Dear User, regarding {ticket}: your ticket needs further investigation and has been "
                    f"assigned to {state.get('triage_assignment_group')} for manual handling.")
        final_status = "PENDING MANUAL REVIEW"

    else:
        message = (f"Dear User, regarding {ticket}: your ticket has been assigned to "
                    f"{state.get('triage_assignment_group')} and is being worked on.")
        final_status = "ASSIGNED"

    print(f"  USER MESSAGE: {message.splitlines()[0][:80]}...")
    print(f"\u2705 FINAL STATUS: {final_status}")

    return {"user_message": message, "final_status": final_status,
            "audit_log": log("CommunicationAgent", "draft_message", final_status)}

# -- Conditional routing -----------------------------------------------------------

def route_after_sla(state: TicketState) -> str:
    return "hitl" if state.get("hitl_required") else "communication"

# -- Build the graph ----------------------------------------------------------------

def build_graph():
    graph = StateGraph(TicketState)
    graph.add_node("triage", triage_node)
    graph.add_node("resolution", resolution_node)
    graph.add_node("sla", sla_node)
    graph.add_node("hitl", hitl_node)
    graph.add_node("communication", communication_node)

    graph.add_edge(START, "triage")
    graph.add_edge("triage", "resolution")
    graph.add_edge("resolution", "sla")
    graph.add_conditional_edges("sla", route_after_sla, {"hitl": "hitl", "communication": "communication"})
    graph.add_edge("hitl", "communication")
    graph.add_edge("communication", END)

    return graph.compile()

# -- Run --------------------------------------------------------------------------

if __name__ == "__main__":
    app = build_graph()

    test_tickets = [
        # P2 VPN - same wording as the Lab C4 KB match, sla_due picked for a true
        # AT_RISK reading (90 of 240 min = 37.5%) - see the SLA math note in chat.
        {"ticket_number": "INC0001001", "short_description": "VPN not connecting after password change",
         "description": "User reports VPN client fails to connect after AD password was reset. Error: authentication failed.",
         "category": "Network", "priority": "P2", "sla_due": "2024-01-15 12:00:00"},
        # P1 SAP outage - sla_due picked for CRITICAL (10 of 60 min = 16.7%), same as Lab C5.
        # Triggers HITL: SLA.
        {"ticket_number": "INC0001002", "short_description": "Cannot access ERP system - login error",
         "description": "Multiple Finance users unable to login to SAP. Error: DBCON_FAIL.",
         "category": "Application", "priority": "P1", "sla_due": "2024-01-15 10:40:00"},
        # Lab C7 Step 3 - no KB coverage, expect LOW confidence.
        # Triggers HITL: LOW_CONFIDENCE (fires even though this is only P3).
        {"ticket_number": "TEST-004", "short_description": "Cisco Webex not launching on MacBook M2 after Sonoma update",
         "description": "Cisco Webex not launching on MacBook M2 after Sonoma update.",
         "category": "Software", "priority": "P3", "sla_due": "2024-01-17 09:00:00"},
        # Lab C7 Step 4 - access grant request.
        # Triggers HITL: ACCESS_GRANT (fires regardless of priority - note request_type,
        # which the doc's own Step 4 ticket omits but the trigger condition requires).
        {"ticket_number": "REQ-1002", "short_description": "VPN access for new contractor",
         "description": "Contractor needs VPN access. Email: contractor@client.com",
         "category": "Access", "request_type": "Access Grant", "priority": "P2", "sla_due": "2024-01-15 15:00:00"},
    ]

    all_results = []
    for ticket in test_tickets:
        final_state = app.invoke(ticket)
        all_results.append(final_state)

    print(f"\n\n{'=' * 60}")
    print("AUDIT LOG")
    print("=" * 60)
    for result in all_results:
        print(f"\n--- {result['ticket_number']} ({result['final_status']}) ---")
        for entry in result["audit_log"]:
            print(f"  [{entry['timestamp']}] {entry['agent']}: {entry['action']} \u2014 {entry['detail']}")