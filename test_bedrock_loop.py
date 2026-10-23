"""Tests the Bedrock tool-use loop WITHOUT AWS credentials by replacing boto3 with a scripted fake.
It proves the plumbing (Converse request shape, toolUse -> MCP call -> toolResult -> final text).
It does NOT prove the model's own judgement: run the real thing with AWS credentials for that.

    python test_bedrock_loop.py
"""
import asyncio
import copy
import os
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.update({"CARELOOP_DB": os.path.join(tempfile.mkdtemp(), "b.db"), "CARELOOP_PORT": "18020",
                   "CARELOOP_MCP_URL": "http://127.0.0.1:18020/mcp", "CARELOOP_AGENT_MODE": "bedrock"})
sys.path.insert(0, HERE)


class FakeBedrock:
    def __init__(self):
        self.calls = []
        self.script = [
            ("get_daily_status", {"person": "Grandma"}),
            ("flag_missed_dose", {"person": "Grandma"}),
            ("schedule_reminder", {"person": "Grandma", "message": "Follow-up check: Metformin", "remind_at": "09:45"}),
            None,
        ]

    def converse(self, **kw):
        self.calls.append(copy.deepcopy(kw))  # snapshot: the agent keeps appending to messages
        step = self.script[len(self.calls) - 1]
        if step is None:
            return {"stopReason": "end_turn", "output": {"message": {"role": "assistant",
                    "content": [{"text": "I alerted the family and will check again at 9:45."}]}}}
        name, args = step
        return {"stopReason": "tool_use", "output": {"message": {"role": "assistant", "content": [
            {"toolUse": {"toolUseId": f"t{len(self.calls)}", "name": name, "input": args}}]}}}


def main():
    env = os.environ
    subprocess.run([sys.executable, "seed_demo.py"], cwd=HERE, env=env, check=True, stdout=subprocess.DEVNULL)
    srv = subprocess.Popen([sys.executable, "server.py"], cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        time.sleep(3)
        import agent
        import llm
        from mcp import Client
        fake = FakeBedrock()
        llm.PROVIDER = "bedrock"
        llm._bedrock_client = lambda: fake

        async def go():
            async with Client(agent.MCP_URL) as c:
                await c.call_tool("set_demo_time", {"time_of_day": "09:30"})
            return await agent.run_agent("tick", "System event: the clock moved to 09:30.")

        out = asyncio.run(go())
        assert out["mode"] == "bedrock", out
        assert [s["tool"] for s in out["steps"]] == ["get_daily_status", "flag_missed_dose", "schedule_reminder"], out["steps"]
        assert "family" in out["text"]
        first = fake.calls[0]
        names = {t["toolSpec"]["name"] for t in first["toolConfig"]["tools"]}
        assert {"flag_missed_dose", "claim_alert", "escalate_alert"} <= names
        assert not names & {"set_demo_time", "reset_demo_day", "approve_proposal", "reject_proposal"}, "demo controls and approvals must be hidden from the model"
        last_user = fake.calls[1]["messages"][-1]
        assert last_user["role"] == "user" and "toolResult" in last_user["content"][0]
        assert "current_time" in last_user["content"][0]["toolResult"]["content"][0]["text"]
        print("PASS Bedrock tool-use loop: request shape, MCP execution, toolResult wiring, final text")
        print("PASS demo-only tools are hidden from the model")
    finally:
        srv.terminate()


main()
