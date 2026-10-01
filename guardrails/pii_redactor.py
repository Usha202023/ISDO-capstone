"""
ISDO Lab C9 — PII Redaction Middleware
Masks PII before any ticket data is sent to Claude.
Patterns covered: names (via spaCy NER + a contextual backstop), login-style
usernames, email addresses, employee IDs, IP addresses, and phone numbers.

Why both NER and a contextual regex for names: spaCy's en_core_web_sm has
real recall gaps on single first names and non-Western names ("Ankit" is
missed outright in testing) - a known limitation of the small model, not a
bug in how it's called here. A regex can't identify a name as language, but
it CAN catch "the token right after a role keyword like User/Contractor/
Employee/Requested by", which covers both that NER gap and login-style
usernames (jdoe, rshah123, priya.shah) that were never natural-language
names NER could ever be expected to catch in the first place.

This is deliberately biased toward over-redaction: an occasional false
positive (masking an ordinary word) is treated as the safe failure mode,
since under-redaction means real PII reaching the LLM. The USERNAME_STOPLIST
below exists to keep that bias from being noisy - extend it as real ticket
text surfaces new false positives.

Usage:
    from guardrails.pii_redactor import redact, restore

    clean_text, mapping = redact(raw_text)
    # ... send clean_text to Claude ...
    original_text = restore(claude_response, mapping)
"""

import os
import re
import json
from datetime import datetime

# Try to import spaCy — graceful fallback if not installed.
# Model is configurable (PII_SPACY_MODEL env var) so a better-recall model
# (e.g. en_core_web_trf) can be swapped in without touching this file -
# trf is NOT installed by default here since it pulls in torch and a much
# larger download; the contextual regex below is what actually closes the
# gap this lab hit, independent of which spaCy model is loaded.
SPACY_MODEL = os.environ.get("PII_SPACY_MODEL", "en_core_web_sm")
try:
    import spacy
    nlp = spacy.load(SPACY_MODEL)
    SPACY_AVAILABLE = True
except (ImportError, OSError):
    SPACY_AVAILABLE = False
    print(f"⚠  spaCy model '{SPACY_MODEL}' not available — using regex-only PII detection.")

# ── REGEX PATTERNS ────────────────────────────────────────────────────────────
# Most patterns mask their whole match (group 0). CONTEXT_PATTERNS below mask
# only a named 'value' group, so the anchoring keyword itself stays in the text.

PATTERNS = {
    "EMAIL":       r'\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b',
    "IP_ADDRESS":  r'\b(?:\d{1,3}\.){3}\d{1,3}\b',
    "EMPLOYEE_ID": r'\b(?:EMP|ZEN|EMP-|ZEN-)\d{3,6}\b',
    "PHONE":       r'\b(?:\+91[\-\s]?)?\d{10}\b|\b\d{3}[\-\s]\d{3}[\-\s]\d{4}\b',
    "TICKET_REF":  r'\b(?:INC|REQ|CHG)\d{7}\b',   # keep ticket refs — not PII
}

# Words that commonly follow USERNAME_WEAK's keywords in ordinary ITSM
# sentences - excluded so "user reports VPN failure" doesn't get masked.
# Extend this list as real ticket text surfaces new false positives; a
# word slipping through here is a minor miss, not the system's main defense.
USERNAME_STOPLIST = {
    "reports", "reported", "report", "cannot", "needs", "need", "requested",
    "unable", "experiencing", "facing", "has", "had", "have", "is", "was",
    "were", "will", "would", "locked", "disabled", "enabled", "account",
    "login", "access", "vpn", "email", "password", "ticket", "id", "reset",
    "request", "issue", "error", "failure", "problem", "system", "application",
    "service", "device", "laptop", "desktop", "network", "server", "data",
    "the", "a", "an", "this", "that", "their", "his", "her", "and", "or",
    "but", "for", "from", "to", "with", "without", "new", "name",
    "after", "before", "since", "during", "when", "once", "upon", "while",
    "until", "then", "now", "still", "already", "again", "here", "there",
    "also", "so", "because", "due", "pending", "under", "over", "within",
    "review", "investigation", "created", "updated", "modified", "changed",
    "information", "details", "info", "number", "record", "type", "status",
    "level", "group", "team", "support", "desk", "department", "remains",
    "continues", "keeps", "attempts", "attempt", "times", "minutes", "hours",
    "emp-id", "empid", "emp", "manager", "supervisor", "lead", "owner",
}

# Strong anchors: the next token is masked regardless of its shape - these
# keywords are specific enough that false positives are rare.
# Weak anchors: the next token is masked only if it's not in the stoplist -
# these keywords (user, account...) show up constantly in ordinary sentences.
# The value group never consumes a trailing '.' as sentence punctuation: a
# dot is only included when followed by more word characters (e.g. the
# internal dot in "p.makwana" or "priya.shah"), never a sentence-ending one.
_VALUE = r'(?P<value>[A-Za-z][\w\-]{0,18}(?:\.[A-Za-z0-9\-]{1,18}){0,3})'

CONTEXT_PATTERNS = [
    # One tier, always stoplist-checked. Originally split into a "strong"
    # tier (keyword alone = always mask) and a "weak" one (stoplist-checked)
    # on the idea that keywords like "username:" are unambiguous - but
    # "login" and "AD account" turned out just as likely to appear in an
    # ordinary sentence with no identifier after them ("login attempts",
    # "AD account is locked") as "user" or "account" are. Checking the
    # stoplist unconditionally is simpler and doesn't cost a real username
    # unless that username happens to BE a common English word.
    ("USER_ID", r'\b(?:username|login(?:\s*id)?|a(?:d|ctive directory)\s*account(?:\s*id)?|'
                r'user|account|contractor|employee|requested\s+by|reported\s+by|assigned\s+to)'
                r'\b\s*[:\-]?\s*' + _VALUE),
]

# ── AUDIT LOGGER ──────────────────────────────────────────────────────────────

audit_log = []

def _audit(action, detail):
    entry = {
        "timestamp": datetime.now().isoformat(),
        "module": "PIIRedactor",
        "action": action,
        "detail": detail
    }
    audit_log.append(entry)
    return entry

# ── REDACTION FUNCTION ────────────────────────────────────────────────────────

def redact(text: str) -> tuple[str, dict]:
    """
    Redact PII from text. Returns:
      - clean_text: text with PII replaced by tokens like [EMAIL_1], [NAME_1]
      - mapping: dict to restore original values later

    Example:
      clean, m = redact("Contact john.doe@corp.com or call 9876543210")
      # clean  = "Contact [EMAIL_1] or call [PHONE_1]"
      # m      = {"[EMAIL_1]": "john.doe@corp.com", "[PHONE_1]": "9876543210"}
    """
    mapping = {}
    counters = {}
    clean = text

    # Step 1: Named Entity Recognition (spaCy) — catches PERSON names
    if SPACY_AVAILABLE:
        doc = nlp(text)
        for ent in doc.ents:
            # Guard against a known spaCy false-positive: short ALL-CAPS acronyms
            # (PII, SLA, KB, VPN...) occasionally get tagged PERSON. Real names
            # in ticket text are essentially never written fully uppercase, so
            # this is a safe filter, not a real name being skipped.
            if ent.label_ == "PERSON" and not ent.text.isupper() and ent.text not in mapping.values():
                counters["NAME"] = counters.get("NAME", 0) + 1
                token = f"[NAME_{counters['NAME']}]"
                mapping[token] = ent.text
                clean = clean.replace(ent.text, token)

    # Step 2: plain regex patterns (mask the whole match). These run BEFORE
    # the contextual backstop below so EMAIL/EMPLOYEE_ID/etc. claim their
    # full, well-defined span first - otherwise a weak anchor like
    # "Contractor <email>" or "Employee <emp-id>" would grab part of that
    # span for itself before the specific pattern gets a turn at it.
    for label, pattern in PATTERNS.items():
        if label == "TICKET_REF":
            continue  # Preserve ticket numbers — not PII
        for match in re.finditer(pattern, clean, re.IGNORECASE):
            matched = match.group(0)
            # Skip if already replaced
            if matched.startswith("[") and matched.endswith("]"):
                continue
            counters[label] = counters.get(label, 0) + 1
            token = f"[{label}_{counters[label]}]"
            if token not in mapping:
                mapping[token] = matched
            clean = clean.replace(matched, token, 1)

    # Step 3: contextual username/name backstop - runs last, on whatever
    # text Steps 1-2 left untouched. The value capture requires its first
    # character to be a letter, so it can never re-grab an already-masked
    # [TOKEN] (which starts with '[').
    for label, pattern in CONTEXT_PATTERNS:
        # Two passes: first scan left-to-right so counters/tokens number in
        # reading order, THEN splice right-to-left so each splice's indices
        # stay valid against the ones still pending (splicing left-to-right
        # while re-slicing `clean` would shift every later match's position).
        pending = []
        for match in re.finditer(pattern, clean, re.IGNORECASE):
            value = match.group("value")
            if value.lower() in USERNAME_STOPLIST:
                continue
            if value in mapping.values():
                continue   # already has a token from an earlier match
            counters[label] = counters.get(label, 0) + 1
            token = f"[{label}_{counters[label]}]"
            mapping[token] = value
            pending.append((*match.span("value"), token))

        for start, end, token in reversed(pending):
            # Replace only the captured value, not the whole anchored match,
            # so the keyword ("username", "user", ...) stays in the text.
            clean = clean[:start] + token + clean[end:]

    pii_count = len(mapping)
    if pii_count > 0:
        _audit("redact", f"{pii_count} PII item(s) masked: {list(mapping.keys())}")
    else:
        _audit("redact", "No PII detected")

    return clean, mapping

def restore(text: str, mapping: dict) -> str:
    """Restore PII tokens back to original values (for system-of-record logging only)."""
    restored = text
    for token, original in mapping.items():
        restored = restored.replace(token, original)
    _audit("restore", f"{len(mapping)} PII item(s) restored")
    return restored

def get_audit_log() -> list:
    """Return all PII redaction audit entries."""
    return audit_log

# ── AUDIT TRAIL LOGGER ────────────────────────────────────────────────────────

class AuditLogger:
    """Logs every agent action with timestamp, agent name, tool, rationale, approval."""

    def __init__(self, log_file: str = "logs/audit_trail.jsonl"):
        import os
        os.makedirs(os.path.dirname(log_file), exist_ok=True)
        self.log_file = log_file
        self.entries = []

    def log(self, agent: str, action: str, ticket_number: str = "",
            tool: str = "", rationale: str = "", approval_status: str = "N/A"):
        entry = {
            "timestamp": datetime.now().isoformat(),
            "agent": agent,
            "action": action,
            "ticket_number": ticket_number,
            "tool": tool,
            "rationale": rationale[:200] if rationale else "",
            "approval_status": approval_status
        }
        self.entries.append(entry)

        # Append to JSONL file
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")

        print(f"  [AUDIT] {agent} | {action} | {ticket_number} | {approval_status}")
        return entry

    def print_trail(self):
        print(f"\n{'='*55}")
        print(f"FULL AUDIT TRAIL ({len(self.entries)} entries)")
        print(f"{'='*55}")
        for e in self.entries:
            print(f"  {e['timestamp'][:19]}  {e['agent']:<22} {e['action']:<20} {e['approval_status']}")

# ── DEMO ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 55)
    print("PII REDACTION DEMO")
    print("=" * 55)

    sample_tickets = [
        "User John Smith (emp ID ZEN-9823) reports VPN failure. Contact: john.smith@zensar.com or +91-9876543210.",
        "Contractor sarah.jones@client.com needs access to REQ-1002. IP: 192.168.1.45.",
        "Password reset for Michael D'Souza. Employee EMP-00142. No PII in this part.",
        "VPN not connecting after password change. Error: authentication failed. Ticket INC0001001.",
    ]

    for i, ticket in enumerate(sample_tickets, 1):
        print(f"\n--- Ticket {i} ---")
        print(f"Original : {ticket}")
        clean, mapping = redact(ticket)
        print(f"Redacted : {clean}")
        if mapping:
            print(f"Mapping  : {mapping}")

    print("\n" + "=" * 55)
    print("AUDIT TRAIL DEMO")
    print("=" * 55)

    logger = AuditLogger("logs/demo_audit.jsonl")
    logger.log("TriageAgent", "classify_ticket", "INC0001001", "classify_ticket",
               "Network/P2 — VPN failure after password change", "Auto")
    logger.log("ResolutionAgent", "search_kb", "INC0001001", "search_kb",
               "KB article found: vpn_troubleshooting.md (85% confidence)", "Auto")
    logger.log("SLAAgent", "get_sla_status", "INC0001001", "get_sla_status",
               "SLA AT_RISK — 210 min remaining of 240 min total", "Auto")
    logger.log("HITLGate", "approval_request", "INC0001002", "",
               "P1 escalation requires human approval", "PENDING")
    logger.log("HITLGate", "approval_decision", "INC0001002", "",
               "Human operator approved P1 escalation", "APPROVED")
    logger.log("CommunicationAgent", "post_comment", "INC0001001", "post_comment",
               "Resolution sent to user — auto-resolved L1 ticket", "Auto")

    logger.print_trail()
    print(f"\nAudit log saved to: logs/demo_audit.jsonl")