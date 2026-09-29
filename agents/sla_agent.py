"""
ISDO Lab C5 - SLA & Escalation Agent
Monitors SLA deadlines, predicts breach risk, and escalates CRITICAL/BREACHED tickets.
A HITL gate pauses for human approval before any P1 escalation is executed.

Run from the project root:  python agents/sla_agent.py
"""

import json
import os
import sys
from datetime import datetime

import anthropic
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")   # safe printing of warning icons on Windows

load_dotenv()
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")   # override via .env if needed
SIMULATED_NOW = datetime(2024, 1, 15, 10, 30)                  # fixed "now" for reproducible demo output
SLA_MINUTES = {"P1": 60, "P2": 240, "P3": 480, "P4": 1440}
MAX_ROUNDS = 4

# -- Tool definitions ----------------------------------------------------------

tools = [
    {
        "name": "get_sla_status",
        "description": "Check the SLA status of a ticket. Returns minutes remaining and a breach_risk level.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_number": {"type": "string"},
                "sla_due": {"type": "string", "description": "SLA due datetime, format YYYY-MM-DD HH:MM:SS"},
                "priority": {"type": "string", "enum": ["P1", "P2", "P3", "P4"]},
            },
            "required": ["ticket_number", "sla_due", "priority"],
        },
    },
    {
        "name": "update_ticket",
        "description": "Update the ticket in ServiceNow: escalate, add a work note, or change state.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_number": {"type": "string"},
                "action": {"type": "string", "enum": ["escalate", "add_note", "update_state"]},
                "escalation_team": {"type": "string", "description": "Team to escalate to, e.g. L2-Network-Ops"},
                "note": {"type": "string"},
                "new_state": {"type": "string"},
            },
            "required": ["ticket_number", "action"],
        },
    },
]

# -- Tool implementation --------------------------------------------------------

def get_sla_status(ticket_number, sla_due, priority):
    """breach_risk: BREACHED (past due) / CRITICAL (<20% left) / AT_RISK (<50% left) / ON_TRACK."""
    try:
        due_dt = datetime.strptime(sla_due, "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return {"error": f"Invalid sla_due format: {sla_due}"}

    minutes_remaining = int((due_dt - SIMULATED_NOW).total_seconds() / 60)
    total_minutes = SLA_MINUTES.get(priority, 480)

    if minutes_remaining < 0:
        risk, msg = "BREACHED", f"SLA BREACHED by {abs(minutes_remaining)} minutes"
    elif minutes_remaining < total_minutes * 0.2:
        risk, msg = "CRITICAL", f"Only {minutes_remaining} minutes remaining - breach imminent"
    elif minutes_remaining < total_minutes * 0.5:
        risk, msg = "AT_RISK", f"{minutes_remaining} minutes remaining - at risk"
    else:
        risk, msg = "ON_TRACK", f"{minutes_remaining} minutes remaining - on track"

    return {
        "ticket_number": ticket_number, "sla_due": sla_due, "priority": priority,
        "minutes_remaining": minutes_remaining, "breach_risk": risk, "status_message": msg,
        "requires_escalation": risk in ("BREACHED", "CRITICAL"),
    }


def update_ticket(ticket_number, action, escalation_team=None, note=None, new_state=None):
    """Simulated ServiceNow PATCH."""
    result = {"ticket_number": ticket_number, "action": action, "success": True,
              "timestamp": SIMULATED_NOW.strftime("%Y-%m-%d %H:%M:%S")}
    if action == "escalate":
        result["message"] = f"Ticket {ticket_number} escalated to {escalation_team}"
        print(f"  [ServiceNow Mock] ESCALATED {ticket_number} -> {escalation_team}")
    elif action == "add_note":
        result["message"] = f"Work note added to {ticket_number}: {(note or '')[:50]}"
        print(f"  [ServiceNow Mock] NOTE ADDED to {ticket_number}")
    elif action == "update_state":
        result["message"] = f"Ticket {ticket_number} state changed to: {new_state}"
        print(f"  [ServiceNow Mock] STATE CHANGED {ticket_number} -> {new_state}")
    return result


def handle_tool(name, inp):
    if name == "get_sla_status":
        return get_sla_status(inp["ticket_number"], inp["sla_due"], inp["priority"])
    if name == "update_ticket":
        return update_ticket(inp["ticket_number"], inp["action"], inp.get("escalation_team"),
                              inp.get("note"), inp.get("new_state"))
    return {"error": "Unknown tool"}

# -- HITL gate ------------------------------------------------------------------

def hitl_approve(ticket_number, action, detail):
    """Pause for human approval. Returns True only if the operator types 'y'."""
    print(f"\n  {'!!! ' * 5}")
    print("  HITL APPROVAL REQUIRED")
    print(f"  Ticket:  {ticket_number}")
    print(f"  Action:  {action}")
    print(f"  Detail:  {detail}")
    print(f"  {'!!! ' * 5}")
    decision = input("  Approve escalation? [y/n]: ").strip().lower()
    print(f"  Decision: {'APPROVED' if decision == 'y' else 'REJECTED'}")
    return decision == "y"

# -- SLA agent --------------------------------------------------------------------

ESCALATION_TEAMS = {
    "Network": "L2-Network-Ops", "Application": "L2-App-Support",
    "Server": "L2-Server-Ops", "Access": "L2-Security-Ops", "Security": "L2-Security-Ops",
}

SYSTEM_PROMPT = """You are the ISDO SLA & Escalation Agent for Zensar's IT Service Desk.

For each ticket:
1. Call get_sla_status ONCE to check breach risk.
2. If breach_risk is CRITICAL or BREACHED, call update_ticket with action="escalate" and an
   escalation_team appropriate to the ticket's category (Network -> L2-Network-Ops,
   Application -> L2-App-Support, Server -> L2-Server-Ops, Access/Security -> L2-Security-Ops,
   otherwise L2-Service-Desk).
3. If breach_risk is AT_RISK or ON_TRACK, do not escalate - the ticket is being monitored only.

Never call get_sla_status more than once for the same ticket."""


def monitor_ticket(ticket_number, short_description, category, priority, sla_due) -> dict:
    """Run SLA monitoring for one ticket. Returns the final status (used by the C6 orchestrator).
    The HITL gate is enforced here in code for every P1 escalation, regardless of what the
    model does or doesn't ask for - the model cannot bypass it."""
    print(f"\n{'=' * 55}")
    print(f"SLA Check: {ticket_number} | {priority} | Category: {category}")
    print("=" * 55)

    messages = [{
        "role": "user",
        "content": (f"Monitor SLA for this ticket and escalate if needed:\n\nTicket: {ticket_number}\n"
                    f"Description: {short_description}\nCategory: {category}\nPriority: {priority}\n"
                    f"SLA Due: {sla_due}"),
    }]
    final = {"ticket_number": ticket_number, "priority": priority, "breach_risk": None,
             "escalated": False, "hitl_decision": None}

    for _ in range(MAX_ROUNDS):
        response = client.messages.create(
            model=MODEL, max_tokens=600, output_config={"effort": "low"},
            system=SYSTEM_PROMPT, tools=tools, messages=messages,
        )

        if response.stop_reason != "tool_use":
            for block in response.content:
                if hasattr(block, "text"):
                    print(block.text)
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []

        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "get_sla_status":
                result = handle_tool(block.name, block.input)
                final["breach_risk"] = result.get("breach_risk")
                print(f"  -> Risk Level: {result.get('breach_risk')}")
                print(f"  -> Status:     {result.get('status_message')}")

            elif block.name == "update_ticket" and block.input.get("action") == "escalate":
                team = block.input.get("escalation_team") or ESCALATION_TEAMS.get(category, "L2-Service-Desk")
                # HITL gate: enforced by priority, not by a flag passed in from the caller.
                if priority == "P1":
                    approved = hitl_approve(ticket_number, "Escalate ticket", f"Escalate to {team}")
                    final["hitl_decision"] = "APPROVED" if approved else "REJECTED"
                    if not approved:
                        result = {"success": False, "message": "Escalation rejected by human approver"}
                        print("  Escalation cancelled and logged.")
                        tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})
                        continue
                result = update_ticket(ticket_number, "escalate", escalation_team=team)
                final["escalated"] = True

            else:
                result = handle_tool(block.name, block.input)

            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        messages.append({"role": "user", "content": tool_results})

    return final

# -- Run SLA monitoring -----------------------------------------------------------

if __name__ == "__main__":
    # Simulated "now" = 2024-01-15 10:30. Four tickets, one per breach_risk state.
    test_tickets = [
        # P1, 10 min remaining of 60 (<20%) -> CRITICAL -> escalates, HITL gate fires
        ("INC0001002", "Cannot access ERP - SAP login failure", "Application", "P1", "2024-01-15 10:40:00"),
        # P1, already past due -> BREACHED -> escalates, HITL gate fires again
        ("INC0001010", "Exchange server high CPU", "Server", "P1", "2024-01-15 09:30:00"),
        # P2, 90 min remaining of 240 (20-50%) -> AT_RISK -> monitored only, no escalation
        ("INC0001001", "VPN not connecting", "Network", "P2", "2024-01-15 12:00:00"),
        # P3, days remaining -> ON_TRACK -> monitored only, no escalation
        ("INC0001003", "Laptop running slowly", "Hardware", "P3", "2024-01-17 09:00:00"),
    ]

    for ticket_number, desc, category, priority, sla_due in test_tickets:
        monitor_ticket(ticket_number, desc, category, priority, sla_due)