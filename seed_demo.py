"""Seeds deterministic demo data: Grandma with 3 meds and 6 days of history.
Run once before filming:  python seed_demo.py   (wipes careloop.db first)
"""
import os
import sqlite3
from datetime import datetime, timedelta

import server

if os.path.exists(server.DB_PATH):
    os.remove(server.DB_PATH)

PERSON = "Grandma"
MEDS = [("Metformin", "500 mg", "08:00"), ("Lisinopril", "10 mg", "09:00"), ("Vitamin D", "1000 IU", "20:00")]
# days_ago -> meds skipped that day (to make the weekly summary interesting)
SKIPPED = {1: {"Vitamin D"}, 3: {"Lisinopril"}, 4: {"Lisinopril", "Vitamin D"}}
LAG = {"Vitamin D": 75}
SUPPLY = {"Metformin": 24, "Lisinopril": 4, "Vitamin D": 40}  # pills on hand; Lisinopril is running low
NOTES = {2: "Feeling good, walked in the garden", 5: "A bit tired today"}

with server.db() as conn:
    ids = {}
    for name, dose, t in MEDS:
        cur = conn.execute("INSERT INTO medications (person,name,dose,time_of_day) VALUES (?,?,?,?)", (PERSON, name, dose, t))
        ids[name] = (cur.lastrowid, t)
        conn.execute("UPDATE medications SET supply=? WHERE id=?", (SUPPLY[name], cur.lastrowid))
    today = datetime.now().replace(second=0, microsecond=0)
    for ago in range(1, 7):
        day = today - timedelta(days=ago)
        for name, (mid, t) in ids.items():
            if name in SKIPPED.get(ago, set()):
                continue
            h, m = map(int, t.split(":"))
            # Vitamin D: she reliably takes it ~75 min after the scheduled time (after her TV show), which is
            # the habit CareLoop learns. The others are taken ~5 minutes after the scheduled time.
            lag = LAG[name] + (ago * 7) % 11 - 5 if name in LAG else 5
            ts = (day.replace(hour=h, minute=m) + timedelta(minutes=lag)).isoformat(timespec="seconds")
            conn.execute("INSERT INTO check_ins (person,kind,medication_id,note,ts) VALUES (?,?,?,?,?)",
                         (PERSON, "medication_taken", mid, "", ts))
        if ago in NOTES:
            conn.execute("INSERT INTO check_ins (person,kind,note,ts) VALUES (?,?,?,?)",
                         (PERSON, "wellbeing", NOTES[ago], day.replace(hour=12).isoformat(timespec="seconds")))
    for name, role in (("Sarah", "primary"), ("Dad", "primary"), ("Raj", "backup")):
        conn.execute("INSERT INTO family (person,name,role) VALUES (?,?,?)", (PERSON, name, role))
print("Seeded Grandma: Metformin 08:00, Lisinopril 09:00, Vitamin D 20:00 + 6 days of history; family: Sarah, Dad (primary), Raj (backup).")
