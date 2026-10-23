"""Time-change flow: proposal-only until approved; No leaves the schedule alone. Also the remove flow.
    python test_timechange.py
"""
import os, sys, tempfile, subprocess
HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["CARELOOP_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
sys.path.insert(0, HERE)
subprocess.run([sys.executable, "seed_demo.py"], cwd=HERE, env=os.environ, check=True, stdout=subprocess.DEVNULL)
import server as s  # noqa: E402
P = "Grandma"

def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond: raise SystemExit(1)

def t(name): return next(m["time"] for m in s.list_medications(P) if m["medication"] == name)

check("starts at 08:00", t("Metformin") == "08:00")
p = s.propose_time_change(P, "metformin", "10:30")
check("proposal only, schedule unchanged", p["kind"] == "time_change" and t("Metformin") == "08:00")
check("works for shifts over 2 hours", s.propose_time_change(P, "Metformin", "21:00")["id"] != p["id"] and t("Metformin") == "08:00")
r = s.reject_proposal(s.propose_time_change(P, "Metformin", "10:30")["id"], "Sarah")
check("No keeps the old time", r["ok"] and t("Metformin") == "08:00")
p = s.propose_time_change(P, "Metformin", "10:30")
check("same time rejected", "already" in s.propose_time_change(P, "Metformin", "08:00")["error"])
check("unknown med rejected", "error" in s.propose_time_change(P, "Aspirin", "10:00"))
check("bad time rejected", "error" in s.propose_time_change(P, "Metformin", "25:99"))
check("non-family cannot approve", not s.approve_proposal(p["id"], "Stranger")["ok"])
r = s.approve_proposal(p["id"], "Sarah")
check("Yes applies the new time", r["ok"] and t("Metformin") == "10:30")
s.reset_demo_day(P)
check("demo reset restores the time", t("Metformin") == "08:00")

rp = s.propose_remove_medication(P, "Vitamin D")
check("remove is proposal only", "Vitamin D" in [m["medication"] for m in s.list_medications(P)])
s.approve_proposal(rp["id"], "Sarah")
check("remove applied after Yes", "Vitamin D" not in [m["medication"] for m in s.list_medications(P)])
print("all good")