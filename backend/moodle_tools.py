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
import base64
import contextvars
import html
from hebdate import describe_ts
import html
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import unquote, urlsplit

import httpx
from http_client import shared_client

MOODLE_BASE_URL = "https://moodle.jct.ac.il/webservice/rest/server.php"

# --- שליחת קבצים לסטודנט ---
# כתובת קובץ של Moodle דורשת את הטוקן של הסטודנט (?token=...). הטוקן לא יכול להופיע בטקסט שהמודל כותב
# (זה נשמר ב-DB ומוצג בצ'אט), לכן הכלי מחזיר "אסמכתא" (ref) אטומה בלבד: הנתיב של הקובץ בתוך
# webservice/pluginfile.php, בקידוד base64url. האפליקציה מבקשת מהשרת את הקובץ באמצעות ה-ref + הטוקן
# (בגוף בקשה מאובטחת, לא בכתובת), והשרת מוריד אותו מ-Moodle ומעביר הלאה.
_MOODLE_HOST = urlsplit(MOODLE_BASE_URL).netloc
_FILE_PREFIX = "/webservice/pluginfile.php/"
MOODLE_FILE_BASE = f"https://{_MOODLE_HOST}{_FILE_PREFIX}"
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._~%!$&'()*+,;=:@/-]+$")
MAX_FILES_PER_LISTING = 40


def make_file_ref(fileurl: Any) -> Optional[str]:
    """הופך fileurl של Moodle ל-ref בטוח, או None אם זו לא כתובת קובץ תקינה באתר ה-Moodle שלנו."""
    try:
        u = urlsplit(str(fileurl or ""))
    except ValueError:
        return None
    if u.netloc != _MOODLE_HOST or not u.path.startswith(_FILE_PREFIX):
        return None
    rest = u.path[len(_FILE_PREFIX):]
    if not rest or ".." in unquote(rest) or not _SAFE_PATH.match(rest):
        return None
    return base64.urlsafe_b64encode(rest.encode("utf-8")).decode("ascii").rstrip("=")


def decode_file_ref(ref: str) -> Optional[str]:
    """מפענח ref ומאמת אותו. מחזיר את הנתיב בתוך pluginfile.php, או None אם הוא לא תקין (למניעת כל ניסיון להגיע לכתובת אחרת)."""
    try:
        raw = base64.urlsafe_b64decode((ref or "") + "=" * (-len(ref or "") % 4)).decode("utf-8")
    except Exception:
        return None
    if not raw or len(raw) > 1200 or raw.startswith("/") or "://" in raw or not _SAFE_PATH.match(raw) or ".." in unquote(raw):
        return None
    return raw


def _fmt_size(n: Any) -> str:
    try:
        n = int(n or 0)
    except (TypeError, ValueError):
        return ""
    if n <= 0:
        return ""
    if n < 1024:
        return f"{n}B"
    if n < 1024 * 1024:
        return f"{n / 1024:.0f}KB"
    return f"{n / (1024 * 1024):.1f}MB"


def _file_kind(filename: str, mimetype: str) -> str:
    ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
    names = {"pdf": "PDF", "doc": "Word", "docx": "Word", "ppt": "PowerPoint", "pptx": "PowerPoint", "xls": "Excel", "xlsx": "Excel",
             "zip": "ZIP", "rar": "RAR", "txt": "טקסט", "py": "Python", "java": "Java", "c": "C", "cpp": "C++", "png": "תמונה", "jpg": "תמונה", "jpeg": "תמונה"}
    return names.get(ext) or (mimetype.split("/")[-1].upper() if mimetype else ext.upper() or "קובץ")


def _clean_label(s: Any) -> str:
    return re.sub(r"[\[\]|\r\n]+", " ", str(s or "")).strip()[:90] or "קובץ"

_CACHE_TTL_SECONDS = 300  # 5 דקות - מספיק כדי לא להכביד על השרת, קצר מספיק כדי לא להציג מידע מיושן
_cache: Dict[tuple, tuple] = {}  # key -> (expires_at_monotonic, value)
_cache_lock = asyncio.Lock()


class MoodleTokenInvalid(Exception):
    pass


# קורסים שהסטודנט אמר שעזב (מהזיכרון שלו): נשארים ב-Moodle אבל לא מוצגים כ"פעילים". נקבע פעם בבקשה ע"י מנוע הצ'אט.
_HIDDEN_COURSES: contextvars.ContextVar = contextvars.ContextVar("hidden_courses", default=())


def set_hidden_courses(names) -> None:
    _HIDDEN_COURSES.set(tuple(str(n).strip() for n in (names or []) if str(n).strip()))


def _name_hidden(name: Any) -> bool:
    n = _norm_plain(name)
    if not n:
        return False
    for h in _HIDDEN_COURSES.get():
        hn = _norm_plain(h)
        if hn and (hn in n or n in hn):
            return True
    return False


def _norm_plain(s: Any) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-\.\(\)\[\]\{\},;:!?\"'/\\|]+", " ", str(s or "").lower())).strip()


async def _moodle_call(wstoken: str, wsfunction: str, params: Optional[Dict[str, Any]] = None) -> Any:
    query = {"wstoken": wstoken, "wsfunction": wsfunction, "moodlewsrestformat": "json"}
    query.update(params or {})

    try:
        async with shared_client(15) as client:
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


async def _get_courses(wstoken: str, moodle_user_id: Any, include_hidden: bool = False):
    courses = await _cached_moodle_call(
        (moodle_user_id, "courses"), wstoken, "core_enrol_get_users_courses", {"userid": moodle_user_id}
    )
    if include_hidden or not courses or not _HIDDEN_COURSES.get():
        return courses
    return [c for c in courses if not (_name_hidden(c.get("fullname")) or _name_hidden(c.get("shortname")))]


async def _hidden_course_ids(wstoken: str, moodle_user_id: Any) -> set:
    if not _HIDDEN_COURSES.get():
        return set()
    allc = await _get_courses(wstoken, moodle_user_id, include_hidden=True)
    return {c.get("id") for c in (allc or []) if _name_hidden(c.get("fullname")) or _name_hidden(c.get("shortname"))}


async def _resolve_course(wstoken: str, moodle_user_id: Any, course_name: str) -> Optional[dict]:
    """מתאים שם קורס חופשי (כפי שהסטודנט/המנהל כתב) לאחד הקורסים הרשומים בפועל (גם קורס שעזב - אם שאל עליו במפורש)."""
    courses = await _get_courses(wstoken, moodle_user_id, include_hidden=True)
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
    """תאריך מלא בשעון ישראל (השרת רץ ב-UTC, ולכן לא משתמשים בשעון המקומי שלו), עם יום בשבוע, תאריך עברי והזמן היחסי: בעוד X / עבר המועד לפני X."""
    return describe_ts(ts)


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
    text = "רשימת הקורסים של הסטודנט:\n" + "\n".join(f"- [{_reg('c', n=_clean_course_name(c.get('fullname'))[:60])}] {c.get('fullname')}" for c in courses)
    left = [c.get("fullname") for c in (await _get_courses(wstoken, moodle_user_id, include_hidden=True) or []) if c not in courses]
    if left:
        text += "\n\n(הסטודנט אמר שעזב ולכן לא מוצגים כפעילים: " + "; ".join(str(x) for x in left) + ")"
    return courses, text


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
    cname = _clean_course_name(course.get("fullname"))
    lines = []
    for a in assignments:
        due = int(a.get("duedate") or 0)
        ref = f"[{_reg('d', n=_clean_event_name(a.get('name'))[:90], c=cname[:60], t=due)}] " if due else ""
        lines.append(f"- {ref}{a.get('name')} (מועד הגשה: {_fmt_ts(due)})")
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
    lines = []
    for i in items:
        if not (i.get("itemname") or i.get("gradeformatted")):
            continue
        gv = (i.get("gradeformatted") or "").strip()
        ref = f"[{_reg('g', n=str(i.get('itemname') or 'ציון קורס כולל')[:60], v=gv[:14])}] " if gv else ""
        lines.append(f"- {ref}{i.get('itemname') or 'ציון קורס כולל'}: {gv or 'אין עדיין ציון'}")
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
    hidden_ids = await _hidden_course_ids(wstoken, moodle_user_id)
    if hidden_ids:
        events = [e for e in events if e.get("courseid") not in hidden_ids]
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
        lines.append(f"{section_name}:")
        lines.extend(f"  - {m.get('name')} ({m.get('modname')})" for m in modules)
    if not lines:
        return data, f"לא נמצא תוכן בקורס '{course.get('fullname')}'."
    return data, f"תוכן הקורס '{course.get('fullname')}':\n" + "\n".join(lines)


# ---------------------------------------------------------------------
# חיפוש קבצים חכם: ממיין לפי התאמה, מבין מה כל קובץ (סוג/נושא/תיאור), ולא זורק קבצים אקראיים
# ---------------------------------------------------------------------
_NIQQUD = re.compile(r"[֑-ׇ]")
_STOP = {
    # עברית
    "של", "את", "על", "עם", "אל", "לי", "לך", "לו", "לנו", "בבקשה", "הקובץ", "קובץ", "קבצים", "קבצי", "מסמך", "מסמכים", "תביא", "הבא", "שלח", "תשלח",
    "תן", "תני", "רוצה", "צריך", "צריכה", "אפשר", "אני", "אותו", "אותה", "מה", "איזה", "איפה", "יש", "זה", "זאת", "הוא", "היא", "או", "גם",
    "רק", "פשוט", "מהקורס", "בקורס", "קורס", "הזה", "הזו", "שלי", "שיש", "קיים", "קיימים", "שקיימים", "לקבל", "להוריד", "הורדה", "בשביל", "של",
    "ל", "ב", "מ", "ו", "ה", "כמו", "אותם", "אותן", "בעניין", "לגבי", "בנושא", "אם", "כן", "לא", "נא", "תוכל", "תוכלי", "מודל", "במודל",
    # אנגלית
    "the", "a", "an", "of", "for", "to", "me", "my", "please", "send", "give", "get", "file", "files", "document", "documents", "i", "want",
    "need", "can", "you", "in", "from", "course", "with", "and", "or", "that", "this", "moodle", "download",
}
_BULK = {"כל", "כולם", "כולן", "הכל", "כלל", "all", "everything", "every", "כוללים"}

_KIND_WORDS = {
    "solution": {"פתרון", "פתרונות", "פתרונו", "solution", "solutions", "sol", "answers", "תשובות", "פתרונותיו"},
    "exam": {"מבחן", "מבחנים", "בוחן", "בחינה", "בחינות", "מועד", "exam", "exams", "test", "midterm", "final", "מבחני"},
    "exercise": {"תרגיל", "תרגילים", "תרגול", "תרגילי", "ex", "exercise", "exercises", "hw", "homework", "assignment", "מטלה", "מטלות", "דף", "worksheet", "lab", "מעבדה", "שיעורי"},
    "lecture": {"הרצאה", "הרצאות", "מצגת", "מצגות", "שקפים", "שקף", "lecture", "lectures", "slides", "slide", "presentation", "ppt", "pptx", "הרצאת"},
    "syllabus": {"סילבוס", "syllabus", "תכנית", "תוכנית"},
    "summary": {"סיכום", "סיכומים", "summary", "notes", "cheatsheet", "נוסחאות"},
    "code": {"קוד", "code", "src", "source", "project", "פרויקט", "starter", "תבנית", "template"},
    "reading": {"מאמר", "קריאה", "article", "reading", "ספר", "book", "paper"},
}
_KIND_ORDER = ["solution", "summary", "exam", "exercise", "lecture", "syllabus", "code", "reading"]
_KIND_LABEL = {
    "solution": "פתרון", "exam": "מבחן/בוחן", "exercise": "תרגיל/מטלה", "lecture": "הרצאה/מצגת", "syllabus": "סילבוס",
    "summary": "סיכום", "code": "קוד/פרויקט", "reading": "חומר קריאה", "other": "אחר",
}
_KIND_PLURAL = {
    "solution": "פתרונות", "exam": "מבחנים", "exercise": "תרגילים/מטלות", "lecture": "הרצאות/מצגות", "syllabus": "סילבוס",
    "summary": "סיכומים", "code": "קוד/פרויקטים", "reading": "חומרי קריאה", "other": "אחר",
}
_EXT_KIND = {"pptx": "lecture", "ppt": "lecture", "key": "lecture", "py": "code", "java": "code", "c": "code", "cpp": "code", "zip": "code"}
MIN_MATCH_SCORE = 2.5
MAX_FILES_PER_ANSWER = 5


def _norm(s: Any) -> str:
    s = re.sub(r"<[^>]+>", " ", html.unescape(str(s or "")))
    s = _NIQQUD.sub("", s).lower()
    s = re.sub(r"[_\-\.\(\)\[\]\{\},;:!?\"'׳״/\\|]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _variants(t: str) -> set:
    """צורות של מילה לצורך התאמה: בלי תחילית עברית נפוצה (ה/ו/ב/ל/מ/ש/כ), ורבים של ים/ות."""
    vs = {t}
    if len(t) > 3 and t[0] in "והבלמכש":
        vs.add(t[1:])
    if len(t) > 4 and t[:2] in ("וה", "ול", "וב", "מה", "שה", "כש", "לה", "בה"):
        vs.add(t[2:])
    for v in list(vs):
        if len(v) > 4 and v.endswith(("ים", "ות")):
            vs.add(v[:-2])
            vs.add(v[:-2] + "ה")
            vs.add(v[:-2] + "ת")
    return vs


def _tokens(s: Any) -> List[str]:
    return [t for t in _norm(s).split(" ") if t]


def _kind_of_tokens(tokens: List[str]) -> Optional[str]:
    toks = set()
    for t in tokens:
        toks |= _variants(t)
    for kind in _KIND_ORDER:
        if toks & _KIND_WORDS[kind]:
            return kind
    return None


def _infer_kind(filename: str, activity: str, section: str, desc: str) -> str:
    """מה הקובץ הזה בעצם: קודם לפי שם הקובץ והפעילות (הכי אמין), אחר כך לפי הנושא והתיאור, ולבסוף לפי הסיומת."""
    k = _kind_of_tokens(_tokens(filename) + _tokens(activity))
    if k:
        return k
    k = _kind_of_tokens(_tokens(section) + _tokens(desc)[:25])
    if k:
        return k
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return _EXT_KIND.get(ext, "other")


def _parse_query(search: str, kind_arg: str):
    """מפרק בקשה למילות תוכן, מספרים, וסוגי קבצים; ומזהה בקשה גורפת ('כל הקבצים') או ריקה."""
    toks = _tokens(search)
    numbers = [t for t in toks if t.isdigit()]
    kinds = set()
    if kind_arg and kind_arg.strip().lower() in _KIND_WORDS:
        kinds.add(kind_arg.strip().lower())
    content = []
    for t in toks:
        if t.isdigit() or t in _STOP:
            continue
        if t in _BULK:
            continue
        k = _kind_of_tokens([t])
        if k:
            kinds.add(k)
            continue
        content.append(t)
    bulk = any(t in _BULK for t in toks)
    if "solution" in kinds:
        kinds = {"solution"}
    return content, numbers, kinds, bulk


def _score_file(f: Dict[str, Any], content: List[str], numbers: List[str], kinds: set) -> float:
    name_n, act_n, sec_n, desc_n = f["_name_n"], f["_act_n"], f["_sec_n"], f["_desc_n"]
    name_t, act_t, sec_t, desc_t = f["_name_t"], f["_act_t"], f["_sec_t"], f["_desc_t"]
    score = 0.0
    for q in content:
        vs = _variants(q)
        if vs & name_t:
            score += 4
        elif any(len(v) >= 3 and v in name_n for v in vs):
            score += 2
        if vs & act_t:
            score += 3
        elif any(len(v) >= 3 and v in act_n for v in vs):
            score += 1.5
        if vs & sec_t:
            score += 2.5
        if vs & desc_t:
            score += 1.5
    file_nums = set(re.findall(r"\d+", f["_name_n"] + " " + f["_act_n"]))
    sec_nums = set(re.findall(r"\d+", f["_sec_n"]))
    for n in numbers:
        if n in file_nums:
            score += 4
        elif n in sec_nums and not file_nums:
            score += 3
        elif file_nums:
            score -= 3                       # "תרגיל 3" ≠ "תרגיל 13" ≠ "תרגיל 4"
    for k in kinds:
        score += 3 if f["kind"] == k else 0
    if kinds and f["kind"] not in kinds:
        score -= 1.5
    if f["kind"] == "solution" and "solution" not in kinds:
        score -= 2                           # פתרון לא נשלח אלא אם ביקשו פתרון
    return score


def _short(s: Any, n: int = 130) -> str:
    t = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html.unescape(str(s or "")))).strip()
    return (t[: n - 1] + "…") if len(t) > n else t


def _fmt_day(ts: Any) -> str:
    try:
        ts = int(ts)
        return time.strftime("%d/%m/%Y", time.localtime(ts)) if ts else ""
    except Exception:
        return ""


def _overview_text(course_name: str, files: List[Dict[str, Any]]) -> str:
    by_kind: Dict[str, int] = {}
    by_section: Dict[str, int] = {}
    for f in files:
        by_kind[f["kind"]] = by_kind.get(f["kind"], 0) + 1
        by_section[f["section"] or "כללי"] = by_section.get(f["section"] or "כללי", 0) + 1
    kinds_line = ", ".join(f"{_KIND_PLURAL[k]} ({by_kind[k]})" for k in _KIND_ORDER + ["other"] if k in by_kind)
    secs = list(by_section.items())[:14]
    secs_line = "; ".join(f"{name} ({n})" for name, n in secs) + ("…" if len(by_section) > 14 else "")
    return f"בקורס '{course_name}' יש {len(files)} קבצים.\nלפי סוג: {kinds_line}.\nלפי נושא: {secs_line}."


@_register(
    name="get_course_files", moodle_function="core_course_get_contents",
    icon="📎", label="קבצים ומסמכים בקורס (שליחה לסטודנט)",
    description_admin="מחפש קבצים בקורס בצורה חכמה: מבין מה כל קובץ (תרגיל/פתרון/הרצאה/מבחן/סיכום... לפי השם, הפעילות, הנושא והתיאור), מדרג לפי התאמה לבקשה, ומחזיר רק את הטובים ביותר (עד 5) עם 'סמן קובץ' לשליחה בצ'אט. בקשה גורפת או כללית מדי ('כל הקבצים') מחזירה סקירה בלבד, לא קבצים; ואם אין התאמה טובה לא נשלחים קבצים אקראיים. הטוקן אף פעם לא מופיע בסמן.",
    description_ai="מחפש קבצים בקורס מסוים (מסמכי תרגילים, פתרונות, הרצאות/מצגות, מבחנים, סילבוס, סיכומים, קוד) ומחזיר רק את המתאימים ביותר לבקשה, עם הסבר על כל קובץ ועם סמן [[file:...]] לשליחה. חובה לציין course_name, ובמיוחד search (מה בדיוק מחפשים: נושא, מספר תרגיל, שבוע...) ו/או kind (סוג). אם לא מציינים מה מחפשים - מקבלים רק סקירה של הקורס ללא קבצים.",
    params=[
        {"name": "course_name", "type": "STRING", "description": "שם הקורס", "required": True},
        {"name": "search", "type": "STRING", "description": "מה מחפשים, בקצרה ובמילים של הסטודנט: נושא, מספר תרגיל או שבוע (למשל 'תרגיל 3', 'מצגת רקורסיה', 'מבחן מועד א'). בלי מילות נימוס.", "required": False},
        {"name": "kind", "type": "STRING", "description": "סוג הקובץ אם ידוע: exercise (תרגיל/מטלה), solution (פתרון), lecture (הרצאה/מצגת), exam (מבחן), syllabus, summary (סיכום), code, reading. אופציונלי.", "required": False},
    ],
)
async def _exec_get_course_files(wstoken, moodle_user_id, args) -> Tuple[Any, str]:
    course_name = args.get("course_name", "")
    search = str(args.get("search") or "").strip()
    kind_arg = str(args.get("kind") or "").strip()
    course = await _resolve_course(wstoken, moodle_user_id, course_name)
    if not course:
        courses = await _get_courses(wstoken, moodle_user_id)
        names = "; ".join(c.get("fullname") or "" for c in (courses or [])[:12])
        hint = f" הקורסים של הסטודנט: {names}." if names else ""
        return None, (f"לא זוהה באיזה קורס מדובר ('{course_name}').{hint} שאל את הסטודנט מאיזה קורס הוא צריך קובץ - "
                      "אי אפשר לשלוף קבצים מכל הקורסים בבת אחת.")

    content = await _cached_moodle_call(
        (moodle_user_id, "content", course["id"]), wstoken, "core_course_get_contents", {"courseid": course["id"]},
    )
    found: List[Dict[str, Any]] = []
    seen = set()

    def add(filename, size, mimetype, fileurl, activity, section, desc="", modified=0, due=0):
        key = str(fileurl)
        if key in seen:
            return
        ref = make_file_ref(fileurl)
        if not ref:
            return
        seen.add(key)
        filename = filename or "קובץ"
        activity = activity or ""
        section = section or ""
        d = _short(desc, 140)
        kind = _infer_kind(filename, activity, section, d)
        found.append({
            "filename": filename, "size": size, "mimetype": mimetype or "", "ref": ref, "activity": activity, "section": section,
            "desc": d, "kind": kind, "modified": modified or 0, "due": due or 0,
            "_name_n": _norm(filename), "_act_n": _norm(activity), "_sec_n": _norm(section), "_desc_n": _norm(d),
            "_name_t": {v for t in _tokens(filename) for v in _variants(t)}, "_act_t": {v for t in _tokens(activity) for v in _variants(t)},
            "_sec_t": {v for t in _tokens(section) for v in _variants(t)}, "_desc_t": {v for t in _tokens(d) for v in _variants(t)},
        })

    for s in (content if isinstance(content, list) else []):
        for m in (s.get("modules") or []):
            for c in (m.get("contents") or []):
                if c.get("type") == "file":
                    sub = (c.get("filepath") or "").strip("/")
                    add(c.get("filename"), c.get("filesize"), c.get("mimetype"), c.get("fileurl"),
                        (m.get("name") or "") + (f" / {sub}" if sub else ""), s.get("name"), m.get("description") or "", c.get("timemodified"))

    # קבצים מצורפים לתיאור של מטלות (מסמכי התרגיל עצמם) - לא תמיד מופיעים ב-contents של הקורס
    try:
        a_data = await _cached_moodle_call(
            (moodle_user_id, "assignments", course["id"]), wstoken, "mod_assign_get_assignments", {"courseids[0]": course["id"]},
        )
        c0 = ((a_data or {}).get("courses") or [{}])[0]
        for a in (c0.get("assignments") or []):
            for att in (a.get("introattachments") or []):
                add(att.get("filename"), att.get("filesize"), att.get("mimetype"), att.get("fileurl"), a.get("name"), "מטלות",
                    a.get("intro") or "", att.get("timemodified"), a.get("duedate"))
    except MoodleTokenInvalid:
        raise
    except Exception:
        pass  # אם הפונקציה הזו חסומה בשרת - עדיין מחזירים את הקבצים שנמצאו בתוכן הקורס

    fullname = course.get("fullname") or course_name
    if not found:
        return found, f"לא נמצאו קבצים בקורס '{fullname}'."

    q_content, q_numbers, q_kinds, bulk = _parse_query(search, kind_arg)
    specific = bool(q_content or q_numbers or q_kinds)

    # בקשה כללית/גורפת ("כל הקבצים", או בלי שום פירוט) - לא שולחים קבצים, רק מציגים סקירה כדי שהסטודנט יבחר
    if not specific:
        why = "הבקשה כללית מדי (כל הקבצים של הקורס)" if (bulk or search) else "לא צוין מה מחפשים"
        return [], (_overview_text(fullname, found) + f"\n\nלא נשלפו קבצים: {why}. אל תשלח קבצים אקראיים ואל תנסה 'כל הקבצים'. "
                    "הסבר לסטודנט בקצרה שאי אפשר לשלוח הכל, והצע לו לבחור: סוג (תרגיל/הרצאה/מבחן...) ומספר או נושא או שבוע. אחר כך חפש שוב עם search/kind.")

    scored = sorted(((_score_file(f, q_content, q_numbers, q_kinds), i, f) for i, f in enumerate(found)), key=lambda x: (-x[0], -(x[2].get("modified") or 0), x[1]))
    best = scored[0][0] if scored else 0
    good = [(sc, f) for sc, _, f in scored if sc >= MIN_MATCH_SCORE and sc >= best * 0.5]
    if not good:
        return [], (f"לא נמצאה התאמה טובה ל-'{search or kind_arg}' בקורס '{fullname}'.\n" + _overview_text(fullname, found) +
                    "\n\nאל תשלח קבצים אקראיים. אמור לסטודנט שלא מצאת התאמה, וציין מה כן יש בקורס (לפי הסקירה), ושאל מה בדיוק הוא מחפש.")
    shown = good[:MAX_FILES_PER_ANSWER]
    lines = []
    for i, (sc, f) in enumerate(shown, 1):
        label = _clean_label(f["filename"])
        meta = ", ".join(x for x in (_file_kind(f["filename"], f["mimetype"]), _fmt_size(f["size"])) if x)
        marker = f"[[file:{f['ref']}|{label}" + (f"|{int(f['size'])}" if str(f["size"] or "").isdigit() and int(f["size"]) > 0 else "") + "]]"
        parts = [f"סוג: {_KIND_LABEL[f['kind']]}"]
        if f["activity"]:
            parts.append(f"מתוך: \"{f['activity']}\"" + (f" (נושא: {f['section']})" if f["section"] else ""))
        elif f["section"]:
            parts.append(f"נושא: {f['section']}")
        if f["due"]:
            parts.append(f"מועד הגשה: {_fmt_day(f['due'])}")
        if f["modified"]:
            parts.append(f"עודכן: {_fmt_day(f['modified'])}")
        if f["desc"]:
            parts.append(f"תיאור: {f['desc']}")
        conf = "גבוהה" if sc >= 8 else "בינונית"
        lines.append(f"{i}. {marker} ({meta}) - התאמה: {conf} | " + " | ".join(parts))
    total = len(good)
    head = f"קבצים שמתאימים ל-'{search or kind_arg}' בקורס '{fullname}' ({'התאמה אחת' if total == 1 else str(total) + ' התאמות'}" + (f", מוצגות {len(shown)} המתאימות ביותר" if total > len(shown) else "") + "):\n"
    note = ("\n\nהוראות: אל תשלח את כל הרשימה בהכרח - בחר את מה שבאמת עונה על הבקשה (בדרך כלל 1-3 קבצים), הסבר בשורה קצרה מה כל קובץ שאתה שולח, "
            "והעתק את הסמן [[file:...|...]] שלו בדיוק כפי שהוא, בשורה משלו. אם כמה קבצים מתאימים באותה מידה ואי אפשר לדעת למי הכוונה - שאל את הסטודנט. "
            "אל תכתוב כתובות URL ואל תמציא סמנים.")
    raw = [{k: v for k, v in f.items() if not k.startswith("_")} | {"score": round(sc, 2)} for sc, f in shown]
    return raw, head + "\n".join(lines) + note


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
    match = _best_assignment(assignments, assignment_name)
    if not match:
        return assignments_data, f"לא נמצאה מטלה בשם '{args.get('assignment_name')}' בקורס '{course.get('fullname')}'."

    data = await _moodle_call(wstoken, "mod_assign_get_submission_status", {"assignid": match["id"], "userid": moodle_user_id})
    submission = (data or {}).get("lastattempt", {}).get("submission", {})
    status = submission.get("status")
    status_he = {"submitted": "הוגשה", "new": "טרם הוגשה", "draft": "טיוטה בלבד, עדיין לא הוגשה סופית"}.get(status, status or "לא ידוע")
    due = int(match.get("duedate") or 0)
    text = f"סטטוס הגשת '{match.get('name')}' בקורס '{course.get('fullname')}': {status_he}."
    text += f" מועד ההגשה: {_fmt_ts(due)}." if due else " אין מועד הגשה מוגדר."
    if status == "submitted" and submission.get("timemodified"):
        text += f" הוגשה ב: {_fmt_ts(submission.get('timemodified'))}."
    elif due and due < int(time.time()):
        text += " המטלה באיחור: מועד ההגשה עבר והיא טרם הוגשה."
    return data, text


def _clean_event_name(name: str) -> str:
    """שמות אירועי היומן של Moodle מלאים ברעש ("... submission is due", "יש להגיש את '...'") - מנקים כדי שהכרטיס יהיה קריא."""
    n = html.unescape(str(name or ""))
    n = re.sub(r"\s*[-–]?\s*(submission\s+)?is due\s*$", "", n, flags=re.I)
    n = re.sub(r"^\s*(יש להגיש את|תאריך הגשה)\s*", "", n)
    n = re.sub(r"\s*[-–]\s*בודק אוטומטי\s*$", "", n)
    return n.strip(" '\"‘’-–")


def _clean_course_name(name: str) -> str:
    """מסיר את קוד הקורס ("150005.2.5787 - ...") מההתחלה או מהסוף."""
    n = re.sub(r"^\s*\d{4,}(?:\.\d+)*\s*[-–]\s*", "", str(name or ""))
    return re.sub(r"\s*[-–]?\s*\d{4,}(?:\.\d+)*\s*$", "", n).strip(" -–")


def _norm_words(s: Any) -> set:
    return {w for w in re.sub(r"[^\w\s]", " ", str(s or "").lower()).split() if len(w) > 1}


def _best_assignment(assignments: list, wanted: str):
    """התאמת מטלה לפי שם: תת-מחרוזת, ואם אין - החפיפה הגבוהה ביותר של מילים (השם בכרטיס מנוקה ולכן לא תמיד זהה לשם ב-Moodle)."""
    w = str(wanted or "").strip().lower()
    for a in assignments:
        if w and w in (a.get("name") or "").lower():
            return a
    ww = _norm_words(w)
    best, score = None, 0.0
    for a in assignments:
        aw = _norm_words(a.get("name"))
        if ww and aw:
            sc = len(ww & aw) / len(ww | aw)
            if sc > score:
                best, score = a, sc
    return best if score >= 0.5 else None


def _plain_html(html: Any, n: int = 240) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", str(html or "")).replace("&nbsp;", " ").replace("&amp;", "&").split())[:n]


def _moodle_url(u: Any) -> str:
    u = str(u or "")
    return u if u.startswith("https://moodle.jct.ac.il/") else ""


def _deadline_items(events) -> list:
    now = int(time.time())
    rows = []
    extras: dict = {}
    for e in events:
        if str(e.get("eventtype") or "") in ("open", "gradingdue"):
            continue                       # "נפתח ב..." (פתיחת הגשה) ו"מועד בדיקה" אינם מועדי הגשה של הסטודנט
        t = int(e.get("timesort") or 0)
        if t:
            name, course = _clean_event_name(e.get("name")), _clean_course_name((e.get("course") or {}).get("fullname", ""))
            rows.append((name, course, t))
            extras.setdefault((name, course, t), {"d": _plain_html(e.get("description")), "u": _moodle_url(e.get("url") or (e.get("action") or {}).get("url")), "o": 1 if t < now else 0, "g": 1 if str(e.get("modulename") or "") in ("zoom", "bigbluebuttonbn", "webex", "teams", "googlemeet") else 0})
    # אותו תרגיל עם כמה שאלות באותו מועד -> שורה אחת ("תרגיל 4 - שאלות 3, 4, 5")
    merged: dict = {}
    order: list = []
    for name, course, t in rows:
        m = re.match(r"^(.*?)\s*שאלה(?:\s+מספר)?\s*(\d+)\s*$", name)
        key = (m.group(1).strip(" -–") if m else name, course, t, bool(m))
        if key not in merged:
            merged[key] = []
            order.append(key)
        if m:
            merged[key].append(int(m.group(2)))
    out = []
    for key in order:
        base, course, t, is_q = key
        qs = sorted(set(merged[key]))
        label = base if not is_q else (f"{base} - שאלה {qs[0]}" if len(qs) == 1 else f"{base} - שאלות {', '.join(str(q) for q in qs)}")
        ex = extras.get((base if not is_q else "", course, t)) or next((v for k, v in extras.items() if k[1] == course and k[2] == t), {})
        item = {"n": label[:90], "c": course[:60], "t": t}
        if ex.get("d"):
            item["d"] = ex["d"]
        if ex.get("u"):
            item["u"] = ex["u"]
        if ex.get("o"):
            item["o"] = 1
        if ex.get("g"):
            item["g"] = 1
        out.append(item)
    seen = set()
    uniq = []
    for it in sorted(out, key=lambda x: x["t"]):
        k = (it["n"], it["c"], it["t"])
        if k not in seen:
            seen.add(k)
            uniq.append(it)
    return uniq


# נתונים מובנים שהאפליקציה מציירת ככרטיס (במקום שהמודל ינסח רשימה): נשמרים במילון של הבקשה הנוכחית
_CARDS: contextvars.ContextVar = contextvars.ContextVar("moodle_cards", default=None)


def start_cards() -> dict:
    d: dict = {}
    _CARDS.set(d)
    return d


def get_cards():
    return _CARDS.get()


def _reg(kind: str, **fields) -> str:
    """רושם פריט שהמודל יוכל להציג בכרטיס (show_ui) ומחזיר את המזהה שלו (d1, g2, c3...). הנתונים נשארים בשרת - המודל רק מצביע."""
    d = _CARDS.get()
    if d is None:
        return ""
    n = d.setdefault("_n", {})
    n[kind] = n.get(kind, 0) + 1
    i = f"{kind}{n[kind]}"
    d.setdefault("items", {})[i] = {"k": kind, **fields}
    return i


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
        {"timesortfrom": now - 14 * 86400, "limitnum": 30},
    )
    events = (data or {}).get("events", [])
    if _HIDDEN_COURSES.get():
        events = [e for e in events if not _name_hidden((e.get("course") or {}).get("fullname"))]
    if not events:
        return data, "אין כרגע דברים דחופים ממתינים."
    items = _deadline_items(events)
    lines = [f"- [{_reg('d', **it)}] {it['n']} ({it['c']}) - {_fmt_ts(it['t'])}" + (" [באיחור - טרם הוגש]" if it.get("o") else "") for it in items]
    note = ("(הרשימה כוללת רק פריטים שעדיין דורשים פעולה, כלומר טרם הוגשו. פריט שמועדו כבר עבר ועדיין ברשימה הוא באיחור. "
            "פריט שאינו ברשימה כבר הוגש או שאינו דורש הגשה. הזמנים כבר בשעון ישראל, ה'עבר המועד'/'בעוד' מחושבים מראש - אל תחשב מחדש.)")
    return data, "דברים שממתינים לסטודנט (כל הקורסים):\n" + note + "\n" + "\n".join(lines)


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
    hidden_ids = await _hidden_course_ids(wstoken, moodle_user_id)
    if hidden_ids:
        grades = [g for g in grades if g.get("courseid") not in hidden_ids]
    if not grades:
        return data, "לא נמצאו ציונים כוללים זמינים."
    lines = []
    for g in grades:
        cn = _clean_course_name(course_names.get(g.get("courseid"), g.get("courseid")))
        gv = g.get("grade") or ""
        ref = f"[{_reg('g', n=str(cn)[:60], v=str(gv)[:14])}] " if gv else ""
        lines.append(f"- {ref}{cn}: {gv or 'אין עדיין ציון'}")
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
