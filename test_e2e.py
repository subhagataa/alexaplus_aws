"""End-to-end test of the CareLoop demo flow through the agent API (offline mode, no AWS needed).

Starts the MCP server and the agent on test ports with a throwaway database, then walks the demo:
missed dose -> family alerted -> one person claims it -> follow-up stands down -> unclaimed one escalates.

    python test_e2e.py
"""
import json
import os
import urllib.error
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(tempfile.mkdtemp(), "e2e.db")
ENV = {**os.environ, "CARELOOP_DB": DB, "CARELOOP_PORT": "18000", "CARELOOP_AGENT_PORT": "18001",
       "CARELOOP_MCP_URL": "http://127.0.0.1:18000/mcp", "CARELOOP_AGENT_MODE": "offline"}
BASE = "http://127.0.0.1:18001"


def post(path, body=None):
    req = urllib.request.Request(BASE + path, data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def tool(name, **args):
    return post("/api/tool", {"name": name, "args": args})["result"]


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        raise SystemExit(1)


procs = []
try:
    subprocess.run([sys.executable, "seed_demo.py"], cwd=HERE, env=ENV, check=True, stdout=subprocess.DEVNULL)
    for script in ("server.py", "agent.py"):
        procs.append(subprocess.Popen([sys.executable, script], cwd=HERE, env=ENV, stdout=open(f"/tmp/e2e_{script}.log", "w"), stderr=subprocess.STDOUT))
        time.sleep(3)

    tool("reset_demo_day", person="Grandma")
    tool("set_demo_time", time_of_day="09:30")
    d = post("/api/tick")
    names = [s["tool"] for s in d["steps"]]
    check("tick flags missed doses and schedules follow-ups", "flag_missed_dose" in names and "schedule_reminder" in names)
    alerts = tool("get_open_alerts", person="Grandma")
    check("two open alerts (Metformin, Lisinopril)", {a["medication"] for a in alerts} == {"Metformin", "Lisinopril"})
    feed = tool("get_family_feed", person="Grandma", limit=20)
    check("Sarah, Dad and Raj were all notified", {f["to"] for f in feed} == {"Sarah", "Dad", "Raj"})
    props = tool("list_proposals", person="Grandma")
    check("tick also suggests a Lisinopril refill and a Vitamin D time change (proposals only)",
          {(p["kind"], p["medication"]) for p in props} >= {("refill", "Lisinopril"), ("reschedule", "Vitamin D")})
    check("proposals do not change the schedule by themselves",
          next(m for m in tool("get_daily_status", person="Grandma")["medications"] if m["medication"] == "Vitamin D")["time"] == "20:00")
    again = post("/api/tick")
    check("second tick does not re-alert (idempotent)", "flag_missed_dose" not in [s["tool"] for s in again["steps"]] or
          not next(s for s in again["steps"] if s["tool"] == "flag_missed_dose")["result"]["new_alerts"])

    check("Raj claims Metformin", "claimed" in tool("claim_alert", person="Grandma", medication="Metformin", claimed_by="Raj"))
    check("second claim is refused", "already claimed" in tool("claim_alert", person="Grandma", medication="Metformin", claimed_by="Sarah"))
    check("stranger cannot claim", "not in" in tool("claim_alert", person="Grandma", medication="Lisinopril", claimed_by="Mallory"))

    tool("set_demo_time", time_of_day="09:46")
    d = post("/api/tick")
    esc = [s for s in d["steps"] if s["tool"] == "escalate_alert"]
    check("only the unclaimed alert escalates", len(esc) == 1 and esc[0]["args"]["medication"] == "Lisinopril" and esc[0]["result"]["escalated"])
    check("claimed alert is left alone", not any(s["args"].get("medication") == "Metformin" for s in esc))

    tool("log_check_in", person="Grandma", kind="medication_taken", medication="Lisinopril")
    check("taking the dose clears its alert", {a["medication"] for a in tool("get_open_alerts", person="Grandma")} == {"Metformin"})
    try:
        post("/api/tool", {"name": "remove_medication", "args": {}})
        check("remove_medication blocked", False)
    except urllib.error.HTTPError as e:
        check("remove_medication blocked from UI (403)", e.code == 403)
    print("\nAll end-to-end checks passed.")
finally:
    for p in procs:
        p.terminate()
