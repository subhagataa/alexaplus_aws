# Devpost submission draft: CareLoop

Fill every `TODO` with what you actually experienced. Judges and Amazon's review team read the feedback and friction
log, so honest specifics score better than polish. Delete this note before submitting.

## Tracks and mini challenges
* Primary track: **Alexa+** (simulated Alexa+ experience + self-hosted MCP server)
* Mini challenge: **AWS Builder** (Claude on Amazon Bedrock tool use) — only tick this after you have run it with real AWS credentials
* Mini challenge: Open Source (optional) — only if you make a real contribution during the hackathon window

## Text description (paste into Devpost, edit freely)
**CareLoop is an Alexa+ caretaking agent that closes the loop.** Medication reminder apps notify people. They do not
resolve anything: when Grandma misses a dose, Sarah, Raj and Dad all get the same alert and either all call her or
each assumes someone else will.

CareLoop runs as a self-hosted MCP server (Streamable HTTP, SQLite state) with 24 tools. A Claude agent on Amazon
Bedrock decides which tools to call. When a dose is missed it alerts the whole family circle, schedules a follow-up
reminder on Grandma's Echo, and waits. Any family member can answer "I've got this": the others are told to stand
down and escalation stops. If nobody claims it, the agent escalates to the backup contact after 15 minutes. It can
also log doses, add medications from plain language, and produce a doctor-ready weekly adherence summary.

It also does the things a reminder app cannot. It learns when Grandma really takes each dose and proposes moving the
reminder (fewer false alarms), notices when a medicine is running low and proposes a refill, and reads a photo of a new
bottle to propose adding it. Alerts go out as real SMS or email through Amazon SNS. Every change is a proposal that a
family member must approve; the AI can suggest but never approve.

Guardrails live in the tools: the agent cannot escalate a claimed alert, double-alert, or change a dose.

**Built with:** Python, MCP (Streamable HTTP), Amazon Bedrock Converse API (Claude, including image input), Amazon SNS, SQLite, Starlette, vanilla JS.

**What is simulated:** pharmacy refill orders (nothing is purchased).

**Also simulated:** Alexa+ itself runs as a web app (voice input/output via the browser).

## Demo video checklist (under 3 minutes, public on YouTube or Vimeo)
- [ ] Show the badge reading "agent: Claude on Bedrock" (proves the AWS path is live)
- [ ] Tap +2h: agent acts unprompted, tool calls visible
- [ ] Switch family member, "I've got this", others stand down
- [ ] Tap +15m: stands down on the claimed dose, escalates the unclaimed one
- [ ] "Show the week" summary
- [ ] Approve the refill and the Vitamin D time change; photograph a bottle and approve it
- [ ] Show a real SMS or email arriving (only if SNS really delivered it)
- [ ] No copyrighted music or third-party logos

## Product feedback (TODO: replace with your real experience)
**Tools used and for what**
* MCP Python SDK 2.x: server (`MCPServer`, Streamable HTTP) and client (`Client`) — 24 tools
* Amazon Bedrock Converse API with tool use and image input (Claude): the agent loop and the bottle-label reader — TODO: confirm both ran on Bedrock
* Amazon SNS: real family SMS/email — TODO: only claim this after you have received a real message — TODO: model ID and region you used
* TODO: Kiro / Strands / AgentCore, only if you actually used them

**What worked well:** TODO
**What needs work:** TODO. Things seen while building that may be worth reporting if you hit them too:
dict-returning MCP tools reached the client as JSON text rather than structured content, so the client had to parse
it; the SDK major version (2.x) changed import paths from older examples.
**Onboarding (zero to hello world):** TODO
**Would you build with it again? Yes/No, and why:** TODO

## Friction log (optional, up to 10% bonus)
One entry per real problem. Template:

| Field | Entry |
|---|---|
| Task attempted | |
| Steps taken | |
| Expected vs actual | |
| Severity | |
| Workaround | |
| Suggestion | |

## Feature requests (optional)
TODO: description, why it matters, priority (Critical / Important / Nice-to-have)

## Repository checklist
- [ ] Replace the name in `LICENSE`; public repo needs the license visible in the About section
- [ ] OR private repo shared with testing@devpost.com and the Amazon team GitHub users from the rules
- [ ] Do NOT commit `venv/` or `careloop.db` (the zip your teammate sent contained a 27 MB Windows `venv`)
- [ ] README setup steps verified from a clean clone
