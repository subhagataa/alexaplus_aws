"""Tests the Alexa+ agent's OpenAI-compatible provider (Gemini/Groq/Ollama route) against a scripted local fake,
through the real agent HTTP API and real MCP server. No API key or AWS needed. Also checks multi-turn chat
history and the offline fallback when the provider is unreachable.

    python test_llm_providers.py
"""
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
FAKE_PORT = 18031
requests_seen = []


class Fake(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        requests_seen.append(body)
        last = body["messages"][-1]
        if last["role"] == "tool" and last["name"] == "get_daily_status":
            msg = {"role": "assistant", "content": "Grandma is on track.", "tool_calls": None}
        elif last["role"] == "user" and "status" in last["content"].lower():
            msg = {"role": "assistant", "content": None, "tool_calls": [{"id": "c1", "type": "function", "function": {
                "name": "get_daily_status", "arguments": json.dumps({"person": "Grandma"})}}]}
        else:
            msg = {"role": "assistant", "content": "Anything else?", "tool_calls": None}
        out = json.dumps({"choices": [{"message": msg}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(out)

    def log_message(self, *a):
        pass


def post(base, path, body=None):
    req = urllib.request.Request(base + path, data=json.dumps(body or {}).encode(), headers={"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req, timeout=30))


def check(label, cond):
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        raise SystemExit(1)


def start(script, env):
    return subprocess.Popen([sys.executable, script], cwd=HERE, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


threading.Thread(target=HTTPServer(("127.0.0.1", FAKE_PORT), Fake).serve_forever, daemon=True).start()
base_env = {**os.environ, "CARELOOP_DB": os.path.join(tempfile.mkdtemp(), "p.db"), "CARELOOP_PORT": "18030",
            "CARELOOP_MCP_URL": "http://127.0.0.1:18030/mcp"}
procs = []
try:
    subprocess.run([sys.executable, "seed_demo.py"], cwd=HERE, env=base_env, check=True, stdout=subprocess.DEVNULL)
    procs.append(start("server.py", base_env))
    good = {**base_env, "CARELOOP_AGENT_PORT": "18032", "LLM_PROVIDER": "openai_compat", "LLM_API_KEY": "k",
            "LLM_BASE_URL": f"http://127.0.0.1:{FAKE_PORT}/v1", "LLM_MODEL": "fake"}
    dead = {**good, "CARELOOP_AGENT_PORT": "18033", "LLM_BASE_URL": "http://127.0.0.1:1/v1"}
    procs.append(start("agent.py", good)); procs.append(start("agent.py", dead))
    time.sleep(4)

    d = post("http://127.0.0.1:18032", "/api/chat", {"message": "What's her status?"})
    check("LLM provider answers through the agent", d["text"] == "Grandma is on track." and d["mode"] == "llm:openai_compat")
    check("tool step has the UI's {tool,args,result} shape with parsed data",
          d["steps"][0]["tool"] == "get_daily_status" and "medications" in d["steps"][0]["result"])
    sent = requests_seen[0]
    names = {t["function"]["name"] for t in sent["tools"]}
    check("MCP tools exposed to the model; demo controls hidden", "flag_missed_dose" in names and not names & {"set_demo_time", "reset_demo_day"})
    d2 = post("http://127.0.0.1:18032", "/api/chat", {"message": "thanks"})
    check("chat history carries across turns", len(requests_seen[-1]["messages"]) > 4 and d2["text"] == "Anything else?")
    a = post("http://127.0.0.1:18032", "/chat", {"session_id": "x", "text": "status please"})
    check("Alexa+ /chat contract works", a["reply"] and a["tool_calls"][0]["tool"] == "get_daily_status")
    h = json.load(urllib.request.urlopen("http://127.0.0.1:18032/api/health"))
    check("health reports provider", h["provider"] == "openai_compat")

    f = post("http://127.0.0.1:18033", "/api/chat", {"message": "How is she doing?"})
    check("unreachable provider falls back to offline planner with a note", f["mode"] == "offline" and f["note"] and f["text"])
    print("\nAll provider checks passed.")
finally:
    for p in procs:
        p.terminate()
