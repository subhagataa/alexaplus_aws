"""CareLoop MCP server: caretaking tools for a simulated Alexa+ assistant.

Served over Streamable HTTP at http://127.0.0.1:8000/mcp
State lives in SQLite (careloop.db) so it persists across sessions.
"""
import json
import os
import sqlite3
import statistics
from datetime import datetime, timedelta

from mcp.server.mcpserver import MCPServer

import notify

DB_PATH = os.environ.get("CARELOOP_DB", "careloop.db")

mcp = MCPServer(
    "CareLoop",
    instructions=(
        "CareLoop helps families look after an aging relative. Use these tools "
        "to record medication schedules, log check-ins, and report daily status."
    ),
)


def db() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS medications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            name TEXT NOT NULL,
            dose TEXT NOT NULL,
            time_of_day TEXT NOT NULL  -- HH:MM, 24h
        );
        CREATE TABLE IF NOT EXISTS check_ins (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            kind TEXT NOT NULL,        -- 'medication_taken' or 'wellbeing'
            medication_id INTEGER,
            note TEXT,
            ts TEXT NOT NULL           -- ISO timestamp
        );
        CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            medication_id INTEGER NOT NULL,
            date TEXT NOT NULL,        -- YYYY-MM-DD
            message TEXT NOT NULL,
            ts TEXT NOT NULL,
            claimed_by TEXT,
            escalated INTEGER NOT NULL DEFAULT 0,
            UNIQUE (medication_id, date)
        );
        CREATE TABLE IF NOT EXISTS family (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,      -- the loved one they look after
            name TEXT NOT NULL,
            role TEXT NOT NULL,        -- 'primary' or 'backup'
            UNIQUE (person, name)
        );
        CREATE TABLE IF NOT EXISTS notifications (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            recipient TEXT NOT NULL,
            message TEXT NOT NULL,
            ts TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS reminders (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            message TEXT NOT NULL,
            remind_at TEXT NOT NULL,   -- ISO timestamp
            delivered INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            person TEXT NOT NULL,
            kind TEXT NOT NULL,        -- 'reschedule', 'refill', 'new_med' or 'remove_med'
            medication TEXT NOT NULL,
            summary TEXT NOT NULL,
            caution TEXT,
            payload TEXT NOT NULL,     -- JSON
            status TEXT NOT NULL DEFAULT 'pending',   -- pending / approved / rejected
            decided_by TEXT,
            resolution TEXT,
            created TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS demo_clock (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            offset_seconds INTEGER NOT NULL,
            frozen_at TEXT             -- ISO timestamp; demo time stands still here
        );
        """
    )
    for table, col, ddl in (("alerts", "claimed_by", "TEXT"), ("alerts", "escalated", "INTEGER NOT NULL DEFAULT 0"),
                            ("demo_clock", "frozen_at", "TEXT"),
                            ("medications", "supply", "INTEGER"), ("notifications", "channel", "TEXT"),
                            ("notifications", "delivery", "TEXT")):
        try:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}")
        except sqlite3.OperationalError:
            pass  # column already exists
    return conn


def now() -> datetime:
    """Real time plus a demo offset (set via set_demo_time) so the clock can be fast-forwarded."""
    with db() as conn:
        row = conn.execute("SELECT offset_seconds, frozen_at FROM demo_clock WHERE id=1").fetchone()
    if row and row["frozen_at"]:
        return datetime.fromisoformat(row["frozen_at"])
    return datetime.now() + timedelta(seconds=row["offset_seconds"] if row else 0)


def _status_for(conn, m, today: str, current: str) -> str:
    taken = conn.execute(
        "SELECT 1 FROM check_ins WHERE kind='medication_taken' AND medication_id=? AND ts LIKE ?",
        (m["id"], today + "%"),
    ).fetchone()
    if taken:
        return "taken"
    return "missed" if m["time_of_day"] <= current else "due"


def _family(person: str) -> list:
    with db() as conn:
        return conn.execute(
            "SELECT name, role FROM family WHERE person=? ORDER BY id", (person,)
        ).fetchall()


def _notify_family(conn, person: str, message: str, n: datetime, only=None, exclude=None) -> list:
    """Record a notification for each family member (the demo stand-in for push/SMS)."""
    sent = []
    for f in conn.execute("SELECT name FROM family WHERE person=? ORDER BY id", (person,)).fetchall():
        if only and f["name"] not in only:
            continue
        if exclude and f["name"] in exclude:
            continue
        channel, delivery = notify.send(f["name"], message)
        conn.execute(
            "INSERT INTO notifications (person, recipient, message, ts, channel, delivery) VALUES (?,?,?,?,?,?)",
            (person, f["name"], message, n.isoformat(timespec="seconds"), channel, delivery),
        )
        sent.append(f["name"])
    return sent


@mcp.tool()
def add_medication_schedule(person: str, medication: str, dose: str, time_of_day: str) -> str:
    """Add a daily medication to a person's schedule.

    Args:
        person: Who takes it, e.g. "Grandma".
        medication: Medication name, e.g. "Metformin".
        dose: Dose, e.g. "500 mg".
        time_of_day: 24h time as HH:MM, e.g. "08:30".
    """
    try:
        datetime.strptime(time_of_day, "%H:%M")
    except ValueError:
        return "time_of_day must be HH:MM in 24h format, e.g. 08:30."
    with db() as conn:
        existing = conn.execute(
            "SELECT id FROM medications WHERE person=? AND lower(name)=lower(?)",
            (person, medication),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE medications SET dose=?, time_of_day=? WHERE id=?",
                (dose, time_of_day, existing["id"]),
            )
            return f"Updated {medication} for {person}: {dose} daily at {time_of_day}."
        conn.execute(
            "INSERT INTO medications (person, name, dose, time_of_day) VALUES (?,?,?,?)",
            (person, medication, dose, time_of_day),
        )
    return f"Added {medication} {dose} for {person} daily at {time_of_day}."


@mcp.tool()
def log_check_in(person: str, kind: str, medication: str = "", note: str = "") -> str:
    """Record a check-in for a person.

    Args:
        person: Who is checking in.
        kind: "medication_taken" or "wellbeing".
        medication: Medication name (required when kind is "medication_taken").
        note: Optional free text, e.g. "feeling tired but ok".
    """
    if kind not in ("medication_taken", "wellbeing"):
        return 'kind must be "medication_taken" or "wellbeing".'
    med_id = None
    with db() as conn:
        if kind == "medication_taken":
            row = conn.execute(
                "SELECT id FROM medications WHERE person=? AND lower(name)=lower(?)",
                (person, medication),
            ).fetchone()
            if not row:
                return f"No medication named '{medication}' is scheduled for {person}."
            med_id = row["id"]
            today = now().strftime("%Y-%m-%d")
            if not conn.execute("SELECT 1 FROM check_ins WHERE kind='medication_taken' AND medication_id=? AND ts LIKE ?",
                                (med_id, today + "%")).fetchone():
                conn.execute("UPDATE medications SET supply=supply-1 WHERE id=? AND supply IS NOT NULL AND supply>0", (med_id,))
        conn.execute(
            "INSERT INTO check_ins (person, kind, medication_id, note, ts) VALUES (?,?,?,?,?)",
            (person, kind, med_id, note, now().isoformat(timespec="seconds")),
        )
    what = f"took {medication}" if kind == "medication_taken" else "checked in"
    return f"Logged: {person} {what}."


@mcp.tool()
def get_daily_status(person: str) -> dict:
    """Get today's medication status and check-ins for a person.

    Each medication is "taken", "due" (time not reached yet) or "missed"
    (time has passed and no dose was logged today).
    """
    today = now().strftime("%Y-%m-%d")
    current = now().strftime("%H:%M")
    meds = []
    with db() as conn:
        for m in conn.execute(
            "SELECT * FROM medications WHERE person=? ORDER BY time_of_day", (person,)
        ):
            status = _status_for(conn, m, today, current)
            meds.append(
                {"medication": m["name"], "dose": m["dose"], "time": m["time_of_day"], "status": status}
            )
        wellbeing = [
            {"time": r["ts"], "note": r["note"]}
            for r in conn.execute(
                "SELECT ts, note FROM check_ins WHERE person=? AND kind='wellbeing' AND ts LIKE ? ORDER BY ts",
                (person, today + "%"),
            )
        ]
    return {"person": person, "date": today, "current_time": current, "medications": meds, "wellbeing_check_ins": wellbeing}


@mcp.tool()
def list_medications(person: str) -> list[dict]:
    """List all scheduled medications for a person."""
    with db() as conn:
        return [
            {"medication": m["name"], "dose": m["dose"], "time": m["time_of_day"]}
            for m in conn.execute(
                "SELECT * FROM medications WHERE person=? ORDER BY time_of_day", (person,)
            )
        ]


@mcp.tool()
def remove_medication(person: str, medication: str) -> str:
    """Remove a medication from a person's schedule."""
    with db() as conn:
        cur = conn.execute(
            "DELETE FROM medications WHERE person=? AND lower(name)=lower(?)", (person, medication)
        )
    return f"Removed {medication} for {person}." if cur.rowcount else f"No medication '{medication}' found for {person}."


@mcp.tool()
def flag_missed_dose(person: str) -> dict:
    """Find today's missed doses for a person, record an alert for each new one,
    and return a ready-to-send message for the family caregiver."""
    n = now()
    today, current = n.strftime("%Y-%m-%d"), n.strftime("%H:%M")
    newly_flagged, still_missed = [], []
    with db() as conn:
        for m in conn.execute("SELECT * FROM medications WHERE person=? ORDER BY time_of_day", (person,)):
            if _status_for(conn, m, today, current) != "missed":
                continue
            msg = f"{person} has not taken {m['name']} {m['dose']} (was due at {m['time_of_day']})."
            try:
                conn.execute(
                    "INSERT INTO alerts (person, medication_id, date, message, ts) VALUES (?,?,?,?,?)",
                    (person, m["id"], today, msg, n.isoformat(timespec="seconds")),
                )
                newly_flagged.append(msg)
                _notify_family(conn, person, msg + " Reply 'I've got this' if you will call her.", n)
            except sqlite3.IntegrityError:
                still_missed.append(msg)  # already alerted today
    all_msgs = newly_flagged + still_missed
    return {
        "person": person,
        "missed_count": len(all_msgs),
        "new_alerts": newly_flagged,
        "family_notified": [f["name"] for f in _family(person)] if newly_flagged else [],
        "already_alerted": still_missed,
        "caregiver_message": (" ".join(all_msgs) + " Please check in with them.") if all_msgs
        else f"All of {person}'s doses so far today are on track.",
    }


@mcp.tool()
def schedule_reminder(person: str, message: str, remind_at: str) -> str:
    """Schedule a reminder for a person.

    Args:
        person: Who the reminder is for.
        message: What to say, e.g. "Time for your Metformin".
        remind_at: Time as HH:MM today (24h) or a full ISO timestamp.
    """
    try:
        if len(remind_at) <= 5:
            t = datetime.strptime(remind_at, "%H:%M").time()
            when = datetime.combine(now().date(), t)
        else:
            when = datetime.fromisoformat(remind_at)
    except ValueError:
        return "remind_at must be HH:MM (24h) or an ISO timestamp."
    with db() as conn:
        conn.execute(
            "INSERT INTO reminders (person, message, remind_at) VALUES (?,?,?)",
            (person, message, when.isoformat(timespec="seconds")),
        )
    return f"Reminder set for {person} at {when.strftime('%Y-%m-%d %H:%M')}: {message}"


@mcp.tool()
def get_due_reminders(person: str = "") -> list[dict]:
    """Return reminders whose time has arrived and mark them delivered.
    Leave person empty for everyone. Poll this from the agent or frontend."""
    n = now().isoformat(timespec="seconds")
    with db() as conn:
        q = "SELECT * FROM reminders WHERE delivered=0 AND remind_at<=?"
        args = [n]
        if person:
            q += " AND person=?"
            args.append(person)
        rows = conn.execute(q + " ORDER BY remind_at", args).fetchall()
        for r in rows:
            conn.execute("UPDATE reminders SET delivered=1 WHERE id=?", (r["id"],))
    return [{"person": r["person"], "message": r["message"], "remind_at": r["remind_at"]} for r in rows]


@mcp.tool()
def get_weekly_summary(person: str) -> dict:
    """Adherence summary for the last 7 days (including today): doses taken vs
    scheduled per medication, overall percentage, and wellbeing notes."""
    n = now()
    days = [(n - timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    meds_out, total_taken, total_sched = [], 0, 0
    with db() as conn:
        for m in conn.execute("SELECT * FROM medications WHERE person=?", (person,)):
            taken_days = 0
            for d in days:
                if conn.execute(
                    "SELECT 1 FROM check_ins WHERE kind='medication_taken' AND medication_id=? AND ts LIKE ?",
                    (m["id"], d + "%"),
                ).fetchone():
                    taken_days += 1
            total_taken += taken_days
            total_sched += len(days)
            meds_out.append({
                "medication": m["name"], "taken_days": taken_days, "scheduled_days": len(days),
                "adherence_pct": round(100 * taken_days / len(days)),
            })
        notes = [
            {"time": r["ts"], "note": r["note"]}
            for r in conn.execute(
                "SELECT ts, note FROM check_ins WHERE person=? AND kind='wellbeing' AND ts>=? ORDER BY ts",
                (person, days[0]),
            )
        ]
        alerts = conn.execute(
            "SELECT COUNT(*) c FROM alerts WHERE person=? AND date>=?", (person, days[0])
        ).fetchone()["c"]
    return {
        "person": person, "period": f"{days[0]} to {days[-1]}", "medications": meds_out,
        "overall_adherence_pct": round(100 * total_taken / total_sched) if total_sched else None,
        "missed_dose_alerts": alerts, "wellbeing_notes": notes,
    }


@mcp.tool()
def add_family_member(person: str, name: str, role: str = "primary") -> str:
    """Add someone to a person's family circle. role is "primary" (alerted first) or "backup"
    (only contacted if nobody claims the alert)."""
    if role not in ("primary", "backup"):
        return 'role must be "primary" or "backup".'
    with db() as conn:
        conn.execute(
            "INSERT INTO family (person, name, role) VALUES (?,?,?) "
            "ON CONFLICT(person, name) DO UPDATE SET role=excluded.role",
            (person, name, role),
        )
    return f"{name} is now a {role} contact for {person}."


@mcp.tool()
def get_open_alerts(person: str) -> list[dict]:
    """Today's missed-dose alerts for a person whose dose has still not been taken. Shows
    who (if anyone) has claimed it, whether it was escalated, and how many minutes ago it
    was raised."""
    n = now()
    today = n.strftime("%Y-%m-%d")
    out = []
    with db() as conn:
        for a in conn.execute(
            "SELECT a.*, m.name AS med, m.dose FROM alerts a JOIN medications m ON m.id=a.medication_id "
            "WHERE a.person=? AND a.date=? ORDER BY a.id", (person, today)):
            taken = conn.execute(
                "SELECT 1 FROM check_ins WHERE kind='medication_taken' AND medication_id=? AND ts LIKE ?",
                (a["medication_id"], today + "%")).fetchone()
            if taken:
                continue
            raised = datetime.fromisoformat(a["ts"])
            out.append({
                "medication": a["med"], "dose": a["dose"], "message": a["message"],
                "claimed_by": a["claimed_by"], "escalated": bool(a["escalated"]),
                "minutes_open": int((n - raised).total_seconds() // 60),
            })
    return out


@mcp.tool()
def claim_alert(person: str, medication: str, claimed_by: str) -> str:
    """A family member says "I've got this" for a missed-dose alert. Stops escalation and
    tells the rest of the family so nobody else calls."""
    n = now()
    today = n.strftime("%Y-%m-%d")
    with db() as conn:
        row = conn.execute(
            "SELECT a.id, a.claimed_by FROM alerts a JOIN medications m ON m.id=a.medication_id "
            "WHERE a.person=? AND lower(m.name)=lower(?) AND a.date=?", (person, medication, today)).fetchone()
        if not row:
            return f"There is no open alert for {medication} today."
        if not conn.execute("SELECT 1 FROM family WHERE person=? AND lower(name)=lower(?)", (person, claimed_by)).fetchone():
            return f"{claimed_by} is not in {person}'s family circle."
        if row["claimed_by"]:
            return f"{row['claimed_by']} already claimed this alert. No action needed from {claimed_by}."
        conn.execute("UPDATE alerts SET claimed_by=? WHERE id=?", (claimed_by, row["id"]))
        told = _notify_family(conn, person,
            f"{claimed_by} is handling the missed {medication}. No need to call {person}.", n, exclude=[claimed_by])
    return f"{claimed_by} claimed the {medication} alert. Told {', '.join(told) or 'nobody else'}; escalation stopped."


@mcp.tool()
def escalate_alert(person: str, medication: str) -> dict:
    """Escalate an unclaimed missed-dose alert to the backup contact. Refuses if the alert
    was already claimed, already escalated, the dose was taken, or it is under 15 minutes old."""
    n = now()
    today = n.strftime("%Y-%m-%d")
    with db() as conn:
        row = conn.execute(
            "SELECT a.*, m.name AS med FROM alerts a JOIN medications m ON m.id=a.medication_id "
            "WHERE a.person=? AND lower(m.name)=lower(?) AND a.date=?", (person, medication, today)).fetchone()
        if not row:
            return {"escalated": False, "reason": f"No alert exists for {medication} today."}
        if conn.execute("SELECT 1 FROM check_ins WHERE kind='medication_taken' AND medication_id=? AND ts LIKE ?",
                        (row["medication_id"], today + "%")).fetchone():
            return {"escalated": False, "reason": "The dose was taken. Nothing to escalate."}
        if row["claimed_by"]:
            return {"escalated": False, "reason": f"{row['claimed_by']} has claimed this alert. Not escalating."}
        if row["escalated"]:
            return {"escalated": False, "reason": "Already escalated."}
        mins = int((n - datetime.fromisoformat(row["ts"])).total_seconds() // 60)
        if mins < 15:
            return {"escalated": False, "reason": f"Alert is only {mins} min old. Wait until 15 min."}
        backups = [f["name"] for f in conn.execute(
            "SELECT name FROM family WHERE person=? AND role='backup'", (person,))]
        who = backups or [f["name"] for f in conn.execute("SELECT name FROM family WHERE person=?", (person,))]
        _notify_family(conn, person,
            f"ESCALATION: {person} still has not taken {row['med']} after {mins} minutes. Please call her now.", n, only=who)
        conn.execute("UPDATE alerts SET escalated=1 WHERE id=?", (row["id"],))
    return {"escalated": True, "notified": who, "minutes_open": mins}


@mcp.tool()
def get_family_feed(person: str, limit: int = 8) -> list[dict]:
    """Most recent notifications sent to the family circle (newest first)."""
    with db() as conn:
        return [{"to": r["recipient"], "message": r["message"], "time": r["ts"], "delivery": r["delivery"] or "in-app"}
                for r in conn.execute(
            "SELECT * FROM notifications WHERE person=? ORDER BY id DESC LIMIT ?", (person, limit))]


@mcp.tool()
def reset_demo_day(person: str) -> str:
    """DEMO ONLY: clear today's check-ins, alerts, reminders and notifications so the demo
    can be re-filmed. Keeps the medication schedule and 6 days of history."""
    today = datetime.now().strftime("%Y-%m-%d")
    with db() as conn:
        for r in conn.execute("SELECT DISTINCT medication_id FROM check_ins WHERE person=? AND kind='medication_taken' AND ts LIKE ?",
                              (person, today + "%")).fetchall():
            conn.execute("UPDATE medications SET supply=supply+1 WHERE id=? AND supply IS NOT NULL", (r["medication_id"],))
        for p in conn.execute("SELECT * FROM proposals WHERE person=? AND status='approved'", (person,)).fetchall():
            data = json.loads(p["payload"])
            if p["kind"] in ("reschedule", "time_change") and data.get("old_time"):
                conn.execute("UPDATE medications SET time_of_day=? WHERE person=? AND lower(name)=lower(?)",
                             (data["old_time"], person, p["medication"]))
            if p["kind"] == "remove_med" and data.get("snapshot"):
                sn = data["snapshot"]
                if not conn.execute("SELECT 1 FROM medications WHERE id=? OR (person=? AND lower(name)=lower(?))",
                                    (sn["id"], person, p["medication"])).fetchone():
                    conn.execute("INSERT INTO medications (id, person, name, dose, time_of_day, supply) VALUES (?,?,?,?,?,?)",
                                 (sn["id"], person, p["medication"], sn["dose"], sn["time_of_day"], sn["supply"]))
            if p["kind"] == "new_med" and data.get("created_med"):
                mid = conn.execute("SELECT id FROM medications WHERE person=? AND lower(name)=lower(?)",
                                   (person, p["medication"])).fetchone()
                if mid:
                    conn.execute("DELETE FROM check_ins WHERE medication_id=?", (mid["id"],))
                    conn.execute("DELETE FROM medications WHERE id=?", (mid["id"],))
        conn.execute("DELETE FROM proposals WHERE person=?", (person,))
        conn.execute("DELETE FROM check_ins WHERE person=? AND ts LIKE ?", (person, today + "%"))
        conn.execute("DELETE FROM alerts WHERE person=?", (person,))
        conn.execute("DELETE FROM reminders WHERE person=?", (person,))
        conn.execute("DELETE FROM notifications WHERE person=?", (person,))
    return f"Reset today's activity for {person}."


# ---------------------------------------------------------------------------------------------
# Patterns, refills and proposals. The agent may PROPOSE changes; only a family member can approve
# them (approve_proposal is hidden from the model), so nothing about the care plan changes silently.
# ---------------------------------------------------------------------------------------------

def _mins(hhmm: str) -> int:
    return int(hhmm[:2]) * 60 + int(hhmm[3:5])


def _hm(minutes: int) -> str:
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def _valid_time(t: str) -> bool:
    try:
        datetime.strptime(t, "%H:%M")
        return True
    except ValueError:
        return False


def _proposal_dict(r) -> dict:
    return {"id": r["id"], "kind": r["kind"], "medication": r["medication"], "summary": r["summary"],
            "caution": r["caution"], "payload": json.loads(r["payload"]), "status": r["status"],
            "decided_by": r["decided_by"], "resolution": r["resolution"]}


def _create_proposal(conn, person, kind, medication, summary, caution, payload, n) -> dict:
    dup = conn.execute("SELECT * FROM proposals WHERE person=? AND kind=? AND lower(medication)=lower(?) AND status='pending'",
                       (person, kind, medication)).fetchone()
    if dup:
        return {**_proposal_dict(dup), "duplicate": True}
    cur = conn.execute(
        "INSERT INTO proposals (person, kind, medication, summary, caution, payload, created) VALUES (?,?,?,?,?,?,?)",
        (person, kind, medication, summary, caution, json.dumps(payload), n.isoformat(timespec="seconds")))
    _notify_family(conn, person, f"CareLoop suggestion: {summary} Open the app to approve or decline.", n)
    row = conn.execute("SELECT * FROM proposals WHERE id=?", (cur.lastrowid,)).fetchone()
    return {**_proposal_dict(row), "duplicate": False}


@mcp.tool()
def analyze_patterns(person: str) -> list[dict]:
    """Learn from the last 14 days of history when each medication is ACTUALLY taken, compared with its
    schedule. If the person reliably takes a dose 30 to 120 minutes away from the scheduled time, a
    suggestion with a better time is included. This only reads data; use propose_reschedule to suggest it."""
    n = now()
    today = n.strftime("%Y-%m-%d")
    since = (n - timedelta(days=14)).strftime("%Y-%m-%d")
    out = []
    with db() as conn:
        for m in conn.execute("SELECT * FROM medications WHERE person=? ORDER BY time_of_day", (person,)).fetchall():
            sched = _mins(m["time_of_day"])
            lags = []
            for r in conn.execute("SELECT ts FROM check_ins WHERE kind='medication_taken' AND medication_id=? AND ts>=? AND ts NOT LIKE ?",
                                  (m["id"], since, today + "%")):
                t = datetime.fromisoformat(r["ts"])
                lags.append(t.hour * 60 + t.minute - sched)
            pending = bool(conn.execute("SELECT 1 FROM proposals WHERE person=? AND kind='reschedule' AND lower(medication)=lower(?) AND status='pending'",
                                        (person, m["name"])).fetchone())
            item = {"medication": m["name"], "dose": m["dose"], "scheduled": m["time_of_day"], "samples": len(lags),
                    "typical_time": None, "median_lag_min": None, "late_share_pct": None,
                    "suggestion": None, "pending_proposal": pending}
            if lags:
                med_lag = int(statistics.median(lags))
                typical = sched + med_lag
                item.update(median_lag_min=med_lag, typical_time=_hm(typical) if 0 <= typical < 1440 else None,
                            late_share_pct=round(100 * sum(1 for x in lags if x >= 30) / len(lags)))
                if len(lags) >= 4 and 30 <= abs(med_lag) <= 120 and 0 <= typical < 1440:
                    new_time = _hm(int(round(typical / 5)) * 5)
                    item["suggestion"] = {
                        "new_time": new_time,
                        "reason": f"Over {len(lags)} days she usually takes it around {new_time}, "
                                  f"{abs(med_lag)} minutes {'after' if med_lag > 0 else 'before'} the scheduled {m['time_of_day']}. "
                                  f"Moving it would stop false missed-dose alarms."}
            out.append(item)
    return out


@mcp.tool()
def propose_reschedule(person: str, medication: str, new_time: str, reason: str = "") -> dict:
    """Suggest moving a medication to a new time (HH:MM, at most 2 hours from the current time). This
    creates a PROPOSAL for the family. It does not change the schedule. Safe to call repeatedly."""
    if not _valid_time(new_time):
        return {"error": "new_time must be HH:MM in 24h format."}
    n = now()
    with db() as conn:
        m = conn.execute("SELECT * FROM medications WHERE person=? AND lower(name)=lower(?)", (person, medication)).fetchone()
        if not m:
            return {"error": f"No medication named '{medication}' is scheduled for {person}."}
        if abs(_mins(new_time) - _mins(m["time_of_day"])) > 120:
            return {"error": "Proposed shift is more than 2 hours. Ask the prescriber before changing timing that much."}
        if new_time == m["time_of_day"]:
            return {"error": "That is already the scheduled time."}
        return _create_proposal(
            conn, person, "reschedule", m["name"],
            f"Move {m['name']} from {m['time_of_day']} to {new_time}. {reason}".strip(),
            "Check with her prescriber first if the timing matters (for example, with meals).",
            {"new_time": new_time, "old_time": m["time_of_day"]}, n)


@mcp.tool()
def get_refill_status(person: str, threshold_days: int = 5) -> list[dict]:
    """Days of medication left (each medicine is one dose a day). A medicine is "low" when it has
    threshold_days or fewer left. Medicines with no recorded supply report days_left null."""
    with db() as conn:
        out = []
        for m in conn.execute("SELECT * FROM medications WHERE person=? ORDER BY name", (person,)).fetchall():
            pending = bool(conn.execute("SELECT 1 FROM proposals WHERE person=? AND kind='refill' AND lower(medication)=lower(?) AND status='pending'",
                                        (person, m["name"])).fetchone())
            requested = bool(conn.execute("SELECT 1 FROM proposals WHERE person=? AND kind='refill' AND lower(medication)=lower(?) AND status='approved'",
                                          (person, m["name"])).fetchone())
            left = m["supply"]
            out.append({"medication": m["name"], "dose": m["dose"], "days_left": left,
                        "low": left is not None and left <= threshold_days,
                        "pending_proposal": pending, "refill_requested": requested})
    return out


@mcp.tool()
def propose_refill(person: str, medication: str, quantity: int = 30) -> dict:
    """Suggest a pharmacy refill. This creates a PROPOSAL for the family to approve. Nothing is ordered
    until a family member approves, and even then the order is simulated in this demo."""
    if not 1 <= quantity <= 180:
        return {"error": "quantity must be between 1 and 180."}
    n = now()
    with db() as conn:
        m = conn.execute("SELECT * FROM medications WHERE person=? AND lower(name)=lower(?)", (person, medication)).fetchone()
        if not m:
            return {"error": f"No medication named '{medication}' is scheduled for {person}."}
        left = m["supply"]
        tail = f"about {left} day{'s' if left != 1 else ''} left" if left is not None else "supply unknown"
        return _create_proposal(
            conn, person, "refill", m["name"], f"Refill {quantity} of {m['name']} ({m['dose']}): {tail}.",
            "Demo only: approving records a SIMULATED pharmacy order. Nothing is purchased.",
            {"quantity": quantity, "days_left": left}, n)


@mcp.tool()
def propose_new_medication(person: str, medication: str, dose: str, time_of_day: str = "",
                           quantity: int = 0, source: str = "label photo") -> dict:
    """Suggest adding a medication (for example one read from a bottle label). time_of_day may be empty
    if unknown; the approver then chooses it. This creates a PROPOSAL only."""
    medication, dose = medication.strip(), dose.strip()
    if not medication or len(medication) > 60 or len(dose) > 40:
        return {"error": "Medication name and dose are required and must be short."}
    if time_of_day and not _valid_time(time_of_day):
        return {"error": "time_of_day must be HH:MM or empty."}
    if not 0 <= quantity <= 365:
        return {"error": "quantity must be between 0 and 365."}
    n = now()
    when = f" daily at {time_of_day}" if time_of_day else ", time still to be chosen"
    with db() as conn:
        # A newer request replaces an older pending one for the same medicine (e.g. a corrected time).
        conn.execute("UPDATE proposals SET status='rejected', resolution='Replaced by a newer request.' "
                     "WHERE person=? AND kind='new_med' AND lower(medication)=lower(?) AND status='pending'", (person, medication))
        return _create_proposal(
            conn, person, "new_med", medication, f"Add {medication} {dose}{when} (from {source}).",
            "Read by AI from a photo. Check the name and dose against the bottle and the prescriber's instructions before approving.",
            {"dose": dose, "time_of_day": time_of_day, "quantity": quantity, "source": source}, n)


@mcp.tool()
def propose_time_change(person: str, medication: str, new_time: str) -> dict:
    """The user asked to change the time of a medicine that is already on the schedule (HH:MM, 24h).
    This creates a PROPOSAL only: the time stays the same until a family member taps Yes in the
    "Are you sure about changing the time?" popup. Use this whenever the user asks to move or change a time."""
    if not _valid_time(new_time):
        return {"error": "new_time must be HH:MM in 24h format."}
    with db() as conn:
        m = conn.execute("SELECT * FROM medications WHERE person=? AND lower(name)=lower(?)", (person, medication)).fetchone()
        if not m:
            return {"error": f"No medication named '{medication}' is scheduled for {person}."}
        if new_time == m["time_of_day"]:
            return {"error": f"{m['name']} is already scheduled at {new_time}."}
        # A newer request replaces an older pending one for the same medicine.
        conn.execute("UPDATE proposals SET status='rejected', resolution='Replaced by a newer request.' "
                     "WHERE person=? AND kind='time_change' AND lower(medication)=lower(?) AND status='pending'", (person, medication))
        return _create_proposal(
            conn, person, "time_change", m["name"],
            f"Change {m['name']} {m['dose']} from {m['time_of_day']} to {new_time}.",
            "Check with her prescriber first if the timing matters (for example, with meals).",
            {"new_time": new_time, "old_time": m["time_of_day"]}, now())


@mcp.tool()
def propose_remove_medication(person: str, medication: str) -> dict:
    """Suggest removing a medication from the schedule. This creates a PROPOSAL only: the medicine stays on
    the schedule until a family member approves. Use this whenever the user asks to remove or stop a medicine."""
    with db() as conn:
        m = conn.execute("SELECT * FROM medications WHERE person=? AND lower(name)=lower(?)", (person, medication)).fetchone()
        if not m:
            return {"error": f"No medication named '{medication}' is scheduled for {person}."}
        return _create_proposal(
            conn, person, "remove_med", m["name"],
            f"Remove {m['name']} {m['dose']} (daily at {m['time_of_day']}) from the schedule.",
            "Only remove a medicine if her prescriber has said to stop it.",
            {"dose": m["dose"], "time_of_day": m["time_of_day"]}, now())


@mcp.tool()
def list_proposals(person: str, status: str = "pending") -> list[dict]:
    """Proposals for the family. status is "pending" or "all"."""
    with db() as conn:
        q = "SELECT * FROM proposals WHERE person=?" + ("" if status == "all" else " AND status='pending'") + " ORDER BY id"
        return [_proposal_dict(r) for r in conn.execute(q, (person,)).fetchall()]


@mcp.tool()
def approve_proposal(proposal_id: int, approved_by: str, time_of_day: str = "") -> dict:
    """A family member approves a proposal and it is applied. For a new medicine without a time,
    pass time_of_day (HH:MM). Only family members can approve."""
    n = now()
    with db() as conn:
        p = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if not p:
            return {"ok": False, "message": "No such proposal."}
        if p["status"] != "pending":
            return {"ok": False, "message": f"This proposal was already {p['status']}."}
        person = p["person"]
        if not conn.execute("SELECT 1 FROM family WHERE person=? AND lower(name)=lower(?)", (person, approved_by)).fetchone():
            return {"ok": False, "message": f"{approved_by} is not in {person}'s family circle."}
        data = json.loads(p["payload"])
        name = p["medication"]
        if p["kind"] in ("reschedule", "time_change"):
            conn.execute("UPDATE medications SET time_of_day=? WHERE person=? AND lower(name)=lower(?)",
                         (data["new_time"], person, name))
            msg = f"{name} is now scheduled at {data['new_time']}."
        elif p["kind"] == "refill":
            data["order_ref"] = f"R-{p['id']:04d}"
            msg = f"Refill of {data['quantity']} {name} requested (SIMULATED pharmacy order {data['order_ref']}; nothing was purchased)."
        elif p["kind"] == "remove_med":
            m = conn.execute("SELECT * FROM medications WHERE person=? AND lower(name)=lower(?)", (person, name)).fetchone()
            if not m:
                return {"ok": False, "message": f"{name} is no longer on the schedule."}
            data["snapshot"] = {"id": m["id"], "dose": m["dose"], "time_of_day": m["time_of_day"], "supply": m["supply"]}
            conn.execute("DELETE FROM medications WHERE id=?", (m["id"],))
            msg = f"{name} was removed from the schedule."
        else:  # new_med
            t = time_of_day or data.get("time_of_day", "")
            if not _valid_time(t):
                return {"ok": False, "message": "Choose a time for this medicine (HH:MM) before approving."}
            existing = conn.execute("SELECT id FROM medications WHERE person=? AND lower(name)=lower(?)", (person, name)).fetchone()
            supply = data.get("quantity") or None
            if existing:
                conn.execute("UPDATE medications SET dose=?, time_of_day=?, supply=COALESCE(?, supply) WHERE id=?",
                             (data["dose"], t, supply, existing["id"]))
            else:
                conn.execute("INSERT INTO medications (person, name, dose, time_of_day, supply) VALUES (?,?,?,?,?)",
                             (person, name, data["dose"], t, supply))
            data["created_med"] = not existing
            msg = f"Added {name} {data['dose']} daily at {t}."
        conn.execute("UPDATE proposals SET status='approved', decided_by=?, resolution=?, payload=? WHERE id=?",
                     (approved_by, msg, json.dumps(data), proposal_id))
        _notify_family(conn, person, f"{approved_by} approved: {msg}", n, exclude=[approved_by])
    return {"ok": True, "message": msg}


@mcp.tool()
def reject_proposal(proposal_id: int, rejected_by: str) -> dict:
    """A family member declines a proposal. Nothing changes."""
    with db() as conn:
        p = conn.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
        if not p or p["status"] != "pending":
            return {"ok": False, "message": "No pending proposal with that id."}
        if not conn.execute("SELECT 1 FROM family WHERE person=? AND lower(name)=lower(?)", (p["person"], rejected_by)).fetchone():
            return {"ok": False, "message": f"{rejected_by} is not in {p['person']}'s family circle."}
        msg = f"{rejected_by} declined this suggestion."
        conn.execute("UPDATE proposals SET status='rejected', decided_by=?, resolution=? WHERE id=?", (rejected_by, msg, proposal_id))
    return {"ok": True, "message": msg}


@mcp.tool()
def set_demo_time(time_of_day: str = "", reset: bool = False) -> str:
    """DEMO ONLY: jump the server clock to HH:MM today (e.g. "21:00") so missed/due
    states can be shown instantly. Pass reset=true to return to real time."""
    with db() as conn:
        if reset:
            conn.execute("DELETE FROM demo_clock")
            return "Demo clock reset to real time."
        try:
            t = datetime.strptime(time_of_day, "%H:%M").time()
        except ValueError:
            return "time_of_day must be HH:MM in 24h format."
        target = datetime.combine(datetime.now().date(), t)
        offset = int((target - datetime.now()).total_seconds())
        conn.execute("INSERT OR REPLACE INTO demo_clock (id, offset_seconds, frozen_at) VALUES (1, ?, ?)",
                     (offset, target.isoformat(timespec="seconds")))
    return f"Demo clock set to {time_of_day} today (time is frozen until the next jump or reset)."


if __name__ == "__main__":
    import uvicorn
    from starlette.middleware.cors import CORSMiddleware

    HOST = os.environ.get("CARELOOP_HOST", "127.0.0.1")   # use 0.0.0.0 to share on your network
    PORT = int(os.environ.get("CARELOOP_PORT", "8000"))
    # Comma-separated origins, or "*" so any frontend (e.g. localhost:5173) can call the server.
    ORIGINS = [o.strip() for o in os.environ.get("CARELOOP_CORS_ORIGINS", "*").split(",")]

    app = mcp.streamable_http_app(host=HOST)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=ORIGINS,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["*"],
        expose_headers=["Mcp-Session-Id"],
    )
    print(f"CareLoop MCP server: http://{HOST}:{PORT}/mcp  (CORS origins: {ORIGINS})")
    uvicorn.run(app, host=HOST, port=PORT)