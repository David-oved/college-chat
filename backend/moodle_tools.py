"""
קריאות אמיתיות ל-Moodle webservice (JCT) בשביל function calling של Gemini, ובשביל בדיקת
המנהל (הדבקת טוקן אמיתי בפאנל הניהול ובדיקת כל פונקציה בנפרד).

חוק הברזל שסוכם: הטוקן של Moodle אף פעם לא נשמר אצלנו - לא ב-DB ולא בזיכרון השרת
בין בקשות. הוא מגיע מה-frontend בכל בקשה רלוונטית (צ'אט סטודנט, או בדיקת מנהל), משמש כאן
לקריאה המיידית הזו בלבד, ונעלם בסוף הבקשה. מה שכן שמור זמנית בזיכרון (לא ב-DB) הוא
*תוצאות* הקריאות (לא הטוקן עצמו) עם TTL קצר - כדי לא להציף את שרת ה-Moodle של המכללה
בקריאות חוזרות על כל הודעה בצ'אט.

כל פונקציה רשומה פעם אחת ב-FUNCTION_REGISTRY (שם, תיאור למנהל, תיאור ל-AI, פרמטרים,
ומבצע) - משם נגזרים גם ה-function declarations של Gemini וגם רשימת הפונקציות שמוצגת
בפאנל הניהול, כדי שלא יהיו שלושה מקומות שונים שצריך לעדכן בכל הוספת פונקציה.
"""
import asyncio
import time
from typing import Any, Dict, List, Optional, Tuple

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
    """מתאים שם קורס חופשי (כפי שהסטודנט/המנהל כתב) לאחד הקורסים הרשומים בפועל."""
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


# =====================================================================
# רישום הפונקציות - מקור אמת יחיד. כל רשומה: שם, אייקון+תווית לתצוגה, תיאור
# למנהל, תיאור ל-AI (מה שגמיני רואה), פרמטרים (לבניית ה-schema וגם שדות הקלט
# בפאנל הבדיקה), ומבצע אסינכרוני שמחזיר (raw, formatted).
# =====================================================================
FUNCTION_REGISTRY: List[Dict[str, Any]] = []


def _register(name, moodle_function, icon, label, description_admin, description_ai, params):
    def decorator(fn):
        FUNCTION_REGISTRY.append({
            "name": name,
            "moodle_function": moodle_function,
            "icon": icon,
            "label": label,
            "description_admin": description_admin,
            "description_ai": description_ai,
            "params": params,  # list of {"name", "type" (STRING), "description", "required"}
            "executor": fn,
        })
        return fn
    return decorator


@_register(
    name="get_my_courses", moodle_function="core_enrol_get_users_courses",
    icon="📚", label="רשימת הקורסים שלי",
    description_admin="מחזיר את כל הקורסים שהסטודנט רשום אליהם ב-Moodle.",
    description_ai="מחזיר את רשימת הקורסים שהסטודנט רשום אליהם ב-Moodle השנה.",
    params=[],
)
async def _exec_get_my_courses(wstoken, moodle_user_id, args) -> Tuple[Any, str]:
    courses = await _get_courses(wstoken, moodle_user_id)
    if not courses:
        return courses, "לא נמצאו קורסים רשומים."
    return courses, "רשימת הקורסים של הסטודנט:\n" + "\n".join(f"- {c.get('fullname')}" for c in courses)


@_register(
    name="get_course_assignments", moodle_function="mod_assign_get_assignments",
    icon="📝", label="מטלות ומועדי הגשה",
    description_admin="מחזיר את כל המטלות ומועדי ההגשה בקורס מסוים.",
    description_ai="מחזיר את המטלות ומועדי ההגשה בקורס מסוים של הסטודנט.",
    params=[{"name": "course_name", "type": "STRING", "description": "שם הקורס", "required": True}],
)
async def _exec_get_course_assignments(wstoken, moodle_user_id, args) -> Tuple[Any, str]:
    course_name = args.get("course_name", "")
    course = await _resolve_course(wstoken, moodle_user_id, course_name)
    if not course:
        return None, f"לא נמצא קורס בשם '{course_name}' ברשימת הקורסים."
    data = await _cached_moodle_call(
        (moodle_user_id, "assignments", course["id"]), wstoken, "mod_assign_get_assignments",
        {"courseids[0]": course["id"]},
    )
    courses_data = (data or {}).get("courses", [])
    assignments = courses_data[0].get("assignments", []) if courses_data else []
    if not assignments:
        return data, f"לא נמצאו מטלות פתוחות בקורס '{course.get('fullname')}'."
    lines = [f"- {a.get('name')} (מועד הגשה: {_fmt_ts(a.get('duedate'))})" for a in assignments]
    return data, f"מטלות בקורס '{course.get('fullname')}':\n" + "\n".join(lines)


@_register(
    name="get_course_grades", moodle_function="gradereport_user_get_grade_items",
    icon="🎯", label="ציונים בקורס",
    description_admin="מחזיר את הציונים המפורטים של הסטודנט בקורס מסוים (לפי פריטי הערכה).",
    description_ai="מחזיר את הציונים של הסטודנט בקורס מסוים.",
    params=[{"name": "course_name", "type": "STRING", "description": "שם הקורס", "required": True}],
)
async def _exec_get_course_grades(wstoken, moodle_user_id, args) -> Tuple[Any, str]:
    course_name = args.get("course_name", "")
    course = await _resolve_course(wstoken, moodle_user_id, course_name)
    if not course:
        return None, f"לא נמצא קורס בשם '{course_name}' ברשימת הקורסים."
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
        return data, f"לא נמצאו ציונים זמינים כרגע לקורס '{course.get('fullname')}'."
    return data, f"ציונים בקורס '{course.get('fullname')}':\n" + "\n".join(lines)


@_register(
    name="get_calendar_events", moodle_function="core_calendar_get_calendar_events",
    icon="📅", label="יומן ואירועים",
    description_admin="מחזיר אירועים אישיים מהיומן של הסטודנט ב-Moodle.",
    description_ai="מחזיר אירועים ומועדים קרובים מהיומן האישי של הסטודנט ב-Moodle.",
    params=[],
)
async def _exec_get_calendar_events(wstoken, moodle_user_id, args) -> Tuple[Any, str]:
    data = await _cached_moodle_call(
        (moodle_user_id, "calendar"), wstoken, "core_calendar_get_calendar_events",
        {"options[userevents]": 1, "options[siteevents]": 0},
    )
    events = (data or {}).get("events", [])
    if not events:
        return data, "לא נמצאו אירועים קרובים ביומן."
    lines = [f"- {e.get('name')} ({_fmt_ts(e.get('timestart'))})" for e in events[:20]]
    return data, "אירועים קרובים ביומן:\n" + "\n".join(lines)


@_register(
    name="get_course_content", moodle_function="core_course_get_contents",
    icon="🗂️", label="תוכן הקורס (נושאים וחומרים)",
    description_admin="מחזיר את המבנה המלא של קורס - נושאים/שיעורים וכל החומרים/קבצים/קישורים בכל נושא.",
    description_ai="מחזיר את המבנה והתוכן של קורס מסוים - אילו נושאים/שיעורים יש ואילו חומרים (קבצים, קישורים, פעילויות) יש בכל נושא.",
    params=[{"name": "course_name", "type": "STRING", "description": "שם הקורס", "required": True}],
)
async def _exec_get_course_content(wstoken, moodle_user_id, args) -> Tuple[Any, str]:
    course_name = args.get("course_name", "")
    course = await _resolve_course(wstoken, moodle_user_id, course_name)
    if not course:
        return None, f"לא נמצא קורס בשם '{course_name}' ברשימת הקורסים."
    data = await _cached_moodle_call(
        (moodle_user_id, "content", course["id"]), wstoken, "core_course_get_contents", {"courseid": course["id"]},
    )
    sections = data if isinstance(data, list) else []
    if not sections:
        return data, f"לא נמצא תוכן בקורס '{course.get('fullname')}'."
    lines = []
    for s in sections:
        section_name = s.get("name") or "נושא"
        modules = s.get("modules") or []
        if not modules:
            continue
        lines.append(f"📂 {section_name}:")
        lines.extend(f"  - {m.get('name')} ({m.get('modname')})" for m in modules)
    if not lines:
        return data, f"לא נמצא תוכן בקורס '{course.get('fullname')}'."
    return data, f"תוכן הקורס '{course.get('fullname')}':\n" + "\n".join(lines)


@_register(
    name="get_assignment_submission_status", moodle_function="mod_assign_get_submission_status",
    icon="✅", label="סטטוס הגשת מטלה",
    description_admin="בודק אם מטלה מסוימת כבר הוגשה בפועל על ידי הסטודנט, ומה הסטטוס שלה (לעומת get_course_assignments שרק מראה אילו מטלות קיימות).",
    description_ai="בודק אם הסטודנט כבר הגיש מטלה מסוימת בקורס, ומה הסטטוס/ציון שלה.",
    params=[
        {"name": "course_name", "type": "STRING", "description": "שם הקורס", "required": True},
        {"name": "assignment_name", "type": "STRING", "description": "שם המטלה", "required": True},
    ],
)
async def _exec_get_assignment_submission_status(wstoken, moodle_user_id, args) -> Tuple[Any, str]:
    course_name = args.get("course_name", "")
    assignment_name = (args.get("assignment_name") or "").strip().lower()
    course = await _resolve_course(wstoken, moodle_user_id, course_name)
    if not course:
        return None, f"לא נמצא קורס בשם '{course_name}' ברשימת הקורסים."

    assignments_data = await _cached_moodle_call(
        (moodle_user_id, "assignments", course["id"]), wstoken, "mod_assign_get_assignments",
        {"courseids[0]": course["id"]},
    )
    courses_data = (assignments_data or {}).get("courses", [])
    assignments = courses_data[0].get("assignments", []) if courses_data else []
    match = next((a for a in assignments if assignment_name in (a.get("name") or "").lower()), None)
    if not match:
        return assignments_data, f"לא נמצאה מטלה בשם '{args.get('assignment_name')}' בקורס '{course.get('fullname')}'."

    data = await _moodle_call(wstoken, "mod_assign_get_submission_status", {"assignid": match["id"], "userid": moodle_user_id})
    submission = (data or {}).get("lastattempt", {}).get("submission", {})
    status = submission.get("status")
    status_he = {"submitted": "הוגשה", "new": "טרם הוגשה", "draft": "טיוטה בלבד, עדיין לא הוגשה סופית"}.get(status, status or "לא ידוע")
    return data, f"סטטוס הגשת '{match.get('name')}' בקורס '{course.get('fullname')}': {status_he}."


@_register(
    name="get_upcoming_deadlines", moodle_function="core_calendar_get_action_events_by_timesort",
    icon="⏰", label="דברים דחופים (כל הקורסים)",
    description_admin="מחזיר את כל המטלות/בחינות הקרובות בכל הקורסים יחד, ממוינות לפי זמן - שונה מ'יומן ואירועים' כי זה ממוקד רק בדברים שדורשים פעולה.",
    description_ai="מחזיר את הדברים הכי דחופים שממתינים לסטודנט בכל הקורסים יחד (מטלות/מבחנים קרובים), ממוינים לפי זמן.",
    params=[],
)
async def _exec_get_upcoming_deadlines(wstoken, moodle_user_id, args) -> Tuple[Any, str]:
    now = int(time.time())
    data = await _cached_moodle_call(
        (moodle_user_id, "upcoming"), wstoken, "core_calendar_get_action_events_by_timesort",
        {"timesortfrom": now, "limitnum": 15},
    )
    events = (data or {}).get("events", [])
    if not events:
        return data, "אין כרגע דברים דחופים ממתינים."
    lines = [f"- {e.get('name')} ({(e.get('course') or {}).get('fullname', '')}) - {_fmt_ts(e.get('timesort'))}" for e in events]
    return data, "דברים דחופים שממתינים (כל הקורסים):\n" + "\n".join(lines)


@_register(
    name="get_overall_grades", moodle_function="gradereport_overview_get_course_grades",
    icon="📈", label="סקירת ציונים כללית",
    description_admin="מחזיר את הציון/אחוז הכולל בכל הקורסים יחד - סקירה כללית, לא פירוט לפי קורס בודד.",
    description_ai="מחזיר סקירה כללית של הציון הכולל בכל הקורסים של הסטודנט בבת אחת.",
    params=[],
)
async def _exec_get_overall_grades(wstoken, moodle_user_id, args) -> Tuple[Any, str]:
    courses = await _get_courses(wstoken, moodle_user_id)
    course_names = {c["id"]: c.get("fullname") for c in (courses or [])}
    data = await _cached_moodle_call(
        (moodle_user_id, "overall-grades"), wstoken, "gradereport_overview_get_course_grades",
        {"userid": moodle_user_id},
    )
    grades = (data or {}).get("grades", [])
    if not grades:
        return data, "לא נמצאו ציונים כוללים זמינים."
    lines = [f"- {course_names.get(g.get('courseid'), g.get('courseid'))}: {(g.get('grade') or 'אין עדיין ציון')}" for g in grades]
    return data, "סקירת ציונים כללית בכל הקורסים:\n" + "\n".join(lines)


@_register(
    name="get_quiz_attempts", moodle_function="mod_quiz_get_user_attempts",
    icon="🧪", label="ניסיונות במבחן מקוון",
    description_admin="מחזיר את כל הניסיונות (ותוצאותיהם) של הסטודנט במבחן מקוון (Quiz) מסוים בקורס.",
    description_ai="מחזיר את הניסיונות וההישגים של הסטודנט במבחן מקוון (Quiz) מסוים.",
    params=[
        {"name": "course_name", "type": "STRING", "description": "שם הקורס", "required": True},
        {"name": "quiz_name", "type": "STRING", "description": "שם המבחן המקוון", "required": True},
    ],
)
async def _exec_get_quiz_attempts(wstoken, moodle_user_id, args) -> Tuple[Any, str]:
    course_name = args.get("course_name", "")
    quiz_name = (args.get("quiz_name") or "").strip().lower()
    course = await _resolve_course(wstoken, moodle_user_id, course_name)
    if not course:
        return None, f"לא נמצא קורס בשם '{course_name}' ברשימת הקורסים."

    quizzes_data = await _cached_moodle_call(
        (moodle_user_id, "quizzes", course["id"]), wstoken, "mod_quiz_get_quizzes_by_courses",
        {"courseids[0]": course["id"]},
    )
    quizzes = (quizzes_data or {}).get("quizzes", [])
    match = next((q for q in quizzes if quiz_name in (q.get("name") or "").lower()), None)
    if not match:
        return quizzes_data, f"לא נמצא מבחן מקוון בשם '{args.get('quiz_name')}' בקורס '{course.get('fullname')}'."

    data = await _moodle_call(wstoken, "mod_quiz_get_user_attempts", {"quizid": match["id"], "userid": moodle_user_id})
    attempts = (data or {}).get("attempts", [])
    if not attempts:
        return data, f"אין עדיין ניסיונות ב-'{match.get('name')}'."
    lines = [f"- ניסיון {a.get('attempt')}: סטטוס {a.get('state')}, ציון {a.get('sumgrade', 'אין עדיין')}" for a in attempts]
    return data, f"ניסיונות ב-'{match.get('name')}':\n" + "\n".join(lines)


@_register(
    name="get_completion_status", moodle_function="core_completion_get_activities_completion_status",
    icon="☑️", label="מה הושלם בקורס",
    description_admin="מחזיר אילו פעילויות/משימות בקורס כבר סומנו כ'הושלמו' ואילו עדיין לא, לפי מעקב ההשלמה של Moodle.",
    description_ai="מחזיר מה הסטודנט כבר השלים ומה עוד נשאר לו להשלים בקורס מסוים, לפי מעקב ההשלמה של Moodle.",
    params=[{"name": "course_name", "type": "STRING", "description": "שם הקורס", "required": True}],
)
async def _exec_get_completion_status(wstoken, moodle_user_id, args) -> Tuple[Any, str]:
    course_name = args.get("course_name", "")
    course = await _resolve_course(wstoken, moodle_user_id, course_name)
    if not course:
        return None, f"לא נמצא קורס בשם '{course_name}' ברשימת הקורסים."
    data = await _cached_moodle_call(
        (moodle_user_id, "completion", course["id"]), wstoken, "core_completion_get_activities_completion_status",
        {"courseid": course["id"], "userid": moodle_user_id},
    )
    statuses = (data or {}).get("statuses", [])
    if not statuses:
        return data, f"אין מעקב השלמה זמין בקורס '{course.get('fullname')}'."
    done = sum(1 for s in statuses if s.get("state") in (1, 2))
    lines = [f"- {s.get('cmid')}: {'הושלם' if s.get('state') in (1, 2) else 'טרם הושלם'}" for s in statuses]
    summary = f"הושלמו {done} מתוך {len(statuses)} פעילויות בקורס '{course.get('fullname')}':\n"
    return data, summary + "\n".join(lines)


# =====================================================================
# API ציבורי - נגזר מ-FUNCTION_REGISTRY
# =====================================================================
_REGISTRY_BY_NAME = {f["name"]: f for f in FUNCTION_REGISTRY}
MOODLE_FUNCTION_NAMES = [f["name"] for f in FUNCTION_REGISTRY]


def _build_function_declaration(entry: dict) -> dict:
    properties = {p["name"]: {"type": p["type"], "description": p["description"]} for p in entry["params"]}
    required = [p["name"] for p in entry["params"] if p.get("required")]
    parameters: Dict[str, Any] = {"type": "OBJECT", "properties": properties}
    if required:
        parameters["required"] = required
    return {"name": entry["name"], "description": entry["description_ai"], "parameters": parameters}


MOODLE_TOOLS = [{"functionDeclarations": [_build_function_declaration(e) for e in FUNCTION_REGISTRY]}]


def get_enabled_tools(enabled_names: Optional[set]) -> list:
    """
    מסנן את MOODLE_TOOLS לפי מה שהמנהל השאיר דלוק בפאנל הניהול.
    enabled_names=None פירושו "הכול דלוק" (ברירת המחדל, לתאימות אחורה - גם לפונקציות חדשות).
    אם אחרי הסינון לא נשאר כלום - מחזיר רשימה ריקה (הקריאה תדע לדלג על Moodle לגמרי).
    """
    if enabled_names is None:
        return MOODLE_TOOLS
    declarations = [_build_function_declaration(e) for e in FUNCTION_REGISTRY if e["name"] in enabled_names]
    return [{"functionDeclarations": declarations}] if declarations else []


def get_function_catalog() -> List[Dict[str, Any]]:
    """לתצוגה בפאנל הניהול - שם, אייקון, תווית, תיאור ופרמטרים, בלי פרטי המימוש הפנימיים."""
    return [
        {
            "name": e["name"],
            "icon": e["icon"],
            "label": e["label"],
            "description": e["description_admin"],
            "moodle_function": e["moodle_function"],
            "params": [{"name": p["name"], "description": p["description"], "required": p.get("required", False)} for p in e["params"]],
        }
        for e in FUNCTION_REGISTRY
    ]


async def execute_moodle_function(name: str, args: Dict[str, Any], wstoken: str, moodle_user_id: Any) -> str:
    """
    מבצע בפועל את הקריאה ל-Moodle לפי מה שגמיני ביקש, ומחזיר טקסט עברי קריא (לא JSON גולמי) -
    זה מה שחוזר ל-Gemini כ"תוצאת הפונקציה" לניסוח התשובה הסופית לסטודנט.
    בכוונה לעולם לא מעלה חריגה רגילה החוצה - כל כשל (טוקן שפג, Moodle לא זמין, פונקציה לא
    מאופשרת בשרת) הופך להודעה עברית ברורה שהמודל יכול להעביר הלאה לסטודנט בלי לקרוס.
    """
    if not wstoken or not moodle_user_id:
        return "אין חיבור פעיל ל-Moodle כרגע - יש להתחבר דרך האפליקציה הראשית כדי לראות מידע אישי."

    entry = _REGISTRY_BY_NAME.get(name)
    if not entry:
        return f"פונקציה לא מוכרת התבקשה: {name}"

    try:
        _, formatted = await entry["executor"](wstoken, moodle_user_id, args)
        return formatted
    except MoodleTokenInvalid:
        return "ההתחברות ל-Moodle פגה - יש להתחבר מחדש באפליקציה הראשית כדי לראות מידע אישי."
    except Exception as e:
        return f"לא ניתן היה לשלוף את המידע הזה מ-Moodle כרגע ({type(e).__name__}). אפשר לנסות שוב בעוד רגע, או לבדוק ישירות ב-Moodle."


async def test_function(name: str, args: Dict[str, Any], wstoken: str, moodle_user_id: Any) -> Dict[str, Any]:
    """
    לבדיקת מנהל בלבד (לא בזרימת הצ'אט): מריץ פונקציה ספציפית ומחזיר גם את ה-JSON הגולמי
    מ-Moodle וגם את הניסוח העברי - כדי שאפשר יהיה לוודא שהפונקציה באמת עובדת ולראות בדיוק
    מה מתקבל, לפני שסומכים עליה בצ'אט האמיתי.
    """
    entry = _REGISTRY_BY_NAME.get(name)
    if not entry:
        return {"ok": False, "error": f"פונקציה לא מוכרת: {name}"}
    try:
        raw, formatted = await entry["executor"](wstoken, moodle_user_id, args)
        return {"ok": True, "raw": raw, "formatted": formatted}
    except MoodleTokenInvalid:
        return {"ok": False, "error": "הטוקן לא תקף או פג - יש להתחבר מחדש"}
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
