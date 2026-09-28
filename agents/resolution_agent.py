"""
ISDO Lab C4 - Resolution / KB Agent
Searches ChromaDB ('isdo_kb') for matching KB articles and drafts a resolution.

Confidence is derived from the KB match score (1 - distance), and the auto_resolve
decision is enforced in code, not left to the model:
    HIGH   score > 0.60  -> auto_resolve only if priority is P2/P3/P4 (never P1)
    MEDIUM score > 0.35  -> human reviews before sending
    LOW    score <= 0.35 -> escalate to L2

Run from the project root:  python agents/resolution_agent.py
"""

import json
import os
import sys
from pathlib import Path

import anthropic
import chromadb
from dotenv import load_dotenv

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")   # safe printing of arrows/warning icons on Windows

load_dotenv()
client = anthropic.Anthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-opus-5-5")   # override via .env if needed
ROOT = Path(__file__).resolve().parent.parent
KB_DIR = ROOT / "data" / "kb"

HIGH_THRESHOLD = 0.60
MEDIUM_THRESHOLD = 0.35
AUTO_RESOLVE_PRIORITIES = {"P2", "P3", "P4"}   # P1 always needs a human
MAX_ROUNDS = 4                                 # hard cap on model round-trips per ticket

# -- KB (same chunking as Lab C1 kb_setup.py) ---------------------------------

def chunk_article(text: str, filename: str) -> list[dict]:
    """Split a markdown article at '## ' headings. Each section = one chunk."""
    chunks, lines, heading = [], [], "Introduction"
    for line in text.split("\n"):
        if line.startswith("## ") and lines:
            chunks.append({"content": "\n".join(lines).strip(), "heading": heading, "filename": filename})
            lines, heading = [], line[3:].strip()
        lines.append(line)
    if lines:
        chunks.append({"content": "\n".join(lines).strip(), "heading": heading, "filename": filename})
    return chunks


def build_kb():
    """Load KB articles into the 'isdo_kb' ChromaDB collection."""
    db = chromadb.Client()
    try:
        db.delete_collection("isdo_kb")
    except Exception:
        pass
    # cosine space: distance = 1 - cosine similarity, so score = 1 - distance is a true 0-1 similarity
    kb = db.create_collection("isdo_kb", metadata={"hnsw:space": "cosine"})

    docs, ids, metas = [], [], []
    md_files = sorted(KB_DIR.glob("*.md"))
    if not md_files:
        raise FileNotFoundError(f"No .md files found in {KB_DIR}")
    for md_file in md_files:
        text = md_file.read_text(encoding="utf-8")
        for chunk in chunk_article(text, md_file.name):
            docs.append(chunk["content"])
            ids.append(f"kb_{len(ids)}")
            metas.append({"filename": md_file.name, "heading": chunk["heading"]})
    kb.add(documents=docs, ids=ids, metadatas=metas)
    print(f"KB loaded: {len(docs)} chunks from {len(md_files)} articles")
    return kb


KB = build_kb()

# -- Tool definitions ---------------------------------------------------------

tools = [
    {
        "name": "search_kb",
        "description": "Search the knowledge base for articles matching the ticket. Returns the top 2 articles with confidence scores (1 - distance).",
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "The ticket's short description and symptoms"}
            },
            "required": ["query"],
        },
    },
    {
        "name": "draft_resolution",
        "description": "Draft the resolution for the ticket using steps from the matched KB article.",
        "input_schema": {
            "type": "object",
            "properties": {
                "ticket_number": {"type": "string"},
                "resolution_text": {
                    "type": "string",
                    "description": "3-4 numbered steps taken from the KB article, written for the requester",
                },
                "auto_resolve": {"type": "boolean", "description": "True only if this is an L1 issue fully covered by the KB article"},
                "confidence": {"type": "string", "enum": ["HIGH", "MEDIUM", "LOW"]},
                "kb_article_used": {"type": "string", "description": "File name of the KB article used, or 'None'"},
            },
            "required": ["ticket_number", "resolution_text", "auto_resolve", "confidence", "kb_article_used"],
        },
    },
]

# -- Tool implementation ------------------------------------------------------

def search_kb(query: str, state: dict) -> dict:
    """Search all chunks, then return the top 2 ARTICLES (best chunk per file) with full text."""
    raw = KB.query(query_texts=[query], n_results=10)
    best = {}
    for meta, dist in zip(raw["metadatas"][0], raw["distances"][0]):
        fname = meta["filename"]
        if fname not in best or dist < best[fname]:
            best[fname] = dist

    articles = []
    for fname, dist in sorted(best.items(), key=lambda kv: kv[1])[:2]:
        score = max(0.0, 1 - dist)
        articles.append({
            "article": fname,
            "confidence_score": round(score, 2),
            "content": (KB_DIR / fname).read_text(encoding="utf-8"),
            "_raw": score,
        })

    state["top_score"] = articles[0]["_raw"] if articles else 0.0
    return {"query": query, "articles": [{k: v for k, v in a.items() if k != "_raw"} for a in articles]}


def confidence_level(score: float) -> str:
    if score > HIGH_THRESHOLD:
        return "HIGH"
    if score > MEDIUM_THRESHOLD:
        return "MEDIUM"
    return "LOW"


def apply_guardrail(draft: dict, top_score: float, priority: str) -> dict:
    """Code-enforced confidence + auto_resolve. The model's values are overridden if they disagree."""
    level = confidence_level(top_score)
    auto = level == "HIGH" and priority in AUTO_RESOLVE_PRIORITIES
    result = dict(draft)
    result["overridden"] = (draft.get("confidence") != level) or (draft.get("auto_resolve") != auto)
    result.update(confidence=level, auto_resolve=auto, top_score=round(top_score, 2),
                  priority=priority, hitl_required=not auto)
    return result


def hitl_reason(result: dict) -> str:
    if result["confidence"] == "HIGH":
        return f"{result['priority']} ticket - human approval required even with a strong KB match."
    if result["confidence"] == "MEDIUM":
        return "Partial KB match - human review required before sending."
    return "No clear KB match - escalate to L2."

# -- Resolution agent ---------------------------------------------------------

SYSTEM_PROMPT = f"""You are the ISDO Resolution Agent for Zensar's IT Service Desk.

For each ticket:
1. Call search_kb ONCE, using the ticket's short description and details as the query.
   Do not rephrase or retry. A weak match is a valid outcome, not a reason to search again.
2. Call draft_resolution with 3-4 numbered steps taken from the best matching KB article.

Confidence (based on the top article's confidence_score):
- HIGH   : score > {HIGH_THRESHOLD}
- MEDIUM : score > {MEDIUM_THRESHOLD}
- LOW    : score <= {MEDIUM_THRESHOLD}
Set auto_resolve=True only for HIGH confidence on P2/P3/P4 tickets. Never for P1.
The system re-checks confidence and auto_resolve against the score and will override mistakes.

Use exact steps from the KB article. If confidence is LOW, do not invent steps: say the ticket
needs escalation to L2 and set kb_article_used to 'None'."""


def resolve_ticket(ticket_number, short_description, description, category, priority="P3") -> dict:
    """Resolve one ticket. Returns the final resolution dict (used by the C6 orchestrator)."""
    print(f"\n{'=' * 55}")
    print(f"Resolving: {ticket_number} | Category: {category} | Priority: {priority}")
    print("=" * 55)
    print(f"Issue: {short_description}")

    state = {"top_score": None}
    messages = [{
        "role": "user",
        "content": (f"Find a resolution for this ticket:\n\nTicket: {ticket_number}\nCategory: {category}\n"
                    f"Priority: {priority}\nSummary: {short_description}\nDetails: {description}"),
    }]

    for _ in range(MAX_ROUNDS):
        response = client.messages.create(
            model=MODEL,
            max_tokens=1000,
            output_config={"effort": "low"},
            system=SYSTEM_PROMPT,
            tools=tools,
            messages=messages,
        )

        if response.stop_reason != "tool_use":
            for block in response.content:
                if hasattr(block, "text"):
                    print(block.text)
            break

        messages.append({"role": "assistant", "content": response.content})
        tool_results = []
        final = None

        for block in response.content:
            if block.type != "tool_use":
                continue

            if block.name == "search_kb":
                result = search_kb(block.input["query"], state)
                print(f"  -> KB search: '{block.input['query']}'")
                for art in result["articles"]:
                    print(f"     [{art['confidence_score']:.0%}] {art['article']}")

            elif block.name == "draft_resolution":
                final = apply_guardrail(block.input, state["top_score"] or 0.0, priority)
                print(f"\n  -> Confidence: {final['confidence']} ({final['top_score']:.0%})  |  Auto-resolve: {final['auto_resolve']}")
                print(f"  -> KB Article: {final.get('kb_article_used')}")
                if final["overridden"]:
                    print("  -> Note: model's confidence/auto_resolve was corrected by the score guardrail.")
                print("\n  RESOLUTION DRAFT:")
                for line in str(final["resolution_text"]).splitlines():
                    print(f"  {line}")
                if final["hitl_required"]:
                    print(f"\n  \u26a0\ufe0f  HITL FLAG: {hitl_reason(final)}")
                result = final
            else:
                result = {"error": "Unknown tool"}

            tool_results.append({"type": "tool_result", "tool_use_id": block.id, "content": json.dumps(result)})

        if final is not None:
            return final          # decision made - no need for another model round-trip
        messages.append({"role": "user", "content": tool_results})

    # No decision reached: fail safe to LOW / human review
    fallback = {"ticket_number": ticket_number, "resolution_text": "No resolution drafted - escalate to L2.",
                "confidence": "LOW", "auto_resolve": False, "kb_article_used": "None",
                "top_score": round(state["top_score"] or 0.0, 2), "priority": priority,
                "hitl_required": True, "overridden": False}
    print(f"\n  \u26a0\ufe0f  HITL FLAG: Agent did not reach a decision - escalate to L2.")
    return fallback

# -- Run on sample tickets ----------------------------------------------------

if __name__ == "__main__":
    test_tickets = [
        ("INC0001001", "VPN not connecting after password change",
         "User reports VPN client fails to connect after AD password was reset.", "Network", "P2"),
        ("INC0001006", "Password reset request",
         "User locked out of AD account after 5 failed attempts.", "Access", "P2"),
        ("INC0001002", "Cannot access ERP system - login error",
         "Multiple Finance users unable to login to SAP. Error: DBCON_FAIL.", "Application", "P1"),
        # Step 5 - no KB coverage: expect LOW + HITL flag
        ("TEST-004", "Cisco Webex not launching on Mac M2",
         "Cisco Webex not launching on Mac M2.", "Software", "P3"),
    ]

    for number, short_desc, desc, cat, prio in test_tickets:
        resolve_ticket(number, short_desc, desc, cat, prio)