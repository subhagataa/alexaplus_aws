# CareLoop

**An Alexa+ caretaking agent that closes the loop.** Reminder apps tell a family that Grandma missed a dose, and then
Sarah, Raj and Dad all call her at once, or nobody does. CareLoop acts: it alerts the family circle, lets one person
say "I've got this" so everyone else stands down, follows up, and escalates only if nobody has it.

```
 Browser (simulated Alexa+ UI)  --HTTP-->  agent.py + llm.py  --> Bedrock | Gemini/Groq/Ollama | Anthropic API
                                              |
                                              +--MCP, Streamable HTTP-->  server.py (CareLoop MCP server, SQLite)
```

* `server.py` is the **MCP server** (Streamable HTTP at `/mcp`). It holds all state and business rules.
* `agent.py` is the **Alexa+ AI agent**: an LLM tool-use loop that decides which MCP tools to call. It also serves the UI.
* `llm.py` is the provider-agnostic LLM layer (from the Alexa+ project): `bedrock`, `openai_compat` (Gemini/Groq/Ollama), `anthropic`.
* `prompts.py` holds the system prompt (CareLoop caretaking rules + Alexa+ spoken-style rules).
* `cli.py` is a terminal chat with the agent.
* `static/index.html` is the **simulated Alexa+ experience** (chat, voice in/out, cards). Every number on screen comes from the MCP server.

## Setup (Python 3.10+)

```bash
python -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate
pip install -r requirements.txt
```

## Run the demo

```bash
python seed_demo.py             # fresh data: Grandma, 3 meds, 6 days of history, family Sarah/Dad/Raj
python server.py                # terminal 1: MCP server on http://127.0.0.1:8000/mcp
python agent.py                 # terminal 2: agent + UI on http://127.0.0.1:8001
```

Open <http://127.0.0.1:8001>. The badge under the title shows which agent is answering.

### Choosing the LLM provider

Set `LLM_PROVIDER` (default: `bedrock`, or `openai_compat` if only `LLM_API_KEY` is set):

| Provider | `LLM_PROVIDER` | Other vars |
|---|---|---|
| Amazon Bedrock | `bedrock` | AWS creds, `AWS_REGION`, `BEDROCK_MODEL_ID` (or `LLM_MODEL`) |
| Gemini (free) | `openai_compat` | `LLM_API_KEY`, `LLM_MODEL` (default `gemini-2.5-flash`) |
| Groq (free) | `openai_compat` | `LLM_BASE_URL=https://api.groq.com/openai/v1`, `LLM_API_KEY`, `LLM_MODEL` |
| Ollama (local) | `openai_compat` | `LLM_BASE_URL=http://localhost:11434/v1`, `LLM_API_KEY=ollama`, `LLM_MODEL` |
| Anthropic API | `anthropic` | `pip install anthropic`, `LLM_API_KEY`, `LLM_MODEL` |

The UI badge reads "Claude on Bedrock" for Bedrock, "<provider> (live)" for any other live provider, and "offline fallback"
when no model answered. For the AWS Builder challenge, record the demo with the Bedrock badge showing.

### Using Claude on Amazon Bedrock (needed for the AWS Builder challenge)

1. In the AWS console, open **Bedrock > Model access** and enable a Claude model in your region.
2. Configure credentials (`aws configure` or environment variables).
3. Set the model/inference-profile ID you enabled, then start the agent:

```bash
export AWS_REGION=us-east-1
export BEDROCK_MODEL_ID=<your Claude model or inference profile ID>
python agent.py
```

The badge turns green: **agent: Claude on Bedrock**. If Bedrock is unreachable, `CARELOOP_AGENT_MODE=auto` (default)
falls back to a deterministic *offline* planner that calls the same MCP tools, and the badge says so honestly.
Use `CARELOOP_AGENT_MODE=bedrock` to fail loudly instead, or `offline` to skip AWS entirely.

### The 60-second demo script

1. Tap **↺** to reset the day (07:30). Grandma has taken nothing yet.
2. Tap **+2h**. Nobody asks anything. The agent checks status, alerts Sarah, Dad and Raj, and schedules a follow-up.
3. Tap the **name pill** to switch family member, then **I've got this**. The others are told; escalation stops for that dose.
4. Tap **+15m**. The agent stands down on the claimed dose and escalates the unclaimed one to Raj (the backup).
5. Type *"Show the week"* for the doctor-ready adherence summary, or *"She took her Lisinopril"* to resolve an alert.
6. After any jump, the agent also **suggests** two things: a **Lisinopril refill** (4 days left) and moving **Vitamin D**
   from 20:00 to about 21:15 (she reliably takes it after her TV show). Each appears as a card; a family member taps
   **Approve** or **Not now**. Nothing changes until someone approves.
7. Say or type **"Change Metformin to 9 am"**. A popup asks "Are you sure about changing the time?" and nothing changes until you tap Yes.

## New: what makes it more than a reminder app

**Real notifications (Amazon SNS).** Off by default. When on, alerts go out as real SMS or as email via an SNS topic, and the
family feed shows "sent by SNS" or why it wasn't sent. Phone numbers live only in an environment variable, never in the
database. A hard hourly cap (`CARELOOP_SNS_MAX_PER_HOUR`, default 20) protects against loops and surprise bills.

```bash
export CARELOOP_NOTIFY=sns
export AWS_REGION=us-east-1
export CARELOOP_SNS_TARGETS='{"Sarah": "+14155550100", "Dad": "arn:aws:sns:us-east-1:123456789012:careloop-dad"}'
```

A target is an E.164 phone number (SMS) or an SNS topic ARN. The easiest start is a topic with your email subscribed
(confirm the subscription email first). The IAM user needs `sns:Publish`. SMS has account limits (a sandbox that only
reaches verified numbers, and country-specific registration rules): check the current AWS SNS SMS documentation for your
country before relying on it. Demo clock jumps send real messages, so use your own numbers while filming.

**Pattern learning.** `analyze_patterns` compares when each dose is actually taken (last 14 days) with its schedule. If she
reliably takes it 30 to 120 minutes off, the agent proposes the better time, which also stops false missed-dose alarms.

**Refills.** The server tracks supply (one dose a day). Low stock (5 days or fewer) triggers a refill **proposal**.
Approving records a **simulated** pharmacy order. Nothing is purchased.

**Bottle photo.** The vision model reads name, dose and timing and creates a proposal. Labels that need more than one dose
a day are refused (CareLoop tracks one dose a day per medicine), unreadable photos are rejected, and the model gets no
tools during this step, so text printed on a photo cannot make it do anything. Needs a real model (Bedrock, or another
vision-capable provider). In offline mode it says so instead of guessing.

**Humans approve.** The agent can only *propose*. `approve_proposal` and `reject_proposal` are hidden from the model, and
the server only accepts family members.

## MCP tools (24)

| Tool | Purpose |
|---|---|
| `add_medication_schedule` | Add or update a daily medication (`HH:MM`) |
| `remove_medication` / `list_medications` | Manage / read the schedule |
| `log_check_in` | Record a dose taken or a wellbeing note |
| `get_daily_status` | Today's taken / due / missed per medication, plus the (demo) clock |
| `flag_missed_dose` | Record one alert per missed dose (never duplicated) and notify the whole family circle |
| `get_open_alerts` | Unresolved alerts: who claimed them, escalated or not, minutes open |
| `claim_alert` | "I've got this": tells the others, stops escalation. Second claims and non-family are refused |
| `escalate_alert` | Notify the backup contact. Refuses if claimed, taken, already escalated, or under 15 minutes old |
| `add_family_member` / `get_family_feed` | Family circle (primary/backup) and their notifications |
| `schedule_reminder` / `get_due_reminders` | Reminders for Grandma's Echo |
| `get_weekly_summary` | 7-day adherence per medication, alerts, wellbeing notes |
| `analyze_patterns` | When each dose is actually taken vs scheduled; suggests a better time |
| `get_refill_status` | Days of supply left per medicine; flags low stock |
| `propose_reschedule` / `propose_refill` / `propose_new_medication` | Create a PROPOSAL (idempotent). Never changes the plan by itself |
| `list_proposals` / `approve_proposal` / `reject_proposal` | Family decisions. Approve and reject are hidden from the model |
| `set_demo_time` / `reset_demo_day` | Demo only: freeze the clock at a time; clear today (and undo approved changes) for re-filming |

Safety rules live in the tools, not only in the prompt: the agent cannot escalate a claimed alert or spam the family,
and the demo-only tools are hidden from the model. The agent is told never to change doses.

## Tests

```bash
python test_client.py           # with server.py running: calls every MCP tool
python test_e2e.py              # starts everything on test ports; full demo flow, no AWS needed
python test_bedrock_loop.py     # Bedrock tool-use plumbing against a scripted fake (no AWS needed)
python test_llm_providers.py    # OpenAI-compatible provider, chat history, offline fallback (no key needed)
python test_features.py         # patterns, refills, approvals, SNS (fake client), bottle-photo pipeline (fake model)
```

`test_bedrock_loop.py` and `test_features.py` verify request shapes, tool wiring and safety rules using fakes. They cannot verify
the model's judgement or how accurately it reads real bottle labels, and they cannot prove real SMS delivery; those need real credentials.

## Configuration

| Variable | Default | Notes |
|---|---|---|
| `CARELOOP_HOST` / `CARELOOP_PORT` | `127.0.0.1` / `8000` | MCP server |
| `CARELOOP_CORS_ORIGINS` | `*` | MCP server CORS |
| `CARELOOP_DB` | `careloop.db` | SQLite file |
| `CARELOOP_MCP_URL` | `http://127.0.0.1:8000/mcp` | Where the agent finds the MCP server |
| `CARELOOP_AGENT_PORT` | `8001` | Agent + UI |
| `CARELOOP_AGENT_MODE` | `auto` | `auto` (LLM, else offline), `llm` (alias `bedrock`: fail loudly), `offline` |
| `LLM_PROVIDER`, `LLM_MODEL`, `LLM_API_KEY`, `LLM_BASE_URL` | see above | Alexa+ agent provider settings |
| `CONFIRM_WRITES` | `false` | Ask "should I go ahead?" before tools the MCP server marks `readOnlyHint=False` (none today) |
| `BEDROCK_MODEL_ID`, `AWS_REGION` | see `agent.py` | Must match a model enabled in your account |
| `CARELOOP_NOTIFY` | `off` | `sns` turns on real notifications (see above) |
| `CARELOOP_SNS_TARGETS` | `{}` | JSON: family name to phone number or SNS topic ARN |
| `CARELOOP_SNS_MAX_PER_HOUR` | `20` | Hard cap on real messages |

## Limitations (honest list)

* Real notifications are off unless you configure SNS. The code is tested against a fake SNS client; **you must test real delivery yourself**.
* Refill orders are simulated. Nothing is purchased.
* Photo reading accuracy has not been measured on real bottles. The family must always check the proposal against the bottle.
* Pattern learning is a simple median over 14 days; it only proposes shifts of up to 2 hours.
* Alexa+ is **simulated** in a web app. The MCP server is real and can be pointed at by any MCP client.
* Single care recipient in the UI (the data model supports several).
* Not a medical device. It never advises on doses.

## License
MIT