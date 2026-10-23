"""Connects to the running CareLoop server over Streamable HTTP and exercises every tool.

Start the server first:  python server.py
Then run:                python test_client.py
(Run `python seed_demo.py` first for a clean, repeatable dataset; stop the server before seeding.)
"""
import asyncio

from mcp import Client

URL = "http://127.0.0.1:8000/mcp"


async def main() -> None:
    async with Client(URL) as client:
        tools = await client.list_tools()
        print("Tools:", [t.name for t in tools.tools])

        async def call(name: str, args: dict) -> None:
            result = await client.call_tool(name, args)
            print(f"\n{name}({args})\n  ->", result.structured_content or result.content[0].text)

        p = {"person": "Grandma"}
        await call("add_medication_schedule", {**p, "medication": "Metformin", "dose": "500 mg", "time_of_day": "08:00"})
        await call("add_medication_schedule", {**p, "medication": "Metformin", "dose": "500 mg", "time_of_day": "08:00"})  # no duplicate
        await call("add_medication_schedule", {**p, "medication": "Vitamin D", "dose": "1000 IU", "time_of_day": "20:00"})
        await call("add_medication_schedule", {**p, "medication": "X", "dose": "1", "time_of_day": "8am"})  # bad time
        await call("list_medications", p)
        await call("set_demo_time", {"time_of_day": "10:00"})
        await call("get_daily_status", p)                      # Metformin missed, Vitamin D due
        await call("flag_missed_dose", p)                      # new alert
        await call("flag_missed_dose", p)                      # already alerted, no duplicate
        await call("log_check_in", {**p, "kind": "medication_taken", "medication": "Metformin"})
        await call("log_check_in", {**p, "kind": "medication_taken", "medication": "Nope"})  # unknown med
        await call("log_check_in", {**p, "kind": "wellbeing", "note": "Feeling good, walked in the garden"})
        await call("get_daily_status", p)                      # Metformin taken
        await call("schedule_reminder", {**p, "message": "Time for your Vitamin D", "remind_at": "20:00"})
        await call("get_due_reminders", p)                     # none yet (10:00)
        await call("set_demo_time", {"time_of_day": "20:01"})
        await call("get_due_reminders", p)                     # fires now
        await call("get_weekly_summary", p)
        # Family circle: "I've got this" + escalation (added for the multi-person demo)
        await call("reset_demo_day", p)
        await call("set_demo_time", {"time_of_day": "09:30"})
        await call("flag_missed_dose", p)                      # notifies Sarah, Dad, Raj
        await call("get_open_alerts", p)
        await call("claim_alert", {**p, "medication": "Metformin", "claimed_by": "Sarah"})   # tells the others
        await call("escalate_alert", {**p, "medication": "Metformin"})                       # refused: claimed
        await call("set_demo_time", {"time_of_day": "09:46"})
        await call("escalate_alert", {**p, "medication": "Lisinopril"})                      # unclaimed: goes to Raj
        await call("get_family_feed", p)
        await call("reset_demo_day", p)
        await call("set_demo_time", {"reset": True})


if __name__ == "__main__":
    asyncio.run(main())