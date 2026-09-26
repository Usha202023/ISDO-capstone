"""
ISDO Lab C2 - Mock ServiceNow Table API (Flask), port 5001.

  GET   /api/now/table/incident              all incidents (?category= ?priority= ?state= ?assignment_group=)
  GET   /api/now/table/incident/<number>     one incident
  PATCH /api/now/table/incident/<number>     update fields in memory (e.g. {"state": "Escalated"})
  GET   /health                              service status

Run from the lab root:  python mcp_server/snow_shim.py
"""
import csv
from pathlib import Path

from flask import Flask, jsonify, request

PORT = 5001
DATA_FILE = Path(__file__).resolve().parent.parent / "data" / "incidents.csv"
FILTERS = ["category", "priority", "state", "assignment_group"]
READ_ONLY = {"number"}  # the record key must never change via PATCH

app = Flask(__name__)


def load_incidents() -> dict:
    """Load incidents.csv into an in-memory dict keyed by incident number."""
    if not DATA_FILE.exists():
        print(f"WARNING: {DATA_FILE} not found - starting empty")
        return {}
    incidents = {}
    with DATA_FILE.open(newline="", encoding="utf-8-sig") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            if None in row:  # more values than headers -> unquoted comma in a field
                print(f"WARNING: line {line_no} ({row.get('number')}) has extra columns "
                      f"- quote fields that contain commas. Row skipped.")
                continue
            incidents[row["number"]] = {k: (v or "").strip() for k, v in row.items()}
    return incidents


INCIDENTS = load_incidents()  # simulates the ServiceNow DB for this session


@app.get("/api/now/table/incident")
def list_incidents():
    results = list(INCIDENTS.values())
    for key in FILTERS:
        value = request.args.get(key)
        if value:
            results = [r for r in results if r.get(key, "").lower() == value.lower()]
    return jsonify({"result": results, "total": len(results)})


@app.get("/api/now/table/incident/<number>")
def get_incident(number):
    incident = INCIDENTS.get(number)
    if incident is None:
        return jsonify({"error": f"Incident {number} not found"}), 404
    return jsonify({"result": incident})


@app.patch("/api/now/table/incident/<number>")
def update_incident(number):
    if number not in INCIDENTS:
        return jsonify({"error": f"Incident {number} not found"}), 404
    updates = request.get_json(silent=True)
    if not isinstance(updates, dict) or not updates:
        return jsonify({"error": "Body must be a non-empty JSON object"}), 400
    blocked = READ_ONLY & updates.keys()
    if blocked:
        return jsonify({"error": f"Read-only field(s): {sorted(blocked)}"}), 400
    INCIDENTS[number].update({k: str(v) for k, v in updates.items()})
    print(f"[ServiceNow Mock] Updated {number}: {updates}")
    return jsonify({"result": INCIDENTS[number], "message": "Updated successfully"})


@app.get("/health")
def health():
    return jsonify({"status": "ok", "service": "ServiceNow Mock",
                    "incidents_loaded": len(INCIDENTS)})


if __name__ == "__main__":
    print(f"ServiceNow Mock API starting on http://localhost:{PORT}")
    print(f"Loaded {len(INCIDENTS)} incidents from {DATA_FILE}")
    # debug/reloader off: a reload would silently wipe in-memory PATCH updates
    app.run(host="127.0.0.1", port=PORT, debug=False)