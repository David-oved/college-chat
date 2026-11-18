"""
The bot's prompt, split into named sections the admin can see and edit from the admin app (no deploy needed).
- The default text of every section lives in the code (registered by the module that owns it); the admin's override lives in the database.
- Notes: extra instructions the admin writes, each with its own switch and optional conditions (first message, new student, Moodle connected, language, once per student).
- Design controls: switch a block kind off, give it a priority and a note ("use it when ...").
- Every save keeps the previous state as a version, so a bad edit can be rolled back.
"""
import contextvars
import json
import time
import uuid
from typing import Any, Dict, List, Optional

from db import db
import prompt_rules as PR

# key -> (group, title, what it is). The order is the order the sections are put in the prompt and shown in the admin.
SECTIONS = [
    ("persona", "זהות", "מי הבוט", "המשפט הפותח: מי אתה ומה תפקידך."),
    ("rules", "זהות", "חוקי ברזל", "אי המצאת מידע, מתי לענות 'אין לי מידע', בלי אימוג'י, פתיחה ישירה."),
    ("rich", "תגובה", "תגובות עשירות, סקרים וכפתורים", "השם אורי, עיצוב טקסט, סקרים, כפתורי תשובה מהירה וזיכרון הסקרים."),
    ("ui_iron", "עיצוב", "כלל הברזל: תשובות חזותיות", "מתי תשובה חייבת להיות כרטיסים ולא טקסט."),
    ("ui_recipes", "עיצוב", "מתכוני עיצוב לפי סוג שאלה", "איזה שילוב בלוקים מתאים לאיש קשר, שעות, תהליך, מחיר, השוואה ועוד."),
    ("ui_standard", "עיצוב", "הסטנדרט לעיצוב ודוגמאות", "דוגמאות לתשובה יפה, פתיחה בולטת, צבע ואנימציה."),
    ("ui_moodle", "עיצוב", "בלוקים של Moodle", "איך מציגים מטלות, ציונים וקורסים."),
    ("ui_reference", "עיצוב", "מדריך סוגי הבלוקים", "הסבר לכל סוג בלוק: שדות, סגנונות ומתי משתמשים בו."),
    ("moodle", "Moodle", "כלי Moodle, תאריכים, מבחנים וקבצים", "מה עושים עם נתוני Moodle, איך שולחים קבצים, איך מציגים מבחנים. פעיל רק כשהסטודנט מחובר."),
    ("no_moodle", "Moodle", "כשאין גישה ל-Moodle", "מה הבוט אומר כשהסטודנט שואל על מידע אישי ואין חיבור."),
    ("memory", "זיכרון", "זיכרון על הסטודנט", "מה הבוט שומר ומה אסור לו לשמור."),
    ("kb_tool", "חיפוש", "חיפוש במידע המכללה", "מתי לקרוא לחיפוש ואיך לענות לפי מה שחזר."),
    ("precedence", "זהות", "סדר עדיפויות ושפה", "מה גובר על מה, באיזו שפה עונים, פנייה בשם."),
]
SECTION_KEYS = [s[0] for s in SECTIONS]

_DEFAULTS: Dict[str, str] = {}
_EMPTY = {"sections": {}, "notes": [], "designs": {}, "custom": [], "rules": [], "ruled": []}
_CTX: contextvars.ContextVar = contextvars.ContextVar("prompt_ctx", default=None)
_CONV: Dict[str, Any] = {}
_STATE: Dict[str, Any] = json.loads(json.dumps(_EMPTY))
_LOADED_AT = 0.0
_TTL = 15.0
_RAW = ""


def register(key: str, text: str):
    _DEFAULTS[key] = text


def default(key: str) -> str:
    return _DEFAULTS.get(key, "")


def set_ctx(ctx: Dict[str, Any]):
    """The facts about the current request (first message, new student, Moodle connected, language, has account) that rule conditions are checked against."""
    _CTX.set(ctx)


def _legacy_text(key: str) -> str:
    sec = (_STATE.get("sections") or {}).get(key) or {}
    over = sec.get("text")
    return over if isinstance(over, str) and over.strip() else _DEFAULTS.get(key, "")


def text(key: str) -> str:
    """The text the bot gets for this section: compiled from the admin's rules when the section was moved to the rule builder, otherwise the admin's
    text or the built-in one; empty if the admin switched the section off."""
    sec = (_STATE.get("sections") or {}).get(key) or {}
    if sec.get("enabled") is False:
        return ""
    if key in (_STATE.get("ruled") or []):
        return PR.compile_rules([r for r in _STATE.get("rules", []) if r.get("section") == key], _CTX.get())
    return _legacy_text(key)


def rules_for(key: str) -> list:
    """The rules of a section as the builder shows them: the saved ones, or the existing text converted (losslessly) into rules."""
    if key in (_STATE.get("ruled") or []):
        return [r for r in _STATE.get("rules", []) if r.get("section") == key]
    src = _legacy_text(key)
    hit = _CONV.get(key)
    if not hit or hit[0] != src:
        _CONV[key] = (src, PR.convert_section(key, src))
    return json.loads(json.dumps(_CONV[key][1]))


def state() -> Dict[str, Any]:
    return _STATE


def _clean(st: Any) -> Dict[str, Any]:
    out = json.loads(json.dumps(_EMPTY))
    if not isinstance(st, dict):
        return out
    for k, v in (st.get("sections") or {}).items():
        if k in SECTION_KEYS and isinstance(v, dict):
            t = v.get("text")
            out["sections"][k] = {"text": t[:30000] if isinstance(t, str) and t.strip() else None, "enabled": v.get("enabled") is not False}
    for n in (st.get("notes") or [])[:60]:
        if not isinstance(n, dict) or not str(n.get("text") or "").strip():
            continue
        w = n.get("when") if isinstance(n.get("when"), dict) else {}
        out["notes"].append({
            "id": str(n.get("id") or uuid.uuid4().hex[:8])[:16],
            "title": str(n.get("title") or "")[:80],
            "text": str(n.get("text"))[:2500],
            "enabled": n.get("enabled") is not False,
            "when": {
                "scope": w.get("scope") if w.get("scope") in ("always", "first_message", "new_student") else "always",
                "moodle": w.get("moodle") if w.get("moodle") in ("any", "yes", "no") else "any",
                "lang": w.get("lang") if w.get("lang") in ("he", "en", "fr") else "",
                "once": bool(w.get("once")),
            },
        })
    for k, v in (st.get("designs") or {}).items():
        if isinstance(v, dict) and isinstance(k, str) and len(k) <= 30:
            out["designs"][k] = {
                "enabled": v.get("enabled") is not False,
                "priority": v.get("priority") if v.get("priority") in ("low", "normal", "high") else "normal",
                "note": str(v.get("note") or "")[:500],
            }
    out["rules"] = [r for r in (PR.clean_rule(x) for x in (st.get("rules") or [])[:400]) if r]
    out["ruled"] = [k for k in (st.get("ruled") or []) if k in SECTION_KEYS or k == "mine"]
    if out["notes"] and "mine" not in out["ruled"]:
        out["rules"] += [PR.note_to_rule(n) for n in out["notes"]]          # notes written in the earlier editor become rules (same behaviour)
        out["ruled"].append("mine")
        out["notes"] = []
    out["custom"] = [c for c in (st.get("custom") or [])[:30] if isinstance(c, dict) and c.get("id") and c.get("template") is not None]
    return out


async def refresh(force: bool = False):
    global _STATE, _LOADED_AT, _RAW
    if not force and time.monotonic() - _LOADED_AT < _TTL:
        return
    _LOADED_AT = time.monotonic()
    try:
        raw = await db.get_prompt_state()
        _RAW = raw or ""
        _STATE = _clean(json.loads(raw)) if raw else json.loads(json.dumps(_EMPTY))
    except Exception as e:
        print(f"[prompt] could not load the prompt state: {type(e).__name__}: {e}")


async def save(new_state: Any, label: str = "") -> Dict[str, Any]:
    """Validates, keeps the previous state as a version, stores the new one and applies it at once."""
    global _STATE, _LOADED_AT, _RAW
    cleaned = _clean(new_state)
    if _RAW:
        try:
            await db.add_prompt_version(label or "גרסה קודמת", _RAW)
        except Exception as e:
            print(f"[prompt] could not keep a version: {e}")
    raw = json.dumps(cleaned, ensure_ascii=False)
    await db.set_prompt_state(raw)
    _RAW, _STATE, _LOADED_AT = raw, cleaned, time.monotonic()
    return cleaned


async def restore(version_id: int) -> Optional[Dict[str, Any]]:
    raw = await db.get_prompt_version(version_id)
    if not raw:
        return None
    return await save(json.loads(raw), f"לפני חזרה לגרסה {version_id}")


# ------------------------------------------------------------------ notes (extra instructions with conditions)
def notes_text(ctx: Dict[str, Any]) -> str:
    """The admin's own rules (section "mine"), those whose server-checkable conditions hold for this request."""
    body = PR.compile_rules([r for r in _STATE.get("rules", []) if r.get("section") == "mine"], ctx)
    if not body.strip():
        return ""
    return "\n\n=== הוראות נוספות מהמנהל (מעודכנות, יש לפעול לפיהן; הן לא מבטלות את חוקי הברזל) ===" + body + "\n"


# ------------------------------------------------------------------ design controls
def disabled_kinds() -> set:
    return {k for k, v in (_STATE.get("designs") or {}).items() if v.get("enabled") is False}


def design_text() -> str:
    d = _STATE.get("designs") or {}
    off = sorted(k for k, v in d.items() if v.get("enabled") is False)
    lines = []
    if off:
        lines.append("אסור להשתמש בסוגי הבלוקים האלה בכלל: " + ", ".join(off) + ".")
    pr = {"high": "עדיפות גבוהה", "low": "עדיפות נמוכה, רק כשאין ברירה"}
    for k, v in d.items():
        if v.get("enabled") is False:
            continue
        pri = pr.get(v.get("priority"), "")
        if pri or v.get("note"):
            lines.append(f"- {k}" + (f" ({pri})" if pri else "") + (f": {v['note']}" if v.get("note") else ""))
    custom = [c for c in (_STATE.get("custom") or []) if c.get("enabled") is not False]
    if custom:
        lines.append("עיצובים מיוחדים נוספים (משתמשים בהם דרך show_ui עם type=המזהה; השדות: title, subtitle, text, color, icon, items עם label/value/detail):")
        for c in custom:
            lines.append(f"- {c['id']} ({c.get('name', '')}): {c.get('when', '')}")
    if not lines:
        return ""
    return "\n\n=== הנחיות עיצוב מהמנהל (גוברות על המתכונים) ===\n" + "\n".join(lines) + "\n"


def custom_design(kind: str) -> Optional[Dict[str, Any]]:
    for c in _STATE.get("custom") or []:
        if c.get("id") == kind and c.get("enabled") is not False:
            return c
    return None
