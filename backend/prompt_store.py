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


# ------------------------------------------------------------------ comparing two saved states (for "go back to version")
def _describe(raw: str) -> Dict[str, Any]:
    """A saved state as comparable pieces: every rule with the text the bot gets from it, the section switches, the design controls and the custom designs."""
    st = _clean(json.loads(raw)) if raw else json.loads(json.dumps(_EMPTY))
    titles = {k: t for k, _, t, _ in SECTIONS}
    titles["mine"] = "ההוראות שלי"
    ruled = set(st["ruled"])
    rules = list(st["rules"])
    for key in SECTION_KEYS:
        if key not in ruled:                                        # a section still in its text form is compared through the same lossless conversion the builder uses
            sec = (st["sections"].get(key) or {})
            over = sec.get("text")
            rules += PR.convert_section(key, over if isinstance(over, str) and over.strip() else _DEFAULTS.get(key, ""))
    out_rules: Dict[str, Any] = {}
    for r in rules:
        text = PR.compile_rule({**r, "enabled": True, "pre": "", "post": ""}, None).strip()
        first = next((c.get("text") for c in r["cubes"] if c.get("text")), "")
        out_rules[r["id"]] = {"section": titles.get(r["section"], r["section"]), "title": (r.get("title") or first or "כלל")[:70], "enabled": r.get("enabled", True), "text": text or "(ריק)"}
    secs = {k: ((st["sections"].get(k) or {}).get("enabled") is not False) for k in SECTION_KEYS}
    return {"rules": out_rules, "sections": secs, "titles": titles, "designs": st["designs"], "custom": {c["id"]: c for c in st["custom"]}}


def diff_states(old_raw: str, new_raw: str) -> list:
    """What differs between a saved version (old) and the live state (new). Written from the point of view of going back to the old one."""
    a, b = _describe(old_raw), _describe(new_raw)
    ch: list = []
    for rid, nb in b["rules"].items():
        na = a["rules"].get(rid)
        if na is None:
            ch.append({"kind": "rule", "change": "added", "where": nb["section"], "title": nb["title"], "old": "", "new": nb["text"] + ("" if nb["enabled"] else "\n(הכלל כבוי)")})
        elif na["text"] != nb["text"] or na["enabled"] != nb["enabled"]:
            ch.append({"kind": "rule", "change": "changed", "where": nb["section"], "title": nb["title"],
                       "old": na["text"] + ("" if na["enabled"] else "\n(הכלל כבוי)"), "new": nb["text"] + ("" if nb["enabled"] else "\n(הכלל כבוי)")})
    for rid, na in a["rules"].items():
        if rid not in b["rules"]:
            ch.append({"kind": "rule", "change": "removed", "where": na["section"], "title": na["title"], "old": na["text"] + ("" if na["enabled"] else "\n(הכלל כבוי)"), "new": ""})
    for k, on in b["sections"].items():
        if a["sections"].get(k) != on:
            ch.append({"kind": "section", "change": "changed", "where": "חלקי הפרומט", "title": b["titles"].get(k, k), "old": "פעיל" if a["sections"].get(k) else "כבוי", "new": "פעיל" if on else "כבוי"})
    fmt = lambda d: ("פעיל" if d.get("enabled", True) else "כבוי") + " · עדיפות " + {"high": "גבוהה", "low": "נמוכה"}.get(d.get("priority"), "רגילה") + ((" · " + d["note"]) if d.get("note") else "")
    for k in sorted(set(a["designs"]) | set(b["designs"])):
        da, db_ = a["designs"].get(k, {"enabled": True, "priority": "normal", "note": ""}), b["designs"].get(k, {"enabled": True, "priority": "normal", "note": ""})
        if fmt(da) != fmt(db_):
            ch.append({"kind": "design", "change": "changed", "where": "בקרת עיצובים", "title": k, "old": fmt(da), "new": fmt(db_)})
    for cid in sorted(set(a["custom"]) | set(b["custom"])):
        ca, cb = a["custom"].get(cid), b["custom"].get(cid)
        if ca is None:
            ch.append({"kind": "custom", "change": "added", "where": "עיצוב שיצרתי", "title": cb.get("name", cid), "old": "", "new": cb.get("when", "")})
        elif cb is None:
            ch.append({"kind": "custom", "change": "removed", "where": "עיצוב שיצרתי", "title": ca.get("name", cid), "old": ca.get("when", ""), "new": ""})
        elif (ca.get("template"), ca.get("css"), ca.get("enabled"), ca.get("when")) != (cb.get("template"), cb.get("css"), cb.get("enabled"), cb.get("when")):
            ch.append({"kind": "custom", "change": "changed", "where": "עיצוב שיצרתי", "title": cb.get("name", cid), "old": ca.get("when", "") + ("" if ca.get("enabled", True) else " (כבוי)"), "new": cb.get("when", "") + ("" if cb.get("enabled", True) else " (כבוי)")})
    return ch


async def diff_with_version(version_id: int) -> Optional[Dict[str, Any]]:
    raw = await db.get_prompt_version(version_id)
    if raw is None:
        return None
    return {"changes": diff_states(raw, _RAW)}


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
