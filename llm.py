"""Provider-agnostic LLM layer (from the Alexa+ agent), used by CareLoop's agent.py.

Neutral history format used by agent.py:
  {"role": "user", "text": str}
  {"role": "assistant", "text": str, "tool_calls": [{"id", "name", "input"}]}
  {"role": "tool", "results": [{"id", "name", "text"}], "extra_text": str | None}
Providers (LLM_PROVIDER): openai_compat (default, free options) | bedrock | anthropic
"""
import asyncio
import json
import os

import httpx

# CareLoop default: keep Bedrock (the original behaviour) unless an API key is set without a provider,
# in which case the free OpenAI-compatible route (Gemini/Groq/Ollama) is the obvious intent.
PROVIDER = (os.getenv("LLM_PROVIDER") or ("openai_compat" if os.getenv("LLM_API_KEY") else "bedrock")).lower()
_DEFAULT_MODELS = {
    "openai_compat": "gemini-2.5-flash",  # verify the current free model name in AI Studio
    "anthropic": "claude-sonnet-5-5",
    "bedrock": os.getenv("BEDROCK_MODEL_ID", "us.anthropic.claude-sonnet-4-5-20250929-v1:0"),
}
MODEL = os.getenv("LLM_MODEL") or _DEFAULT_MODELS.get(PROVIDER, "")
# Gemini by default. Groq: https://api.groq.com/openai/v1   Ollama: http://localhost:11434/v1
BASE_URL = os.getenv("LLM_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai").rstrip("/")
API_KEY = os.getenv("LLM_API_KEY", "")
REGION = os.getenv("AWS_REGION", os.getenv("AWS_DEFAULT_REGION", "us-east-1"))
MAX_TOKENS = 700


async def complete(system: str, history: list, tools: list) -> dict:
    """tools: [{"name", "description", "schema"}] -> neutral assistant message."""
    if PROVIDER == "openai_compat":
        return await _openai_compat(system, history, tools)
    if PROVIDER == "anthropic":
        return await asyncio.to_thread(_anthropic, system, history, tools)
    if PROVIDER == "bedrock":
        return await asyncio.to_thread(_bedrock, system, history, tools)
    raise ValueError(f"Unknown LLM_PROVIDER: {PROVIDER}")


# ---------- OpenAI-compatible (Gemini, Groq, OpenRouter, Ollama, ...) ----------
def _to_openai(system, history):
    msgs = [{"role": "system", "content": system}]
    for m in history:
        if m["role"] == "user":
            msgs.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            out = {"role": "assistant", "content": m["text"] or None}
            if m["tool_calls"]:
                out["tool_calls"] = []
                for t in m["tool_calls"]:
                    call = {"id": t["id"], "type": "function",
                            "function": {"name": t["name"], "arguments": json.dumps(t["input"])}}
                    if t.get("extra"):  # Gemini 3 thought signature must be echoed back unchanged
                        call["extra_content"] = t["extra"]
                    out["tool_calls"].append(call)
            msgs.append(out)
        else:
            for r in m["results"]:
                msgs.append({"role": "tool", "tool_call_id": r["id"], "name": r["name"], "content": r["text"]})
            if m.get("extra_text"):
                msgs.append({"role": "user", "content": m["extra_text"]})
    return msgs


async def _openai_compat(system, history, tools):
    body = {"model": MODEL, "messages": _to_openai(system, history),
            "max_tokens": MAX_TOKENS, "temperature": 0.2}
    if tools:
        body["tools"] = [{"type": "function", "function": {
            "name": t["name"], "description": t["description"], "parameters": t["schema"]}} for t in tools]
    headers = {"Authorization": f"Bearer {API_KEY}"}
    async with httpx.AsyncClient(timeout=60) as client:
        for attempt in range(3):  # free tiers rate-limit; back off and retry
            r = await client.post(f"{BASE_URL}/chat/completions", json=body, headers=headers)
            if r.status_code in (429, 503) and attempt < 2:
                await asyncio.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                raise RuntimeError(f"{r.status_code} from {BASE_URL}: {r.text[:500]}")
            break
    msg = r.json()["choices"][0]["message"]
    calls = []
    for i, tc in enumerate(msg.get("tool_calls") or []):
        args = tc["function"].get("arguments") or "{}"
        calls.append({"id": tc.get("id") or f"call_{i}", "name": tc["function"]["name"],
                      "input": json.loads(args) if isinstance(args, str) else args,
                      "extra": tc.get("extra_content")})
    return {"role": "assistant", "text": msg.get("content") or "", "tool_calls": calls}


# ---------- Anthropic API ----------
def _anthropic(system, history, tools):
    import anthropic
    msgs = []
    for m in history:
        if m["role"] == "user":
            msgs.append({"role": "user", "content": m["text"]})
        elif m["role"] == "assistant":
            c = [{"type": "text", "text": m["text"]}] if m["text"] else []
            c += [{"type": "tool_use", "id": t["id"], "name": t["name"], "input": t["input"]}
                  for t in m["tool_calls"]]
            msgs.append({"role": "assistant", "content": c})
        else:
            c = [{"type": "tool_result", "tool_use_id": r["id"], "content": r["text"]} for r in m["results"]]
            if m.get("extra_text"):
                c.append({"type": "text", "text": m["extra_text"]})
            msgs.append({"role": "user", "content": c})
    kwargs = {"model": MODEL, "max_tokens": MAX_TOKENS, "system": system, "messages": msgs}
    if tools:
        kwargs["tools"] = [{"name": t["name"], "description": t["description"],
                            "input_schema": t["schema"]} for t in tools]
    resp = anthropic.Anthropic(api_key=API_KEY or None).messages.create(**kwargs)
    text = "".join(b.text for b in resp.content if b.type == "text")
    calls = [{"id": b.id, "name": b.name, "input": b.input} for b in resp.content if b.type == "tool_use"]
    return {"role": "assistant", "text": text, "tool_calls": calls}


# ---------- Amazon Bedrock (Converse API) ----------
def _bedrock_client():
    import boto3
    return boto3.client("bedrock-runtime", region_name=REGION)


def _bedrock(system, history, tools):
    msgs = []
    for m in history:
        if m["role"] == "user":
            msgs.append({"role": "user", "content": [{"text": m["text"]}]})
        elif m["role"] == "assistant":
            c = [{"text": m["text"]}] if m["text"] else []
            c += [{"toolUse": {"toolUseId": t["id"], "name": t["name"], "input": t["input"]}}
                  for t in m["tool_calls"]]
            msgs.append({"role": "assistant", "content": c})
        else:
            c = [{"toolResult": {"toolUseId": r["id"], "content": [{"text": r["text"]}]}} for r in m["results"]]
            if m.get("extra_text"):
                c.append({"text": m["extra_text"]})
            msgs.append({"role": "user", "content": c})
    kwargs = {"modelId": MODEL, "system": [{"text": system}], "messages": msgs,
              "inferenceConfig": {"maxTokens": MAX_TOKENS, "temperature": 0.2}}
    if tools:
        kwargs["toolConfig"] = {"tools": [{"toolSpec": {
            "name": t["name"], "description": t["description"], "inputSchema": {"json": t["schema"]}}}
            for t in tools]}
    resp = _bedrock_client().converse(**kwargs)
    blocks = resp["output"]["message"]["content"]
    text = " ".join(b["text"] for b in blocks if "text" in b)
    calls = [{"id": b["toolUse"]["toolUseId"], "name": b["toolUse"]["name"], "input": b["toolUse"]["input"]}
             for b in blocks if "toolUse" in b]
    return {"role": "assistant", "text": text, "tool_calls": calls}


# ---------- Vision: read a photo (used for medicine-bottle labels) ----------
async def vision(image: bytes, media_type: str, prompt: str) -> str:
    """Send one image plus an instruction to the configured provider and return its text reply.
    No tools are offered, so text printed on the photo can only ever influence the returned text."""
    import base64
    if PROVIDER == "openai_compat":
        url = f"data:{media_type};base64,{base64.b64encode(image).decode()}"
        body = {"model": MODEL, "max_tokens": 500, "temperature": 0, "messages": [{"role": "user", "content": [
            {"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": url}}]}]}
        async with httpx.AsyncClient(timeout=90) as client:
            r = await client.post(f"{BASE_URL}/chat/completions", json=body, headers={"Authorization": f"Bearer {API_KEY}"})
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code} from {BASE_URL}: {r.text[:300]}")
        return r.json()["choices"][0]["message"].get("content") or ""
    if PROVIDER == "anthropic":
        def run():
            import anthropic
            resp = anthropic.Anthropic(api_key=API_KEY or None).messages.create(
                model=MODEL, max_tokens=500, messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type,
                                                 "data": base64.b64encode(image).decode()}},
                    {"type": "text", "text": prompt}]}])
            return "".join(b.text for b in resp.content if b.type == "text")
        return await asyncio.to_thread(run)
    if PROVIDER == "bedrock":
        fmt = {"image/jpeg": "jpeg", "image/png": "png", "image/webp": "webp"}[media_type]

        def run():
            resp = _bedrock_client().converse(
                modelId=MODEL, messages=[{"role": "user", "content": [
                    {"image": {"format": fmt, "source": {"bytes": image}}}, {"text": prompt}]}],
                inferenceConfig={"maxTokens": 500, "temperature": 0})
            return " ".join(b["text"] for b in resp["output"]["message"]["content"] if "text" in b)
        return await asyncio.to_thread(run)
    raise ValueError(f"Unknown LLM_PROVIDER: {PROVIDER}")
