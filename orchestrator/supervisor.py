"""
ISDO Lab C6 - LangGraph Orchestrator
Wires the Triage, Resolution, SLA, HITL and Communication agents (Labs C3-C5)
into a single StateGraph.

Flow:
    triage -> resolution -> sla -> [hitl if hitl_required] -> communication

hitl_required is set only for P1 tickets at CRITICAL/BREACHED SLA risk (same
rule used standalone in Lab C5). Non-P1 CRITICAL/BREACHED tickets still get
escalated, just without a human pause, matching Lab C5's escalation rule.

Run from the project root:  python orchestrator/supervisor.py
"""

import operator
import os
import sys
from datetime import datetime, timezone
from typing import Annotated, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agents"))

import resolution_agent   # noqa: E402  (path set above)
import sla_agent          # noqa: E402
import triage_agent       # noqa: E402

# -- Shared state ---------------------------------------------------------------

class TicketState(TypedDict, total=False):
    # Input
    ticket_number: str
    short_description: str
    description: str
    category: str
    priority: str
    sla_due: str
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

    return {
        "kb_article": result.get("kb_article_used", "None"),
        "resolution_text": result.get("resolution_text", ""),
        "auto_resolve": bool(result.get("auto_resolve")),
        "confidence": result.get("confidence", "LOW"),
        "audit_log": log("ResolutionAgent", "search_kb",
                          f"{result.get('kb_article_used', 'None')} - {result.get('confidence')} "
                          f"({result.get('top_score', 0):.0%})"),
    }


def sla_node(state: TicketState) -> dict:
    print(f"\n\u25b6 SLA AGENT \u2014 checking deadline")

    status = sla_agent.get_sla_status(state["ticket_number"], state["sla_due"], state["triage_priority"])
    breach_risk = status.get("breach_risk", "ON_TRACK")
    escalation_required = bool(status.get("requires_escalation"))
    # HITL gate: only P1 tickets at CRITICAL/BREACHED pause for a human (Lab C5's rule).
    hitl_required = escalation_required and state["triage_priority"] == "P1"

    print(f"  SLA Risk: {breach_risk}  |  Minutes remaining: {status.get('minutes_remaining')}")

    return {
        "sla_breach_risk": breach_risk,
        "escalation_required": escalation_required,
        "hitl_required": hitl_required,
        "escalation_team": sla_agent.ESCALATION_TEAMS.get(state["triage_category"], "L2-Service-Desk"),
        "audit_log": log("SLAAgent", "get_sla_status", f"{breach_risk} ({status.get('minutes_remaining')} min remaining)"),
    }


def hitl_node(state: TicketState) -> dict:
    approved = sla_agent.hitl_approve(state["ticket_number"], "Escalate ticket",
                                       f"Escalate to {state['escalation_team']}")
    if approved:
        sla_agent.update_ticket(state["ticket_number"], "escalate", escalation_team=state["escalation_team"])
        detail = f"Approved - escalated to {state['escalation_team']}"
    else:
        detail = "Rejected by human approver - escalation cancelled"

    return {"hitl_approved": approved, "audit_log": log("HITLGate", "approve_escalation", detail)}


def communication_node(state: TicketState) -> dict:
    print(f"\n\u25b6 COMMUNICATION AGENT")

    ticket = state["ticket_number"]
    if state.get("auto_resolve"):
        message = (f"Dear User, regarding {ticket}: we found a known fix for this issue "
                    f"({state.get('kb_article')}) and applied it automatically.\n\n{state.get('resolution_text')}")
        final_status = "RESOLVED"
    elif state.get("hitl_approved") is True:
        message = (f"Dear User, regarding {ticket}: this ticket has been escalated to "
                    f"{state.get('escalation_team')} following approval. You will be contacted shortly.")
        final_status = "ESCALATED"
    elif state.get("hitl_approved") is False:
        message = (f"Dear User, regarding {ticket}: escalation was reviewed and held for manual handling "
                    f"by {state.get('triage_assignment_group')}.")
        final_status = "ESCALATION REJECTED - MANUAL REVIEW"
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
        {"ticket_number": "INC0001002", "short_description": "Cannot access ERP system - login error",
         "description": "Multiple Finance users unable to login to SAP. Error: DBCON_FAIL.",
         "category": "Application", "priority": "P1", "sla_due": "2024-01-15 10:40:00"},
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