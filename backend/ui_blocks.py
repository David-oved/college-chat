"""
ערכת רכיבי תצוגה שה-AI מרכיב בעצמו: הוא קורא ל-show_ui ומחליט איזה מידע להציג, באיזה סגנון ובאיזה סדר.
הנתונים עצמם לא עוברים דרך המודל: הכלים של Moodle נותנים לכל פריט מזהה (d1, g2, c3...) והמודל רק מצביע על מזהים - כך אי אפשר שהוא ימציא או יטעה בתאריך.
השרת מאמת ומנקה כל מה שהמודל שלח (סוגים וסגנונות מותרים, אורכים), והאפליקציה מציירת.
"""
import base64
import json
from typing import Any, Dict, List, Tuple

STYLES = {
    "deadlines": ("tickets", "timeline", "focus"),
    "grades": ("gauges", "list"),
    "courses": ("chips", "list"),
}
TONES = ("info", "warn", "success", "tip")

UI_ADDENDUM = """

=== תצוגה חזותית (show_ui) ===
הסטודנטים לא אוהבים לקרוא הרבה טקסט. כשהתשובה היא רשימה או נתונים (מטלות, מועדים, ציונים, קורסים) - אל תכתוב אותם כטקסט. תציג אותם עם הכלי show_ui, ואתה מחליט הכול: איזה פריטים, באיזה סגנון ובאיזה סדר.
איך: קודם שלוף נתונים עם הכלי המתאים. כל פריט בתוצאה מגיע עם מזהה בסוגריים מרובעים, למשל [d3]. אחר כך קרא ל-show_ui עם blocks, וכל בלוק מצביע על מזהים (ids) בלבד. בחר רק את מה שהסטודנט ביקש: אם ביקש קורס מסוים, שבוע מסוים, או "רק הדחופות" - הצג בדיוק את זה (אפשר לשלוף כמה קורסים בקריאות נפרדות ולהציג יחד).
סוגי בלוקים:
- deadlines: מטלות/מועדים לפי ids של [d..]. style: "tickets" (כרטיסי כרטיסייה, מתאים לרוב המקרים), "timeline" (ציר זמן לפי ימים, טוב כשיש הרבה פריטים על פני כמה ימים), "focus" (המטלה הדחופה גדולה והשאר קטנות, טוב כשיש משהו אחד דחוף במיוחד).
- grades: ציונים לפי ids של [g..]. style: "gauges" (מדי עיגול) או "list".
- courses: קורסים לפי ids של [c..]. style: "chips" או "list".
- callout: תיבת הדגשה עם text קצר ו-tone: info/warn/success/tip. מתאים להתראה ("מחר יש לך 3 הגשות") או טיפ.
- stats: מספרים גדולים, items של {label, value} (עד 4), למשל כמה מטלות, ציון ממוצע.
- steps: רשימת צעדים או משימות קצרה, items של {label}.
- progress: פסי התקדמות, items של {label, value} כשהערך הוא מספר 0 עד 100 (למשל התקדמות בקורס, אחוז הגשות).
- buttons: כפתורים שהסטודנט לוחץ והם שולחים הודעה בשמו: items של {label, value} (value הוא הטקסט שיישלח, ואם חסר נשלח ה-label). מתאים להצעת המשך ("הצג רק את הדחופות", "שלח לי את הקובץ").
לכל בלוק אפשר title קצר (עד 6 מילים). אפשר לשלב כמה בלוקים: למשל callout אחד ואחריו deadlines.
מהירות: אם צריך כמה קורסים או כמה כלים - קרא להם כולם באותו צעד, במקביל. הכלים מוצאים קורס לפי שם (גם חלקי), אז אם אתה כבר יודע את שם הקורס אל תקרא קודם ל-get_my_courses.
אחרי show_ui כתוב לכל היותר משפט קצר אחד (או כלום): אל תחזור על מה שמוצג. בשאלות פשוטות וקצרות ("מה השעה", "תודה") - טקסט רגיל בלי show_ui. אל תשתמש ב-show_ui עם מזהים שלא קיבלת מכלי בשיחה הזו.
"""

_ITEM = {
    "type": "OBJECT",
    "properties": {
        "label": {"type": "STRING", "description": "תווית"},
        "value": {"type": "STRING", "description": "ערך (למשל מספר)"},
    },
}

UI_TOOLS = [{"functionDeclarations": [{
    "name": "show_ui",
    "description": "מציג לסטודנט רכיבים חזותיים (כרטיסים, ציר זמן, מדי ציונים, תיבת הדגשה...) במקום טקסט. מצביעים על פריטים לפי המזהים שהכלים החזירו.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "blocks": {
                "type": "ARRAY",
                "description": "רשימת הבלוקים להצגה, לפי הסדר",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "type": {"type": "STRING", "description": "deadlines | grades | courses | callout | stats | steps | progress | buttons"},
                        "style": {"type": "STRING", "description": "deadlines: tickets/timeline/focus; grades: gauges/list; courses: chips/list"},
                        "title": {"type": "STRING", "description": "כותרת קצרה (אופציונלי)"},
                        "ids": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "מזהי פריטים כמו d1, g2, c3"},
                        "text": {"type": "STRING", "description": "לבלוק callout"},
                        "tone": {"type": "STRING", "description": "info | warn | success | tip"},
                        "items": {"type": "ARRAY", "items": _ITEM, "description": "ל-stats, steps, progress ו-buttons"},
                    },
                    "required": ["type"],
                },
            }
        },
        "required": ["blocks"],
    },
}]}]


def _s(v: Any, n: int) -> str:
    return " ".join(str(v or "").split())[:n]


def resolve_blocks(blocks: Any, items: Dict[str, Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], str]:
    """מאמת את מה שהמודל ביקש. מחזיר (בלוקים נקיים, הודעה למודל)."""
    out: List[Dict[str, Any]] = []
    problems: List[str] = []
    if not isinstance(blocks, list):
        return [], "blocks חייב להיות רשימה."
    for b in blocks[:6]:
        if not isinstance(b, dict):
            continue
        kind = _s(b.get("type"), 20)
        title = _s(b.get("title"), 60)
        if kind in STYLES:
            want = {"deadlines": "d", "grades": "g", "courses": "c"}[kind]
            ids = [str(i) for i in (b.get("ids") or []) if isinstance(i, (str, int))][:24]
            rows = []
            for i in ids:
                it = items.get(i)
                if it and it.get("k") == want:
                    rows.append({k: v for k, v in it.items() if k != "k" and k in ("n", "c", "t", "v", "d", "u", "o", "g")})
            if not rows:
                problems.append(f"בלוק {kind}: לא נמצאו פריטים לפי ה-ids שנשלחו (השתמש רק במזהים שהכלים החזירו).")
                continue
            if kind == "deadlines":
                rows.sort(key=lambda r: r.get("t", 0))
            style = _s(b.get("style"), 12)
            if style not in STYLES[kind]:
                style = STYLES[kind][0]
            out.append({"k": kind, "s": style, "ti": title, "it": rows})
        elif kind == "callout":
            text = _s(b.get("text"), 220)
            if text:
                tone = _s(b.get("tone"), 10)
                out.append({"k": "callout", "tone": tone if tone in TONES else "info", "ti": title, "tx": text})
        elif kind in ("stats", "steps", "progress", "buttons"):
            its = []
            for x in (b.get("items") or [])[: {"stats": 4, "steps": 8, "progress": 8, "buttons": 4}[kind]]:
                if isinstance(x, dict) and _s(x.get("label"), 60):
                    its.append({"l": _s(x.get("label"), 60), "v": _s(x.get("value"), 120 if kind == "buttons" else 14)})
            if its:
                out.append({"k": kind, "ti": title, "it": its})
        else:
            problems.append(f"סוג בלוק לא מוכר: {kind}")
    if not out:
        return [], "לא הוצג כלום. " + " ".join(problems) + " תקן והצג שוב, או כתוב תשובה בטקסט."
    msg = "הוצג לסטודנט בהצלחה. כתוב לכל היותר משפט קצר אחד (או כלום), בלי לחזור על מה שמוצג."
    if problems:
        msg += " (הערות: " + " ".join(problems) + ")"
    return out, msg


def encode_blocks(blocks: List[Dict[str, Any]]) -> str:
    raw = json.dumps({"b": blocks}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")
