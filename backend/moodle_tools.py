"""
קריאות אמיתיות ל-Moodle webservice (JCT) בשביל function calling של Gemini.

חוק הברזל שסוכם: הטוקן של Moodle אף פעם לא נשמר אצלנו - לא ב-DB ולא בזיכרון השרת
בין בקשות. הוא מגיע מה-frontend בכל בקשת צ'אט (כש-הסטודנט מחובר ל-Moodle), משמש כאן
לקריאה המיידית הזו בלבד, ונעלם בסוף הבקשה. מה שכן שמור זמנית בזיכרון (לא ב-DB) הוא
*תוצאות* הקריאות (לא הטוקן עצמו) עם TTL קצר - כדי לא להציף את שרת ה-Moodle של המכללה
בקריאות חוזרות על כל הודעה בצ'אט.
"""
import asyncio
import time
from typing import Any, Dict, Optional

import httpx

MOODLE_BASE_URL = "https://moodle.jct.ac.il/webservice/rest/server.php"

_CACHE_TTL_SECONDS = 300  # 5 דקות - מספיק כדי לא להכביד על השרת, קצר מספיק כדי לא להציג מידע מיושן
_cache: Dict[tuple, tuple] = {}  # key -> (expires_at_monotonic, value)
_cache_lock = asyncio.Lock()


class MoodleTokenInvalid(Exception):
    pass


async def _moodle_call(wstoken: str, wsfunction: str, params: Optional[Dict[str, Any]] = None) -> Any:
    query = {"wstoken": wstoken, "wsfunction": wsfunction, "moodlewsrestformat": "json"}
    query.update(params or {})

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(MOODLE_BASE_URL, params=query)
    except httpx.HTTPError as e:
        raise RuntimeError(f"לא ניתן להתחבר לשרת Moodle כרגע ({type(e).__name__})")

    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError("תשובה לא תקינה מ-Moodle")

    if isinstance(data, dict) and data.get("exception"):
        if data.get("errorcode") == "invalidtoken":
            raise MoodleTokenInvalid()
        raise RuntimeError(f"שגיאה מ-Moodle: {data.get('message') or data.get('errorcode')}")
    return data


async def _cached_moodle_call(cache_key: tuple, wstoken: str, wsfunction: str, params: Optional[Dict[str, Any]] = None) -> Any:
    async with _cache_lock:
        entry = _cache.get(cache_key)
        if entry and entry[0] > time.monotonic():
            return entry[1]

    result = await _moodle_call(wstoken, wsfunction, params)

    async with _cache_lock:
        _cache[cache_key] = (time.monotonic() + _CACHE_TTL_SECONDS, result)
    return result


async def _get_courses(wstoken: str, moodle_user_id: Any):
    return await _cached_moodle_call(
        (moodle_user_id, "courses"), wstoken, "core_enrol_get_users_courses", {"userid": moodle_user_id}
    )


async def _resolve_course(wstoken: str, moodle_user_id: Any, course_name: str) -> Optional[dict]:
    """מתאים שם קורס חופשי (כפי שהסטודנט כתב) לאחד הקורסים שהוא רשום אליהם בפועל."""
    courses = await _get_courses(wstoken, moodle_user_id)
    if not courses:
        return None
    name = (course_name or "").strip().lower()
    if not name:
        return courses[0] if len(courses) == 1 else None
    for c in courses:
        full = (c.get("fullname") or "").lower()
        short = (c.get("shortname") or "").lower()
        if name in full or name in short or full in name:
            return c
    return None


def _fmt_ts(ts: Any) -> str:
    try:
        ts = int(ts)
        if not ts:
            return "ללא מועד"
        return time.strftime("%d/%m/%Y %H:%M", time.localtime(ts))
    except Exception:
        return "ללא מועד"


MOODLE_TOOLS = [{
    # camelCase - זה ה-REST JSON של Gemini (כמו generationConfig/maxOutputTokens שכבר בשימוש
    # בקוד הזה), לא snake_case כמו ב-SDK הפייתוני.
    "functionDeclarations": [
        {
            "name": "get_my_courses",
            "description": "מחזיר את רשימת הקורסים שהסטודנט רשום אליהם ב-Moodle השנה.",
            # ה-type הוא enum מסוג proto (Schema.Type) שמתבטא ב-REST JSON כמחרוזת רישיות
            # גדולות ("OBJECT"/"STRING") - לא "object"/"string" בסגנון JSON Schema רגיל.
            # זו כנראה הייתה הסיבה שהקריאה נדחתה (400) ונפלנו בשקט לנתיב בלי כלים.
            "parameters": {"type": "OBJECT", "properties": {}},
        },
        {
            "name": "get_course_assignments",
            "description": "מחזיר את המטלות ומועדי ההגשה בקורס מסוים של הסטודנט.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "course_name": {"type": "STRING", "description": "שם הקורס, בדיוק כפי שהסטודנט כתב אותו"},
                },
                "required": ["course_name"],
            },
        },
        {
            "name": "get_course_grades",
            "description": "מחזיר את הציונים של הסטודנט בקורס מסוים.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "course_name": {"type": "STRING", "description": "שם הקורס"},
                },
                "required": ["course_name"],
            },
        },
        {
            "name": "get_calendar_events",
            "description": "מחזיר אירועים ומועדים קרובים מהיומן האישי של הסטודנט ב-Moodle.",
            "parameters": {
                "type": "OBJECT",
                "properties": {
                    "from_date": {"type": "STRING", "description": "תאריך התחלה, פורמט YYYY-MM-DD (אופציונלי)"},
                    "to_date": {"type": "STRING", "description": "תאריך סיום, פורמט YYYY-MM-DD (אופציונלי)"},
                },
                "required": [],
            },
        },
    ],
}]

MOODLE_FUNCTION_NAMES = [f["name"] for f in MOODLE_TOOLS[0]["functionDeclarations"]]


def get_enabled_tools(enabled_names: Optional[set]) -> list:
    """
    מסנן את MOODLE_TOOLS לפי מה שהמנהל השאיר דלוק בפאנל הניהול.
    enabled_names=None פירושו "הכול דלוק" (ברירת המחדל, לתאימות אחורה).
    אם אחרי הסינון לא נשאר כלום - מחזיר רשימה ריקה (הקריאה תדע לדלג על Moodle לגמרי).
    """
    if enabled_names is None:
        return MOODLE_TOOLS
    declarations = [f for f in MOODLE_TOOLS[0]["functionDeclarations"] if f["name"] in enabled_names]
    if not declarations:
        return []
    return [{"functionDeclarations": declarations}]


async def execute_moodle_function(name: str, args: Dict[str, Any], wstoken: str, moodle_user_id: Any) -> str:
    """
    מבצע בפועל את הקריאה ל-Moodle לפי מה שגמיני ביקש, ומחזיר טקסט עברי קריא (לא JSON גולמי) -
    זה מה שחוזר ל-Gemini כ"תוצאת הפונקציה" לניסוח התשובה הסופית לסטודנט.
    בכוונה לעולם לא מעלה חריגה רגילה החוצה - כל כשל (טוקן שפג, Moodle לא זמין, פונקציה לא
    מאופשרת בשרת) הופך להודעה עברית ברורה שהמודל יכול להעביר הלאה לסטודנט בלי לקרוס.
    """
    if not wstoken or not moodle_user_id:
        return "אין חיבור פעיל ל-Moodle כרגע - יש להתחבר דרך האפליקציה הראשית כדי לראות מידע אישי."

    try:
        if name == "get_my_courses":
            courses = await _get_courses(wstoken, moodle_user_id)
            if not courses:
                return "לא נמצאו קורסים רשומים."
            return "רשימת הקורסים של הסטודנט:\n" + "\n".join(f"- {c.get('fullname')}" for c in courses)

        if name == "get_course_assignments":
            course_name = args.get("course_name", "")
            course = await _resolve_course(wstoken, moodle_user_id, course_name)
            if not course:
                return f"לא נמצא קורס בשם '{course_name}' ברשימת הקורסים של הסטודנט."
            data = await _cached_moodle_call(
                (moodle_user_id, "assignments", course["id"]), wstoken, "mod_assign_get_assignments",
                {"courseids[0]": course["id"]},
            )
            courses_data = (data or {}).get("courses", [])
            assignments = courses_data[0].get("assignments", []) if courses_data else []
            if not assignments:
                return f"לא נמצאו מטלות פתוחות בקורס '{course.get('fullname')}'."
            lines = [f"- {a.get('name')} (מועד הגשה: {_fmt_ts(a.get('duedate'))})" for a in assignments]
            return f"מטלות בקורס '{course.get('fullname')}':\n" + "\n".join(lines)

        if name == "get_course_grades":
            course_name = args.get("course_name", "")
            course = await _resolve_course(wstoken, moodle_user_id, course_name)
            if not course:
                return f"לא נמצא קורס בשם '{course_name}' ברשימת הקורסים של הסטודנט."
            data = await _cached_moodle_call(
                (moodle_user_id, "grades", course["id"]), wstoken, "gradereport_user_get_grade_items",
                {"courseid": course["id"], "userid": moodle_user_id},
            )
            usergrades = (data or {}).get("usergrades", [])
            items = usergrades[0].get("gradeitems", []) if usergrades else []
            lines = [
                f"- {i.get('itemname') or 'ציון קורס כולל'}: {(i.get('gradeformatted') or '').strip() or 'אין עדיין ציון'}"
                for i in items if i.get("itemname") or i.get("gradeformatted")
            ]
            if not lines:
                return f"לא נמצאו ציונים זמינים כרגע לקורס '{course.get('fullname')}'."
            return f"ציונים בקורס '{course.get('fullname')}':\n" + "\n".join(lines)

        if name == "get_calendar_events":
            data = await _cached_moodle_call(
                (moodle_user_id, "calendar"), wstoken, "core_calendar_get_calendar_events",
                {"options[userevents]": 1, "options[siteevents]": 0},
            )
            events = (data or {}).get("events", [])
            if not events:
                return "לא נמצאו אירועים קרובים ביומן."
            lines = [f"- {e.get('name')} ({_fmt_ts(e.get('timestart'))})" for e in events[:20]]
            return "אירועים קרובים ביומן:\n" + "\n".join(lines)

        return f"פונקציה לא מוכרת התבקשה: {name}"

    except MoodleTokenInvalid:
        return "ההתחברות ל-Moodle פגה - יש להתחבר מחדש באפליקציה הראשית כדי לראות מידע אישי."
    except Exception as e:
        return f"לא ניתן היה לשלוף את המידע הזה מ-Moodle כרגע ({type(e).__name__}). אפשר לנסות שוב בעוד רגע, או לבדוק ישירות ב-Moodle."
