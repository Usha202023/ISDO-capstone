"""
ISDO Lab C9 - LangGraph Orchestrator with PII redaction middleware + audit trail
Routes a ticket through Triage -> Resolution -> SLA -> [HITL] -> Communication.

The HITL gate fires for any of three triggers (Lab C7 Step 1):
  1. P1 ticket whose SLA is CRITICAL or BREACHED            -> approve = escalate to L2
  2. Resolution Agent confidence is LOW (any priority)      -> approve = route to L2 specialist
  3. Access Grant request (category Access, REQ- ticket)    -> approve = grant access in Jira
Rejecting any of them sends the requester a "pending approval" message.

PII (Lab C9): triage_node masks names, emails, IDs, IPs and phones with
guardrails/pii_redactor.py. Every Claude call uses only the masked text; the real values
are restored just before the user message is written to ServiceNow/Jira.
Every node writes to ONE AuditLogger -> logs/audit_trail.jsonl (PII-free).

Uses the Triage and Resolution agents from agents/ (any version that provides
triage_agent.triage_ticket() and resolution_agent.resolve_ticket()). The SLA check is plain
deterministic logic, so it lives here and does not depend on agents/sla_agent.py.

Setup (once, from the lab root):
    python -m pip install -U langgraph anthropic python-dotenv chromadb

Run:
    python orchestrator/supervisor.py            (set ISDO_VERBOSE=1 to see each agent's full trace)
"""

import contextlib
import csv
import getpass
import importlib.util
import io
import json
import operator
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Annotated, TypedDict

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "agents"))
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(errors="replace")  # never crash on the box-drawing characters

try:
    from langgraph.graph import END, START, StateGraph  # noqa: E402
except ImportError:
    sys.exit(f"LangGraph is not installed for this Python:\n  {sys.executable}\n"
             f"Install it into exactly this interpreter with:\n"
             f'  "{sys.executable}" -m pip install -U langgraph')

import resolution_agent  # noqa: E402  (loads the KB on import)
import triage_agent  # noqa: E402
# Load guardrails/pii_redactor.py by its file path, so it works whatever "guardrails" resolves
# to on the import path (agents/guardrails.py from earlier labs, a package, or nothing).
PII_REDACTOR_FILE = ROOT / "guardrails" / "pii_redactor.py"
if not PII_REDACTOR_FILE.exists():
    sys.exit(f"PII redactor not found: {PII_REDACTOR_FILE}")
_spec = importlib.util.spec_from_file_location("pii_redactor", PII_REDACTOR_FILE)
pii_redactor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(pii_redactor)
redact, restore, AuditLogger = pii_redactor.redact, pii_redactor.restore, pii_redactor.AuditLogger

VERBOSE = os.environ.get("ISDO_VERBOSE") == "1"
INCIDENTS_CSV = ROOT / "data" / "incidents.csv"
JIRA_URL = os.environ.get("ISDO_JIRA_URL", "http://localhost:5002")
# Same Anthropic client and model as the other agents (created here if triage_agent has none)
client = getattr(triage_agent, "client", None)
if client is None:
    import anthropic
    client = anthropic.Anthropic()
MODEL = getattr(triage_agent, "MODEL", None) or os.environ.get("ISDO_MODEL", "claude-opus-5")

# ONE audit logger shared by every node (Lab C9) - appends to logs/audit_trail.jsonl as it happens
AUDIT = AuditLogger(str(ROOT / "logs" / "audit_trail.jsonl"))

# Record every request sent to Claude, so the run can prove no raw PII was in any of them
CLAUDE_REQUESTS = []


def _record(original):
    def create(self, *args, **kwargs):
        CLAUDE_REQUESTS.append(json.dumps(kwargs, default=str))
        return original(self, *args, **kwargs)
    return create


try:  # SDK level: catches every Anthropic client in this process, however an agent creates it
    from anthropic.resources.messages import Messages as _Messages
    _Messages.create = _record(_Messages.create)
except (ImportError, AttributeError):  # fallback: the agents' own client objects
    for _mod in (triage_agent, resolution_agent):
        _c = getattr(_mod, "client", None)
        if _c is not None and not getattr(_c.messages, "_isdo_recorded", False):
            _orig = _c.messages.create
            _c.messages.create = (lambda o: lambda **kw: (CLAUDE_REQUESTS.append(
                json.dumps(kw, default=str)), o(**kw))[1])(_orig)
            _c.messages._isdo_recorded = True


# -- SLA POLICY (same rules as Lab C5, owned here so any sla_agent.py version works) ---------
NOW = datetime(2024, 1, 15, 10, 30)          # simulated "now" - the mock data is from Jan 2024
SLA_TARGET_MIN = {"P1": 60, "P2": 240, "P3": 480, "P4": 1440}
CRITICAL_FRACTION, AT_RISK_FRACTION = 0.5, 1.0   # share of the SLA window still left
ESCALATE_PRIORITIES = {"P1", "P2"}           # P3/P4 are monitored, never auto-escalated
HITL_PRIORITIES = {"P1"}                     # P1 escalations need human approval
ESCALATION_TEAMS = {"Network": "L2-Network-Ops", "Application": "L2-App-Support",
                    "Server": "L2-Server-Ops", "Access": "L2-Security-Ops"}
DEFAULT_TEAM = "L2-Service-Desk"
SNOW_URL = os.environ.get("ISDO_SNOW_URL", "http://localhost:5001")


def get_sla_status(ticket_number, sla_due, priority):
    """Minutes left until sla_due, rated against the priority's SLA window."""
    if priority not in SLA_TARGET_MIN:
        return {"error": f"Unknown priority '{priority}' - expected P1-P4"}
    try:
        due = datetime.fromisoformat(str(sla_due).strip())
    except ValueError:
        return {"error": f"Cannot parse sla_due '{sla_due}'"}
    target = SLA_TARGET_MIN[priority]
    left = int((due - NOW).total_seconds() // 60)
    risk = ("BREACHED" if left < 0 else "CRITICAL" if left <= target * CRITICAL_FRACTION
            else "AT_RISK" if left < target * AT_RISK_FRACTION else "ON_TRACK")
    message = {"BREACHED": f"SLA breached by {-left} minutes",
               "CRITICAL": f"Only {left} minutes remaining - breach imminent",
               "AT_RISK": f"{left} minutes remaining - at risk",
               "ON_TRACK": f"{left} minutes remaining - on track"}[risk]
    return {"ticket_number": ticket_number, "minutes_remaining": left, "breach_risk": risk,
            "status_message": message, "sla_target_min": target,
            "requires_escalation": risk in ("BREACHED", "CRITICAL")
                                   and priority in ESCALATE_PRIORITIES}


def patch_servicenow(number, fields):
    """PATCH the Lab C2 ServiceNow mock; 'SIMULATED' if it is not running."""
    req = urllib.request.Request(f"{SNOW_URL}/api/now/table/incident/{number}", method="PATCH",
                                 data=json.dumps(fields).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3):
            return "ServiceNow mock"
    except urllib.error.HTTPError as e:
        return f"ServiceNow mock error {e.code}"
    except (urllib.error.URLError, OSError):
        return "SIMULATED"


# HITL trigger codes -> short label shown in the approval prompt
P1_SLA, LOW_CONFIDENCE, ACCESS_GRANT = "P1_SLA", "LOW_CONFIDENCE", "ACCESS_GRANT"

# -- SHARED STATE -------------------------------------------------------------------

class TicketState(TypedDict, total=False):
    # ticket record (input)
    ticket_number: str
    short_description: str
    description: str
    category: str
    priority: str
    sla_due: str
    request_type: str             # Jira request type, e.g. "Access Grant" (C7)
    # PII middleware (C9) - only the clean_* fields are ever sent to Claude
    clean_short_description: str
    clean_description: str
    pii_mapping: dict             # token -> real value; in memory only, never logged
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
    confidence_score: float       # None if the Resolution Agent gives a band only
    # SLA agent / HITL routing
    sla_breach_risk: str
    escalation_required: bool
    escalation_team: str
    hitl_required: bool
    hitl_reason: str              # human-readable reason(s) shown in the approval prompt (C7)
    hitl_triggers: list           # machine-readable codes: P1_SLA / LOW_CONFIDENCE / ACCESS_GRANT
    # HITL node
    hitl_approved: bool
    # Communication agent
    user_message: str
    final_status: str
    # Every node appends; operator.add merges each node's new entries into the list
    audit_log: Annotated[list, operator.add]


def log(state, agent, action, rationale, tool="", approval="Auto"):
    """Write one entry to the shared AuditLogger (file) and return it for TicketState['audit_log'].
    Nodes return {"audit_log": [log(...)]} and LangGraph appends it to the list."""
    return AUDIT.log(agent, action, ticket_number=state["ticket_number"], tool=tool,
                     rationale=rationale, approval_status=approval)


@contextlib.contextmanager
def quiet():
    """Hide the sub-agents' own step-by-step printing unless ISDO_VERBOSE=1."""
    if VERBOSE:
        yield
    else:
        with contextlib.redirect_stdout(io.StringIO()):
            yield


def working_priority(state):
    """Triage's priority drives automation, but a ticket logged as P1 is never downgraded."""
    return "P1" if "P1" in (state.get("priority"), state.get("triage_priority")) \
        else state.get("triage_priority") or state.get("priority")


def is_access_grant(state):
    """Access Grant request: security-sensitive, always needs a human (Lab C7 trigger 3)."""
    return (state.get("request_type", "").strip().lower() == "access grant"
            and "Access" in (state.get("category"), state.get("triage_category")))


def update_record(number, fields):
    """Write to the system that owns the ticket: REQ- -> Jira mock, INC -> ServiceNow mock."""
    if number.startswith("INC"):
        return patch_servicenow(number, fields)
    if not number.startswith("REQ-"):
        return "SIMULATED"  # lab-only test tickets that exist in neither system
    jira = {"status" if k == "state" else k: v for k, v in fields.items()}
    req = urllib.request.Request(f"{JIRA_URL}/rest/api/2/issue/{number}", method="PUT",
                                 data=json.dumps({"fields": jira}).encode(),
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=3):
            return "Jira mock"
    except urllib.error.HTTPError as e:
        return f"Jira mock error {e.code}"
    except (urllib.error.URLError, OSError):
        return "SIMULATED"

# -- NODES ----------------------------------------------------------------------------

def triage_node(state: TicketState) -> dict:
    n = state["ticket_number"]
    print(f"\n▶ TRIAGE AGENT — {n}")
    # PII middleware: mask once here; every later Claude call uses the clean_* fields.
    # Short and long descriptions are redacted together so a name gets the same token in both.
    sep = "\n|||\n"  # no PII pattern can match across this
    joined, mapping = redact(state["short_description"] + sep + state["description"])
    clean_short, clean_desc = joined.split(sep, 1)
    if mapping:
        print(f"  PII masked: {', '.join(mapping)}")
    with quiet():
        result = triage_agent.triage_ticket(n, clean_short, clean_desc)

    if result is None:  # agent failed -> fall back to the logged values, route to Service-Desk
        result = {"category": state.get("category", "Unknown"), "priority": state.get("priority", "P3"),
                  "assignment_group": "Service-Desk", "pii_detected": bool(mapping)}
        detail = "triage failed - using logged category/priority, routed to Service-Desk"
    else:
        detail = result.get("reasoning", "")
    print(f"  Category: {result['category']}  Priority: {result['priority']}"
          + (f" (logged as {state.get('priority')})" if result["priority"] != state.get("priority") else ""))
    print(f"  Assign To: {result['assignment_group']}  PII: {result['pii_detected']}")
    pii = bool(result["pii_detected"]) or bool(mapping)  # redactor hit is never overruled
    entries = [log(state, "PIIRedactor", "redact", f"{len(mapping)} item(s) masked: "
                   f"{', '.join(mapping) or 'none'}", tool="pii_redactor.redact")]
    entries.append(log(state, "TriageAgent", "classify_ticket",
                       f"{result['category']}/{result['priority']} -> "
                       f"{result['assignment_group']}. {detail}", tool="classify_ticket"))
    return {"clean_short_description": clean_short, "clean_description": clean_desc,
            "pii_mapping": mapping,
            "triage_category": result["category"], "triage_priority": result["priority"],
            "triage_assignment_group": result["assignment_group"],
            "pii_detected": pii, "audit_log": entries}


def _pct(score):
    """Format a 0-1 score as a percentage, or 'n/a' if the agent did not provide one."""
    return "n/a" if score is None else f"{score:.0%}"


_SHAPE_REPORTED = False


def normalise_resolution(out) -> dict:
    """Accept the result of any resolve_ticket() version (C4, C8 with A2A, ...).
    Missing values fall back to the SAFE choice: no auto-resolve, LOW confidence (-> human)."""
    global _SHAPE_REPORTED
    out = out if isinstance(out, dict) else {}

    def first(*keys, default=None):
        return next((out[k] for k in keys if out.get(k) is not None), default)

    score = first("confidence_score", "score", "similarity", "kb_score", "match_score")
    if isinstance(score, (int, float)):
        score = float(score) / 100 if score > 1 else float(score)  # 85 -> 0.85
    else:
        score = None
    band = str(first("confidence", "confidence_level", "confidence_band", default="")).upper()
    if band not in ("HIGH", "MEDIUM", "LOW"):
        band = ("LOW" if score is None else
                "HIGH" if score > resolution_agent_threshold("HIGH_THRESHOLD", 0.60) else
                "MEDIUM" if score > resolution_agent_threshold("MEDIUM_THRESHOLD", 0.35) else "LOW")
    reasons = first("hitl_reasons", "reasons", default=[])
    result = {
        "kb_article_used": first("kb_article_used", "kb_article", "article", "source_article",
                                 default="none"),
        "resolution_text": first("resolution_text", "resolution", "draft", default=""),
        "auto_resolve": bool(first("auto_resolve", default=False)),
        "confidence": band,
        "confidence_score": score,
        "hitl_reasons": reasons if isinstance(reasons, list) else [str(reasons)],
        "source": first("source", "answered_by", "resolved_by", default=""),
    }
    if not _SHAPE_REPORTED:
        _SHAPE_REPORTED = True
        missing = [k for k in ("confidence_score", "kb_article_used", "auto_resolve", "hitl_reasons")
                   if k not in out]
        if missing:
            print(f"  (note: resolve_ticket() returned keys {sorted(out)}; "
                  f"adapted for {', '.join(missing)})")
    return result


def resolution_agent_threshold(name, default):
    return getattr(resolution_agent, name, default)


def resolution_node(state: TicketState) -> dict:
    print("\n▶ RESOLUTION AGENT — searching KB")
    triage = {"category": state["triage_category"], "priority": working_priority(state)}
    with quiet():
        raw = resolution_agent.resolve_ticket(state["ticket_number"], state["clean_short_description"],
                                              state["clean_description"], triage)
    out = normalise_resolution(raw)
    print(f"  KB Article: {out['kb_article_used']}"
          + (f"  (via {out['source']})" if out["source"] else ""))
    print(f"  Confidence: {out['confidence']} ({_pct(out['confidence_score'])})  │  "
          f"Auto-resolve: {out['auto_resolve']}")
    for reason in out["hitl_reasons"]:
        print(f"    - not auto-resolved: {reason}")
    return {"kb_article": out["kb_article_used"], "resolution_text": out["resolution_text"],
            "auto_resolve": out["auto_resolve"], "confidence": out["confidence"],
            "confidence_score": out["confidence_score"],
            "audit_log": [log(state, "ResolutionAgent", "search_kb",
                              f"{out['kb_article_used']} {out['confidence']} "
                              f"({_pct(out['confidence_score'])}), auto_resolve={out['auto_resolve']}"
                              + (f"; reasons: {'; '.join(out['hitl_reasons'])}"
                                 if out["hitl_reasons"] else ""), tool="search_kb")]}


def sla_node(state: TicketState) -> dict:
    print("\n▶ SLA AGENT — checking deadline")
    n = state["ticket_number"]
    team = ESCALATION_TEAMS.get(state["triage_category"], DEFAULT_TEAM)
    triggers, reasons, entries = [], [], []

    # The SLA clock is the one logged on the ticket (its priority + sla_due)
    sla = get_sla_status(n, state["sla_due"], state["priority"])
    if "error" in sla:
        print(f"  !! {sla['error']} - sending to a human")
        risk, escalate = "UNKNOWN", False
        triggers.append(P1_SLA)
        reasons.append(f"SLA UNKNOWN -- {sla['error']}")
        entries.append(log(state, "SLAAgent", "get_sla_status", sla["error"], tool="get_sla_status"))
    else:
        risk = sla["breach_risk"]
        escalate = sla["requires_escalation"] and not state.get("auto_resolve")
        print(f"  SLA Risk: {risk}  │  Minutes remaining: {sla['minutes_remaining']}")
        entries.append(log(state, "SLAAgent", "get_sla_status", sla["status_message"],
                           tool="get_sla_status"))

    # Trigger 1: P1 with CRITICAL/BREACHED SLA
    if escalate and working_priority(state) in HITL_PRIORITIES:
        triggers.append(P1_SLA)
        reasons.append(f"P1 SLA {risk} -- {sla['status_message']}; escalate to {team}")
    # Trigger 2: Resolution Agent could not find a clear fix
    if state.get("confidence") == "LOW":
        triggers.append(LOW_CONFIDENCE)
        reasons.append(f"LOW KB CONFIDENCE ({_pct(state.get('confidence_score'))}) -- "
                       f"no reliable fix found; route to {team} specialist")
    # Trigger 3: security-sensitive access request
    if is_access_grant(state):
        triggers.append(ACCESS_GRANT)
        reasons.append(f"ACCESS GRANT -- {state['clean_short_description']} requires security approval")

    hitl = bool(triggers)
    if escalate and not hitl:  # P2 CRITICAL/BREACHED: escalate automatically (C5 policy)
        target = update_record(n, {"state": "Escalated", "escalated_to": team,
                                   "work_notes": sla["status_message"]})
        print(f"  [{target}] ESCALATED {n} -> {team} (no HITL needed)")
        entries.append(log(state, "SLAAgent", "update_ticket:escalate", f"{team} via {target}",
                           tool="update_ticket"))
    elif hitl:
        print(f"  HITL required: {' + '.join(triggers)}")
        entries.append(log(state, "HITLGate", "approval_request", " | ".join(reasons),
                           approval="PENDING"))
    return {"sla_breach_risk": risk, "escalation_required": escalate, "escalation_team": team,
            "hitl_required": hitl, "hitl_reason": " | ".join(reasons), "hitl_triggers": triggers,
            "audit_log": entries}


def ask_approval(state):
    """Terminal approval prompt. Anything other than y/yes - including no terminal - is a NO."""
    bar = "  " + "WARNING  " * 8
    print(f"\n{bar}\n  Ticket:  {state['ticket_number']}  │  Priority: {working_priority(state)}")
    print(f"  Issue:   {state['short_description']}")
    for reason in state["hitl_reason"].split(" | "):
        print(f"  Reason:  {reason}")
    print(bar)
    while True:
        try:
            answer = input("  Approve action? [y/n]: ").strip().lower()
        except EOFError:
            print("  (no terminal input available - treating as NO)")
            return False
        if answer in ("y", "yes", "n", "no"):
            return answer in ("y", "yes")
        print("  Please type y or n.")


def hitl_node(state: TicketState) -> dict:
    print("\n▶ HITL GATE -- human approval required")
    n, team, triggers = state["ticket_number"], state["escalation_team"], state["hitl_triggers"]
    approver = getpass.getuser()
    approved = ask_approval(state)

    if approved:
        fields = {"work_notes": f"Approved by {approver}: {state['hitl_reason']}"}
        if ACCESS_GRANT in triggers:
            fields["state"] = "Approved"
        elif P1_SLA in triggers or LOW_CONFIDENCE in triggers:
            fields.update({"state": "Escalated", "escalated_to": team})
    else:
        fields = {"state": "Pending Approval",
                  "work_notes": f"Rejected by {approver}: {state['hitl_reason']}"}
    target = update_record(n, fields)
    verdict = "APPROVED" if approved else "REJECTED"
    entry = log(state, "HITLGate", "approval_decision",
                f"{verdict} by {approver} [{', '.join(triggers)}] {state['hitl_reason']}",
                approval=verdict)
    print(f"  Decision: {verdict}  [{target}]")
    return {"hitl_approved": approved, "audit_log": [entry]}


COMMS_PROMPT = """You are the ISDO Communication Agent for Zensar's IT Service Desk.
Write a short, friendly message to the person who raised the ticket (under 120 words).
Start with the greeting given in the facts and sign off as "Zensar IT Service Desk".
Use only the facts provided. No internal jargon (never mention HITL, KB scores, confidence,
agents or SLA percentages), no promises of specific fix times. Plain text only.
Tokens such as [NAME_1] or [EMAIL_1] stand for real details that were masked for privacy:
copy any you need exactly as written, and never invent other placeholders."""


def plan_message(state):
    """Choose final status and the facts the message must convey."""
    wp = working_priority(state)
    triggers = state.get("hitl_triggers") or []
    group = state["triage_assignment_group"]
    team = state.get("escalation_team", "")
    if state.get("auto_resolve"):
        return ("RESOLVED", "self-service resolution", "Dear User,",
                f"The issue can be fixed by the user. Include these steps:\n{state['resolution_text']}")
    if state.get("hitl_required") and not state.get("hitl_approved"):
        what = "access request" if ACCESS_GRANT in triggers else "request"
        return ("PENDING_APPROVAL", "pending approval notice",
                "Dear Requester," if ACCESS_GRANT in triggers else "Dear User,",
                f"The {what} needs additional review and approval before we can proceed. "
                f"It remains open with the {group} team and the user will be updated once "
                f"a decision is made. No action is needed from the user right now.")
    if state.get("hitl_approved") and ACCESS_GRANT in triggers:
        return ("APPROVED", "access grant approval", "Dear Requester,",
                "The access grant request has been approved by the security approver and "
                "is now being set up. The requester will be notified when access is active.")
    if state.get("hitl_approved") and P1_SLA in triggers:
        return ("ESCALATED", "escalation confirmation", "Dear User,",
                f"The ticket has been escalated to the {team} team as a {wp} priority. "
                f"Engineers are working on it.")
    if state.get("hitl_approved") and LOW_CONFIDENCE in triggers:
        return ("ESCALATED", "specialist referral", "Dear User,",
                f"The ticket has been passed to a {team} specialist for investigation. "
                f"Ask the user to reply with these details to speed things up:\n"
                f"{state['resolution_text']}")
    if state.get("escalation_required"):
        return ("ESCALATED", "escalation confirmation", "Dear User,",
                f"The ticket has been escalated to the {team} team as a {wp} priority. "
                f"Engineers are working on it.")
    return ("ASSIGNED", "assignment notification", "Dear User,",
            f"The ticket has been assigned to the {group} team, who will contact the user "
            f"with next steps.")


def communication_node(state: TicketState) -> dict:
    print("\n▶ COMMUNICATION AGENT")
    n = state["ticket_number"]
    summary = state["clean_short_description"]  # masked - Claude never sees the real values
    status, kind, greeting, facts = plan_message(state)

    brief = f"Ticket: {n}\nIssue: {summary}\nMessage type: {kind}\nGreeting: {greeting}\n{facts}"
    try:
        response = client.messages.create(
            model=MODEL, max_tokens=1500, output_config={"effort": "low"},
            system=COMMS_PROMPT, messages=[{"role": "user", "content": brief}])
        message = "\n".join(b.text for b in response.content if b.type == "text").strip()
    except Exception as e:  # never lose the requester update because of an API hiccup
        print(f"  !! Drafting failed ({type(e).__name__}) - using template")
        message = ""
    if not message:
        message = f"{greeting}\n\nRegarding {n} ({summary}): {facts}\n\nZensar IT Service Desk"
    masked_message = message
    # restore(): put the real values back only for the system of record / the requester
    message = restore(masked_message, state.get("pii_mapping") or {})

    record_state = {"RESOLVED": "Resolved", "ESCALATED": "Escalated", "APPROVED": "Approved",
                    "ASSIGNED": "In Progress", "PENDING_APPROVAL": "Pending Approval"}[status]
    target = update_record(n, {"state": record_state, "comments": message})
    print("  USER MESSAGE (restored, as written to the ticket):")
    for line in message.splitlines():
        print(f"    {line}")
    if masked_message != message:
        print(f"  (Claude drafted it with tokens: "
              f"{', '.join(t for t in state['pii_mapping'] if t in masked_message)})")
    print(f"\n✅ FINAL STATUS: {status}  [{target}]")
    return {"user_message": message, "final_status": status,
            "audit_log": [log(state, "CommunicationAgent", "post_comment",
                              f"{kind} sent, status {status}", tool="post_comment")]}

# -- GRAPH ----------------------------------------------------------------------------

def route_after_sla(state: TicketState) -> str:
    return "hitl" if state.get("hitl_required") else "communication"


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
    graph.add_conditional_edges("sla", route_after_sla,
                                {"hitl": "hitl", "communication": "communication"})
    graph.add_edge("hitl", "communication")
    graph.add_edge("communication", END)
    return graph.compile()


def load_tickets(ids):
    """Load incidents from incidents.csv as TicketState dicts."""
    with INCIDENTS_CSV.open(newline="", encoding="utf-8-sig") as f:
        rows = {r["number"]: r for r in csv.DictReader(f) if None not in r}
    missing = [i for i in ids if i not in rows]
    if missing:
        sys.exit(f"Not found in {INCIDENTS_CSV}: {missing}")
    return [{"ticket_number": r["number"], "short_description": r["short_description"],
             "description": r["description"], "category": r["category"],
             "priority": r["priority"], "sla_due": r["sla_due"]} for r in (rows[i] for i in ids)]


if __name__ == "__main__":
    app = build_graph()
    tickets = load_tickets(["INC0001001", "INC0001002"])  # P2 VPN (no HITL), P1 SAP (HITL: P1 SLA)
    tickets += [
        # Step 3 - LOW KB confidence -> HITL even at P3 (a separate ticket: changing only the VPN
        # ticket's short_description keeps its VPN description, which still matches HIGH)
        {"ticket_number": "TEST-0003",
         "short_description": "Cisco Webex not launching on MacBook M2 after Sonoma update",
         "description": "Webex app bounces in the dock and closes immediately since the macOS "
                        "Sonoma update. Reinstalling did not help.",
         "category": "Software", "priority": "P3", "sla_due": "2024-01-15 17:00:00"},
        # Step 4 - Access Grant -> HITL regardless of priority
        # C9: description carries a name, employee ID, email and phone to prove masking
        {"ticket_number": "REQ-1002", "short_description": "VPN access for new contractor",
         "description": "Contractor Sarah Jones (ZEN-9823) joining project Phoenix needs VPN "
                        "access. Email: sarah.jones@client.com, phone +91-9876543210.",
         "category": "Access", "priority": "P2", "sla_due": "2024-01-15 15:00:00",
         "request_type": "Access Grant"},
    ]

    results = []
    for t in tickets:
        print(f"\n{'═' * 55}\nPROCESSING TICKET: {t['ticket_number']}\n{'═' * 55}")
        results.append(app.invoke({**t, "audit_log": []}))

    # -- 1) Audit trail per ticket, with final status --------------------------------
    for final in results:
        route = " → ".join(dict.fromkeys(e["agent"].replace("Agent", "")
                                         for e in final["audit_log"] if e["agent"] != "PIIRedactor"))
        print(f"\n{'═' * 72}\nAUDIT TRAIL: {final['ticket_number']}  │  FINAL STATUS: "
              f"{final['final_status']}\nRoute: {route}\n{'═' * 72}")
        for e in final["audit_log"]:
            print(f"  {e['timestamp'][11:19]}  {e['agent']:<19} {e['action']:<23} "
                  f"{e['approval_status']:<9} {e['rationale'][:90]}")

    # -- 2) What Claude actually saw ---------------------------------------------------
    print(f"\n{'═' * 72}\nPII MASKING — original ticket vs. what Claude received\n{'═' * 72}")
    for final in results:
        if not final.get("pii_mapping"):
            continue
        print(f"  {final['ticket_number']}")
        print(f"    Original : {final['description']}")
        print(f"    To Claude: {final['clean_description']}")

    # -- 3) Proof: no raw PII value appears in ANY request sent to Claude ---------------
    leaks = [(f["ticket_number"], tok) for f in results for tok, value in
             (f.get("pii_mapping") or {}).items() if any(value in r for r in CLAUDE_REQUESTS)]
    masked = sum(len(f.get("pii_mapping") or {}) for f in results)
    print(f"\n  PII LEAK CHECK: {masked} value(s) masked, {len(CLAUDE_REQUESTS)} Claude request(s) "
          f"scanned -> {'NO LEAKS' if not leaks else 'LEAKED: ' + str(leaks)}")

    print(f"\n{'═' * 72}\nSUMMARY\n{'═' * 72}")
    for f in results:
        print(f"  {f['ticket_number']:<11} HITL: {', '.join(f.get('hitl_triggers') or ['-']):<26}"
              f" {f['final_status']}")
    print(f"\n  Audit trail file: {AUDIT.log_file}  ({len(AUDIT.entries)} entries this run)")