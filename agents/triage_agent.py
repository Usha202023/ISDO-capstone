"""
ISDO Lab C3 — Triage Agent
Reads a ticket and assigns: category, priority, assignment group, and PII flag.
Uses the Anthropic SDK with tool calling.
"""

import anthropic
import csv
import json
import os
from dotenv import load_dotenv

load_dotenv()
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")   # override via .env if needed
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSV_PATH = os.path.join(ROOT, "data", "incidents.csv")          # works from any working directory

# ── TOOL DEFINITIONS ──────────────────────────────────────────────────────────

tools = [
    {
        "name": "classify_ticket",
        "description": "Classify an IT support ticket. Returns category, priority, assignment_group, and whether PII was detected.",
        "input_schema": {
            "type": "object",
            "properties": {
                "category": {
                    "type": "string",
                    "enum": ["Network", "Application", "Hardware", "Access", "Email", "Server", "Software"],
                    "description": "The ticket category"
                },
                "priority": {
                    "type": "string",
                    "enum": ["P1", "P2", "P3", "P4"],
                    "description": "P1=Critical/many users affected, P2=High/some users, P3=Medium/single user, P4=Low/request"
                },
                "assignment_group": {
                    "type": "string",
                    "description": "Team to assign the ticket to e.g. Network-Ops, App-Support, Desktop-Support, Service-Desk, Security-Ops, Server-Ops"
                },
                "pii_detected": {
                    "type": "boolean",
                    "description": "True if the ticket contains names, email addresses, employee IDs, or IP addresses"
                },
                "reasoning": {
                    "type": "string",
                    "description": "One sentence explaining the classification decision"
                }
            },
            "required": ["category", "priority", "assignment_group", "pii_detected", "reasoning"]
        }
    },
    {
        "name": "get_open_tickets",
        "description": "Get a summary count of currently open tickets by category from the incidents CSV.",
        "input_schema": {
            "type": "object",
            "properties": {
                "csv_path": {
                    "type": "string",
                    "description": "Path to incidents.csv file"
                }
            },
            "required": ["csv_path"]
        }
    }
]

# ── TOOL IMPLEMENTATION ───────────────────────────────────────────────────────

def get_open_tickets(csv_path):
    """Read incidents.csv and return count by category."""
    counts = {}
    try:
        with open(csv_path, newline="") as f:
            for row in csv.DictReader(f):
                row.pop(None, None)   # ignore overflow columns from malformed rows
                if row.get("state") == "Open":
                    cat = row.get("category", "Unknown")
                    counts[cat] = counts.get(cat, 0) + 1
    except FileNotFoundError:
        return {"error": f"File not found: {csv_path}"}
    return counts

def handle_tool_call(tool_name, tool_input):
    """Route tool calls to their implementations."""
    if tool_name == "get_open_tickets":
        return get_open_tickets(tool_input["csv_path"])
    elif tool_name == "classify_ticket":
        return tool_input   # classification is the tool's own output
    return {"error": "Unknown tool"}

# ── TRIAGE AGENT ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are the ISDO Triage Agent for Zensar's IT Service Desk.

Your job is to classify incoming IT support tickets. For each ticket:
1. Use the classify_ticket tool to assign category, priority, and assignment group
2. Flag if any PII (names, emails, employee IDs, IP addresses) is present

Priority rules:
- P1: Service down, many users affected, or security breach
- P2: Significant impact, single department or function affected
- P3: Single user impacted, workaround exists
- P4: Request (new software, access, equipment)

Always use temperature=0 logic: consistent, rule-based classification."""

def triage_ticket(ticket_number, short_description, description):
    """Run the triage agent on a single ticket."""
    print(f"\n{'='*55}")
    print(f"Triaging: {ticket_number}")
    print(f"{'='*55}")
    print(f"Description: {short_description}")

    messages = [
        {
            "role": "user",
            "content": f"Please triage this ticket:\n\nTicket: {ticket_number}\nSummary: {short_description}\nDetails: {description}"
        }
    ]

    # Agentic loop
    while True:
        response = client.messages.create(
            model=MODEL,
            max_tokens=500,
            output_config={"effort": "low"},
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages
        )

        if response.stop_reason == "end_turn":
            # Extract text response if any
            for block in response.content:
                if hasattr(block, "text"):
                    print(block.text)
            break

        if response.stop_reason == "tool_use":
            # Process tool calls
            messages.append({"role": "assistant", "content": response.content})
            tool_results = []

            for block in response.content:
                if block.type == "tool_use":
                    print(f"  → Tool called: {block.name}")
                    result = handle_tool_call(block.name, block.input)

                    if block.name == "classify_ticket":
                        print(f"  → Category:    {result.get('category')}")
                        print(f"  → Priority:    {result.get('priority')}")
                        print(f"  → Assign To:   {result.get('assignment_group')}")
                        print(f"  → PII Found:   {result.get('pii_detected')}")
                        print(f"  → Reason:      {result.get('reasoning')}")

                    tool_results.append({
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": json.dumps(result)
                    })

            messages.append({"role": "user", "content": tool_results})
            continue

        # max_tokens / refusal / anything else: stop instead of looping forever
        print(f"  ! Stopped with stop_reason={response.stop_reason}")
        break

# ── RUN ON SAMPLE TICKETS ─────────────────────────────────────────────────────

if __name__ == "__main__":
    # 5 lab tickets + 1 custom ticket (Step 5)
    test_tickets = [
        ("INC0001001", "VPN not connecting after password change",
         "User reports VPN client fails to connect after AD password was reset. Error: authentication failed."),
        ("INC0001002", "Cannot access ERP system - login error",
         "Multiple users in Finance unable to login to SAP. Error code: DBCON_FAIL. Started 09:00 today."),
        ("INC0001008", "Network switch down - Building C",
         "Network switch in Building C server room unresponsive. 40 users in Building C affected."),
        ("INC0001006", "Password reset request",
         "User locked out of AD account after 5 failed attempts. Needs immediate reset."),
        ("REQ-1002", "VPN access for new contractor joining project Phoenix",
         "New contractor [REDACTED NAME] emp-id ZEN-9823 joining next Monday. Email: contractor@client.com"),
        # Step 5 - your own ticket
        ("TEST-001", "Salesforce CRM access issue",
         "User cannot access Salesforce CRM from company laptop since this morning."),
        # Step 5 variant - uncomment to see priority change with multiple users
        # ("TEST-002", "Salesforce CRM access issue",
        #  "Entire Sales team (25 users) cannot access Salesforce CRM since this morning."),
    ]

    for number, short_desc, desc in test_tickets:
        triage_ticket(number, short_desc, desc)

    print("\n" + "="*55)
    print("OPEN TICKET COUNTS BY CATEGORY")
    print("="*55)
    # Also demo the get_open_tickets tool
    counts = get_open_tickets(CSV_PATH)
    for cat, count in sorted(counts.items()):
        print(f"  {cat:<20} {count} open")