"""
OpenAI and xAI (Grok) as alternatives to Gemini for the bot's text generation. Both speak the "chat completions" protocol, so one adapter serves both.
The engine keeps talking in Gemini's format (contents / parts / functionCall); this module converts it to chat-completions messages and tools,
and converts the answer (text pieces and tool calls) back to the same events the engine already understands.
Embeddings (the knowledge search) always stay with Google: the stored vectors are Gemini's.
"""
import asyncio
import json
import re
import time
from typing import Any, AsyncGenerator, Dict, List, Optional

import httpx
from http_client import shared_client

BASES = {"openai": "https://api.openai.com/v1", "xai": "https://api.x.ai/v1", "groq": "https://api.groq.com/openai/v1"}
LABELS = {"google": "Google Gemini", "openai": "OpenAI", "xai": "xAI Grok", "groq": "Groq"}
DEFAULT_MODELS = {"openai": ["gpt-4.1-mini", "gpt-4o-mini"], "xai": ["grok-3-mini", "grok-3"], "groq": ["openai/gpt-oss-20b", "openai/gpt-oss-120b"]}
_NOT_CHAT = ("allam", "embedding", "audio", "tts", "whisper", "image", "dall", "realtime", "transcribe", "moderation", "guard", "playai", "orpheus", "distil-whisper", "search", "imagine", "davinci", "babbage", "computer", "codex", "instruct", "vision-preview")
_CHAT_PREFIX = {"openai": ("gpt-", "o1", "o3", "o4", "chatgpt-"), "xai": ("grok",), "groq": ("",)}


# a model that answered "no" for good (wrong name, no tool support, too small, no access) is skipped for a while instead of failing on every question
_BAD: Dict[tuple, float] = {}


def _usable(kind: str, model: str) -> bool:
    return _BAD.get((kind, model), 0) < time.time()


def _mark_bad(kind: str, model: str, status: int) -> None:
    _BAD[(kind, model)] = time.time() + (600 if status in (400, 401, 403, 404, 413) else 30 if status in (429, 500, 502, 503) else 0)


def _lower_schema(x: Any) -> Any:
    if isinstance(x, dict):
        return {k: (v.lower() if k == "type" and isinstance(v, str) else _lower_schema(v)) for k, v in x.items()}
    if isinstance(x, list):
        return [_lower_schema(i) for i in x]
    return x


def to_messages(turns: List[dict]) -> List[dict]:
    """Gemini turns -> chat-completions messages (a model turn with functionCall -> assistant tool_calls; functionResponse -> tool messages)."""
    msgs: List[dict] = []
    pending: List[str] = []
    n = 0
    for t in turns:
        parts = t.get("parts") or []
        texts = "".join(p.get("text", "") for p in parts if isinstance(p, dict) and "text" in p)
        calls = [p["functionCall"] for p in parts if isinstance(p, dict) and p.get("functionCall")]
        resps = [p["functionResponse"] for p in parts if isinstance(p, dict) and p.get("functionResponse")]
        if resps:
            for r in resps:
                cid = pending.pop(0) if pending else f"call_x{n}"
                res = (r.get("response") or {}).get("result", r.get("response"))
                msgs.append({"role": "tool", "tool_call_id": cid, "content": res if isinstance(res, str) else json.dumps(res, ensure_ascii=False)})
        elif calls:
            tcs = []
            for c in calls:
                n += 1
                cid = f"call_{n}"
                pending.append(cid)
                tcs.append({"id": cid, "type": "function", "function": {"name": c.get("name", ""), "arguments": json.dumps(c.get("args") or {}, ensure_ascii=False)}})
            msgs.append({"role": "assistant", "content": texts or None, "tool_calls": tcs})
        else:
            msgs.append({"role": "assistant" if t.get("role") == "model" else "user", "content": texts})
    return msgs


def to_tools(tools: Optional[List[dict]]) -> List[dict]:
    out = []
    for g in tools or []:
        for d in g.get("functionDeclarations", []):
            out.append({"type": "function", "function": {"name": d["name"], "description": d.get("description", ""), "parameters": _lower_schema(d.get("parameters") or {"type": "object", "properties": {}})}})
    return out


def _body(kind: str, model: str, payload: dict, stream: bool) -> dict:
    gc = payload.get("generationConfig") or {}
    maxt = int(gc.get("maxOutputTokens") or 800)
    reasoning = kind == "openai" and bool(re.match(r"^(o\d|gpt-5)", model))      # these models think first and take no temperature
    body: Dict[str, Any] = {"model": model, "messages": to_messages(payload.get("contents") or []), "stream": stream}
    if kind == "openai":
        body["max_completion_tokens"] = maxt * 5 if reasoning else maxt
    else:
        thinking = kind == "groq" and ("gpt-oss" in model or "qwen3" in model)       # reasoning models: they need room to think
        body["max_tokens"] = maxt * 4 if thinking else maxt
        if kind == "groq" and "gpt-oss" in model:
            body["reasoning_effort"] = "low"
        elif kind == "groq" and "qwen3" in model:
            body["reasoning_effort"] = "none"
    if not reasoning and "temperature" in gc:
        body["temperature"] = gc["temperature"]
    tools = to_tools(payload.get("tools"))
    if tools:
        body["tools"] = tools
        forced = ((payload.get("toolConfig") or {}).get("functionCallingConfig") or {})
        names = forced.get("allowedFunctionNames") or []
        if forced.get("mode") == "ANY" and names:
            body["tool_choice"] = {"type": "function", "function": {"name": names[0]}}
    return body


def _loads(s: str) -> dict:
    try:
        v = json.loads(s or "{}")
        return v if isinstance(v, dict) else {}
    except Exception:
        return {}


async def oa_stream(kind: str, key: str, model: str, payload: dict) -> AsyncGenerator[tuple, None]:
    """("text", piece) as it arrives, then ("calls", [functionCall parts]). Ends silently on any failure."""
    calls: Dict[int, Dict[str, str]] = {}
    try:
        async with shared_client(30.0) as client:
            async with client.stream("POST", BASES[kind] + "/chat/completions", json=_body(kind, model, payload, True),
                                     headers={"Authorization": "Bearer " + key}, timeout=httpx.Timeout(connect=6.0, read=25.0, write=10.0, pool=6.0)) as r:
                if r.status_code != 200:
                    print(f"[{kind}] {model} -> {r.status_code}: {(await r.aread()).decode('utf-8', errors='ignore')[:300]}")
                    _mark_bad(kind, model, r.status_code)
                    return
                async for line in r.aiter_lines():
                    if not line.startswith("data:"):
                        continue
                    d = line[5:].strip()
                    if d == "[DONE]":
                        break
                    try:
                        delta = json.loads(d)["choices"][0]["delta"]
                    except Exception:
                        continue
                    if delta.get("content"):
                        yield ("text", delta["content"])
                    for tc in delta.get("tool_calls") or []:
                        e = calls.setdefault(tc.get("index", 0), {"name": "", "args": ""})
                        fn = tc.get("function") or {}
                        if fn.get("name"):
                            e["name"] = fn["name"]
                        e["args"] += fn.get("arguments") or ""
        if calls:
            yield ("calls", [{"functionCall": {"name": e["name"], "args": _loads(e["args"])}} for e in calls.values() if e["name"]])
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[{kind}] stream error with {model}: {type(e).__name__}: {e}")


async def stream_with_fallback(cfg: dict, payload: dict) -> AsyncGenerator[tuple, None]:
    """The models of the chosen provider in order (no parallel asking: this costs money). Stops at the first one that produces anything."""
    for model in cfg["models"]:
        if not _usable(cfg["kind"], model):
            continue
        got = False
        async for ev in oa_stream(cfg["kind"], cfg["key"], model, payload):
            got = True
            yield ev
        if got:
            return


async def oa_generate(cfg: dict, payload: dict) -> Optional[dict]:
    """A single non-streamed call; returns a Gemini-shaped content {"parts": [...]} or None."""
    for model in cfg["models"]:
        if not _usable(cfg["kind"], model):
            continue
        try:
            async with shared_client(30.0) as client:
                r = await client.post(BASES[cfg["kind"]] + "/chat/completions", json=_body(cfg["kind"], model, payload, False),
                                      headers={"Authorization": "Bearer " + cfg["key"]}, timeout=httpx.Timeout(connect=6.0, read=30.0, write=10.0, pool=6.0))
            if r.status_code != 200:
                print(f"[{cfg['kind']}] {model} -> {r.status_code}: {r.text[:300]}")
                _mark_bad(cfg["kind"], model, r.status_code)
                continue
            msg = (r.json().get("choices") or [{}])[0].get("message") or {}
            parts: List[dict] = []
            if msg.get("content"):
                parts.append({"text": msg["content"]})
            for tc in msg.get("tool_calls") or []:
                fn = tc.get("function") or {}
                if fn.get("name"):
                    parts.append({"functionCall": {"name": fn["name"], "args": _loads(fn.get("arguments"))}})
            if parts:
                return {"parts": parts}
        except asyncio.CancelledError:
            raise
        except Exception as e:
            print(f"[{cfg['kind']}] call error with {model}: {type(e).__name__}: {e}")
    return None


async def list_models(kind: str, key: str) -> Dict[str, Any]:
    """The chat models of this key (for the admin's picker)."""
    try:
        async with httpx.AsyncClient(timeout=15.0) as c:
            r = await c.get(BASES[kind] + "/models", headers={"Authorization": "Bearer " + key})
        if r.status_code != 200:
            return {"success": False, "status_code": r.status_code, "error": r.text[:300]}
        ids = sorted({m.get("id", "") for m in r.json().get("data", [])})
        names = [i for i in ids if i.startswith(_CHAT_PREFIX[kind]) and not any(x in i.lower() for x in _NOT_CHAT)]
        return {"success": True, "models": [{"name": n, "display_name": n} for n in names]}
    except Exception as e:
        return {"success": False, "status_code": None, "error": f"{type(e).__name__}: {e}"}


async def test_model(kind: str, key: str, model: str) -> Dict[str, Any]:
    """A real tiny request with this key and model: does it answer, and how fast."""
    payload = {"contents": [{"role": "user", "parts": [{"text": "Reply with one word: OK"}]}], "generationConfig": {"maxOutputTokens": 40, "temperature": 0}}
    t0 = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=25.0) as c:
            r = await c.post(BASES[kind] + "/chat/completions", json=_body(kind, model, payload, False), headers={"Authorization": "Bearer " + key})
        ms = round((time.monotonic() - t0) * 1000)
        if r.status_code == 200:
            return {"success": True, "status_code": 200, "latency_ms": ms, "model_used": model}
        return {"success": False, "status_code": r.status_code, "latency_ms": ms, "model_used": model, "error": r.text[:400]}
    except Exception as e:
        return {"success": False, "status_code": None, "latency_ms": round((time.monotonic() - t0) * 1000), "model_used": model, "error": f"{type(e).__name__}: {e}"}
