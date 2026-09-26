"""
ISDO Lab C2 - Mock Jira Service Management REST API (Flask), port 5002.

  GET /rest/agile/1.0/board/requests   all requests (?request_type= ?priority= ?assignee= ?status=)
  GET /rest/api/2/issue/<key>          one request, Jira-style nested "fields" object
  PUT /rest/api/2/issue/<key>          update in memory, e.g. {"fields": {"status": "Done"}}
  GET /health                          service status

Run from the lab root:  python mcp_server/jira_shim.py
"""
import csv
from pathlib import Path

from flask import Flask, jsonify, request

PORT = 5002
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "requests.csv"
FILTERS = ["request_type", "priority", "assignee", "status"]

app = Flask(__name__)


def load_requests() -> dict:
    """Load requests.csv into an in-memory dict keyed by request key."""
    if not DATA_FILE.exists():
        print(f"WARNING: {DATA_FILE} not found - starting empty")
        return {}
    data = {}
    with DATA_FILE.open(newline="", encoding="utf-8-sig") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            if None in row:  # more values than headers -> unquoted comma in a field
                print(f"WARNING: line {line_no} ({row.get('key')}) has extra columns - skipped.")
                continue
            data[row["key"]] = {k: (v or "").strip() for k, v in row.items()}
    return data


REQUESTS = load_requests()  # simulates the Jira DB for this session


def to_jira(req: dict) -> dict:
    """Convert a flat CSV row into Jira's nested issue shape."""
    return {
        "key": req["key"],
        "fields": {
            "summary": req.get("summary"),
            "issuetype": {"name": req.get("request_type")},
            "priority": {"name": req.get("priority")},
            "status": {"name": req.get("status")},
            "assignee": {"displayName": req.get("assignee")},
            "customfield_sla": req.get("sla"),
        },
    }


@app.get("/rest/agile/1.0/board/requests")
def list_requests():
    results = list(REQUESTS.values())
    for key in FILTERS:
        value = request.args.get(key)  # Flask already decodes "Access+Grant" -> "Access Grant"
        if value:
            results = [r for r in results if r.get(key, "").lower() == value.lower()]
    return jsonify({"issues": results, "total": len(results)})


@app.get("/rest/api/2/issue/<key>")
def get_request(key):
    req = REQUESTS.get(key)
    if req is None:
        return jsonify({"errorMessages": [f"Issue {key} does not exist"]}), 404
    return jsonify(to_jira(req))


@app.put("/rest/api/2/issue/<key>")
def update_request(key):
    if key not in REQUESTS:
        return jsonify({"errorMessages": [f"Issue {key} does not exist"]}), 404
    body = request.get_json(silent=True)
    fields = body.get("fields", body) if isinstance(body, dict) else None
    if not isinstance(fields, dict) or not fields:
        return jsonify({"errorMessages": ["Body must be a non-empty JSON object"]}), 400
    # Accept Jira-style nested values too, e.g. {"status": {"name": "Done"}}
    flat = {k: str(v.get("name", v.get("displayName", "")) if isinstance(v, dict) else v)
            for k, v in fields.items() if k != "key"}
    REQUESTS[key].update(flat)
    print(f"[Jira Mock] Updated {key}: {flat}")
    return jsonify({"key": key, "message": "Updated successfully"})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "Jira Mock", "requests_loaded": len(REQUESTS)})


if __name__ == "__main__":
    print(f"Jira Mock API starting on http://localhost:{PORT}")
    print(f"Loaded {len(REQUESTS)} requests from {DATA_FILE}")
    app.run(host="127.0.0.1", port=PORT, debug=False)