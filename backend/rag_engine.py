import asyncio
import base64
import contextvars
import json
import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, AsyncGenerator, Optional
import httpx
from http_client import shared_client
import ai_providers
import gemini_stats
from hebdate import hebrew_date_str
from ui_blocks import UI_ADDENDUM, UI_TOOLS, resolve_blocks, encode_blocks, ui_text
import prompt_store as PS

from db import db

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# =====================================================================
# מנגנון תורים (Queue) למניעת חסימות Rate-Limit ב-API החינמי של גוגל
# מאפשר עד 2 בקשות מקבילות ל-Gemini בו-זמנית; השאר ממתינים בתור מסודר
# =====================================================================
gemini_queue_semaphore = asyncio.Semaphore(8)

# =====================================================================
# System Prompt קשיח למניעת הזיות (Anti-Hallucination)
# =====================================================================
def build_system_prompt(bot_identity: str = "", catalog: Optional[List[Dict[str, str]]] = None) -> str:
    """
    בונה את ה-System Prompt. מידע הזהות של הבוט (מי הוא, מטעם מי, מה הוא יודע לעשות)
    מוזרק תמיד, בלי קשר לתוצאות ה-RAG - כדי שהבוט ידע לענות על "מי אתה" בעקביות,
    ולא רק כשהמידע הזה במקרה עלה בחיפוש הדמיון. אותו דבר לגבי הקטלוג (ה"מפה") -
    רשימת הנושאים הקיימים במאגר הידע, שמתעדכנת ע"י AI בכל הוספת מידע ומוצגת תמיד למודל.
    """
    identity_block = ""
    if bot_identity and bot_identity.strip():
        identity_block = f"""
=== מידע בסיסי על עצמך (זמין תמיד, לא תלוי בשאלה) ===
{bot_identity.strip()}
========================================================
"""

    catalog_block = ""
    if catalog:
        catalog_lines = "\n".join(f"- {c['category']}: {c['summary']}" for c in catalog)
        catalog_block = f"""
=== מפת הנושאים הקיימים במאגר הידע (לידיעתך - לא להציג כמו שהוא לסטודנט) ===
{catalog_lines}
=============================================================================
"""

    return f"""{PS.text("persona")}
{identity_block}{catalog_block}
{PS.text("rules")}"""


PERSONA_DEFAULT = """אתה העוזר האקדמי הרשמי והחכם של המכללה.
תפקידך לענות על שאלות של סטודנטים ("למי פונים כש...", "מה עושים כש...") בצורה אדיבה, ברורה ותמציתית בעברית."""

RULES_DEFAULT = """חוקי ברזל שאסור לך להפר בשום מצב:
1. על שאלות הנוגעות לנהלי המכללה - ענה אך ורק על סמך "מידע המכללה הרלוונטי" המצורף למטה. על שאלות הנוגעות אליך עצמך (מי אתה, מה שמך, מטעם מי אתה פועל, מה אתה יודע לעשות) - ענה אך ורק על סמך "מידע בסיסי על עצמך" שמופיע למעלה, אם סופק.
2. אל תמציא שום מידע, מספרי טלפון, שמות, חדרים, שעות פעילות או נהלים שאינם כתובים במפורש במידע המצורף!
3. אם התשובה לשאלת הסטודנט אינה מופיעה באף אחד מהמקורות המצורפים, אמור בנימוס: "אין לי מידע רשמי על כך בנהלי המכללה המעודכנים במערכת. מומלץ לפנות למזכירות הסטודנטים לבירור."
4. שמור על תשובות מסודרות ונעימות לקריאה (השתמש בנקודות במידת הצורך).
5. אל תשתמש באימוג'ים או בסמלי אימוג'י בתשובות - כתוב טקסט נקי בלבד (האפליקציה מציגה אייקונים משלה).
6. פתח ישר בתוכן התשובה. אל תפתח ב"בדקתי", "בדקתי לך", "בדקתי במאגר" או בתיאור של מה שעשית - הסטודנט רואה בעצמו שבדקת. אם אין מידע, אמור זאת במשפט ישיר וקצר.
"""


def chunk_text(text: str, title: str = "מסמך מכללה", chunk_size: int = 600, overlap: int = 100) -> List[Dict[str, Any]]:
    """
    חותך טקסט ארוך שהמנהל מעלה למקטעים (Chunks) עם חפיפה (Overlap),
    כדי שמשפטים והקשרים לא ייקטעו באמצע.
    """
    cleaned = text.strip()
    if not cleaned:
        return []

    chunks = []
    start = 0
    part = 1
    while start < len(cleaned):
        end = min(start + chunk_size, len(cleaned))
        # ננסה לחתוך בסוף משפט או שורה חדשה אם אפשר
        if end < len(cleaned):
            last_period = max(cleaned.rfind(".", start, end), cleaned.rfind("\n", start, end))
            if last_period > start + (chunk_size // 2):
                end = last_period + 1

        chunk_str = cleaned[start:end].strip()
        if chunk_str:
            chunks.append({
                "title": f"{title} (חלק {part})" if len(cleaned) > chunk_size else title,
                "text": chunk_str,
            })
            part += 1

        if end >= len(cleaned):
            break
        start = max(start + 1, end - overlap)

    return chunks


async def get_embedding(text: str, api_key: str) -> List[float]:
    """
    הופך טקסט לוקטור (Embedding) באמצעות ה-API של Gemini.
    מבקש embedding מוקטן ל-768 מימדים (output_dimensionality) במקום 3072 המלאים -
    זה בתוך המגבלה שבה pgvector עדיין יודע לבנות אינדקס HNSW מהיר (המגבלה היא 2000).
    אם אין מפתח או שיש תקלה זמנית, מחזיר רשימה ריקה.
    """
    if not api_key:
        return []

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={api_key}"
    payload = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": 768,
    }
    # עד 3 ניסיונות: שגיאות זמניות (429 = עומס/מכסה, 5xx = תקלה בצד גוגל) מנוסות שוב
    # עם השהיה גדלה, במקום להיכשל בשקט ולשמור מקטע בלי וקטור.
    for attempt in range(3):
        try:
            db.bump_api_usage("embedding")
            async with shared_client(15.0) as client:
                resp = await client.post(url, json=payload)
            gemini_stats.record("embedding", resp.status_code)
            if resp.status_code == 200:
                return resp.json().get("embedding", {}).get("values", [])
            if resp.status_code not in (429, 500, 502, 503, 504):
                break  # שגיאה קבועה (מפתח לא תקין וכד') - אין טעם לנסות שוב
        except Exception:
            pass
        if attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))
    return []


# "gemini-flash-latest" וכו' הם כינויים (aliases) שגוגל מצמידה תמיד למודל הנוכחי,
# כך שלא צריך לעדכן שם מודל קשיח בקוד כל פעם שגוגל משיקה גרסה חדשה.
# ה-"lite" מודל ראשון בכוונה: בבדיקה בפועל (ראה שיחה) התגלה שלמפתח חינמי יש מכסה יומית
# נמוכה מאוד (20 בקשות/יום) על המודל הכבד (gemini-flash-latest), בעוד שהגרסה ה-lite
# עדיין הייתה זמינה - אז היא סיכוי טוב יותר להצלחה בפועל במכסה חינמית.
# Measured: the lite models answer in ~2s, while the heavier flash/pro models are often overloaded (503) or hang for 30-40s. So several lite
# models come first (they have separate capacity), the heavy ones are only the last resort, and every model gets a short timeout.
DEFAULT_MODELS_TO_TRY = ["gemini-flash-lite-latest", "gemini-3.5-flash-lite", "gemini-3.1-flash-lite", "gemini-flash-latest", "gemini-pro-latest"]


def _model_timeout(model: str) -> "httpx.Timeout":
    read = 11.0 if "lite" in model else (28.0 if "pro" in model else 18.0)      # how long a model may stay silent before the next one is tried
    return httpx.Timeout(connect=5.0, read=read, write=10.0, pool=5.0)


async def _stream_from_gemini(
    contents,
    api_key: str,
    models_to_try: List[str] = None,
    max_output_tokens: int = 600,
    call_type: str = "other",
) -> AsyncGenerator[str, None]:
    """A plain streamed answer (no tools): the same hedged, fail-fast engine as the chat. `contents` is a prompt string or ready turns."""
    async for kind, value in _stream_gemini_events(contents, api_key, tools=None, max_output_tokens=max_output_tokens, call_type=call_type, models=models_to_try):
        if kind == "text":
            yield value
        elif kind == "failed":
            yield "המערכת בעומס רגעי מול שרתי ה-AI או שמפתח ה-API אינו זמין. אנא נסה שוב בעוד מספר שניות."


# The speed of each model changes through the day (Google's free capacity): what the server learned about it recently decides who is asked first.
_SPEED: Dict[str, float] = {}


def _speed_note(model: str, secs: float) -> None:
    old = _SPEED.get(model, 4.0)
    _SPEED[model] = round(0.6 * old + 0.4 * min(float(secs), 30.0), 2)


def _ordered(models: List[str]) -> List[str]:
    return sorted(models, key=lambda m: _SPEED.get(m, 4.0) + (0.0 if "lite" in m else 6.0))


async def _model_order(models: Optional[List[str]] = None) -> List[str]:
    """Who is asked, in order. The admin's list (main model first) is used exactly as chosen; the built-in models follow only as a safety net.
    Without an admin choice: the built-in models, fastest recently first."""
    if models:
        return list(models)
    try:
        cfg = await db.get_gemini_models()
    except Exception:
        cfg = None
    if cfg:
        return list(cfg) + [m for m in _ordered(list(DEFAULT_MODELS_TO_TRY)) if m not in cfg]
    return _ordered(list(DEFAULT_MODELS_TO_TRY))


HEDGE_AFTER = 5.0            # a model that has not answered after this many seconds is not waited for alone: the next one is asked too, the first to answer wins
ATTEMPT_LIMIT = 22.0         # no answer at all from anyone within this time: that round is given up


async def _one_model_stream(model_name: str, payload: dict, api_key: str, call_type: str, personal: Optional[str] = None):
    """One request to one model (no retry here). Yields ("text", piece) / ("calls", [functionCall...]); simply ends when it fails or has nothing."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:streamGenerateContent?alt=sse&key={api_key}"
    calls: List[dict] = []
    try:
        if personal:
            db.bump_user_ai(personal, "google/" + model_name)                      # the student's own calls: counted for them only, not in the public numbers
        else:
            db.bump_api_usage(call_type)
            db.bump_api_usage("model:google/" + model_name)
        async with shared_client(30.0) as client:
            async with client.stream("POST", url, json=payload, timeout=_model_timeout(model_name)) as response:
                gemini_stats.record(model_name, response.status_code)
                if response.status_code == 200:
                    async for line in response.aiter_lines():
                        if not line.startswith("data: "):
                            continue
                        data_str = line[6:].strip()
                        if not data_str or data_str == "[DONE]":
                            continue
                        try:
                            cand = json.loads(data_str).get("candidates", [])
                        except Exception:
                            continue
                        if not cand:
                            continue
                        for part in cand[0].get("content", {}).get("parts", []):
                            if not isinstance(part, dict):
                                continue
                            if part.get("functionCall"):
                                calls.append(part)
                            elif part.get("text"):
                                yield ("text", part["text"])
                    if calls:
                        yield ("calls", calls)
                    return
                if response.status_code in (400, 403):
                    err = (await response.aread()).decode("utf-8", errors="ignore")
                    if "API_KEY_INVALID" in err or response.status_code == 403:
                        yield ("text", "שגיאה: מפתח ה-API של Gemini שהוזן בפאנל הניהול אינו תקין. אנא בדוק אותו במסך המנהל.")
                        return
                    print(f"[chat] Gemini {response.status_code} with {model_name}: {err[:300]}")
                # 429 / 5xx / schema error: this model gave nothing
    except asyncio.CancelledError:
        raise
    except Exception as e:
        print(f"[chat] Gemini stream error with {model_name}: {type(e).__name__}: {e}")


def _gemini_safe(turns: List[dict]) -> List[dict]:
    """A tool call that another provider made (OpenAI / Groq / Grok) has no thought signature; Gemini 3 refuses history without one, so the documented dummy value is added."""
    out = []
    for t in turns:
        parts = t.get("parts") or []
        if t.get("role") == "model" and any(isinstance(x, dict) and x.get("functionCall") and "thoughtSignature" not in x for x in parts):
            parts = [({**x, "thoughtSignature": "skip_thought_signature_validator"} if isinstance(x, dict) and x.get("functionCall") and "thoughtSignature" not in x else x) for x in parts]
            t = {**t, "parts": parts}
        out.append(t)
    return out


# the student's own key for THIS request (set by the chat endpoint); None = the public setup
_USER_AI: contextvars.ContextVar = contextvars.ContextVar("user_ai", default=None)


async def _other_provider():
    """The chosen non-Google provider with its key and models, or None (then Google is used)."""
    ua = _USER_AI.get()
    if ua and ua.get("kind") in ai_providers.BASES:
        chosen = ua.get("models") or []
        return {"kind": ua["kind"], "key": ua["key"], "models": chosen + [m for m in ai_providers.DEFAULT_MODELS[ua["kind"]] if m not in chosen], "user_id": ua.get("user_id")}
    try:
        cfg = await db.get_ai_provider_cfg()
    except Exception:
        return None
    kind = cfg.get("active")
    if kind in ai_providers.BASES and cfg.get(kind + "_key"):
        chosen = cfg.get(kind + "_models") or []
        return {"kind": kind, "key": cfg[kind + "_key"], "models": chosen + [m for m in ai_providers.DEFAULT_MODELS[kind] if m not in chosen]}
    return None


async def _stream_gemini_events(contents, api_key: str, tools=None, max_output_tokens: int = 800, call_type: str = "chat_answer", models: Optional[List[str]] = None):
    ua = _USER_AI.get()
    if ua and ua.get("kind") == "google":                  # the student's own Google key goes first; if it gives nothing, the public setup answers
        got = False
        async for ev in _stream_gemini_core(contents, ua["key"], tools, max_output_tokens, call_type, ua.get("models") or None, ua.get("user_id")):
            if ev[0] == "failed":
                break
            got = True
            yield ev
        if got:
            return
    async for ev in _stream_gemini_core(contents, api_key, tools, max_output_tokens, call_type, models, None):
        yield ev


async def _stream_gemini_core(contents, api_key: str, tools=None, max_output_tokens: int = 800, call_type: str = "chat_answer", models: Optional[List[str]] = None, personal: Optional[str] = None):
    """
    One streamed call to Gemini (with tools if asked): yields ("text", piece) as it arrives and at the end ("calls", [functionCall...]) if the model asked for tools,
    or ("failed", None) when nobody answered. Speed under load: the models are asked in order, but a model that is slow (HEDGE_AFTER seconds) does not block -
    the next one is asked at the same time and the first to produce anything wins (the others are cancelled). Nothing is repeated once text has started.
    """
    turns = contents if isinstance(contents, list) else [{"role": "user", "parts": [{"text": contents}]}]
    payload = {"contents": turns, "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_output_tokens}}
    if tools:
        payload["tools"] = tools
    other = None if personal else await _other_provider()
    if other:                                                  # the admin chose OpenAI / Grok: they go first; if they give nothing, Google below is the safety net
        got = False
        async for ev in ai_providers.stream_with_fallback(other, payload):
            got = True
            yield ev
        if got:
            return
    order = await _model_order(models)
    if other:
        payload["contents"] = _gemini_safe(payload["contents"])
    async with gemini_queue_semaphore:
        for attempt in range(2):
            q: asyncio.Queue = asyncio.Queue()
            tasks: List[asyncio.Task] = []
            nxt = 0
            active = 0
            started: Dict[str, float] = {}

            async def pump(model_name: str):
                try:
                    async for ev in _one_model_stream(model_name, payload, api_key, call_type, personal):
                        await q.put((model_name, ev))
                finally:
                    await q.put((model_name, None))                    # this model is finished (with or without an answer)

            def start_next():
                nonlocal nxt, active
                if nxt >= len(order):
                    return False
                started[order[nxt]] = time.monotonic()
                tasks.append(asyncio.create_task(pump(order[nxt])))
                nxt += 1
                active += 1
                return True

            start_next()
            winner = None
            t_start = time.monotonic()
            hedge_at = t_start + HEDGE_AFTER
            try:
                while True:
                    wait = None
                    if winner is None:
                        wait = max(0.05, min(hedge_at, t_start + ATTEMPT_LIMIT) - time.monotonic())
                    try:
                        model_name, ev = await asyncio.wait_for(q.get(), wait)
                    except asyncio.TimeoutError:
                        if time.monotonic() >= t_start + ATTEMPT_LIMIT:
                            break                                       # nobody answered in time: give this round up
                        start_next()                                    # slow: ask the next model too, keep waiting for both
                        hedge_at = time.monotonic() + HEDGE_AFTER
                        continue
                    if ev is None:
                        active -= 1
                        if winner is None:
                            if personal:
                                db.bump_user_ai(personal, "google/" + model_name, fail=True)
                            _speed_note(model_name, max(20.0, time.monotonic() - started.get(model_name, time.monotonic())))      # it gave nothing: it counts as slow
                        if winner is not None and model_name == winner:
                            return                                      # the winner finished: done
                        if winner is None and active == 0:
                            if not start_next():
                                break                                   # everyone failed
                            hedge_at = time.monotonic() + HEDGE_AFTER
                        continue
                    if winner is None:
                        winner = model_name                             # the first to produce anything: the others are not needed
                        if not personal:
                            db.bump_api_usage("win:google/" + winner)
                        _speed_note(winner, time.monotonic() - started.get(winner, time.monotonic()))
                        for t, m in zip(tasks, order[:len(tasks)]):
                            if m != winner and not t.done():
                                t.cancel()
                    if model_name == winner:
                        yield ev
            finally:
                for t in tasks:
                    if not t.done():
                        t.cancel()
            if attempt < 1:
                await asyncio.sleep(1.0)
    yield ("failed", None)


async def _generate_once(contents, api_key, tools=None, max_output_tokens=300, call_type="other", tool_config=None, gen_extra=None):
    ua = _USER_AI.get()
    if ua and ua.get("kind") == "google":
        res = await _generate_core(contents, ua["key"], tools, max_output_tokens, call_type, tool_config, gen_extra, ua.get("models") or None, ua.get("user_id"))
        if res:
            return res
    return await _generate_core(contents, api_key, tools, max_output_tokens, call_type, tool_config, gen_extra, None, None)


async def _generate_core(
    contents: List[dict],
    api_key: str,
    tools: Optional[List[dict]] = None,
    max_output_tokens: int = 300,
    call_type: str = "other",
    tool_config: Optional[dict] = None,
    gen_extra: Optional[dict] = None,
    models: Optional[List[str]] = None,
    personal: Optional[str] = None,
) -> Optional[dict]:
    """
    קריאה בודדת (לא מוזרמת) ל-Gemini - משמשת לסיבוב ה"החלטה" של function calling
    (האם ובאיזה כלי Moodle להשתמש). בכוונה בלי ה-retry/backoff המלא של ההזרמה -
    זו קריאה קצרה וזולה, ואם היא נכשלת פשוט חוזרים לנתיב הרגיל בלי כלים.
    """
    payload = {
        "contents": contents,
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_output_tokens},
    }
    if tools:
        payload["tools"] = tools
    if tool_config:
        payload["toolConfig"] = tool_config
    if gen_extra:
        payload["generationConfig"].update(gen_extra)

    other = None if personal else await _other_provider()
    if other:
        res = await ai_providers.oa_generate(other, payload)
        if res:
            return res
    order = await _model_order(models)
    if other:
        payload["contents"] = _gemini_safe(payload["contents"])
    async with gemini_queue_semaphore:
        for model_name in order:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            try:
                if personal:
                    db.bump_user_ai(personal, "google/" + model_name)
                else:
                    db.bump_api_usage(call_type)
                    db.bump_api_usage("model:google/" + model_name)
                async with shared_client(30.0) as client:
                    resp = await client.post(url, json=payload, timeout=_model_timeout(model_name))
                if resp.status_code == 200:
                    if not personal:
                        db.bump_api_usage("win:google/" + model_name)
                    candidates = resp.json().get("candidates", [])
                    return candidates[0].get("content") if candidates else None
                if personal:
                    db.bump_user_ai(personal, "google/" + model_name, fail=True)
                if resp.status_code not in (429, 500, 502, 503, 504):
                    # לא מנסים שוב (שגיאה קבועה, כמו 400 בבעיית סכמה) - אבל מדפיסים כדי שלא
                    # ייפול בשקט לנתיב-בלי-כלים בלי שום עקבה למה (זה בדיוק מה שקרה בבדיקה בפועל)
                    print(f"[moodle-tools] Gemini decision call נכשל ({resp.status_code}): {resp.text[:500]}")
                    return None
            except Exception as e:
                print(f"[moodle-tools] Gemini decision call החריג עם {model_name}: {type(e).__name__}: {e}")
                continue
    return None


_HEBREW_WEEKDAYS = {0: "שני", 1: "שלישי", 2: "רביעי", 3: "חמישי", 4: "שישי", 5: "שבת", 6: "ראשון"}


def _current_israel_time_block() -> str:
    now = datetime.now(ISRAEL_TZ)
    day_name = _HEBREW_WEEKDAYS[now.weekday()]
    return f"יום {day_name}, {now.strftime('%d/%m/%Y %H:%M')} (שעון ישראל), {hebrew_date_str(now.date())}"


_UI_MARK = re.compile(r"\[\[ui:([A-Za-z0-9_\-]+)\]\]")
_FILE_MARK = re.compile(r"\[\[file:[^|\]\n]*\|([^|\]\n]*)[^\]\n]*\]\]")


def _decode_ui_blocks(text: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for m in _UI_MARK.finditer(text or ""):
        try:
            b = m.group(1)
            data = json.loads(base64.urlsafe_b64decode(b + "=" * (-len(b) % 4)).decode("utf-8"))
            out.extend(x for x in data.get("b", []) if isinstance(x, dict))
        except Exception:
            continue
    return out


def _blocks_digest(blocks: List[Dict[str, Any]], limit: int = 900) -> str:
    parts: List[str] = []
    for blk in blocks:
        bits = [str(blk.get("ti") or ""), str(blk.get("su") or ""), str(blk.get("tx") or "")]
        for it in (blk.get("it") or [])[:10]:
            if isinstance(it, dict):
                bits.append(" ".join(str(it.get(k) or "") for k in ("l", "n", "q", "dt", "v", "d", "a") if it.get(k)))
        parts.append(" / ".join(b for b in bits if b.strip()))
    return " | ".join(p for p in parts if p)[:limit]


def _history_text(content: str) -> str:
    """An earlier answer as the model should read it: cards and files as short descriptions instead of base64."""
    text = str(content or "")
    text = _UI_MARK.sub(lambda m: "[הוצגו כרטיסים: " + _blocks_digest(_decode_ui_blocks(m.group(0)), 500) + "]", text)
    text = _FILE_MARK.sub(lambda m: "[נשלח קובץ: " + m.group(1) + "]", text)
    return text


def _build_chat_user_turn(
    question: str,
    context_chunks: List[Dict[str, Any]],
    bot_identity: str,
    catalog: Optional[List[Dict[str, str]]],
    conversation_summary: str,
    recent_messages: Optional[List[Dict[str, str]]],
    username: str = "",
    extra_system_instructions: str = "",
    ui_lang: str = "",
) -> str:
    """מרכיב את תור הפתיחה (system prompt + הקשר RAG + היסטוריה + השאלה) - משותף לנתיב הרגיל ולנתיב Moodle."""
    if context_chunks:
        context_text = "\n\n---\n\n".join(
            [f"מקור: {c.get('title', 'נהלי מכללה')}\nתוכן: {c['text']}" for c in context_chunks]
        )
    else:
        context_text = "לא נמצאו מסמכים במאגר המכללה."

    # מידע בסיסי אמיתי שתמיד זמין למודל - לא תלוי בשליפת RAG - כדי שיוכל לחשב בעצמו
    # דברים כמו "האם המטלה הזו באיחור" (צריך גם תאריך נוכחי וגם תאריך הגשה, לא רק אחד מהם).
    student_name = username if (username and not str(username).startswith("moodle_")) else "לא ידוע"
    lang_name = {"he": "עברית", "en": "English", "fr": "Français"}.get(str(ui_lang or "").lower()[:2], "לא ידוע")
    basic_info_block = f"""
=== מידע בסיסי בזמן אמת (עובדתי ומדויק - אפשר להסתמך עליו לחישובים) ===
שם פרטי של הסטודנט: {student_name}
שפת הממשק של האפליקציה אצלו: {lang_name}
תאריך ושעה נוכחיים: {_current_israel_time_block()}
=========================================================================
"""

    history_block = ""
    if conversation_summary:
        history_block += f"\n=== תקציר השיחה עד כה ===\n{conversation_summary}\n"
    if recent_messages:
        turns = "\n".join(
            f"{'סטודנט' if m['role'] == 'user' else 'בוט'}: {_history_text(m['content']) if m['role'] != 'user' else m['content']}" for m in recent_messages
        )
        history_block += f"\n=== הודעות אחרונות בשיחה ===\n{turns}\n"

    return f"""{build_system_prompt(bot_identity, catalog)}{extra_system_instructions}
{basic_info_block}{history_block}
=== מידע המכללה הרלוונטי לשאלה הנוכחית ===
{context_text}
=============================

שאלת הסטודנט (הנוכחית): {question}{_answer_language_note(question, ui_lang)}
תשובה (בהתבסס אך ורק על המידע המצורף למעלה, ותוך המשכיות עם השיחה אם רלוונטי):"""


def _answer_language_note(question: str, ui_lang: str = "") -> str:
    """Found by testing: asked in English (with the app in English) the bot answered with Hebrew cards: the language rule sat far above, among Hebrew instructions. When the
    question is clearly not Hebrew, the language is said again right at the end, next to the question, where the model follows it."""
    q = str(question or "").split("\n\n[")[0]
    if re.search(r"[א-ת]", q):
        return ""
    if re.search(r"[؀-ۿ]", q):
        name = "العربية (Arabic)"
    elif re.search(r"[A-Za-zÀ-ÿ]", q):
        french = str(ui_lang or "").lower().startswith("fr") or re.search(r"\b(?:combien|comment|bonjour|merci|quand|pourquoi|est-ce|quelle?s?|je|les|des)\b|[àâçéèêëîïôûù]", q, re.I)
        name = "Français" if french else "English"
    else:
        return ""
    return f"\n(השפה של השאלה: {name}. כתוב את התשובה כולה, כולל כל הכרטיסים (כותרות, שורות, הערות וכפתורים), ב-{name} ולא בעברית. מספרי טלפון, מיילים ושמות מקומות נשארים כפי שהם.)"


RICH_ADDENDUM = """

שם: קוראים לך אורי. כשמציגים אותך או שואלים "מי אתה / איך קוראים לך" - אתה אורי, העוזר של המרכז האקדמי לב. אל תציג את עצמך כמודל שפה, כ-Gemini או כ"עוזר AI" כללי.

=== תגובות עשירות באפליקציה ===
אפשר לעצב: **טקסט מודגש** להדגשה (שמות קורסים, תאריכים, מילים חשובות), רשימות עם - או 1. וכותרת קצרה עם ## . אבל תשובה ארוכה מ-3 שורות או מורכבת לא נכתבת כטקסט בכלל: היא מוצגת עם show_ui (כלל הברזל בהמשך). הטקסט הרגיל נשאר לתשובות קצרות ופשוטות.
סקר ותגובות מהירות: כל סימן בשורה משלו, בסוף התשובה, לכל היותר אחד בהודעה, בלי אימוג'י, ובלי התווים | או ] בתוך הטקסטים:
[[poll:multi|השאלה|אפשרות 1|אפשרות 2|אפשרות 3]]   - בחירה מרובה (2 עד 12 אפשרויות)
[[poll:single|השאלה|אפשרות 1|אפשרות 2]]   - בחירה אחת
[[quick|כן|לא]]   - כפתורי תשובה מהירה (2 עד 4)
הסטודנט מקיש, ותשובתו מגיעה אליך כהודעה חדשה שמתחילה ב"בחרתי: " (או "I chose: "). כתוב לפני הסקר משפט קצר כמו "סמן את מה שנכון" ואל תחזור על האפשרויות בטקסט.
מתי להשתמש: כשצריך לברר משהו שקל לענות עליו בלחיצה. למשל, אחרי שהצגת את רשימת הקורסים של הסטודנט אתה יכול ביוזמתך לשאול ב-poll:multi אילו מהם באמת שלו (כולל אפשרות "כולם" אם מתאים).
כשהתשובה מגיעה, עדכן את הזיכרון: כל קורס שלא סומן הוא קורס שהסטודנט לא לומד - שמור אותו עם remember_about_student ו-kind="left_course" (קריאה אחת לכל קורס, בלי לשמור את מה שסומן). אשר בקצרה מה עודכן.
חשוב: "לא לומד קורס X" נשמר רק כ-kind="left_course" עם course=שם הקורס המדויק, אף פעם לא כ-fact רגיל. נסח את שאלת הסקר בצורה חד-משמעית ("אילו מהקורסים האלה אתה לומד עכשיו?"), ואם הסטודנט סימן את כל הקורסים - אל תשמור כלום.
זיכרון הסקרים - חשוב: אחרי שסקר הקורסים נענה, שמור גם עובדה אחת עם remember_about_student (kind="fact") בנוסח קבוע: "אימת אילו קורסים פעילים אצלו (סקר)". אם העובדה הזו כבר ברשימת מה שאתה זוכר, או שכבר שאלת את אותו סקר בשיחה הזו - אל תשלח אותו שוב, גם אם הסטודנט שואל שוב "מה הקורסים שלי": פשוט ענה לפי מה שאתה יודע (הקורסים שהוא עזב כבר מוסתרים אוטומטית). חזור לשאול רק אם הסטודנט עצמו אומר שמשהו השתנה או מבקש לעדכן את הרשימה. הכלל חל על כל סקר: אל תשאל שוב על מה שכבר נענה, בשיחה או בזיכרון.
כלל: הסטודנט לא אמור להקליד כדי להמשיך. אל תסיים תשובה בשאלה פתוחה בטקסט ("רוצה שאסביר?", "מה מעניין אותך?") ואל תבקש ממנו לענות "כן" או "לא" - כל שאלה שלך אליו מוצגת ככפתורים (quick) או כסקר (poll), עם אפשרויות קונקרטיות שקשורות למה שהוא שאל. כשחסר לך פרט כדי לענות (איזה קורס? איזה קמפוס? איזו שנה?) - שאל בסקר או בכפתורים, אל תנחש. בסוף כל תשובה מוסיפים כפתורי המשך (ראה "כפתורי המשך"); poll או quick נוספים רק כשיש שאלה ממוקדת שאתה צריך את תשובתה.
אל תציף בסקרים: רק כשזה באמת חוסך הקלדה או מבהיר משהו, לא בכל תשובה ולא על מידע רגיש. אל תחזור על רשימת האפשרויות בטקסט שמעל הסקר.
"""

PARALLEL_ADDENDUM = """

=== כמה נושאים ומקורות בשאלה אחת ===
סטודנט שואל לא פעם על כמה דברים בבת אחת ("דרכי הגעה ומדיניות המעונות", "כמה עולים המעונות ומה המטלות שלי השבוע"). אז פורקים את השאלה לנושאים וקוראים לכל מה שצריך באותו צעד, במקביל, ולא אחד אחרי השני:
1. לכל נושא של המכללה - קריאה נפרדת ל-search_college_info עם שאילתה ממוקדת בנושא הזה בלבד. שאילתה אחת לכל נושא; לעולם לא שני נושאים בשאילתה אחת, כי אז החיפוש מחזיר רק חלק מהמידע.
2. אם השאלה כוללת גם מידע אישי מ-Moodle (מטלות, ציונים, מבחנים, קורסים, קבצים) - קרא לכלי ה-Moodle המתאים באותו צעד, יחד עם החיפושים (אלא אם נתוני Moodle כבר מצורפים לך למעלה).
3. עד שש קריאות בצעד. אל תקרא ל-show_ui באותו צעד עם כלי נתונים: קודם הנתונים, ואז show_ui אחד שמכסה את כל הנושאים.
4. אם חומר המכללה שמצורף למעלה (נשלף מראש) מכסה רק חלק מהנושאים - חפש את החסרים בלבד. נושא שכבר מכוסה היטב לא מחפשים שוב.
5. עונים על כל הנושאים שנשאלו, כל נושא בבלוק או בקבוצת בלוקים משלו עם כותרת קצרה, והמידע האישי לצד המידע של המכללה באותה תשובה. אסור להשמיט נושא; אם על נושא אין מידע - אמור זאת בקצרה, ואל תמלא ממה שאתה זוכר.
"""

FOLLOWUPS_ADDENDUM = """

=== כפתורי המשך: אתה שואל את הסטודנט מה הלאה ===
כל תשובה מסתיימת בהצעה להמשך, והסטודנט לא צריך להקליד. אחרי כל תשובה (כרטיסים או טקסט) הוסף 2 עד 4 כפתורי המשך, בניסוח שהסטודנט עצמו היה כותב (גוף ראשון, עד 4 מילים, עד 28 תווים), למשל "איך נרשמים", "מה שעות המזכירות", "עוד על המעונות".
- בתשובה עם show_ui: בלוק buttons אחרון באותה קריאה (בכל שורה label=השאלה). בתשובה בטקסט: [[quick|שאלה 1|שאלה 2|שאלה 3]] בשורה משלה, בסוף.
- הכפתורים קשורים ישירות למה שנשאל ולמה שענית, ומובילים למידע שיש לך לענות עליו (מהמכללה, או מ-Moodle האישי שלו כשהוא מחובר). אם נושא מהשאלה נשאר בלי מענה מלא - אחד הכפתורים מבקש בדיוק אותו.
- אסור: כן/לא, "יש לי שאלה נוספת", "תודה", שאלה כללית, וחזרה על מה שכבר נענה. בלי כפתורים בתשובה לברכה או לתודה, ובלי כפתורים כשכבר שלחת סקר (poll) או שאלה ממוקדת באותה תשובה.
- הסעיף הזה גובר על כל אמירה קודמת בפרומפט שלפיה השרת מוסיף כפתורי המשך בעצמו או שאין צורך להוסיף אותם: אתה מוסיף אותם (השרת מוסיף כפתורים רק אם שכחת).
- כשחסר לך פרט כדי לענות (איזה קורס? איזה קמפוס? איזו שנה?) - שאל בסקר או בכפתורי quick במקום לנחש.
"""

ITEM_CTX_ADDENDUM = """

=== הסטודנט לחץ על פריט בכרטיס ===
בסוף ההודעה יכול להופיע בלוק "[הסטודנט לחץ על ...]" עם הפריט שהסטודנט לחץ עליו בכרטיס שבתשובה קודמת (הטקסט של הפריט, ושם הכרטיס שהוא בתוכו). זו שאלה על הפריט הזה בלבד, והוא הנושא של השיחה עכשיו.
- ענה על הפריט עצמו, בקצרה ובמדויק. אל תחזור על מה שכבר כתוב בכרטיס: הוסף מה שחסר שם (הסבר, פרטים, הקשר, מה עושים בפועל, למי פונים).
- כשצריך מידע מהמכללה - חפש עם search_college_info לפי שם הפריט (שאילתה קצרה וממוקדת), ואל תענה מהזיכרון. אם הפריט הוא נתון אישי מ-Moodle - השתמש בכלי Moodle.
- אל תשאל "למה התכוונת": ההקשר נתון. אם אין לך מידע נוסף מעבר למה שכתוב בפריט - אמור זאת בקצרה והצע המשך.
"""

# The question goes straight to the model; it decides if it needs the college's information (this tool), Moodle data (the Moodle tools), or nothing.
SEARCH_DECL = {
    "name": "search_college_info",
    "description": "מחפש במאגר המידע הרשמי של המכללה (נהלים, שכר לימוד, מעונות, שעות פעילות, אנשי קשר, קמפוסים, לוח שנה, מלגות, רישום ועוד) ומחזיר את הקטעים הרלוונטיים. חובה לקרוא לו לפני כל תשובה על המכללה עצמה.",
    "parameters": {"type": "OBJECT", "properties": {"query": {"type": "STRING", "description": "מה לחפש, בעברית, בניסוח קצר וברור של הנושא (למשל: שכר לימוד תואר ראשון, שעות מזכירות, ערעור על ציון)"}}, "required": ["query"]},
}

KB_TOOL_NOTE = """

=== מידע על המכללה: חיפוש לפי צורך ===
מידע על המכללה אינו מצורף מראש. כל שאלה על המכללה עצמה (נהלים, מחירים, מעונות, שעות, אנשי קשר, קמפוסים, מלגות, רישום, לוח שנה) - קודם קרא לכלי search_college_info, ורק אחרי שחזר ענה לפי מה שהוא החזיר ובדיוק לפיו (אל תענה על המכללה מהזיכרון, ואל תקרא ל-show_ui באותו צעד עם החיפוש). שאלה אישית על Moodle, הודעת פתיחה או תודה, או שיחה כללית - ענה ישר בלי לחפש. אם החיפוש לא החזיר כלום: אין לך מידע רשמי על כך, אמור זאת.
"""

PRECEDENCE_ADDENDUM = """

=== סדר עדיפויות (במקרה של סתירה בין ההוראות) ===
"מידע בסיסי על עצמך" (הזהות שלמעלה) קובע מי אתה, טון, שם ותוכן. ההוראות הטכניות של האפליקציה (עיצוב, כרטיסי show_ui, כפתורים וסקרים, אורך תשובה) קובעות איך התשובה נראית, וגוברות על ניסוח הזהות כשיש סתירה: אם הזהות אומרת "בלי מרקדאון" או "שאל שאלה בטקסט" או "פתח ב'בדקתי'" - הכללים של האפליקציה קובעים (כרטיסים לתשובה ארוכה או מורכבת, כפתורים/סקר במקום שאלה פתוחה, ובלי פתיחה כמו "בדקתי"). הטון והסלנג מהזהות נשארים, אבל בקצרה ורק כשזה טבעי.
שפה: ענה בשפה שבה הסטודנט כתב לך (עברית אם לא ברור), וגם תוכן הכרטיסים באותה שפה, אלא אם ביקש במפורש שפה אחרת.
שם: אם ידוע לך השם הפרטי של הסטודנט, אפשר לפנות אליו בשמו לפעמים (לא בכל הודעה).
"""

NO_MOODLE_NOTE = """

=== אין לך גישה למידע האישי ב-Moodle כרגע ===
אין לך כלים לראות מטלות, ציונים, מבחנים, קורסים או קבצים של הסטודנט (הוא לא מחובר ל-Moodle או שהמנהל כיבה את היכולת). אם שואלים על נתון אישי כזה - אמור בקצרה ובכנות שאתה לא רואה את זה כרגע, והפנה אותו לבדוק ישירות ב-Moodle. אל תנחש ואל תמציא נתונים אישיים. ענה במשפט או שניים בטקסט רגיל, בלי show_ui ובלי כרטיסים (הודעה כזו לא צריכה callout, facts וקישורים), ופנה אליו בלשון נקייה ומכבדת.
"""

_MOODLE_WORDS = re.compile(r"הכרז|התראה|התראות|הודעה חדשה|הודעות חדשות|מרצה|מרצים|מה חדש|מה השתנה|עודכן|פורום|קורס|מטלה|מטלות|הגש|ציון|ציונים|מבחן|בוחן|בחינה|קובץ|קבצים|תרגיל|הרצאה|מצגת|סילבוס|מרצה|מודל|moodle|יומן|ממוצע|איחור|דחוף|שלי", re.I)


def _needs_moodle(question: str) -> bool:
    """A pure college-information question ("how much is a dorm room") does not need the Moodle tools and their long instructions (~5k tokens): they are left out."""
    q = str(question or "")
    if "\n\n[הסטודנט לחץ" in q and "פריט בכרטיס המידע" not in q:
        return True
    head = q.split("\n\n[")[0]
    if _MOODLE_WORDS.search(head):
        return True
    return not _KB_MARKERS.search(head)


MOODLE_SYSTEM_ADDENDUM = """

תאריכים והגשות - חשוב: כל תאריך בתוצאות הכלים מגיע בשעון ישראל עם יום בשבוע, תאריך עברי והזמן היחסי ("בעוד 5 שעות" / "עבר המועד לפני 3 ימים"). השתמש בזה כמו שהוא ואל תחשב מחדש. מטלה שמועדה עבר ועדיין ברשימת הדברים הממתינים = באיחור וטרם הוגשה; מטלה שאינה ברשימה = כנראה כבר הוגשה (לוודאות מלאה אפשר get_assignment_submission_status). תמיד הבחן בבירור בין: באיחור (עבר ולא הוגש), דחוף (עד 24 שעות), השבוע, ובעתיד. כשאתה כותב תאריך בטקסט, כתוב יום ותאריך קצר, ואם הסטודנט שאל על מועדים אפשר להוסיף את התאריך העברי.
כשהסטודנט שואל על פריט שהוא לחץ עליו בכרטיס, מגיע בסוף ההודעה בלוק "[הסטודנט לחץ על הפריט הזה...]" עם השם, הקורס, המועד והתיאור מהמערכת: זה ההקשר המלא שלך. אל תאמר "לא מצאתי מידע" כשהבלוק כבר נותן אותו; ענה לפיו, ואם צריך עוד פרטים חפש בקורס הנכון (get_course_assignments, get_assignment_submission_status).
מבחנים (הכלי get_my_exams, אם הוא זמין): מי/מה - המבחנים והבחנים של הסטודנט כפי שהם ב-Moodle: מבחנים מקוונים (Quiz) עם פתיחה, סגירה, משך וניסיונות (כולל אם כבר ניסה והציון), ואירועי יומן שהם מבחנים (מועד א/ב). מתי - כששואלים "מתי המבחן", "מה קרוב", "כמה זמן יש לי", "האם הגשתי", "מה הציון בבוחן". איך - קריאה אחת בלי course_name לכל הקורסים, או עם course_name כשהשאלה על קורס מסוים (במקביל לכלים אחרים אם צריך). אם לא נמצא כלום: אל תמציא מועדים, חדרים או שעות - אמור שב-Moodle אין מבחן מתוזמן, והפנה ללוח המבחנים של המכללה או למזכירות. איך מציגים: ב-show_ui, לא בטקסט: (1) highlight אחד עם ספירה לאחור למבחן הקרוב (value="בעוד 3 ימים", label="שם המבחן"), (2) deadlines (style tickets, או focus כשיש מבחן אחד) עם המזהים d של הכלי, הקרוב ראשון, (3) callout קצר רק אם יש משהו שחשוב לדעת מהתוצאה (נסגר היום, ניסיון אחד בלבד, ניסיון פתוח שטרם הוגש - tone warn כשנשארו פחות מ-24 שעות), (4) אם שאל על ציון: stats או facts עם הציון, (5) buttons להמשך (למשל "מה עוד יש לי השבוע"). מבחנים שהסתיימו מספרים במילה אחת בשורה, לא ככרטיס מועד. את המועדים אל תכתוב בטקסט: הכרטיס מציג אותם.
הכרזות, חידושים, מרצים והתראות (כלים נוספים, אם הם זמינים): get_course_announcements - מה המרצה פרסם בקורס (בלי course_name = בכל הקורסים); get_course_updates - מה חדש או השתנה בקורס בימים האחרונים (קבצים חדשים, מטלות שעודכנו); get_courses_by_status - אילו קורסים פעילים עכשיו, הסתיימו או עתידיים (עדיף על get_my_courses כשהשאלה על מה הוא לומד עכשיו); get_course_teachers - מי המרצה ואיך פונים אליו; get_my_notifications - התראות חדשות. כללים: ציין תמיד את התאריך שהכלי נותן; אל תמציא מייל או שם מרצה, ואם הכלי לא מחזיר - אמור שזה לא גלוי ב-Moodle. איך מציגים: הכרזות והתראות ב-show_ui (timeline או list עם תאריך, ו-callout להכרזה חשובה או לא נקראה), מרצה ב-contact (המייל מהתוצאה בלבד), מה חדש ב-list עם אייקון לפי סוג. אל תקרא לכלי הזה אם הסטודנט לא שאל על זה.
כלל קריטי: אם אתה קורא לכלי, אל תכתוב שום טקסט לפני הקריאה (בלי "רגע אבדוק", בלי הקדמה) - קרא לכלי מיד. את התשובה כותבים רק אחרי שהתוצאה חזרה.

=== מידע אישי מ-Moodle (קורסים/מטלות/ציונים/יומן) ===
לסטודנט הזה יש חיבור פעיל ל-Moodle. אם השאלה נוגעת למידע אישי שלו (הקורסים שלו, מטלות,
ציונים, או אירועים ביומן) - ואך ורק במקרה הזה - תוכל להשתמש בכלים (functions) שסופקו לך
כדי לשלוף את המידע האמיתי והעדכני. אל תמציא קורסים/ציונים/מטלות בעצמך; אם הכלי מחזיר
שאין מידע או שיש שגיאה - העבר את זה לסטודנט בנימוס בדיוק כפי שהתקבל. לשאלות נהלים רגילות
(לא אישיות) המשך כרגיל על בסיס מידע המכללה בלבד, בלי להשתמש בכלים.

=== שליחת קבצים ומסמכים - בחוכמה ובאחריות ===
כשהסטודנט מבקש קובץ, מסמך, דף תרגיל, פתרון, הרצאה/מצגת, מבחן קודם, סילבוס או סיכום מאחד הקורסים שלו - השתמש בכלי get_course_files.
1. תמיד ציין מאיזה קורס (course_name) ומה בדיוק מחפשים: שים ב-search את מה שהסטודנט ביקש במילים שלו (נושא, מספר תרגיל, שבוע; בלי מילות נימוס),
   וב-kind את הסוג אם ברור. אם לא ברור באיזה קורס מדובר והסטודנט בכמה קורסים - שאל אותו קודם, אל תנחש.
2. הכלי עצמו מדרג לפי התאמה ומחזיר רק את הטובים, עם הסבר על כל קובץ (סוג, מאיזו פעילות ונושא, מועד, תיאור). קרא את ההסברים ובחר בעצמך מה באמת עונה
   על הבקשה - בדרך כלל קובץ אחד עד שלושה. הסבר בשורה קצרה מה כל קובץ שאתה שולח. "תרגיל 3" אינו "תרגיל 13"; פתרון נשלח רק אם ביקשו פתרון.
   אם כמה קבצים מתאימים באותה מידה ואי אפשר לדעת למה הסטודנט התכוון - שאל אותו איזה מהם, במקום לשלוח את כולם.
3. כדי לשלוח קובץ - העתק את הסמן [[file:קוד|שם|גודל]] *בדיוק כפי שהוא* מהתוצאה, בשורה משלו; האפליקציה הופכת אותו לכרטיס הורדה. לעולם אל תמציא סמנים
   או קודים, אל תכתוב כתובות URL של קבצים, ואל תכתוב טוקנים.
4. בקשות גורפות או לא הגיוניות - לא נענות כמו שהן: "תביא לי את כל הקבצים", "את כל מה שיש לי במודל", "את כל הקורס", קבצים מכל הקורסים בבת אחת.
   הסבר בנימוס שלשלוח הכל זה לא מועיל ולא אפשרי, ושאל מה הוא צריך בפועל (איזה קורס, איזה סוג - תרגיל/הרצאה/מבחן - ואיזה מספר או נושא). אפשר להציע לו
   את הסקירה שהכלי מחזיר (כמה קבצים מכל סוג) כדי שיבחר. אל תפעיל את הכלי שוב ושוב כדי "לאסוף הכל".
   בקשות שהן בכלל לא קשורות למה שקיים (קבצים של סטודנטים אחרים, מבחנים סודיים שעוד לא פורסמו, עקיפת הרשאות) - סרב בנימוס והצע אלטרנטיבה חוקית.
5. אם הכלי מחזיר שאין התאמה טובה - אל תשלח קבצים אקראיים "לפחות משהו". אמור שלא מצאת, ציין מה כן יש (לפי הסקירה) ושאל מה הוא מחפש.
6. כשהבקשה לקבצים כללית או לא ברורה ("כל הקבצים", "החומר של הקורס", "משהו על X"): אל תשאל בטקסט. שלח סקר (poll:single, או poll:multi כשהגיוני לבחור כמה) שהאפשרויות שלו נגזרות אך ורק מהסקירה שהכלי get_course_files החזיר בפועל ("לפי סוג" ו"לפי נושא"): שמות הסוגים או הנושאים האמיתיים בקורס הזה, עם כמות הקבצים בסוגריים כשיש, ורק כאלה שקיימים. אל תמציא קטגוריות. בכל סקר הסטודנט יכול גם להקליד חופשי, אז אין צורך באפשרות "אחר".
7. אל תכתוב "הנה הקובץ" או ניסוח דומה אלא אם הדבקת בפועל, באותה תשובה, סמן [[file:...]] מתוצאת הכלי. אם הכלי לא החזיר קובץ מתאים או שנכשל: אמור בשורה אחת מה קרה (לא נמצא קובץ מתאים / הייתה בעיה בשליפה) והצע המשך עם כפתורי quick שמבוססים על מה שכן קיים בקורס.
6. אתה לא קורא את תוכן הקבצים - רק שולח אותם ומתאר אותם לפי שם/נושא/תיאור. אם הוא מבקש הסבר על התוכן עצמו, אמור לו זאת בכנות."""

# the built-in text of every section the admin can edit (the admin's version, if any, is read through prompt_store.text)
for _key, _val in (("persona", PERSONA_DEFAULT), ("rules", RULES_DEFAULT), ("rich", RICH_ADDENDUM), ("moodle", MOODLE_SYSTEM_ADDENDUM),
                   ("no_moodle", NO_MOODLE_NOTE), ("kb_tool", KB_TOOL_NOTE), ("precedence", PRECEDENCE_ADDENDUM),
                   ("parallel", PARALLEL_ADDENDUM), ("followups", FOLLOWUPS_ADDENDUM), ("item_ctx", ITEM_CTX_ADDENDUM)):
    PS.register(_key, _val)


# ---------------------------------------------------------------------
# סטטוס חי: אירועים קטנים בתוך זרם התשובה ("מחפש קבצים בקורס X...") - רק ללקוח שביקש (status=true), ולא נשמרים ב-DB
# ---------------------------------------------------------------------
STATUS_OPEN, STATUS_CLOSE = "\x01", "\x02"


def status_event(**kw) -> str:
    return STATUS_OPEN + json.dumps(kw, ensure_ascii=False, separators=(",", ":")) + STATUS_CLOSE


MAX_TOOL_CALLS_PER_TURN = 6
MAX_FILE_SEARCHES_PER_TURN = 2
MAX_TOOL_ROUNDS = 4


async def _stream_chat_inner(
    question: str,
    context_chunks: List[Dict[str, Any]],
    api_key: str,
    bot_identity: str = "",
    catalog: Optional[List[Dict[str, str]]] = None,
    conversation_summary: str = "",
    recent_messages: Optional[List[Dict[str, str]]] = None,
    username: str = "",
    moodle_token: Optional[str] = None,
    moodle_user_id: Any = None,
    enabled_functions: Optional[set] = None,
    memories: Optional[List[Dict[str, Any]]] = None,
    user_id: Optional[str] = None,
    emit_status: bool = False,
    lang: str = "",
    kb_search=None,
) -> AsyncGenerator[str, None]:
    """
    מנוע הצ'אט המאוחד: כלי Moodle (אם יש חיבור) + כלי זיכרון (אם יש משתמש) בסיבוב החלטה אחד, ואז תשובה מוזרמת.
    זיכרון הסטודנט מוזרם לפרומפט בכל הודעה, וקורסים שעזב מוסתרים מתוצאות Moodle. עם emit_status גם מזרים אירועי סטטוס.
    כל כשל בנתיב הכלים נופל חזרה לתשובה רגילה בלי כלים - הצ'אט לא נשבר.
    """
    from moodle_tools import get_enabled_tools, execute_moodle_function, set_hidden_courses, start_cards, get_cards
    from memory import (MEMORY_ADDENDUM, MEMORY_TOOLS, MEMORY_TOOL_NAMES, build_memory_block, left_courses, run_memory_tool)

    memories = memories or []
    set_hidden_courses(left_courses(memories))
    start_cards()

    moodle_tools = get_enabled_tools(enabled_functions) if (moodle_token and moodle_user_id) else []
    moodle_off_note = ""
    if moodle_tools and not _needs_moodle(question):
        moodle_tools = []                                          # a college-information question: no Moodle tools, no Moodle instructions (a third of the prompt)
    elif not moodle_tools and _needs_moodle(question) and _MOODLE_WORDS.search(str(question).split("\n\n[")[0]):
        moodle_off_note = NO_MOODLE_NOTE                          # asks about personal data but there is no access: the bot must say so, not guess
    moodle_names = {d["name"] for grp in moodle_tools for d in grp.get("functionDeclarations", [])}
    declarations = [d for grp in moodle_tools for d in grp.get("functionDeclarations", [])]
    if user_id:
        declarations = declarations + MEMORY_TOOLS[0]["functionDeclarations"]
    if kb_search is not None:
        declarations = declarations + [SEARCH_DECL]               # the model decides whether the college's information is needed
    declarations = declarations + UI_TOOLS[0]["functionDeclarations"]          # the display kit is for every answer, not only Moodle ones
    tools = [{"functionDeclarations": declarations}] if declarations else []
    if _is_smalltalk(question):
        tools = []                     # "hi" / "thanks": a plain, fast answer - no tools, no cards

    # the obvious data of a personal question is fetched right now, in parallel, so the model's first move can already be the cards (one model round instead of two)
    prefetch_text = ""
    pre = []
    if moodle_token and moodle_user_id and not _is_smalltalk(question):
        for n in personal_route(question, True)["tools"]:
            if n == "get_courses_by_status" and n not in moodle_names:
                n = "get_my_courses"                              # the admin switched the status tool off: the plain list
            if n in moodle_names and n not in pre:
                pre.append(n)
    if pre:
        from moodle_tools import execute_moodle_function as _exec_pre
        if emit_status:
            for n in pre:
                yield status_event(s="tool", name=n, arg="", detail="")
        pre_results = await asyncio.gather(*[_exec_pre(n, {}, moodle_token, moodle_user_id) for n in pre])
        db.bump_api_usage("prefetch")
        prefetch_text = "\n\n=== נתוני Moodle שכבר נשלפו עבורך עכשיו (אל תקרא שוב לאותם כלים; הצג מהם ישר ב-show_ui, והמזהים d/g/c בסוגריים) ===\n" + "\n\n".join(str(x) for x in pre_results)

    PS.set_ctx({"first": not conversation_summary and not recent_messages, "new_student": not memories, "moodle": bool(moodle_token and moodle_user_id),
                "lang": lang or "he", "has_user": bool(user_id)})             # the facts that the admin's rule conditions are checked against
    extra = PS.text("rich") + ui_text() + prefetch_text
    if moodle_names:
        extra += PS.text("moodle")
    if user_id:
        extra += PS.text("memory") + build_memory_block(memories)
    extra += (PS.text("no_moodle") if moodle_off_note else "") + (PS.text("kb_tool") if (kb_search is not None and not context_chunks) else "")
    extra += (PS.text("parallel") if (kb_search is not None or moodle_names) else "") + PS.text("followups") + (PS.text("item_ctx") if "[הסטודנט לחץ" in question else "") + PS.text("precedence")
    extra += PS.notes_text({"first": not conversation_summary and not recent_messages, "new_student": not memories, "moodle": bool(moodle_token and moodle_user_id),
                            "lang": lang or "he", "has_user": bool(user_id)})

    user_turn_text = _build_chat_user_turn(
        question, context_chunks, bot_identity, catalog, conversation_summary, recent_messages,
        username=username, extra_system_instructions=extra, ui_lang=lang,
    )

    allowed_parts = [user_turn_text]            # everything the model was given: contacts in a UI block must appear in this material or in a tool result
    _cd()["allowed"] = allowed_parts            # the iron-rule guard (stream_chat_with_tools) validates its own cards against the same material

    async def final_answer(contents):
        if emit_status:
            yield status_event(s="write")
        async for token in _stream_from_gemini(contents, api_key, call_type="chat_answer"):
            yield token

    contents = [{"role": "user", "parts": [{"text": user_turn_text}]}]
    if not tools:
        got = False
        async for token in final_answer(user_turn_text):
            if not token.startswith(STATUS_OPEN):
                got = True
            yield token
        if not got:
            yield "לא הצלחתי לענות כרגע. אפשר לנסות שוב?"
        return

    # קריאה מוזרמת אחת עם הכלים: שאלה רגילה מתחילה להיענות מיד; רק אם המודל מבקש כלי יש סבב שני אחרי ביצוע הכלים
    calls: List[Dict[str, Any]] = []
    emitted_text = False
    failed = False
    if emit_status:
        yield status_event(s="think")
    async for kind, value in _stream_gemini_events(contents, api_key, tools=tools, max_output_tokens=2000):
        if kind == "text":
            if emit_status and not emitted_text:
                yield status_event(s="write")
            emitted_text = True
            yield value
        elif kind == "calls":
            calls = value[:MAX_TOOL_CALLS_PER_TURN]
        else:
            failed = True
    if failed and not emitted_text:
        # נתיב הכלים נכשל (למשל סכמה) - עונים בלי כלים במקום להיכשל
        async for token in final_answer(user_turn_text):
            yield token
        return
    if not calls:
        return

    # עד MAX_TOOL_ROUNDS סבבי כלים: למשל "מה הציונים שלי" דורש get_my_courses ואז כלי הציונים עם מזהה הקורס.
    # בסבב האחרון לא מציעים כלים, כדי שתמיד תצא תשובה.
    file_searches = 0
    round_no = 0
    while calls:
        round_no += 1
        kept = []
        for p in calls:
            if p["functionCall"].get("name") == "get_course_files":
                file_searches += 1
                if file_searches > MAX_FILE_SEARCHES_PER_TURN:
                    continue
            kept.append(p)
        calls = kept or calls[:1]

        if emit_status:
            for p in calls:
                fc = p["functionCall"]
                a = fc.get("args") or {}
                yield status_event(
                    s="tool", name=fc.get("name", ""),
                    arg=str(a.get("course_name") or a.get("quiz_name") or a.get("fact") or "")[:80],
                    detail=str(a.get("search") or a.get("assignment_name") or a.get("kind") or "")[:80],
                )

        async def run(p):
            fc = p["functionCall"]
            name, args = fc.get("name", ""), fc.get("args") or {}
            if name in MEMORY_TOOL_NAMES and user_id:
                return await run_memory_tool(name, args, user_id, memories)
            if name == "show_ui":
                blocks, msg = resolve_blocks(args.get("blocks"), _cd().get("items", {}), "\n".join(allowed_parts))
                # a Moodle block whose ids were wrong is not "shown successfully": the model is told which ids exist and tries again in the same turn
                asked = [str(b.get("type") or "").lower() for b in (args.get("blocks") or []) if isinstance(b, dict)]
                shown = {b.get("k") for b in blocks}
                lost = list(dict.fromkeys(k for k in asked if k in ("deadlines", "grades", "courses") and k not in shown))
                if lost:
                    letter = {"deadlines": "d", "grades": "g", "courses": "c"}
                    valid = "; ".join(f"{k}: " + ", ".join(i for i, v in (_cd().get("items") or {}).items() if v.get("k") == letter[k])[:300] for k in lost)
                    return ("לא הוצג כלום: הבלוקים " + ", ".join(lost) + " נשלחו עם מזהים שלא קיימים. המזהים התקפים (השתמש רק בהם): " + (valid or "אין, קרא קודם לכלי המתאים") + ". שלח שוב את כל הבלוקים.", None)
                # (Found by testing: "when is the library open?" came back as a note "hours may change" plus buttons - the hours themselves were missing.) A set of cards with
                # no content in it (only notes, headings and buttons) while the college material was there is sent back once, with what is missing.
                def _hollow(b):      # a list that promises "addresses" / "hours" / "prices" in its title but whose rows are only names ("the addresses of the campuses: Lev, Tal, Lustig")
                    return (b.get("k") in ("list", "facts", "tags") and bool(b.get("it")) and all(not r.get("d") and not r.get("v") for r in b["it"])
                            and bool(re.search(r"כתובת|כתובות|שעות|טלפון|טלפונים|מחיר|מחירים|עלות|עלויות|תאריכים|פרטים|הגעה", str(b.get("ti") or ""))))
                weak = bool(blocks) and all(b.get("k") in ("buttons", "heading", "callout", "banner") or _hollow(b) for b in blocks)
                if weak and (context_chunks or "search_college_info" in (_cd().get("called") or [])) and not _cd().get("weak_retry"):
                    _cd()["weak_retry"] = True
                    try:
                        material = "\n\n".join([f"{c.get('title', '')}: {c.get('text', '')}" for c in (context_chunks or [])] + [str(x) for x in allowed_parts[1:]])
                        built = await _cards_from_material(question, material, api_key) if material.strip() else []
                    except Exception as e:
                        print(f"[ui-material] skipped: {type(e).__name__}: {e}")
                        built = []
                    if built and not all(b.get("k") in ("buttons", "heading", "callout", "banner") or _hollow(b) for b in built):
                        _cd()["ui"] = built                                # the cards, made straight from the material: they hold the answer
                        db.bump_api_usage("ui:material")
                        return "הוצג לסטודנט בהצלחה. כתוב לכל היותר משפט קצר אחד (או כלום), בלי לחזור על מה שמוצג.", None
                    return "הכרטיסים שהצגת לא כוללים את התשובה עצמה (רק הערה, כותרת וכפתורים). הצג את המידע שנשאל עליו מתוך החומר שקיבלת (שעות ב-hours, פרטי קשר ב-contact, מחיר ב-price או highlight, שלבים ב-steps, פרטים ב-facts), ובלי להוסיף הערה על מה שלא נמצא.", None
                if blocks:
                    _cd()["ui"] = blocks
                return msg, None
            if name == "search_college_info" and kb_search is not None:
                found = await kb_search(str(args.get("query") or question)[:300])
                if not found:
                    return "לא נמצא מידע רלוונטי במאגר המכללה על הנושא הזה.", None
                body = "\n\n---\n\n".join(f"מקור: {c.get('title', 'נהלי מכללה')}\nתוכן: {c['text']}" for c in found)
                return body, {"s": "kbdone", "t": str(found[0].get("title") or "")[:60]}
            if name in moodle_names:
                return await execute_moodle_function(name, args, moodle_token, moodle_user_id), None
            return f"הכלי {name} לא זמין.", None

        outcomes = await asyncio.gather(*[run(p) for p in calls])
        for p in calls:
            db.bump_api_usage("tool:" + str(p["functionCall"].get("name", ""))[:40])            # admin sees what students use (counts only)
        _fm = _cd().setdefault("file_markers", [])
        _cd().setdefault("called", []).extend(p["functionCall"].get("name", "") for p in calls)
        for _t, _ in outcomes:
            allowed_parts.append(str(_t))
            _fm.extend(re.findall(r"\[\[file:[^\]\n]+\]\]", str(_t)))          # (every result's file markers, not only the last one's)
        if emit_status:
            for _, event in outcomes:
                if event:
                    yield status_event(**event)

        card = _cd()
        ui_blocks_out = card.pop("ui", None)
        if ui_blocks_out:
            for blk in ui_blocks_out:
                db.bump_api_usage("ui:" + str(blk.get("k", ""))[:20])
            yield f"[[ui:{encode_blocks(ui_blocks_out)}]]"          # the cards the model chose to show (validated by the server)
            if all(p["functionCall"].get("name") == "show_ui" for p in calls):
                return                       # the cards ARE the answer: no extra model round (saves several seconds). Data tools in the same step still get their final answer.
        contents.append({"role": "model", "parts": calls})
        contents.append({
            "role": "user",
            "parts": [{"functionResponse": {"name": p["functionCall"].get("name", ""), "response": {"result": text}}} for p, (text, _) in zip(calls, outcomes)],
        })

        if round_no >= MAX_TOOL_ROUNDS:
            break
        calls = []
        got_text = False
        failed = False
        async for kind, value in _stream_gemini_events(contents, api_key, tools=tools, max_output_tokens=2000):
            if kind == "text":
                if emit_status and not got_text:
                    yield status_event(s="write")
                got_text = True
                yield value
            elif kind == "calls":
                calls = value[:MAX_TOOL_CALLS_PER_TURN]
            else:
                failed = True
        if got_text:
            return
        if not calls and not failed:
            # המודל החזיר ריק: מבקשים ממנו תשובה סופית בלי כלים
            break
        if failed:
            break

    answered = False
    async for token in final_answer(contents):
        answered = True
        yield token
    if not answered:
        yield "לא הצלחתי להשלים את הבקשה כרגע. נסה שוב בעוד רגע."


# A question about the student's own Moodle data (tasks, grades, exams, courses) needs no search in the college's information - that search costs 1-2 seconds
# (an embedding call and a database search) for nothing. And its data is known in advance, so it is fetched at once instead of after a model round.
_KB_MARKERS = re.compile(r"נוהל|נהלי|תקנון|ערעור|נרשמ|רישום|כמה עולה|מחיר|עלות|מלגה|שכר לימוד|מעונות|שעות (?:פתיחה|פעילות|קבלה)|טלפון|מייל|כתובת|איך מגיעים|הגעה|תחבורה|אוטובוס|הסעות?|חניה|חנייה|קמפוס|מזכירות|יצירת קשר|לוח שנה|סמסטר|חופשה|מדיניות|יוצרים קשר|ליצור קשר|דרכי התקשרות")
_PERSONAL = re.compile(r"(?<![א-ת])(?:שלי|לי|אצלי|בשבילי)(?![א-ת])")
_INTENTS = (
    (re.compile(r"מבחן|מבחנים|בוחן|בחנים|בחינה|בחינות"), "get_my_exams", True),                       # (pattern, tool, needs a personal word like "שלי")
    (re.compile(r"מטלה|מטלות|הגשה|הגשות|מועדי הגשה|דחוף|דחופות|באיחור|איחור|מה קרוב|מה יש לי|מה לא הגשתי"), "get_upcoming_deadlines", False),
    (re.compile(r"ציון|ציונים|ממוצע"), "get_overall_grades", False),
    (re.compile(r"התראות|התראה|הודעות חדשות|הודעה חדשה|יש לי הודעות"), "get_my_notifications", False),
    (re.compile(r"הקורסים|קורסים שלי|איזה קורסים|מה אני לומד"), "get_courses_by_status", False),         # the courses he studies NOW (Moodle's own status), not everything he was ever enrolled in
)


_CLAUSE_START = re.compile(r"\s*[,;]\s*|\s+(?=(?:וגם|בנוסף|ו(?:מה|איך|כמה|מתי|איפה|למי|האם|אילו|איזה)|ו[א-ת]{3,})(?![א-ת]))")


def split_topics(head: str) -> List[str]:
    """A question that asks about several college topics at once ("דרכי הגעה ומדיניות המעונות") becomes one focused search per topic: a single search for the whole
    sentence returns only a blend of them. Clauses about the student's own Moodle data are left out (the Moodle tools answer those); a clause with no topic of its own
    joins the one before it. One topic (or none) -> the whole question, as before."""
    head = " ".join(str(head or "").split())
    if len(head) < 12:
        return [head]
    cuts = [0] + [m.end() for m in _CLAUSE_START.finditer(head)]
    parts = [head[a:b].strip(" ,;") for a, b in zip(cuts, cuts[1:] + [len(head)])]
    clauses: List[List[str]] = []                                 # [text, kind] kind: kb / personal / other
    for p in parts:
        if not p:
            continue
        kind = "kb" if _KB_MARKERS.search(p) else ("personal" if (_MOODLE_WORDS.search(p) or _PERSONAL.search(p)) else "other")
        clauses.append([p, kind])
    merged: List[List[str]] = []
    for text, kind in clauses:
        if kind == "other" and merged:
            merged[-1][0] += " " + text
        else:
            merged.append([text, kind])
    kb = [re.sub(r"^(?:וגם\s+)?ו?(?=[א-ת]{2,})", "", t) if t.startswith("ו") or t.startswith("וגם") else t for t, k in merged if k == "kb"]
    if len(kb) >= 2:
        return kb[:4]
    if len(kb) == 1 and any(k == "personal" for _, k in merged):
        return [kb[0]]
    return [head]


def personal_route(question: str, connected: bool) -> Dict[str, Any]:
    """{"skip_kb": bool, "tools": [names]}: decided from the words alone (instant, no model)."""
    q = str(question or "")
    if "פריט בכרטיס המידע" in q or not connected:                      # a pressed item of an information card: the normal flow (the model searches by the item's name)
        return {"skip_kb": False, "tools": []}
    head = q.split("\n\n[")[0].strip()
    if "\n\n[הסטודנט לחץ" in q:                              # a question about an item he pressed: the item's details come with it
        return {"skip_kb": True, "tools": []}
    if _KB_MARKERS.search(head) and len(head) <= 400:
        # a college question that ALSO asks for his own data ("... ומה המטלות שלי"): the college search still runs, and his data is fetched in parallel
        if _PERSONAL.search(head):
            mixed = [t for pat, t, _np in _INTENTS if pat.search(head) and t not in ("get_my_courses", "get_courses_by_status")]
            return {"skip_kb": False, "tools": mixed[:2]}
        return {"skip_kb": False, "tools": []}
    if _KB_MARKERS.search(head) or len(head) > 160:
        return {"skip_kb": False, "tools": []}
    personal = bool(_PERSONAL.search(head))
    mentions_course = "קורס" in head and "הקורסים" not in head
    hit, tools = False, []
    for pat, tool, need_personal in _INTENTS:
        if pat.search(head) and (personal or not need_personal):
            hit = True
            if tool in ("get_my_courses", "get_courses_by_status") or not mentions_course:     # about one named course: the model picks the course tool itself
                tools.append(tool)
    return {"skip_kb": hit, "tools": tools[:2]}


_SMALLTALK = re.compile(r"^(?:היי+|הי|שלום|אהלן|אהלא|בוקר טוב|ערב טוב|צהריים טובים|תודה(?: רבה| לך)?|תודה!+|סבבה|אוקי|אוקיי|אחלה|מעולה|יופי|מצוין|ביי|להתראות|לילה טוב|hi|hello|hey|thanks|thank you|ok|okay|merci|bonjour|salut)[\s!.,?]*$", re.I)


def _is_smalltalk(q: str) -> bool:
    first = str(q or "").split("\n\n[")[0].strip()
    return len(first) <= 24 and bool(_SMALLTALK.match(first))


VISUAL_TOKEN = status_event(s="visual")          # tells the client: drop the text so far, the answer is being redrawn as cards (main.py also resets what is saved)

_MARKERS = re.compile(r"\[\[[^\]]*\]\]")
_LISTLINE = re.compile(r"^\s*(?:[-*•]|\d+[.)])\s+", re.M)
_NUMBERS = re.compile(r"\d[\d:.,/\-]*")
_CONTACT = re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+|https?://|\b0\d{1,2}[-\s]?\d{6,8}\b|\*\d{3,5}")
_FAILURE_OPEN = re.compile(r"^(?:לא הצלחתי|שגיאה|המערכת בעומס|התשובה התעכבה|אירעה שגיאה|מצטער|אני מוגדר לענות רק|השרתים של ה-AI)")      # (an out-of-scope refusal gets no follow-up buttons: the model invented ones like "the schedule of the reenactments")


def _needs_visual(text: str) -> bool:
    """The iron rule's test: more than three screen lines of text, or something shorter but complex (a list, several numbers, contact details)."""
    if any(m in text for m in ("[[ui:", "[[file:", "[[poll:")):
        return False
    t = _MARKERS.sub("", text).strip()
    if len(t) < 45 or _FAILURE_OPEN.match(t) or _is_smalltalk(t):
        return False
    lines = [ln for ln in t.splitlines() if ln.strip()]
    screen_lines = sum(max(1, -(-len(ln) // 48)) for ln in lines)            # about 48 characters fit in one line of the chat on a phone
    if screen_lines > 3:
        return True
    if len(_LISTLINE.findall(t)) >= 2:
        return True
    if _CONTACT.search(t):
        return True
    return len(_NUMBERS.findall(t)) >= 3 and len(t) > 70


async def _visualize_text(question: str, text: str, api_key: str) -> List[Dict[str, Any]]:
    """Turns an answer that was written as text into validated show_ui blocks (a forced show_ui call). Returns [] when it cannot."""
    try:
        q = str(question or "").split("\n\n[")[0].strip()[:600]
        prompt = (
            "אתה אורי, העוזר של המרכז האקדמי לב. כתבת תשובה בטקסט, אבל כלל הברזל שלך: תשובה ארוכה מ-3 שורות או מורכבת מוצגת ויזואלית.\n"
            "הצג עכשיו את אותה תשובה בדיוק בעזרת הכלי show_ui, כמה בלוקים (2 עד 5) שמסודרים לפי המתכונים שלמטה.\n"
            "אל תוסיף מידע שלא נמצא בתשובה, אל תשמיט עובדה, מספר, תאריך או פרט קשר, ואל תכתוב טקסט מחוץ לקריאה לכלי. "
            "שמור על הניסוח הקצר והישיר; המטרה היא מבנה ובהירות. כתוב את תוכן הבלוקים באותה שפה שבה כתובה התשובה.\n"
            f"{ui_text()}\n\nשאלת הסטודנט:\n{q}\n\nהתשובה שכתבת:\n{text[:3500]}"
        )
        content = await _generate_once(
            [{"role": "user", "parts": [{"text": prompt}]}], api_key, tools=UI_TOOLS, max_output_tokens=2200, call_type="ui_guard",
            tool_config={"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["show_ui"]}},
        )
        for part in (content or {}).get("parts", []):
            fc = part.get("functionCall") if isinstance(part, dict) else None
            if fc and fc.get("name") == "show_ui":
                allowed = "\n".join(str(x) for x in (_cd().get("allowed") or [])) or text
                blocks, _msg = resolve_blocks((fc.get("args") or {}).get("blocks"), _cd().get("items", {}), allowed)
                return blocks
    except Exception as e:
        print(f"[ui-guard] conversion failed: {type(e).__name__}: {e}")
    return []


async def _cards_from_material(question: str, material: str, api_key: str) -> List[Dict[str, Any]]:
    """When the cards the model chose hold no answer (only a note and buttons), the cards are built from the college material itself with a forced show_ui call:
    this path always comes back with the content of the material in the right kind of card. Returns [] when it cannot."""
    try:
        q = str(question or "").split("\n\n[")[0].strip()[:600]
        prompt = (
            "אתה אורי, העוזר של המרכז האקדמי לב. ענה על שאלת הסטודנט אך ורק בעזרת החומר שלמטה, באמצעות הכלי show_ui (2 עד 4 בלוקים). "
            "הבלוק הראשון הוא התשובה עצמה (שעות ב-hours, פרטי קשר ב-contact, מחיר ב-price או highlight, כתובת ב-location, שלבים ב-steps, פרטים ב-facts). "
            "אל תוסיף מידע שלא בחומר ואל תכתוב טקסט מחוץ לקריאה לכלי. כתוב את תוכן הבלוקים באותה שפה שבה נשאלה השאלה.\n"
            f"{ui_text()}\n\nשאלת הסטודנט:\n{q}\n\nהחומר:\n{material[:6000]}"
        )
        content = await _generate_once(
            [{"role": "user", "parts": [{"text": prompt}]}], api_key, tools=UI_TOOLS, max_output_tokens=2200, call_type="ui_guard",
            tool_config={"functionCallingConfig": {"mode": "ANY", "allowedFunctionNames": ["show_ui"]}},
        )
        for part in (content or {}).get("parts", []):
            fc = part.get("functionCall") if isinstance(part, dict) else None
            if fc and fc.get("name") == "show_ui":
                blocks, _msg = resolve_blocks((fc.get("args") or {}).get("blocks"), _cd().get("items", {}), material)
                return blocks
    except Exception as e:
        print(f"[ui-material] failed: {type(e).__name__}: {e}")
    return []


_LEAK = re.compile(r"\[\[show_ui\s*:|(?<![\w.])show_ui\s*\(|\"show_ui\"\s*:")


def _parse_leaked_ui(text: str) -> List[Dict[str, Any]]:
    """Found by testing: now and then the model WRITES the display call as text ("[[show_ui:[{...}]]" or "show_ui(blocks: [...])") instead of calling the tool, and the student
    saw raw JSON. When the text carries the JSON array of blocks it is read straight from it (no model call), validated like any other cards."""
    try:
        i = text.index("show_ui")
        j = text.index("[", i + 7)
        arr, _ = json.JSONDecoder().raw_decode(text[j:])
        if isinstance(arr, dict):
            arr = arr.get("blocks") or [arr]
        allowed = "\n".join(str(x) for x in (_cd().get("allowed") or [])) or text
        blocks, _msg = resolve_blocks(arr, _cd().get("items", {}), allowed)
        return blocks
    except Exception:
        return []


async def _stream_chat_guarded(*args, **kwargs) -> AsyncGenerator[str, None]:
    """עוטף את מנוע הצ'אט ומוודא שהבוט לא מבטיח קובץ שלא צירף: אם כתב "הנה הקובץ" בלי סמן - מצרפים את הקובץ שהכלי מצא, ואם לא נמצא כלום - אומרים את זה בפשטות."""
    from moodle_tools import get_cards as _gc
    acc: List[str] = []
    async for tok in _stream_chat_inner(*args, **kwargs):
        if not tok.startswith(STATUS_OPEN):
            acc.append(tok)
        yield tok
    text = "".join(acc)
    if kwargs.get("emit_status") and _LEAK.search(text):                # the display call written as text: never shown to the student
        question = args[0] if args else kwargs.get("question", "")
        api_key = args[2] if len(args) > 2 else kwargs.get("api_key", "")
        yield VISUAL_TOKEN
        blocks = _parse_leaked_ui(text)
        if not blocks:
            try:
                blocks = await asyncio.wait_for(_visualize_text(question, _LEAK.split(text)[0] + " " + text, api_key), timeout=22)       # (bounded: the 45 s watchdog must not cut the stream before the message)
            except Exception:
                blocks = []
        db.bump_api_usage("ui:leak")
        if blocks:
            for blk in blocks:
                db.bump_api_usage("ui:" + str(blk.get("k", ""))[:20])
            yield f"[[ui:{encode_blocks(blocks)}]]"
        else:
            yield "לא הצלחתי לסדר את התשובה בכרטיסים. אפשר לשאול שוב במילים אחרות?"
        return
    # THE IRON RULE: a long or complex answer is never left as text. If the model wrote one anyway, it is converted to visual cards here.
    if kwargs.get("emit_status") and _needs_visual(text):
        question = args[0] if args else kwargs.get("question", "")
        api_key = args[2] if len(args) > 2 else kwargs.get("api_key", "")
        quick = re.findall(r"\[\[quick\|[^\]\n]*\]\]", text)
        yield VISUAL_TOKEN                                          # the client replaces the text with a "designing it" line
        try:
            blocks = await asyncio.wait_for(_visualize_text(question, text, api_key), timeout=25)
        except Exception:
            blocks = []
        if blocks:
            db.bump_api_usage("ui:guard")
            for blk in blocks:
                db.bump_api_usage("ui:" + str(blk.get("k", ""))[:20])
            yield f"[[ui:{encode_blocks(blocks)}]]"
            if quick:
                yield "\n\n" + "\n".join(quick[:1])
            return
        yield text                                                  # the conversion failed: the answer comes back as it was
    if "[[file:" in text:
        return
    card = _gc() or {}
    markers = card.get("file_markers") or []
    files_called = "get_course_files" in (card.get("called") or [])
    promised = re.search(r"הנה (?:ה)?(?:קובץ|קבצים|תרגיל|מסמך|מצגת)|מצורף|הקובץ הבא|הקבצים הבאים", text)
    if promised and markers:
        yield "\n\n" + "\n".join(markers[:3])
    elif files_called and (promised or (text.rstrip().endswith(":") and re.search(r"קובץ|קבצים|מסמך", text))):
        yield "\n\nלא הצלחתי למשוך את הקובץ כרגע. אפשר לנסות שוב בעוד רגע, או לפתוח אותו ישירות ב-Moodle."


_FOLLOW_SKIP = re.compile(r"\[\[(?:poll|quick)")


def _courses_poll(blocks: List[Dict[str, Any]], question: str, memories: List[Dict[str, Any]], recent: Optional[List[Dict[str, Any]]]) -> str:
    """The list of courses Moodle gives is not always the list he studies. After the courses cards, once, he is asked which ones are really his (the poll's answer
    makes the bot hide the others). Not asked again when it was answered before (memory) or already asked in this conversation."""
    if str(question or "").lstrip().startswith(("בחרתי", "I chose")):
        return ""
    names: List[str] = []
    for b in blocks:
        if b.get("k") == "courses":
            for it in b.get("it") or []:
                n = re.sub(r"[|\[\]\n]", " ", str(it.get("n") or "")).strip()[:60]
                if n and n not in names:
                    names.append(n)
    if len(names) < 2:
        return ""
    if any("אימת אילו קורסים" in str(m.get("content") or "") for m in (memories or [])):
        return ""
    if any("אילו מהקורסים האלה אתה לומד" in str(m.get("content") or "") for m in (recent or [])):
        return ""
    return "\n\nסמן את הקורסים שאתה באמת לומד עכשיו, ואסתיר את השאר.\n[[poll:multi|אילו מהקורסים האלה אתה לומד עכשיו?|" + "|".join(names[:12]) + "]]"


async def _followups(question: str, digest: str, api_key: str) -> List[str]:
    """3-4 short follow-up questions that fit what was just asked and answered (one quick call, never blocks the answer: it runs after it)."""
    try:
        q = str(question or "").split("\n\n[")[0].strip()[:400]
        prompt = (
            "אתה אורי, העוזר של המרכז האקדמי לב. הסטודנט שאל שאלה וקיבל תשובה. הצע 3 עד 4 שאלות המשך שהוא עשוי לרצות לשאול עכשיו, "
            "כפי שהוא עצמו היה כותב אותן (גוף ראשון, עד 5 מילים (עד 30 תווים) כל אחת, באותה שפה של השאלה).\n"
            "הן חייבות להיות קשורות ישירות לשאלה ולתשובה ולהביא מידע שימושי נוסף שאפשר לענות עליו (על המכללה, או על Moodle האישי שלו). "
            "אסור: כן/לא, 'יש לי שאלה נוספת', שאלות כלליות, או חזרה על מה שכבר נענה. מחזיר מערך JSON של מחרוזות בלבד.\n\n"
            f"שאלת הסטודנט: {q}\nתקציר התשובה: {digest}"
        )
        content = await asyncio.wait_for(_generate_once(
            [{"role": "user", "parts": [{"text": prompt}]}], api_key, max_output_tokens=180, call_type="followups",
            gen_extra={"responseMimeType": "application/json", "responseSchema": {"type": "ARRAY", "items": {"type": "STRING"}}},
        ), timeout=3.5)
        txt = "".join(part.get("text", "") for part in (content or {}).get("parts", []) if isinstance(part, dict))
        arr = json.loads(txt)
        out: List[str] = []
        for x in arr if isinstance(arr, list) else []:
            t = " ".join(str(x).split()).strip(" .\"'")
            if 3 <= len(t) <= 36 and t not in out and t not in ("כן", "לא", "כן, תסביר", "תודה"):
                out.append(t)
        return out[:4]
    except Exception as e:
        print(f"[followups] skipped: {type(e).__name__}: {e}")
        return []


async def stream_chat_with_tools(*args, **kwargs) -> AsyncGenerator[str, None]:
    """The chat engine for a student: the guarded answer (iron rule + file promise) and, after it, follow-up buttons so the student can keep going without typing."""
    acc: List[str] = []
    async for tok in _stream_chat_guarded(*args, **kwargs):
        if tok == VISUAL_TOKEN:
            acc.clear()                                          # the text so far was replaced by cards
        elif not tok.startswith(STATUS_OPEN):
            acc.append(tok)
        yield tok
    if not "".join(acc).strip():
        msg = "השרתים של ה-AI עמוסים כרגע ולא הצלחתי לענות. נסה שוב בעוד רגע, זה בדרך כלל עובר מהר."
        acc.append(msg)
        yield msg                                                # never an empty answer, even when every model failed
        return
    if not kwargs.get("emit_status"):
        return
    text = "".join(acc)
    question = args[0] if args else kwargs.get("question", "")
    api_key = args[2] if len(args) > 2 else kwargs.get("api_key", "")
    plain = _MARKERS.sub("", text).strip()
    blocks = _decode_ui_blocks(text)
    if kwargs.get("user_id") and not _FOLLOW_SKIP.search(text):
        poll = _courses_poll(blocks, question, kwargs.get("memories") or [], args[6] if len(args) > 6 else kwargs.get("recent_messages"))
        if poll:
            db.bump_api_usage("ui:coursepoll")
            yield poll
            return
    if _FOLLOW_SKIP.search(text) or any(b.get("k") == "buttons" for b in blocks):
        return                                                   # the answer already carries its own buttons or poll
    digest = (plain[:700] + " " + _blocks_digest(blocks)).strip()
    if _is_smalltalk(question) or _FAILURE_OPEN.match(plain) or len(digest) < 20:
        return
    labels = await _followups(question, digest, api_key)
    if labels:
        db.bump_api_usage("ui:followups")
        yield "\n\n" + f"[[ui:{encode_blocks([{'k': 'buttons', 's': 'quick', 'ti': '', 'it': [{'l': x, 'v': x} for x in labels]}])}]]"


async def stream_gemini_answer_with_moodle_tools(
    question: str,
    context_chunks: List[Dict[str, Any]],
    api_key: str,
    moodle_token: str,
    moodle_user_id: Any,
    bot_identity: str = "",
    catalog: Optional[List[Dict[str, str]]] = None,
    conversation_summary: str = "",
    recent_messages: Optional[List[Dict[str, str]]] = None,
    username: str = "",
    enabled_functions: Optional[set] = None,
) -> AsyncGenerator[str, None]:
    """
    כמו stream_gemini_answer, אבל עם אפשרות ל-Gemini לבצע function calling מול Moodle
    (קורסים/מטלות/ציונים/יומן) כשהשאלה אישית. סיבוב ראשון (לא מוזרם) נותן ל-Gemini
    הזדמנות להחליט אם להשתמש בכלי אחד או יותר (שאלה שנוגעת בכמה נושאים בבת אחת - למשל
    גם ציונים וגם מועד הגשה - מטופלת בכל הקריאות המקבילות שהמודל מבקש, לא רק הראשונה);
    אם כן - מבצעים את כל הקריאות האמיתיות ל-Moodle ומזינים את כל התוצאות בחזרה לסיבוב
    שני (מוזרם) שמנסח את התשובה הסופית לסטודנט.
    אם משהו בנתיב הזה נכשל מכל סיבה - נופלים חזרה לנתיב הרגיל (בלי כלים), בלי לשבור את הצ'אט.
    `enabled_functions` - סט שמות הפונקציות שהמנהל השאיר דלוקות (None = הכל דלוק).
    """
    from moodle_tools import get_enabled_tools, execute_moodle_function

    tools = get_enabled_tools(enabled_functions)
    if not tools:
        # המנהל כיבה את כל הפונקציות - אין טעם לפנות ל-Moodle בכלל, נתיב רגיל
        async for token in stream_gemini_answer(
            question, context_chunks, api_key, bot_identity, catalog, conversation_summary,
            recent_messages, username,
        ):
            yield token
        return

    user_turn_text = _build_chat_user_turn(
        question, context_chunks, bot_identity, catalog, conversation_summary, recent_messages,
        username=username, extra_system_instructions=PS.text("moodle"),
    )
    contents = [{"role": "user", "parts": [{"text": user_turn_text}]}]

    try:
        decision_content = await _generate_once(
            contents, api_key, tools=tools, max_output_tokens=400, call_type="chat_answer"
        )
    except Exception:
        decision_content = None

    parts = (decision_content or {}).get("parts", [])
    function_call_parts = [p for p in parts if isinstance(p, dict) and "functionCall" in p]

    if not function_call_parts:
        # גמיני לא ביקש כלי (שאלה לא אישית, או שלא זיהה צורך) - נתיב רגיל, בלי לבזבז את הסבב שכבר קרה
        async for token in stream_gemini_answer(
            question, context_chunks, api_key, bot_identity, catalog, conversation_summary,
            recent_messages, username,
        ):
            yield token
        return

    # מבצעים את כל הקריאות שהמודל ביקש (יכול להיות יותר מאחת לשאלה מרובת-נושאים), במקביל
    results = await asyncio.gather(*[
        execute_moodle_function(p["functionCall"].get("name", ""), p["functionCall"].get("args") or {}, moodle_token, moodle_user_id)
        for p in function_call_parts
    ])

    contents.append({"role": "model", "parts": function_call_parts})
    contents.append({
        "role": "user",
        "parts": [
            {"functionResponse": {"name": p["functionCall"].get("name", ""), "response": {"result": result}}}
            for p, result in zip(function_call_parts, results)
        ],
    })

    async for token in _stream_from_gemini(contents, api_key, call_type="chat_answer"):
        yield token


async def stream_gemini_answer(
    question: str,
    context_chunks: List[Dict[str, Any]],
    api_key: str,
    bot_identity: str = "",
    catalog: Optional[List[Dict[str, str]]] = None,
    conversation_summary: str = "",
    recent_messages: Optional[List[Dict[str, str]]] = None,
    username: str = "",
) -> AsyncGenerator[str, None]:
    """בונה את הפרומפט לתשובת צ'אט לסטודנט (עם ה-RAG שנשלף + זהות הבוט + מפת הנושאים + הקשר השיחה) ומזרים אותו מ-Gemini."""
    full_prompt = _build_chat_user_turn(
        question, context_chunks, bot_identity, catalog, conversation_summary, recent_messages, username=username
    )
    async for token in _stream_from_gemini(full_prompt, api_key, call_type="chat_answer"):
        yield token


async def summarize_conversation(old_summary: str, messages: List[Dict[str, str]], api_key: str) -> str:
    """
    מכווץ חלק ישן של השיחה לתוך סיכום קצר אחד (2-4 משפטים), כדי שלא יישלח למודל
    טקסט ארוך מדי ככל שהשיחה מתארכת - משלב את הסיכום הקודם עם ההודעות החדשות.
    """
    if not messages:
        return old_summary

    convo_text = "\n".join(
        f"{'סטודנט' if m['role'] == 'user' else 'בוט'}: {m['content']}" for m in messages
    )
    prompt = f"""סכם בקצרה (2-4 משפטים, בעברית) את קטע השיחה הבא בין סטודנט לעוזר AI של מכללה,
כדי לשמר את ההקשר החשוב להמשך השיחה (מה הסטודנט שאל/רצה, מה כבר נענה).
אם יש סיכום קודם - שלב אותו עם המידע החדש לכדי סיכום מעודכן אחד, לא שני סיכומים נפרדים.

סיכום קודם: {old_summary or "(אין סיכום קודם)"}

קטע השיחה החדש:
{convo_text}

החזר אך ורק את הסיכום המעודכן, בלי הקדמות או כותרות."""

    full_response = ""
    async for token in _stream_from_gemini(prompt, api_key, max_output_tokens=200, call_type="summarize"):
        full_response += token
    return full_response.strip()


CATEGORIZE_SYSTEM_PROMPT = """את/ה מסווג/ת מידע חדש למאגר ידע של צ'אטבוט מכללה.

קטגוריות קיימות במאגר: {categories}

מקטע חדש לסיווג:
כותרת: {title}
תוכן: {text}

המשימה שלך:
1. בחר/י קטגוריה מתאימה למקטע - קטגוריה קיימת מהרשימה אם היא מתאימה באמת, אחרת קטגוריה חדשה קצרה (מילה או שתיים, בעברית).
2. כתוב/כתבי תקציר שורה אחת שמתאר באופן כללי מה סוג המידע שנמצא בקטגוריה הזו (לא רק על המקטע הזה הספציפי).

החזר/י אך ורק בפורמט הזה, בלי שום טקסט נוסף לפני או אחרי:
קטגוריה: <קטגוריה>
תקציר: <תקציר שורה אחת>"""


async def categorize_and_summarize(
    title: str, text: str, api_key: str, existing_categories: List[str]
) -> Dict[str, str]:
    """
    מסווג מקטע ידע חדש לקטגוריה (קיימת או חדשה) וכותב תקציר קצר לקטגוריה הזו.
    זו ה"מפה" (קטלוג) שתמיד מוצגת למודל הצ'אט - מתעדכנת ע"י AI בכל הוספת מידע,
    ולא ידנית ע"י המנהל.
    """
    categories_text = ", ".join(existing_categories) if existing_categories else "(אין עדיין קטגוריות במאגר)"
    prompt = CATEGORIZE_SYSTEM_PROMPT.format(categories=categories_text, title=title, text=text)

    full_response = ""
    async for token in _stream_from_gemini(prompt, api_key, max_output_tokens=150, call_type="categorize"):
        full_response += token

    category_match = re.search(r"קטגוריה:\s*(.+)", full_response)
    summary_match = re.search(r"תקציר:\s*(.+)", full_response)

    category = category_match.group(1).strip() if category_match else "כללי"
    summary = summary_match.group(1).strip() if summary_match else f"מידע בנושא {title}"
    return {"category": category, "summary": summary}


KNOWLEDGE_DRAFT_SYSTEM_PROMPT = """אתה עוזר למנהל מערכת שמכין מידע רשמי למאגר הידע של צ'אטבוט המכללה.
המנהל ייתן לך תיאור חופשי, הערה, או טקסט גולמי בשפה חופשית. המשימה שלך:
1. לנסח ממנו "נוהל" קצר, ברור ומסודר שמתאים להיות מוצג לסטודנטים.
2. לשמור בדיוק על העובדות שנמסרו (טלפונים, חדרים, שעות, שמות, תאריכים) - בלי להמציא שום פרט שלא ניתן לך ובלי להשלים מספרים/שעות שלא צוינו.
3. אם משהו חסר או לא ברור - השאר את זה מנוסח בדיוק כפי שנמסר, אל תנחש ואל תמציא.

החזר את התשובה בפורמט הבא בדיוק, בלי שום הקדמה או הסבר נוסף:
כותרת: <כותרת קצרה וברורה לנוהל>
תוכן: <הנוסח המלא של הנוהל, בעברית ברורה ומסודרת>"""


async def stream_knowledge_draft(instruction: str, api_key: str) -> AsyncGenerator[str, None]:
    """
    מנסח עם Gemini טיוטת 'נוהל' (כותרת + תוכן) ממידע חופשי שהמנהל כותב.
    זו טיוטה בלבד להצגה ולאישור המנהל - השמירה בפועל למאגר הידע נעשית
    דרך אותו נתיב הוספת-טקסט הרגיל, רק אחרי שהמנהל בדק ואישר את הניסוח.
    """
    full_prompt = f"{KNOWLEDGE_DRAFT_SYSTEM_PROMPT}\n\nתיאור המנהל:\n{instruction.strip()}"
    async for token in _stream_from_gemini(full_prompt, api_key, max_output_tokens=500, call_type="ai_draft"):
        yield token


def _cd() -> dict:
    """מילון הכרטיסים של הבקשה הנוכחית. חשוב: מילון ריק הוא "שקר" בפייתון, ולכן אסור לכתוב `get_cards() or {}` - זה היה יוצר מילון זמני ומאבד את מה שנשמר בו."""
    from moodle_tools import get_cards
    c = get_cards()
    return c if c is not None else {}
