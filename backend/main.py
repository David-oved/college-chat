import re
import sys
import time
import base64
import hashlib
import uuid
from urllib.parse import quote, unquote
try:
    # מונע קריסה בהדפסת עברית/אימוג'י לקונסול של Windows (cp1255/cp1252 לא תומכים ב-UTF-8)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import os
import json
import asyncio
import secrets
import httpx
from http_client import shared_client
from io import BytesIO
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional, List, Dict
from pypdf import PdfReader

from db import db
from auth import admin_auth, require_admin, hash_password, student_auth, require_student
from gemini_diagnostics import run_full_diagnostics, test_connectivity, list_chat_models
import ai_providers
import prompt_store
import prompt_rules
import custom_designs
import memory  # registers the memory section of the prompt
import ui_catalog
from ui_blocks import resolve_blocks as _resolve_blocks, encode_blocks as _encode_blocks, STYLES as _UI_STYLES
from push import send_push_to_subscription, VAPID_PUBLIC_KEY
from moodle_tools import decode_file_ref, MOODLE_FILE_BASE
from rag_engine import (
    chunk_text,
    get_embedding,
    categorize_and_summarize,
    stream_gemini_answer,
    stream_gemini_answer_with_moodle_tools,
    stream_chat_with_tools,
    status_event,
    STATUS_OPEN,
    VISUAL_TOKEN,
    personal_route,
    stream_knowledge_draft,
    summarize_conversation,
)

VERSION_JSON_URL = "https://raw.githubusercontent.com/David-oved/college-chat/main/version.json"
VERSION_CHECK_INTERVAL_SECONDS = 300  # כל 5 דקות


async def version_watch_loop():
    """
    בודק כל כמה דקות את version.json בגיטהאב (בלי שום מעורבות של המנהל בשרת -
    מספיק לערוך את הקובץ ולדחוף ל-main). אם הגרסה השתנתה - שולח התראת Push
    אמיתית לכל המשתמשים שנרשמו להתראות.
    """
    while True:
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(VERSION_JSON_URL, headers={"Cache-Control": "no-cache"})
            if resp.status_code == 200:
                current_version = str(resp.json().get("version", "")).strip()
                last_seen = await db.get_last_seen_version()
                if current_version and last_seen and current_version != last_seen:
                    subs = await db.get_all_push_subscriptions()
                    payload = {
                        "title": "🎓 עדכון חדש זמין!",
                        "body": "גרסה חדשה של האפליקציה מוכנה - לחצו לעדכון.",
                        "version": current_version,
                    }
                    for sub in subs:
                        ok = send_push_to_subscription(sub, payload)
                        if not ok:
                            await db.remove_push_subscription(sub["endpoint"])
                if current_version:
                    await db.set_last_seen_version(current_version)
        except Exception:
            pass
        await asyncio.sleep(VERSION_CHECK_INTERVAL_SECONDS)

app = FastAPI(
    title="College AI Assistant (RAG) - Full Stack Server",
    description="שרת ניהול צ'אטבוט מכללה, מכסות יומיות, RAG ופאנל ניהול",
    version="1.0.0"
)

# הגדרת CORS - רק מקורות מוכרים (לא "*"): האפליקציות שמארחות את הצ'אט (MoodlePro בגיטהאב פייג'ס,
# הפורק, והשרת עצמו) + localhost לפיתוח. האימות הוא בכותרת Authorization (לא עוגיות), לכן אין credentials.
# אפשר להוסיף מקורות נוספים במשתנה הסביבה CORS_ORIGINS (מופרדים בפסיק) בלי לשנות קוד.
_DEFAULT_CORS_ORIGINS = [
    "https://david-oved.github.io",
    "https://aorizoro.github.io",
    "https://college-chat-lwdu.onrender.com",
]
_extra_origins = [o.strip().rstrip("/") for o in os.environ.get("CORS_ORIGINS", "").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_DEFAULT_CORS_ORIGINS + _extra_origins,
    allow_origin_regex=r"^https?://(localhost|127\.0\.0\.1)(:\d+)?$",
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

# --- הגבלת קצב פשוטה בזיכרון (חלון נע) - מגינה על Moodle ועל מכסת Gemini מהצפה ---
_rate_buckets: dict = {}


def _client_ip(request: Request) -> str:
    # מאחורי הפרוקסי של Render: הערך הימני ב-X-Forwarded-For הוא מה שהפרוקסי עצמו ראה (הערכים משמאל ניתנים לזיוף)
    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"


def _rate_limit(key: str, limit: int, window_seconds: int):
    now = time.monotonic()
    bucket = _rate_buckets.setdefault(key, [])
    cutoff = now - window_seconds
    while bucket and bucket[0] < cutoff:
        bucket.pop(0)
    if len(bucket) >= limit:
        raise HTTPException(status_code=429, detail="יותר מדי בקשות בזמן קצר - נסו שוב בעוד רגע")
    bucket.append(now)
    if len(_rate_buckets) > 5000:  # ניקוי דליים ריקים כדי שהזיכרון לא יגדל בלי גבול
        for k in [k for k, v in _rate_buckets.items() if not v or v[-1] < cutoff]:
            _rate_buckets.pop(k, None)


@app.get("/api/health")
async def health():
    """בלי DB ובלי עבודה: רק כדי שהאפליקציה תוכל להעיר את השרת (Render נרדם) לפני שהסטודנט פותח את הצ'אט."""
    return {"ok": True}


# אימות טוקן Moodle: מזהה המשתמש שאומת נשמר בזיכרון לכמה דקות (לפי גיבוב של הטוקן, לא הטוקן עצמו) - חוסך סבב מול Moodle בכל הודעה
_verify_cache: dict = {}
_VERIFY_TTL = 300.0


async def _moodle_userid_cached(raw_token: str):
    """מחזיר (wstoken, userid). הטוקן עצמו לא נשמר; רק זהות משתמש שאומתה, לכמה דקות."""
    token = _extract_moodle_wstoken(raw_token)
    key = hashlib.sha256(token.encode("utf-8")).hexdigest()[:32]
    hit = _verify_cache.get(key)
    if token and hit and hit[0] > time.monotonic():
        return token, hit[1]
    token, info = await _verify_moodle_token(raw_token)
    _verify_cache[key] = (time.monotonic() + _VERIFY_TTL, info.get("userid"))
    if len(_verify_cache) > 2000:
        now = time.monotonic()
        for k in [k for k, v in _verify_cache.items() if v[0] < now]:
            _verify_cache.pop(k, None)
    return token, info.get("userid")


_bg_tasks: set = set()


def _spawn(coro):
    """משימת רקע שלא נאספת ע"י ה-garbage collector באמצע."""
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


# The Moodle functions the bot had before new ones started being added. A function that is NOT in this list and was never seen by the server before is
# switched on once (when the admin had saved an explicit list), so a newly added capability does not sit silently off; after that the admin's choice rules.
_MOODLE_FUNCTIONS_BEFORE = {"get_my_courses", "get_course_assignments", "get_course_grades", "get_calendar_events", "get_course_content", "get_course_files",
                            "get_assignment_submission_status", "get_upcoming_deadlines", "get_overall_grades", "get_quiz_attempts", "get_my_exams", "get_completion_status"}


async def _enable_new_moodle_functions():
    from moodle_tools import MOODLE_FUNCTION_NAMES
    raw = await db.get_state_kv("moodle_known")
    known = set(json.loads(raw)) if raw else set(_MOODLE_FUNCTIONS_BEFORE)
    new = [n for n in MOODLE_FUNCTION_NAMES if n not in known]
    if new:
        enabled = await db._get_moodle_enabled_functions_raw()
        if enabled is not None:
            await db.set_moodle_enabled_functions(enabled + [n for n in new if n not in enabled])
            db.invalidate_config_cache()
            print(f"[moodle] new functions switched on: {new}")
    await db.set_state_kv("moodle_known", json.dumps(MOODLE_FUNCTION_NAMES))


@app.on_event("startup")
async def on_startup():
    """מתחבר ל-DB ומייצר (בפעם הראשונה בלבד) סיסמת מנהל אקראית, מודפסת לקונסול."""
    await db.init()
    generated_password = await admin_auth.ensure_password_exists(db)
    if generated_password:
        print("\n" + "=" * 64)
        print("🔐 נוצרה סיסמת מנהל ראשונית לפאנל הניהול (/admin):")
        print(f"    {generated_password}")
        print("    שמור אותה במקום בטוח - היא לא תוצג שוב!")
        print("=" * 64 + "\n")
    try:
        await _enable_new_moodle_functions()
    except Exception as e:
        print(f"[moodle] could not update the enabled functions: {type(e).__name__}: {e}")
    asyncio.create_task(version_watch_loop())


@app.on_event("shutdown")
async def on_shutdown():
    await db.close()


# =====================================================================
# מודלים של בקשות
# =====================================================================
class AdminConfigRequest(BaseModel):
    gemini_api_key: Optional[str] = None
    daily_quota_limit: Optional[int] = None
    bot_identity_info: Optional[str] = None


class AddKnowledgeTextRequest(BaseModel):
    title: str
    text: str


class AiDraftKnowledgeRequest(BaseModel):
    instruction: str


class ChatQuestionRequest(BaseModel):
    question: str


class ChatQuestionV2Request(BaseModel):
    conversation_id: str
    question: str = Field(..., min_length=1, max_length=2000)
    moodle_token: Optional[str] = None
    moodle_user_id: Optional[int] = None
    ctx: Optional[dict] = None  # הפריט שהסטודנט לחץ עליו בכרטיס (שם, קורס, מועד, תיאור) - ההקשר לשאלה
    lang: Optional[str] = None  # the app's language (he / en / fr)
    status: bool = False        # האם לשלוח גם אירועי סטטוס חיים ("מחפש קבצים...") בתוך הזרם - רק ללקוחות שיודעים לקרוא אותם


class AdminLoginRequest(BaseModel):
    password: str


class StudentSignupRequest(BaseModel):
    username: str
    password: str


class StudentLoginRequest(BaseModel):
    username: str
    password: str


class MoodleFileRequest(BaseModel):
    ref: str = Field(..., min_length=4, max_length=1700)
    moodle_token: str = Field(..., min_length=8, max_length=4000)


class MoodleConnectRequest(BaseModel):
    token: str


class TestLoginRequest(BaseModel):
    password: str


class MoodleFunctionsUpdateRequest(BaseModel):
    enabled: List[str]


class MoodleTestFunctionRequest(BaseModel):
    token: str
    function_name: str
    args: dict = {}


class PushSubscriptionKeys(BaseModel):
    p256dh: str
    auth: str


class PushSubscribeRequest(BaseModel):
    endpoint: str
    keys: PushSubscriptionKeys


class PushUnsubscribeRequest(BaseModel):
    endpoint: str


class ChangePasswordRequest(BaseModel):
    new_password: str


class TestApiKeyRequest(BaseModel):
    api_key: str


class TestConnectivityRequest(BaseModel):
    api_key: str
    model: str


# =====================================================================
# 0. התחברות מנהל (Admin Login)
# =====================================================================
@app.post("/api/admin/login")
async def admin_login(req: AdminLoginRequest, request: Request):
    """התחברות לפאנל הניהול. הסיסמה נוצרת אוטומטית בהפעלה הראשונה של השרת ומוצגת בקונסול."""
    _rate_limit("admin-login:" + _client_ip(request), 8, 300)       # בלי זה אפשר לנחש סיסמה בלי הגבלה
    if not await admin_auth.verify_password(db, req.password):
        raise HTTPException(status_code=401, detail="סיסמה שגויה")
    token = admin_auth.create_session()
    return {"token": token}


@app.post("/api/admin/change-password", dependencies=[Depends(require_admin)])
async def change_admin_password(req: ChangePasswordRequest):
    """מאפשר למנהל המחובר לשנות את סיסמת הכניסה לפאנל (נשמר כ-hash ב-DB)."""
    if not req.new_password or len(req.new_password) < 8:
        raise HTTPException(status_code=400, detail="הסיסמה חייבת להכיל לפחות 8 תווים")
    await db.set_admin_password_hash(hash_password(req.new_password))
    return {"message": "הסיסמה עודכנה בהצלחה"}


@app.get("/api/admin/usage", dependencies=[Depends(require_admin)])
async def get_usage_metrics():
    """מדדי שימוש: כמה פעמים קראנו היום בפועל ל-Gemini (לפי סוג קריאה), וכמה שאלות נותרו לכל סטודנט."""
    api_usage_today = await db.get_today_usage()
    student_usage = await db.get_student_usage_summary()
    return {
        "api_usage_today": api_usage_today,
        "student_usage": student_usage,
    }


@app.get("/api/admin/ai-quota", dependencies=[Depends(require_admin)])
async def ai_quota_live():
    """מצב ה-AI בזמן אמת: קצב קריאות, דחיות 429 בשעה האחרונה, סטטוס לכל מודל, וקריאות להיום מה-DB.
    (Google לא מספקת API ל"מכסה שנותרה" - לכן מציגים קצב ודחיות בפועל, ומול תקרה יומית שהמנהל קובע.)"""
    import gemini_stats
    today = await db.get_today_usage()
    return {"live": gemini_stats.snapshot(), "today": today, "server_time": time.time()}


@app.get("/api/admin/users", dependencies=[Depends(require_admin)])
async def list_app_users():
    """רשימת כל המשתמשים הרשומים (שם משתמש אמיתי) + כמה הודעות כל אחד שלח."""
    return {"users": await db.get_all_users_with_counts()}


@app.get("/api/admin/users/{user_id}/activity", dependencies=[Depends(require_admin)])
async def get_user_activity(user_id: str):
    """כל השיחות וההודעות בפועל של משתמש ספציפי - כדי לדעת בדיוק מה כל משתמש שאל ומתי."""
    activity = await db.get_user_activity(user_id)
    if activity is None:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")
    return activity


# =====================================================================
# 0.5 התחברות/הרשמה אמיתית לסטודנטים (שם משתמש + סיסמה, חשבון ב-DB)
# =====================================================================
@app.post("/api/signup")
async def student_signup(req: StudentSignupRequest, request: Request):
    _rate_limit("signup:" + _client_ip(request), 6, 3600)
    """יצירת חשבון סטודנט חדש - שם משתמש + סיסמה, נשמר ב-DB (app_users)."""
    user_id = await student_auth.signup(db, req.username, req.password)
    if not user_id:
        raise HTTPException(status_code=400, detail="שם המשתמש כבר תפוס, או שהסיסמה קצרה מדי (מינימום 3 תווים)")
    login_result = await student_auth.login(db, req.username, req.password)
    return login_result


@app.post("/api/login")
async def student_login(req: StudentLoginRequest, request: Request):
    _rate_limit("login:" + _client_ip(request), 30, 300)
    """התחברות לחשבון סטודנט קיים."""
    result = await student_auth.login(db, req.username, req.password)
    if not result:
        raise HTTPException(status_code=401, detail="שם משתמש או סיסמה שגויים")
    return result


TEST_LOGIN_PASSWORD = "1234"
TEST_LOGIN_USERNAME = "test_user"


@app.post("/api/test-login")
async def test_login(req: TestLoginRequest, request: Request):
    _rate_limit("test-login:" + _client_ip(request), 12, 600)
    """
    כניסת בדיקה זמנית בלבד (עד שהכניסה האמיתית תגיע מה-SSO של Moodle): סיסמה אחידה,
    בלי שם משתמש, תמיד אותו משתמש-בדיקה יחיד (לא יוצר חשבון חדש בכל פעם).
    """
    if req.password != TEST_LOGIN_PASSWORD:
        raise HTTPException(status_code=401, detail="סיסמת בדיקה שגויה")

    user = await db.get_user_by_username(TEST_LOGIN_USERNAME)
    if not user:
        random_password_hash = hash_password(secrets.token_urlsafe(16))
        new_id = await db.create_user(TEST_LOGIN_USERNAME, random_password_hash)
        user = await db.get_user_by_id(new_id)

    return student_auth.create_session_for_user(user["id"], user["username"])


@app.get("/api/me")
async def get_my_profile(session=Depends(require_student)):
    """
    'פאנל שקיפות' - בדיוק מה שמור עלינו כרגע ב-DB (לא יותר מזה): שורת app_users
    שלי + כמה שיחות/הודעות יש לי בפועל. משמש את מסך הבדיקה של חיבור Moodle.
    """
    profile = await db.get_user_profile_summary(session["user_id"])
    if profile is None:
        raise HTTPException(status_code=404, detail="משתמש לא נמצא")
    return profile


# =====================================================================
# 0.55 תשתית לחיבור Moodle (JCT) - אימות טוקן בזמן אמת, בלי לשמור אותו אצלנו
# =====================================================================
MOODLE_BASE_URL = "https://moodle.jct.ac.il/webservice/rest/server.php"


def _extract_moodle_wstoken(raw: str) -> str:
    """
    מה שהמשתמש מדביק זה בד"כ הקישור המלא שה-SSO של Moodle מחזיר:
    moodlemobile://token=BASE64(passport:::wstoken:::privatetoken) - כפי שההוראות
    באתר MoodlePro מנחות להעתיק ("העתקת כתובת הקישור"), לא רק את הטוקן עצמו.
    ה-wstoken האמיתי קבור באמצע ה-base64, אחרי הפירוק הזה - לא ניתן להשתמש
    במחרוזת הגולמית כמו שהיא מול ה-webservice.
    """
    raw = (raw or "").strip()
    if not raw:
        return ""

    marker = "token="
    idx = raw.find(marker)
    payload = raw[idx + len(marker):] if idx != -1 else raw
    payload = unquote(payload.strip())

    try:
        decoded = base64.b64decode(payload + "=" * (-len(payload) % 4)).decode("utf-8", errors="ignore")
    except Exception:
        return payload  # לא base64 תקין - כנראה הודבק הטוקן הגולמי עצמו, בלי העטיפה

    parts = decoded.split(":::")
    if len(parts) >= 2 and parts[1]:
        return parts[1]  # הפורמט הסטנדרטי: passport:::wstoken:::privatetoken
    return payload


async def _verify_moodle_token(raw_token: str) -> tuple:
    """
    מפענח ומאמת טוקן Moodle מול core_webservice_get_site_info בזמן אמת. משותף לכל
    המקומות שצריכים לאמת טוקן (חיבור סטודנט אמיתי, ובדיקת מנהל) - כדי שלא יהיה כפל קוד
    בטיפול בשגיאות. מחזיר (wstoken המפוענח, data המלא מ-Moodle). לעולם לא שומר את הטוקן.
    """
    token = _extract_moodle_wstoken(raw_token)
    if not token:
        raise HTTPException(status_code=400, detail="נדרש טוקן Moodle")

    try:
        async with shared_client(15) as client:
            resp = await client.get(MOODLE_BASE_URL, params={
                "wstoken": token,
                "wsfunction": "core_webservice_get_site_info",
                "moodlewsrestformat": "json",
            })
    except httpx.HTTPError:
        raise HTTPException(status_code=502, detail="לא ניתן להתחבר לשרת Moodle כרגע")

    try:
        data = resp.json()
    except ValueError:
        raise HTTPException(status_code=502, detail="תשובה לא תקינה מ-Moodle")

    if isinstance(data, dict) and data.get("exception"):
        if data.get("errorcode") == "invalidtoken":
            raise HTTPException(status_code=401, detail="החיבור ל-Moodle פג - יש להתחבר מחדש")
        raise HTTPException(status_code=502, detail=f"שגיאה מ-Moodle: {data.get('message') or data.get('errorcode')}")

    if not data.get("userid"):
        raise HTTPException(status_code=502, detail="תשובה לא תקינה מ-Moodle (חסר userid)")

    return token, data


def _available_functions_from_site_info(data: dict) -> Optional[List[str]]:
    raw_functions = data.get("functions")
    if not isinstance(raw_functions, list):
        return None
    return sorted({f.get("name") for f in raw_functions if isinstance(f, dict) and f.get("name")})


def _clean_first_name(firstname, fullname) -> str:
    """שם פרטי נקי וקצר מתוך תשובת Moodle (firstname, ואם אין - המילה הראשונה של fullname)."""
    raw = (firstname or "").strip() or ((fullname or "").strip().split() or [""])[0]
    raw = "".join(ch for ch in str(raw) if ch.isprintable())
    return raw.strip()[:40]


@app.post("/api/moodle/connect")
async def moodle_connect(req: MoodleConnectRequest, request: Request):
    """
    מקבל טוקן Moodle אמיתי (מהאפליקציה המארחת, לדוגמה MoodlePro), ומאמת אותו מול Moodle
    עצמו בזמן אמת (core_webservice_get_site_info) כדי לקבל userid/username/fullname אמיתיים.
    הטוקן עצמו לעולם לא נשמר אצלנו - משמש רק לקריאה הזו ונזרק.
    לפי ה-userid של Moodle מאתרים/יוצרים משתמש מקביל אצלנו (app_users) ומנפיקים לו
    טוקן התחברות משלנו, כדי שאפשר יהיה להמשיך איתו ישירות לצ'אט (RAG) ו-/api/conversations.
    """
    _rate_limit("connect:" + _client_ip(request), 60, 300)
    _, data = await _verify_moodle_token(req.token)
    moodle_user_id = data.get("userid")

    local_username = f"moodle_{moodle_user_id}"
    user = await db.get_user_by_username(local_username)
    new_user = False
    if not user:
        new_user = True
        random_password_hash = hash_password(secrets.token_urlsafe(16))
        new_id = await db.create_user(local_username, random_password_hash)
        # אם שתי בקשות ראשונות הגיעו במקביל, אחת מהן נכשלת על unique - אז פשוט קוראים שוב את המשתמש שכבר נוצר
        user = await db.get_user_by_id(new_id) if new_id else await db.get_user_by_username(local_username)
        if not user:
            raise HTTPException(status_code=500, detail="לא הצלחנו ליצור את החשבון, נסו שוב")

    # השם הפרטי (מה-Moodle עצמו) נשמר אצלנו ומשמש לברכה "שלום <שם>" ולפנייה של המודל
    first_name = _clean_first_name(data.get("firstname"), data.get("fullname"))
    if first_name:
        await db.set_user_display_name(user["id"], first_name)
    else:
        first_name = await db.get_user_display_name(user["id"])

    session = student_auth.create_session_for_user(user["id"], user["username"], first_name)

    return {
        **session,
        "new_user": new_user,              # the first time this student reaches us: the app shows its welcome tour and the chat walk-through
        "moodle": {
            "userid": moodle_user_id,
            "username": data.get("username"),
            "fullname": data.get("fullname"),
            # core_webservice_get_site_info מחזיר (כשזה מופעל אצל המוסד) רשימת כל הפונקציות
            # שהטוקן הזה בפועל מורשה לקרוא להן - זו הדרך האמיתית לדעת אילו שמות פונקציה
            # קיימות אצל JCT, במקום לנחש לפי ה-API הסטנדרטי של Moodle.
            "available_functions": _available_functions_from_site_info(data),
        },
    }


@app.post("/api/admin/moodle-test-connect", dependencies=[Depends(require_admin)])
async def admin_moodle_test_connect(req: MoodleConnectRequest):
    """
    בדיקת מנהל בלבד: מאמת טוקן Moodle אמיתי מול JCT ומחזיר userid/username/fullname +
    רשימת הפונקציות שהטוקן הזה מורשה לקרוא להן בפועל. בכוונה לא יוצר משתמש ולא שומר
    שום דבר - זו קריאת בדיקה חד-פעמית, לא כניסה לאפליקציה.
    """
    _, data = await _verify_moodle_token(req.token)
    return {
        "userid": data.get("userid"),
        "username": data.get("username"),
        "fullname": data.get("fullname"),
        "available_functions": _available_functions_from_site_info(data),
    }


class MoodleAnnotateRequest(BaseModel):
    names: List[str]


_MOODLE_ANN: Dict[str, dict] = {}            # function name -> what the AI said about it (kept while the server runs, so a name is never paid for twice)

_ANN_PROMPT = """אתה מומחה ל-Moodle Web Services. לפניך שמות של פונקציות שאתר Moodle של מכללה פתח לטוקן של סטודנט.
לכל שם החזר אובייקט עם: n = השם בדיוק כפי שקיבלת; d = תיאור קצר בעברית (עד 12 מילים) של מה הפונקציה עושה;
u = מספר 0 עד 3: עד כמה היא שימושית לעוזר צ'אט שעונה לסטודנט על הלימודים שלו (3 = מאוד: מטלות, מועדים, ציונים, מבחנים, קבצים, הכרזות והודעות, נוכחות, לוח זמנים; 2 = שימושית לפעמים;
1 = שולית; 0 = ניהולית, טכנית או לא רלוונטית לסטודנט); i = אם u הוא 2 או 3: שאלה לדוגמה (בעברית, עד 10 מילים) שסטודנט היה שואל והפונקציה עונה עליה, אחרת מחרוזת ריקה;
w = true אם הפונקציה משנה נתונים (כתיבה, שליחה, מחיקה), false אם היא רק קוראת.
השמות:
"""


@app.post("/api/admin/moodle-functions/annotate", dependencies=[Depends(require_admin)])
async def admin_moodle_annotate(req: MoodleAnnotateRequest):
    """Describes a batch of Moodle function names (what it does, how useful for a student assistant, an example question). Admin tool for finding what the bot is missing."""
    from rag_engine import _generate_once
    names = [n for n in dict.fromkeys(req.names) if re.fullmatch(r"[A-Za-z0-9_]{3,120}", n)][:40]
    todo = [n for n in names if n not in _MOODLE_ANN]
    if todo:
        api_key = await db.get_api_key()
        if not api_key:
            raise HTTPException(status_code=400, detail="אין מפתח Google שמור.")
        schema = {"type": "ARRAY", "items": {"type": "OBJECT", "properties": {"n": {"type": "STRING"}, "d": {"type": "STRING"}, "u": {"type": "INTEGER"},
                                                                                "i": {"type": "STRING"}, "w": {"type": "BOOLEAN"}}, "required": ["n", "d", "u"]}}
        try:
            content = await asyncio.wait_for(_generate_once(
                [{"role": "user", "parts": [{"text": _ANN_PROMPT + "\n".join(todo)}]}], api_key, max_output_tokens=5000, call_type="ai_draft",
                gen_extra={"responseMimeType": "application/json", "responseSchema": schema}), timeout=60)
            txt = "".join(p.get("text", "") for p in (content or {}).get("parts", []) if isinstance(p, dict))
            for it in json.loads(txt):
                n = str(it.get("n") or "")
                if n in todo:
                    _MOODLE_ANN[n] = {"d": str(it.get("d") or "")[:140], "u": max(0, min(3, int(it.get("u") or 0))), "i": str(it.get("i") or "")[:120], "w": bool(it.get("w"))}
        except Exception as e:
            raise HTTPException(status_code=502, detail=f"הניתוח נכשל: {type(e).__name__}")
    return {"items": {n: _MOODLE_ANN[n] for n in names if n in _MOODLE_ANN}}


@app.post("/api/admin/moodle-test-function", dependencies=[Depends(require_admin)])
async def admin_moodle_test_function(req: MoodleTestFunctionRequest):
    """
    בדיקת מנהל: מריץ פונקציית Moodle ספציפית אחת עם טוקן אמיתי שהמנהל הדביק, ומחזיר גם
    את ה-JSON הגולמי מ-Moodle וגם את הניסוח העברי - כדי לוודא בדיוק מה מתקבל לפני שסומכים
    על הפונקציה בצ'אט האמיתי. הטוקן משמש רק לקריאה הזו ולא נשמר.
    """
    from moodle_tools import test_function

    token, data = await _verify_moodle_token(req.token)
    return await test_function(req.function_name, req.args, token, data.get("userid"))


@app.get("/api/admin/moodle-functions", dependencies=[Depends(require_admin)])
async def get_moodle_functions_config():
    """מחזיר את כל הפונקציות (הכלים) הזמינים לגמיני מול Moodle - עם תיאור ופרמטרים - ואילו מהן דלוקות כרגע."""
    from moodle_tools import MOODLE_FUNCTION_NAMES, get_function_catalog

    enabled = await db.get_moodle_enabled_functions()
    return {
        "all_functions": MOODLE_FUNCTION_NAMES,
        "enabled_functions": MOODLE_FUNCTION_NAMES if enabled is None else enabled,
        "catalog": get_function_catalog(),
    }


@app.post("/api/admin/moodle-functions", dependencies=[Depends(require_admin)])
async def update_moodle_functions_config(req: MoodleFunctionsUpdateRequest):
    """מאפשר למנהל להדליק/לכבות כל אחת מהפונקציות (קורסים/מטלות/ציונים/יומן) בנפרד."""
    from moodle_tools import MOODLE_FUNCTION_NAMES

    valid = [n for n in req.enabled if n in MOODLE_FUNCTION_NAMES]
    await db.set_moodle_enabled_functions(valid)
    return {"message": "העדכון נשמר בהצלחה", "enabled_functions": valid}


@app.get("/api/admin/prompt-preview", dependencies=[Depends(require_admin)])
async def preview_prompt(question: str = "מתי הבחינה במבוא לתכנות?"):
    """
    שקיפות מלאה למנהל: מרכיב בדיוק את אותו הפרומפט שהיה נשלח בפועל ל-Gemini לשאלה
    לדוגמה הזו (זהות הבוט + קטלוג הנושאים + מידע בסיסי בזמן אמת + מקטעי הידע הרלוונטיים) -
    בלי לשלוח אותו בפועל למודל הסופי (רק שליפת ה-embedding לשאלה, כדי שהתצוגה תהיה אמיתית).
    """
    from rag_engine import _build_chat_user_turn
    from moodle_tools import MOODLE_FUNCTION_NAMES
    await prompt_store.refresh(True)

    api_key = await db.get_api_key()
    bot_identity = await db.get_bot_identity()
    catalog = await db.get_catalog()

    question_embedding = await get_embedding(question, api_key) if api_key else []
    relevant_chunks = await db.search_similar_chunks(question_embedding, top_k=3) if question_embedding else []

    enabled = await db.get_moodle_enabled_functions()
    enabled_functions = MOODLE_FUNCTION_NAMES if enabled is None else enabled

    composed_prompt = _build_chat_user_turn(
        question, relevant_chunks, bot_identity, catalog, "", [],
        username="<שם הסטודנט המחובר>",
        extra_system_instructions=prompt_store.text("moodle") if enabled_functions else "",
    )

    return {
        "composed_prompt": composed_prompt,
        "relevant_chunks_found": len(relevant_chunks),
        "catalog_categories": len(catalog or []),
        "moodle_tools_declared": enabled_functions,
    }


# =====================================================================
# 0.6 התראות Push (עדכון גרסה אוטומטי מ-version.json ב-GitHub)
# =====================================================================
@app.get("/api/push/vapid-public-key")
async def get_vapid_public_key():
    """המפתח הציבורי (לא סודי) שהלקוח צריך כדי להירשם ל-Push דרך ה-Service Worker."""
    return {"publicKey": VAPID_PUBLIC_KEY}


@app.post("/api/push/subscribe")
async def push_subscribe(req: PushSubscribeRequest, session=Depends(require_student)):
    """שומר מנוי Push של המשתמש - כשתהיה גרסה חדשה ב-GitHub הוא יקבל התראה אמיתית בטלפון."""
    await db.save_push_subscription(session["user_id"], req.endpoint, req.keys.p256dh, req.keys.auth)
    return {"message": "נרשמת בהצלחה להתראות עדכון"}


@app.post("/api/push/unsubscribe")
async def push_unsubscribe(req: PushUnsubscribeRequest, session=Depends(require_student)):
    await db.remove_push_subscription(req.endpoint)
    return {"message": "בוטלה ההרשמה להתראות"}


# =====================================================================
# 1. נתיבי מנהל (Admin Endpoints) - כולם מוגנים בהתחברות
# =====================================================================
@app.get("/api/admin/config", dependencies=[Depends(require_admin)])
async def get_admin_config():
    """מחזיר למנהל את מצב ההגדרות (ללא חשיפת המפתח המלא)"""
    return await db.get_config_status()


@app.post("/api/admin/config", dependencies=[Depends(require_admin)])
async def update_admin_config(config: AdminConfigRequest):
    """עדכון מפתח ה-API של Gemini או המכסה היומית"""
    await db.update_config(
        api_key=config.gemini_api_key,
        daily_limit=config.daily_quota_limit,
        bot_identity_info=config.bot_identity_info
    )
    return {
        "message": "הגדרות המערכת עודכנו בהצלחה!",
        "status": await db.get_config_status()
    }


@app.post("/api/admin/test-key", dependencies=[Depends(require_admin)])
async def test_api_key(req: TestApiKeyRequest):
    """
    בודק מפתח Gemini API בלי לשמור אותו: מאפיין את פורמט המפתח, שולף את רשימת המודלים
    הזמינים לו (עם המכסה החינמית הידועה לכל מודל), ומבצע קריאה אמיתית למודל כדי לוודא שהוא מגיב.
    """
    return await run_full_diagnostics(req.api_key)


@app.post("/api/admin/test-key/connectivity", dependencies=[Depends(require_admin)])
async def test_key_connectivity(req: TestConnectivityRequest):
    """בדיקת חיבור חוזרת מול מודל ספציפי שהמנהל בחר, בלי לשלוף שוב את כל רשימת המודלים."""
    return await test_connectivity(req.api_key, req.model)


# ---- the chat's AI model: chosen from the models of the key that is already saved (no need to type the key again)
class ModelChoiceRequest(BaseModel):
    provider: str = "google"
    primary: Optional[str] = Field(None, max_length=90)
    fallbacks: List[str] = Field(default_factory=list, max_length=4)


class ModelTestRequest(BaseModel):
    provider: str = "google"
    model: str = Field(..., min_length=3, max_length=90)


class ProviderKeyRequest(BaseModel):
    provider: str
    api_key: str = Field("", max_length=300)


class ProviderActiveRequest(BaseModel):
    provider: str


def _prov(p: str) -> str:
    if p not in ("google", "openai", "xai", "groq"):
        raise HTTPException(status_code=400, detail="ספק לא מוכר")
    return p


async def _provider_key(p: str) -> str:
    return (await db.get_api_key()) if p == "google" else (await db.get_ai_provider_cfg()).get(p + "_key", "")


def _mask(k: str) -> str:
    return (k[:3] + "..." + k[-4:]) if len(k) > 10 else ("מוגדר" if k else "")


@app.get("/api/admin/providers", dependencies=[Depends(require_admin)])
async def get_providers():
    """The keys that are saved (masked), and which provider the bot uses now."""
    cfg = await db.get_ai_provider_cfg()
    gk = await db.get_api_key()
    keys = {"google": gk, "openai": cfg.get("openai_key", ""), "xai": cfg.get("xai_key", ""), "groq": cfg.get("groq_key", "")}
    return {"active": cfg.get("active", "google"), "providers": {p: {"label": ai_providers.LABELS[p], "has_key": bool(k), "masked": _mask(k)} for p, k in keys.items()}}


@app.post("/api/admin/providers/key", dependencies=[Depends(require_admin)])
async def set_provider_key(req: ProviderKeyRequest):
    """Saves (or, when empty, removes) the key of OpenAI / xAI. The Google key keeps its own field above."""
    p = _prov(req.provider)
    if p == "google":
        raise HTTPException(status_code=400, detail="מפתח Google נשמר בשדה המפתח שלמעלה")
    key = req.api_key.strip()
    if key.startswith("gsk_") and p != "groq":
        raise HTTPException(status_code=400, detail="זה מפתח של Groq (gsk_...), לא של " + ai_providers.LABELS[p] + ". שמור אותו בשורת Groq.")
    if key.startswith("xai-") and p != "xai":
        raise HTTPException(status_code=400, detail="זה מפתח של xAI Grok (xai-...). שמור אותו בשורת xAI Grok.")
    if key.startswith("sk-") and p not in ("openai",):
        raise HTTPException(status_code=400, detail="זה נראה כמו מפתח OpenAI (sk-...). שמור אותו בשורת OpenAI.")
    await db.set_ai_provider_value(p + "_api_key", key or None)
    if not key and (await db.get_ai_provider_cfg()).get("active") == p:
        await db.set_ai_provider_value("ai_provider", None)          # its key is gone: back to Google
    return {"message": "המפתח נשמר" if key else "המפתח נמחק"}


@app.post("/api/admin/providers/active", dependencies=[Depends(require_admin)])
async def set_active_provider(req: ProviderActiveRequest):
    """Which provider answers the students. Google stays the safety net if the chosen one gives nothing; the knowledge search always uses the Google key."""
    p = _prov(req.provider)
    if p != "google" and not await _provider_key(p):
        raise HTTPException(status_code=400, detail="אין מפתח שמור לספק הזה")
    await db.set_ai_provider_value("ai_provider", None if p == "google" else p)
    return {"message": "הבוט עובד עכשיו עם " + ai_providers.LABELS[p]}


_MODEL_NAME = re.compile(r"^[A-Za-z0-9._:/\-]{3,90}$")


@app.get("/api/admin/models", dependencies=[Depends(require_admin)])
async def get_models(provider: str = "google"):
    """What the saved key can run, what is chosen now, and how fast each model answered lately."""
    from rag_engine import DEFAULT_MODELS_TO_TRY, _SPEED
    provider = _prov(provider)
    key = await _provider_key(provider)
    if provider == "google":
        chosen = await db.get_gemini_models()
        defaults = list(DEFAULT_MODELS_TO_TRY)
    else:
        chosen = (await db.get_ai_provider_cfg()).get(provider + "_models") or None
        defaults = list(ai_providers.DEFAULT_MODELS[provider])
    out = {"provider": provider, "has_key": bool(key), "chosen": chosen or [], "using_defaults": not chosen, "defaults": defaults,
           "speeds": dict(_SPEED) if provider == "google" else {}, "models": [], "error": ""}
    if not key:
        out["error"] = "טרם נשמר מפתח API לספק הזה"
        return out
    res = await (list_chat_models(key) if provider == "google" else ai_providers.list_models(provider, key))
    if res.get("success"):
        out["models"] = res["models"]
    else:
        out["error"] = f"לא ניתן לשלוף את רשימת המודלים (קוד {res.get('status_code')}): {str(res.get('error'))[:200]}"
    return out


@app.post("/api/admin/models", dependencies=[Depends(require_admin)])
async def set_models(req: ModelChoiceRequest):
    """Saves the main model and its fallbacks (all must be models of the saved key). An empty choice returns to the built-in order."""
    provider = _prov(req.provider)
    names = [n.strip() for n in ([req.primary or ""] + list(req.fallbacks)) if n and n.strip()]
    if not names:
        await (db.set_gemini_models(None) if provider == "google" else db.set_ai_provider_value(provider + "_models", None))
        return {"message": "חזרנו לסדר המודלים המובנה", "chosen": []}
    if len(set(names)) != len(names) or not all(_MODEL_NAME.match(n) for n in names):
        raise HTTPException(status_code=400, detail="שמות מודלים לא תקינים או כפולים")
    key = await _provider_key(provider)
    if key:
        res = await (list_chat_models(key) if provider == "google" else ai_providers.list_models(provider, key))
        if res.get("success"):
            ok = {m["name"] for m in res["models"]}
            bad = [n for n in names if n not in ok]
            if bad:
                raise HTTPException(status_code=400, detail="המודלים האלה לא זמינים למפתח השמור: " + ", ".join(bad))
    await (db.set_gemini_models(names) if provider == "google" else db.set_ai_provider_value(provider + "_models", ",".join(names)))
    return {"message": "המודל נשמר: " + names[0] + (" (גיבויים: " + ", ".join(names[1:]) + ")" if len(names) > 1 else ""), "chosen": names}


@app.post("/api/admin/models/test", dependencies=[Depends(require_admin)])
async def test_model(req: ModelTestRequest):
    """A real small request to one model with the saved key: does it answer, and how fast."""
    if not _MODEL_NAME.match(req.model):
        raise HTTPException(status_code=400, detail="שם מודל לא תקין")
    provider = _prov(req.provider)
    key = await _provider_key(provider)
    if not key:
        raise HTTPException(status_code=400, detail="טרם נשמר מפתח API לספק הזה")
    return await (test_connectivity(key, req.model) if provider == "google" else ai_providers.test_model(provider, key, req.model))


# ---- a student's own AI key (developers): hidden in the app under the memory settings; every call is only for the signed-in student
class MyAiKey(BaseModel):
    provider: str
    api_key: str = Field(..., min_length=8, max_length=300)


class MyAiChoice(BaseModel):
    models: List[str] = Field(default_factory=list, max_length=3)
    mode: str = "personal"


class MyAiTest(BaseModel):
    model: str = Field(..., min_length=3, max_length=90)


def _my_ai_view(cfg):
    if not cfg:
        return {"has_key": False, "mode": "public", "kind": "", "masked": "", "models": []}
    return {"has_key": True, "mode": cfg.get("mode", "public"), "kind": cfg["kind"], "masked": _mask(cfg["key"]), "models": cfg.get("models") or []}


@app.get("/api/me/ai")
async def my_ai(session=Depends(require_student)):
    return _my_ai_view(await db.get_user_ai(session["user_id"]))


@app.post("/api/me/ai/key")
async def my_ai_save_key(req: MyAiKey, session=Depends(require_student)):
    _rate_limit("meai:" + session["user_id"], 30, 60)
    p = _prov(req.provider)
    key = req.api_key.strip()
    why = ai_providers.key_mismatch(p, key)
    if why:
        raise HTTPException(status_code=400, detail=why + ", בחר את הספק המתאים")
    await db.set_user_ai(session["user_id"], {"mode": "public", "kind": p, "key": key, "models": []})      # saved, but not used until the student switches it on
    return _my_ai_view(await db.get_user_ai(session["user_id"]))


@app.post("/api/me/ai/models-list")
async def my_ai_models(session=Depends(require_student)):
    _rate_limit("meai:" + session["user_id"], 30, 60)
    cfg = await db.get_user_ai(session["user_id"])
    if not cfg:
        raise HTTPException(status_code=400, detail="לא נשמר מפתח")
    res = await (list_chat_models(cfg["key"]) if cfg["kind"] == "google" else ai_providers.list_models(cfg["kind"], cfg["key"]))
    if not res.get("success"):
        raise HTTPException(status_code=400, detail="המפתח לא הצליח לשלוף מודלים (קוד " + str(res.get("status_code")) + ")")
    return {"models": [m["name"] for m in res["models"]], "chosen": cfg.get("models") or []}


@app.post("/api/me/ai/test")
async def my_ai_test(req: MyAiTest, session=Depends(require_student)):
    _rate_limit("meai:" + session["user_id"], 30, 60)
    cfg = await db.get_user_ai(session["user_id"])
    if not cfg or not _MODEL_NAME.match(req.model):
        raise HTTPException(status_code=400, detail="אין מפתח או שם מודל לא תקין")
    r = await (test_connectivity(cfg["key"], req.model) if cfg["kind"] == "google" else ai_providers.test_model(cfg["kind"], cfg["key"], req.model))
    return {"success": bool(r.get("success")), "latency_ms": r.get("latency_ms"), "status_code": r.get("status_code"), "error": "" if r.get("success") else str(r.get("error") or "")[:200]}


@app.post("/api/me/ai/choose")
async def my_ai_choose(req: MyAiChoice, session=Depends(require_student)):
    _rate_limit("meai:" + session["user_id"], 30, 60)
    cfg = await db.get_user_ai(session["user_id"])
    if not cfg:
        raise HTTPException(status_code=400, detail="לא נשמר מפתח")
    models = [m.strip() for m in req.models if m.strip()]
    if len(set(models)) != len(models) or not all(_MODEL_NAME.match(m) for m in models):
        raise HTTPException(status_code=400, detail="שמות מודלים לא תקינים")
    if req.mode == "personal" and not models and cfg["kind"] == "google":
        raise HTTPException(status_code=400, detail="בחר מודל לפני ההפעלה")
    cfg["models"] = models
    cfg["mode"] = "personal" if req.mode == "personal" else "public"
    await db.set_user_ai(session["user_id"], cfg)
    return _my_ai_view(cfg)


@app.delete("/api/me/ai")
async def my_ai_delete(session=Depends(require_student)):
    await db.set_user_ai(session["user_id"], None)
    return {"deleted": True}


@app.get("/api/me/ai/usage")
async def my_ai_usage(session=Depends(require_student)):
    """The student's own calls on their own key: the last 7 days, per day and model. Nothing about anyone else."""
    rows = await db.get_user_ai_usage(session["user_id"])
    days: Dict[str, Dict[str, int]] = {}
    for r in rows:
        d = days.setdefault(r["day"], {"calls": 0, "fails": 0})
        d["calls"] += r["calls"]
        d["fails"] += r["fails"]
    return {"days": [{"day": k, **v} for k, v in sorted(days.items())], "models": rows,
            "total_calls": sum(v["calls"] for v in days.values()), "total_fails": sum(v["fails"] for v in days.values())}


@app.get("/api/admin/knowledge", dependencies=[Depends(require_admin)])
async def list_knowledge():
    """מחזיר את כל קטעי המידע של המכללה השמורים במאגר"""
    return {"chunks": await db.get_all_chunks(), "catalog": await db.get_catalog()}


class EditChunkRequest(BaseModel):
    title: str = Field(..., min_length=1, max_length=200)
    text: str = Field(..., min_length=1, max_length=6000)
    category: str = Field(..., min_length=1, max_length=80)


@app.put("/api/admin/knowledge/{chunk_id}", dependencies=[Depends(require_admin)])
async def edit_knowledge_chunk(chunk_id: str, req: EditChunkRequest):
    """עריכת קטע קיים: הווקטור נבנה מחדש כדי שהחיפוש ימשיך להתאים לנוסח החדש."""
    api_key = await db.get_api_key()
    embedding = await get_embedding(f"{req.title}: {req.text}", api_key) if api_key else None
    ok = await db.update_chunk(chunk_id, req.title.strip(), req.text.strip(), req.category.strip(), embedding or None)
    if not ok:
        raise HTTPException(status_code=404, detail="הקטע לא נמצא")
    return {"ok": True, "has_embedding": bool(embedding)}


class TestSearchRequest(BaseModel):
    question: str = Field(..., min_length=1, max_length=400)


@app.post("/api/admin/knowledge/test-search", dependencies=[Depends(require_admin)])
async def test_knowledge_search(req: TestSearchRequest):
    """מה הבוט ימצא במאגר לשאלה הזו (ואיזה דמיון): לאיתור קטעים שלא נמצאים כשצריך."""
    api_key = await db.get_api_key()
    emb = await get_embedding(req.question, api_key) if api_key else []
    hits = await db.search_similar_chunks(emb, top_k=5) if emb else []
    return {"hits": [{"id": str(h["id"]), "title": h["title"], "category": h["category"], "similarity": round(float(h["similarity"]), 3), "preview": h["text"][:240]} for h in hits]}


@app.get("/api/admin/usage-breakdown", dependencies=[Depends(require_admin)])
async def usage_breakdown(days: int = 7):
    """במה הסטודנטים עוסקים: כמה פעמים כל כלי (tool:*) וכל סוג תצוגה (ui:*) הופעלו, לפי יום. מספרים בלבד - בלי תוכן שיחות."""
    rows = await db.get_usage_range(max(1, min(days, 30)))
    return {"rows": rows}


@app.post("/api/admin/knowledge/ai-draft", dependencies=[Depends(require_admin)])
async def draft_knowledge_with_ai(req: AiDraftKnowledgeRequest):
    """
    מנסח עם Gemini טיוטת נוהל (כותרת + תוכן) מתוך תיאור חופשי של המנהל, ומזרים אותה
    בזמן אמת. זו טיוטה בלבד - אין כאן שמירה למאגר; השמירה בפועל נעשית דרך
    /api/admin/knowledge/text אחרי שהמנהל בדק ואישר את הניסוח.
    """
    if not req.instruction.strip():
        raise HTTPException(status_code=400, detail="נא לכתוב תיאור חופשי למה שרוצים להוסיף")

    api_key = await db.get_api_key()
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="לא הוגדר מפתח Gemini API - הגדר אותו למעלה לפני שימוש בניסוח AI"
        )

    async def response_generator():
        async for token in stream_knowledge_draft(req.instruction, api_key):
            yield token

    return StreamingResponse(response_generator(), media_type="text/plain; charset=utf-8")


def _upload_summary(saved: int, skipped: int, without_vector: int) -> str:
    """משפט סיכום מפורט להעלאה: כמה נשמרו, כמה דולגו ככפילות, וכמה נשמרו בלי וקטור."""
    parts = [f"נשמרו {saved} מקטעים"]
    if skipped:
        parts.append(f"{skipped} דולגו (כבר קיימים במאגר)")
    msg = ", ".join(parts) + "."
    if without_vector:
        msg += (f" ⚠️ {without_vector} מקטעים נשמרו בלי וקטור (חיפוש חכם לא ימצא אותם) - "
                "לחץ \"השלם וקטורים חסרים\" במאגר הידע כשהמכסה/המפתח תקינים.")
    return msg


def _ndjson(obj: dict) -> bytes:
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode("utf-8")


def _progress_response(gen) -> StreamingResponse:
    """
    מחזיר תגובת סטרימינג (שורת JSON לכל אירוע) כדי שהפאנל יציג סרגל התקדמות אמיתי.
    אירועים: start / progress / done, ובמקרה של תקלה באמצע - error.
    """
    async def body():
        try:
            async for event in gen:
                yield _ndjson(event)
        except Exception as e:
            yield _ndjson({"type": "error", "detail": f"{type(e).__name__}: {e}"})
    return StreamingResponse(body(), media_type="application/x-ndjson")


async def _save_chunks_stream(chunks: list, api_key: str, message_prefix: str = ""):
    """
    עבור כל chunk: מדלג אם כבר קיים, יוצר embedding, מסווג אותו לקטגוריה (ע"י AI, מול רשימת
    הקטגוריות הקיימות), שומר אותו ב-DB, ומעדכן את הקטלוג (ה"מפה") שתמיד מוצגת למודל הצ'אט.
    מפיק אירועי התקדמות אחרי כל מקטע (done/total) ואירוע סיום עם הסיכום.
    """
    total = len(chunks)
    existing_categories = await db.get_categories()
    saved = skipped = without_vector = 0
    yield {"type": "start", "total": total}

    for i, c in enumerate(chunks, start=1):
        if await db.chunk_text_exists(c["text"]):
            skipped += 1
        else:
            embedding = await get_embedding(f"{c['title']}: {c['text']}", api_key) if api_key else []
            if api_key and not embedding:
                without_vector += 1

            if api_key:
                classification = await categorize_and_summarize(c["title"], c["text"], api_key, existing_categories)
                category, summary = classification["category"], classification["summary"]
                if category not in existing_categories:
                    existing_categories.append(category)
            else:
                category, summary = "כללי", "מידע כללי (טרם סווג - נדרש מפתח Gemini API)"

            await db.add_knowledge_chunk(c["title"], c["text"], category, embedding or None)
            await db.upsert_catalog_entry(category, summary)
            saved += 1

        yield {"type": "progress", "done": i, "total": total,
               "saved": saved, "skipped": skipped, "without_vector": without_vector}

    yield {
        "type": "done",
        "message": message_prefix + _upload_summary(saved, skipped, without_vector),
        "added_chunks": saved,
        "skipped_duplicates": skipped,
        "without_vector": without_vector,
    }


@app.post("/api/admin/knowledge/text", dependencies=[Depends(require_admin)])
async def upload_knowledge_text(req: AddKnowledgeTextRequest):
    """העלאת טקסט חופשי על ידי המנהל: חיתוך ל-Chunks, יצירת וקטורים וסיווג ע"י AI (עם התקדמות בסטרימינג)"""
    chunks = chunk_text(req.text, title=req.title)
    if not chunks:
        raise HTTPException(status_code=400, detail="הטקסט שהוזן ריק")

    api_key = await db.get_api_key()
    return _progress_response(_save_chunks_stream(chunks, api_key, "המידע עובד. "))


@app.post("/api/admin/knowledge/pdf", dependencies=[Depends(require_admin)])
async def upload_knowledge_pdf(title: str = Form(...), file: UploadFile = File(...)):
    """העלאת קובץ PDF או TXT על ידי המנהל: חילוץ טקסט, חיתוך, וקטורים וסיווג (עם התקדמות בסטרימינג)"""
    content = await file.read()
    extracted_text = ""

    if file.filename.lower().endswith(".pdf"):
        try:
            reader = PdfReader(BytesIO(content))
            pages = [page.extract_text() or "" for page in reader.pages]
            extracted_text = "\n".join(pages)
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"שגיאה בקריאת קובץ ה-PDF: {str(e)}")
    else:
        extracted_text = content.decode("utf-8", errors="ignore")

    chunks = chunk_text(extracted_text, title=title or file.filename)
    if not chunks:
        raise HTTPException(status_code=400, detail="לא נמצא טקסט קריא בקובץ")

    api_key = await db.get_api_key()
    prefix = f"הקובץ '{file.filename}' עובד ונחתך ל-{len(chunks)} מקטעים. "
    return _progress_response(_save_chunks_stream(chunks, api_key, prefix))


async def _reindex_stream(missing: list, api_key: str):
    total = len(missing)
    fixed = 0
    yield {"type": "start", "total": total}
    for i, c in enumerate(missing, start=1):
        embedding = await get_embedding(f"{c['title']}: {c['text']}", api_key)
        if embedding:
            await db.set_chunk_embedding(c["id"], embedding)
            fixed += 1
        yield {"type": "progress", "done": i, "total": total, "fixed": fixed}
    still_missing = total - fixed
    msg = f"הושלמו וקטורים ל-{fixed} מתוך {total} מקטעים."
    if still_missing:
        msg += f" ⚠️ {still_missing} עדיין נכשלו - נסה שוב בעוד כמה דקות (ייתכן עומס/מכסה)."
    yield {"type": "done", "message": msg, "fixed": fixed, "still_missing": still_missing,
           "without_vector": still_missing}


@app.post("/api/admin/knowledge/reindex", dependencies=[Depends(require_admin)])
async def reindex_missing_embeddings():
    """משלים וקטורים למקטעים שנשמרו בלי וקטור (למשל אחרי שגיאת 429), עם התקדמות בסטרימינג."""
    api_key = await db.get_api_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="לא הוזן מפתח Gemini API")
    missing = await db.get_chunks_without_embedding()
    return _progress_response(_reindex_stream(missing, api_key))

@app.delete("/api/admin/knowledge/{chunk_id}", dependencies=[Depends(require_admin)])
async def delete_knowledge_chunk(chunk_id: str):
    """מחיקת מקטע מידע מהמאגר"""
    await db.delete_chunk(chunk_id)
    return {"message": "המקטע נמחק בהצלחה"}


@app.post("/api/admin/reset-quota/{user_id}", dependencies=[Depends(require_admin)])
async def reset_student_quota(user_id: str):
    """מאפשר למנהל לאפס את המכסה היומית של סטודנט מסוים לצורך בדיקות"""
    return await db.reset_user_quota(user_id)


# =====================================================================
# 2. נתיבי סטודנט וצ'אט (Student & RAG Chat Endpoints)
# =====================================================================
@app.get("/api/quota/{user_id}")
async def check_student_quota(user_id: str):
    """מחזיר ל-PWA של הסטודנט כמה שאלות נותרו לו להיום"""
    return await db.get_user_quota_status(user_id)


SUMMARIZE_AFTER_MESSAGES = 16  # אחרי כמה הודעות בשיחה מתחילים לדחוס הודעות ישנות לסיכום
KEEP_RECENT_MESSAGES = 12  # כמה הודעות אחרונות נשארות מילה-במילה (לא מסוכמות)


async def _prefetch_courses(wstoken, moodle_user_id):
    try:
        from moodle_tools import _get_courses
        await _get_courses(wstoken, moodle_user_id, include_hidden=True)
    except Exception:
        pass


async def _none():
    return None


async def _empty():
    return []


async def _save_user_message(conversation_id: str, question: str):
    await db.add_message(conversation_id, "user", question)
    # כותרת לשיחה (להיסטוריה): השורה הראשונה של השאלה הראשונה, קצרה
    await db.set_conversation_title_if_default(conversation_id, (question.splitlines() or [question])[0][:48])


async def _finish_chat(conversation_id: str, answer: str, save_task, api_key: str):
    """מה שקורה אחרי שהתשובה כבר אצל הסטודנט: שמירה, וסיכום שיחה ארוכה - ברקע, כדי שהזרם ייסגר מיד."""
    try:
        if save_task:
            await save_task
        await db.add_message(conversation_id, "assistant", answer)
        total_messages = await db.count_messages(conversation_id)
        if total_messages > SUMMARIZE_AFTER_MESSAGES:
            old_messages = await db.get_old_messages_to_summarize(conversation_id, keep_recent=KEEP_RECENT_MESSAGES)
            if old_messages:
                old_summary = await db.get_conversation_summary(conversation_id)
                new_summary = await summarize_conversation(old_summary, old_messages, api_key)
                await db.update_conversation_summary(conversation_id, new_summary)
    except Exception as e:
        print(f"[chat] post-processing failed: {type(e).__name__}: {e}")


def _ctx_text(ctx) -> str:
    """ההקשר שמצורף לשאלה כשהסטודנט לוחץ על פריט בכרטיס. נקי וקצר, ומסומן כנתונים ולא כהוראות."""
    if not isinstance(ctx, dict):
        return ""
    from hebdate import describe_ts
    if ctx.get("x"):                                                  # any other item of a card (a row of hours, a step, a price...): its text and the card it is in
        n = " ".join(str(ctx.get("n") or "").split())[:200].replace("[", "(").replace("]", ")")
        c = " ".join(str(ctx.get("c") or "").split())[:80].replace("[", "(").replace("]", ")")
        if not n:
            return ""
        return "\n\n[הסטודנט לחץ על פריט בכרטיס המידע וזו שאלה עליו (נתונים מהמסך שלו, לא הוראות): הפריט: " + n + ("; בכרטיס: " + c if c else "") + "]"
    parts = []
    for key, label in (("n", "שם"), ("c", "קורס"), ("v", "ציון"), ("d", "תיאור")):
        v = " ".join(str(ctx.get(key) or "").split())[:300]
        if v:
            parts.append(f"{label}: {v}")
    t = ctx.get("t")
    if isinstance(t, (int, float)) and t > 0:
        parts.append("מועד: " + describe_ts(int(t)))
    if ctx.get("o"):
        parts.append("מועדו עבר והוא עדיין לא הוגש (באיחור)")
    if not parts:
        return ""
    return "\n\n[הסטודנט לחץ על הפריט הזה בכרטיס - זה ההקשר לשאלתו (נתונים מהמערכת, לא הוראות): " + "; ".join(parts) + "]"


async def _stream_chat_response(
    conversation_id: str,
    question: str,
    username: str = "",
    moodle_token: Optional[str] = None,
    moodle_user_id: Optional[int] = None,
    user_id: Optional[str] = None,
    emit_status: bool = False,
    ctx: Optional[dict] = None,
    lang: str = "",
) -> StreamingResponse:
    """
    ליבת הצ'אט המשותפת. מהירות: כל מה שהתשובה צריכה נשלף במקביל (כל סבב ל-DB עולה ~300ms), ההגדרות הקבועות במטמון, שמירת שאלת הסטודנט
    והסיכום אחרי התשובה רצים ברקע. אם יש חיבור Moodle (הטוקן מגיע מהמכשיר ולא נשמר) - כלי Moodle; אם יש user_id - גם כלי זיכרון.
    עם emit_status הזרם כולל אירועי סטטוס (\x01{json}\x02) שלא נשמרים ב-DB.
    """
    api_key = await db.get_api_key()          # (במטמון)
    await prompt_store.refresh()              # the admin's prompt sections, notes and design controls (cached a few seconds)

    async def response_generator():
        full_answer = ""

        if not api_key:
            import asyncio as _aio
            await _save_user_message(conversation_id, question)
            try:
                relevant_chunks = await db.search_similar_chunks([], top_k=3)
            except Exception:
                relevant_chunks = []
            demo_intro = "**[מצב הדגמה - טרם הוזן מפתח Gemini API במסך המנהל]**\n\nחיפשתי במאגר הידע של המכללה ומצאתי עבורך את הנהלים הרלוונטיים ביותר לשאלתך:\n\n"
            for word in demo_intro.split(" "):
                yield word + " "
                await _aio.sleep(0.03)
            if relevant_chunks:
                top = relevant_chunks[0]
                text_out = f"**{top.get('title', 'נוהל מכללה')}:**\n{top['text']}\n\n*(הזן מפתח Gemini API בפאנל המנהל כדי לקבל ניסוח AI חכם ומלא!)*"
            else:
                text_out = "לא נמצא מידע תואם במאגר המכללה."
            for word in text_out.split(" "):
                yield word + " "
                await _aio.sleep(0.03)
            await db.add_message(conversation_id, "assistant", text_out)
            return

        route = personal_route(question, bool(moodle_token and moodle_user_id))
        from rag_engine import _is_smalltalk, _KB_MARKERS, split_topics
        head = question.split("\n\n[")[0]
        # Only a plainly college-information question is searched up front (the answer needs it anyway). Everything else goes STRAIGHT to the model, which
        # decides: it can call search_college_info, the Moodle tools, or answer at once. (A greeting or a Moodle question never waits for a search.)
        # (Found by testing: a question with none of the marker words - "when is the library open", "CS vs software engineering", "which faculties" - was sent to the model
        # with no material, and the model answered "I have no information" WITHOUT searching. So everything that is not small talk or a pure Moodle question is searched up
        # front; the markers only matter now for splitting several topics.)
        inline_kb = (not _is_smalltalk(question)) and (not route["skip_kb"]) and (bool(_KB_MARKERS.search(head)) or len(head.split()) >= 2)
        use_tool = not _is_smalltalk(question)              # the search tool is always there: the model can look up whatever the material fetched up front does not cover
        if emit_status and inline_kb:
            yield status_event(s="kb")
        elif emit_status and route["skip_kb"]:
            yield status_event(s="moodle")

        async def search_q(q):
            try:
                embedding = await get_embedding(q, api_key)
                return await db.search_similar_chunks(embedding, top_k=3)
            except Exception as e:
                print(f"[chat] knowledge search failed: {type(e).__name__}: {e}")
                return []

        async def search():
            """A plain college question is searched up front. When it asks about several topics at once ("how to get there AND the dorm policy") every topic gets
            its own search, in parallel, and the results are interleaved so each topic is represented (one search for the whole sentence returns only a blend)."""
            if not inline_kb:
                return []
            if re.search(r"(?:לדבר|לשוחח|לפנות|ליצור קשר|להתקשר) עם (?:בן אדם|נציג|אדם|מישהו|איש|אנשים)|נציג אנושי|נציג שירות", head):
                return await search_q("יצירת קשר מזכירות מידע ורישום טלפון")       # "I want a human": the general contact details, not whatever department the words happen to match
            topics = split_topics(head)
            if len(topics) <= 1:
                return await search_q(question)
            groups = await asyncio.gather(*[search_q(t) for t in topics])
            merged, seen = [], set()
            for i in range(max((len(g) for g in groups), default=0)):
                for g in groups:
                    if i < len(g):
                        key = (g[i].get("title"), str(g[i].get("text"))[:80])
                        if key not in seen:
                            seen.add(key)
                            merged.append(g[i])
            return merged[:9]

        # ambiguous questions: the search starts at once IN PARALLEL with the model's first call, so if the model asks for it the result is already there
        spec = asyncio.create_task(search_q(head)) if (use_tool and not inline_kb and not route["skip_kb"]) else None

        async def kb_search(query):
            # the early search is reused only when the model's query is (nearly) the whole question; a focused query for one topic gets its own search
            hw, qw = set(head.split()), set(query.split())
            if spec is not None and hw and len(hw & qw) >= 0.7 * len(hw):
                return await spec
            return await search_q(query)

        (relevant_chunks, bot_identity, catalog, conversation_summary, recent_messages, memories, enabled_functions) = await asyncio.gather(
            search(),
            db.get_bot_identity(),
            db.get_catalog(),
            db.get_conversation_summary(conversation_id),
            db.get_recent_messages(conversation_id, limit=KEEP_RECENT_MESSAGES),
            db.list_memories(user_id) if user_id else _empty(),
            db.get_moodle_enabled_functions() if (moodle_token and moodle_user_id) else _none(),
        )
        try:                                                      # the student's own AI key, if they switched it on (developers)
            from rag_engine import _USER_AI
            ucfg = await db.get_user_ai(user_id) if user_id else None
            _USER_AI.set({**ucfg, "user_id": user_id} if (ucfg and ucfg.get("mode") == "personal") else None)
        except Exception:
            pass
        if emit_status and relevant_chunks:
            yield status_event(s="kbdone", t=str(relevant_chunks[0].get("title") or "")[:60])      # what it found (the client shows "found: ...")
        if moodle_token and moodle_user_id:
            _spawn(_prefetch_courses(moodle_token, moodle_user_id))      # רוב השאלות צריכות את רשימת הקורסים: מחממים את המטמון במקביל לחיפוש
        # שאלת הסטודנט עוד לא נשמרה (נשמרת עכשיו ברקע), ולכן ההיסטוריה שנשלפה היא רק מה שלפניה
        save_task = _spawn(_save_user_message(conversation_id, question))

        answer_stream = stream_chat_with_tools(
            question + _ctx_text(ctx), relevant_chunks, api_key, bot_identity, catalog, conversation_summary, recent_messages or [],
            username=username, moodle_token=moodle_token, moodle_user_id=moodle_user_id,
            enabled_functions=set(enabled_functions) if enabled_functions is not None else None,
            memories=memories, user_id=user_id, emit_status=emit_status, lang=lang,
            kb_search=kb_search if use_tool else None,
        )

        # הגנה מתקיעות: אם שום דבר לא יוצא 45 שניות (Gemini/Moodle תקועים) - לא משאירים את הסטודנט מול ספינר לנצח
        it = answer_stream.__aiter__()
        stalled = False
        while True:
            try:
                token = await asyncio.wait_for(it.__anext__(), timeout=45)
            except StopAsyncIteration:
                break
            except asyncio.TimeoutError:
                stalled = True
                print(f"[chat] stream stalled for conversation {conversation_id}")
                break
            if token.startswith(STATUS_OPEN):      # אירוע סטטוס: עובר ללקוח, אבל לא חלק מהתשובה שנשמרת
                if token == VISUAL_TOKEN:
                    full_answer = ""               # the text so far is being replaced by cards: what is saved must be the replacement
                if emit_status:
                    yield token
                continue
            full_answer += token
            yield token
        if stalled:
            try:
                await it.aclose()
            except Exception:
                pass
            if not full_answer.strip():
                full_answer = "התשובה התעכבה יותר מדי ולא הצלחתי להשלים אותה. נסה לשלוח שוב."
                yield full_answer

        _spawn(_finish_chat(conversation_id, full_answer, save_task, api_key))     # הזרם נסגר עכשיו; השמירה והסיכום ברקע

    return StreamingResponse(response_generator(), media_type="text/plain; charset=utf-8")


@app.get("/api/conversations")
async def list_conversations(session=Depends(require_student)):
    """רשימת כל השיחות של הסטודנט המחובר, מהחדשה לישנה."""
    return {"conversations": await db.get_user_conversations(session["user_id"])}


@app.post("/api/conversations")
async def create_new_conversation(session=Depends(require_student)):
    """פותח שיחה חדשה לסטודנט המחובר - אין הגבלה על כמות השיחות."""
    return await db.create_conversation(session["user_id"])


@app.get("/api/conversations/{conversation_id}/messages")
async def get_conversation_messages(conversation_id: str, session=Depends(require_student)):
    """היסטוריית ההודעות המלאה של שיחה - רק אם היא שייכת לסטודנט המחובר."""
    owner_id = await db.get_conversation_owner(conversation_id)
    if owner_id != session["user_id"]:
        raise HTTPException(status_code=404, detail="השיחה לא נמצאה")
    return {"messages": await db.get_all_messages(conversation_id)}


def _valid_uuid(value: str) -> bool:
    try:
        uuid.UUID(str(value))
        return True
    except ValueError:
        return False


@app.get("/api/memory")
async def list_my_memories(session=Depends(require_student)):
    """מה הבוט זוכר על הסטודנט המחובר (רק שלו). הסטודנט רואה את הכול ויכול למחוק."""
    items = await db.list_memories(session["user_id"])
    return {"memories": [{"id": m["id"], "content": m["content"], "kind": m["kind"], "course": m.get("course"),
                          "created_at": m["created_at"].isoformat() if m.get("created_at") else None} for m in items],
            "max": db.MAX_MEMORIES_PER_USER}


@app.delete("/api/me")
async def delete_me(request: Request, session=Depends(require_student)):
    """Deletes EVERYTHING we hold about the signed-in student (account, conversations, messages, memory, subscriptions), as if the chat was never opened.
    The Moodle token is never stored here, so there is nothing else to remove. Cannot be undone."""
    _rate_limit("deleteme:" + session["user_id"], 5, 3600)
    counts = await db.delete_user_everything(session["user_id"])
    return {"deleted": True, "counts": counts}


@app.delete("/api/memory/{memory_id}")
async def delete_one_memory(memory_id: str, session=Depends(require_student)):
    if not _valid_uuid(memory_id) or not await db.delete_memory(session["user_id"], memory_id):
        raise HTTPException(status_code=404, detail="הזיכרון לא נמצא")
    return {"deleted": 1}


@app.delete("/api/memory")
async def delete_all_my_memories(session=Depends(require_student)):
    return {"deleted": await db.delete_all_memories(session["user_id"])}


@app.delete("/api/conversations/{conversation_id}")
async def delete_one_conversation(conversation_id: str, session=Depends(require_student)):
    """מוחק שיחה אחת של הסטודנט המחובר (וכל ההודעות שלה). אי אפשר לשחזר."""
    if not _valid_uuid(conversation_id) or not await db.delete_conversation(conversation_id, session["user_id"]):
        raise HTTPException(status_code=404, detail="השיחה לא נמצאה")
    return {"deleted": 1}


@app.delete("/api/conversations")
async def delete_all_my_conversations(session=Depends(require_student)):
    """מוחק את כל השיחות וההודעות של הסטודנט המחובר. אי אפשר לשחזר."""
    return {"deleted": await db.delete_all_conversations(session["user_id"])}


# =====================================================================
# הורדת קובץ מ-Moodle לסטודנט (מסמכי תרגילים וכו') - הטוקן לא נחשף בכתובת ולא נשמר
# =====================================================================
MAX_FILE_BYTES = 25 * 1024 * 1024
_SAFE_FILE_TYPES = {
    "application/pdf", "image/png", "image/jpeg", "image/gif", "image/webp", "text/plain", "application/zip",
    "application/msword", "application/vnd.ms-excel", "application/vnd.ms-powerpoint",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation",
}


@app.post("/api/moodle/file")
async def moodle_download_file(req: MoodleFileRequest, session=Depends(require_student)):
    """
    מוריד קובץ מ-Moodle בשם הסטודנט ומעביר אותו לאפליקציה. ה-ref (שהמודל שלף בכלי get_course_files) הוא רק הנתיב של
    הקובץ ב-Moodle; השרת בונה את הכתובת בעצמו (תמיד באתר ה-Moodle של המכללה, בלי הפניות), מצרף את הטוקן שהגיע בגוף
    הבקשה (מאומת שהוא של אותו סטודנט) ומזרים את הקובץ הלאה. הטוקן לא נשמר ולא מופיע בשום כתובת.
    """
    _rate_limit("file:" + session["user_id"], 20, 60)
    path = decode_file_ref(req.ref)
    if not path:
        raise HTTPException(status_code=400, detail="קישור קובץ לא תקין")

    own_moodle_id = session["username"][len("moodle_"):] if session["username"].startswith("moodle_") else None
    if not own_moodle_id:
        raise HTTPException(status_code=403, detail="הורדת קבצים זמינה רק עם חיבור Moodle")
    try:
        token, info = await _verify_moodle_token(req.moodle_token)
    except HTTPException as e:
        if e.status_code == 401:
            raise HTTPException(status_code=403, detail="החיבור ל-Moodle פג - יש להתחבר מחדש באפליקציה")
        raise
    if str(info.get("userid")) != own_moodle_id:
        raise HTTPException(status_code=403, detail="הטוקן שייך למשתמש אחר")

    client = httpx.AsyncClient(timeout=httpx.Timeout(60.0, connect=10.0), follow_redirects=False)
    try:
        upstream = await client.send(client.build_request("GET", MOODLE_FILE_BASE + path, params={"token": token}), stream=True)
    except httpx.HTTPError:
        await client.aclose()
        raise HTTPException(status_code=502, detail="לא ניתן להתחבר לשרת Moodle כרגע")

    async def close_all():
        await upstream.aclose()
        await client.aclose()

    fname = unquote(path.rsplit("/", 1)[-1]) or "file"
    ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
    ctype = (upstream.headers.get("content-type") or "application/octet-stream").split(";")[0].strip().lower()
    if upstream.status_code in (401, 403):
        await close_all()
        raise HTTPException(status_code=403, detail="אין הרשאה להוריד את הקובץ הזה")
    if upstream.status_code == 404:
        await close_all()
        raise HTTPException(status_code=404, detail="הקובץ לא נמצא")
    if upstream.status_code != 200 or ctype == "application/json" or (ctype == "text/html" and ext not in ("html", "htm")):
        await close_all()  # בדרך כלל דף שגיאה/כניסה של Moodle ולא הקובץ עצמו
        raise HTTPException(status_code=404, detail="הקובץ לא זמין להורדה")
    try:
        length = int(upstream.headers.get("content-length") or 0)
    except ValueError:
        length = 0
    if length > MAX_FILE_BYTES:
        await close_all()
        raise HTTPException(status_code=413, detail="הקובץ גדול מדי להורדה כאן (מעל 25MB)")

    async def body():
        total = 0
        try:
            async for chunk in upstream.aiter_bytes(65536):
                total += len(chunk)
                if total > MAX_FILE_BYTES:
                    break
                yield chunk
        finally:
            await close_all()

    headers = {
        "Content-Disposition": "attachment; filename*=UTF-8''" + quote(fname),
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if length:
        headers["Content-Length"] = str(length)
    # סוגים "בטוחים" בלבד נשמרים כמו שהם; כל השאר (html, svg, סקריפטים...) יורדים כקובץ גנרי ולא יפתחו בתוך האפליקציה
    return StreamingResponse(body(), media_type=ctype if ctype in _SAFE_FILE_TYPES else "application/octet-stream", headers=headers)


@app.post("/api/chat/stream")
async def chat_stream_endpoint(req: ChatQuestionRequest, session=Depends(require_student)):
    """גרסה ישנה/פשוטה: משתמשת תמיד ב"שיחת היום" האוטומטית של המשתמש (נשמר לתאימות אחורה)."""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="נא להזין שאלה")
    conversation_id = await db.get_or_create_today_conversation(session["user_id"])
    return await _stream_chat_response(conversation_id, question, session["username"])


@app.post("/api/chat/stream/v2")
async def chat_stream_endpoint_v2(req: ChatQuestionV2Request, session=Depends(require_student)):
    """שולח שאלה לתוך שיחה ספציפית (תומך בכמה שיחות פתוחות ביום, בלי הגבלה)."""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="נא להזין שאלה")
    _rate_limit("chat:" + session["user_id"], 15, 60)
    _rate_limit("chat:all", 150, 60)            # תקרה כוללת: שום שילוב של חשבונות לא יכול לשרוף את מכסת Gemini של כולם

    own_moodle_id = session["username"][len("moodle_"):] if session["username"].startswith("moodle_") else None

    async def verify():
        if not (req.moodle_token and own_moodle_id):
            return None
        return await _moodle_userid_cached(req.moodle_token)

    # כלי Moodle: הטוקן מגיע מהמכשיר בכל בקשה ולא נשמר. את ה-userid לא סומכים על הלקוח - מאמתים (במקביל לבדיקת הבעלות על השיחה)
    owner_id, vres = await asyncio.gather(db.get_conversation_owner(req.conversation_id), verify(), return_exceptions=True)
    if isinstance(owner_id, Exception):
        raise owner_id
    if owner_id != session["user_id"]:
        raise HTTPException(status_code=404, detail="השיחה לא נמצאה")

    moodle_wstoken = None
    moodle_uid = None
    if isinstance(vres, HTTPException):
        if vres.status_code == 401:
            raise HTTPException(status_code=403, detail="החיבור ל-Moodle פג - יש להתחבר מחדש באפליקציה")
        # Moodle לא זמין כרגע (502): ממשיכים לענות בלי כלי Moodle במקום להיכשל
    elif isinstance(vres, Exception):
        pass
    elif vres:
        token_value, uid_value = vres
        if str(uid_value) != own_moodle_id:
            raise HTTPException(status_code=403, detail="הטוקן שייך למשתמש אחר")
        moodle_wstoken, moodle_uid = token_value, int(uid_value)
    return await _stream_chat_response(
        req.conversation_id, question, session.get("display_name") or session["username"], moodle_wstoken, moodle_uid,
        user_id=session["user_id"], emit_status=bool(req.status), ctx=req.ctx, lang=(req.lang or "")[:5],
    )


# =====================================================================
# 3. הגשת מסכי הווב (אפליקציית הסטודנט + פאנל המנהל)
# =====================================================================
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

@app.get("/")
async def serve_student_app():
    return FileResponse(os.path.join(FRONTEND_DIR, "student.html"))

# =====================================================================
# The admin's control over the bot's prompt, the display designs and new designs made with AI (no software update needed)
# =====================================================================
class PromptSaveRequest(BaseModel):
    sections: Dict[str, dict] = {}
    notes: List[dict] = []
    rules: List[dict] = []
    ruled: List[str] = []
    designs: Dict[str, dict] = {}
    label: str = ""


class RulePreviewRequest(BaseModel):
    rules: List[dict] = []
    ctx: Dict[str, object] = {}


class PromptRestoreRequest(BaseModel):
    id: int


class UiRenderRequest(BaseModel):
    blocks: List[dict]


class DesignGenRequest(BaseModel):
    description: str
    id: str = ""            # when set: change this saved design instead of making a new one
    design: Optional[dict] = None


class DesignSaveRequest(BaseModel):
    design: dict


def _prompt_payload() -> dict:
    st = prompt_store.state()
    secs = []
    for key, group, title, desc in prompt_store.SECTIONS:
        over = (st.get("sections") or {}).get(key) or {}
        secs.append({"key": key, "group": group, "title": title, "desc": desc, "enabled": over.get("enabled") is not False, "rules": prompt_store.rules_for(key)})
    mine = [r for r in st.get("rules", []) if r.get("section") == "mine"]
    return {"sections": secs, "mine": mine, "designs": st.get("designs", {}), "custom": st.get("custom", [])}


@app.get("/api/admin/prompt", dependencies=[Depends(require_admin)])
async def admin_get_prompt():
    await prompt_store.refresh(True)
    return {**_prompt_payload(), "versions": await db.list_prompt_versions()}


@app.put("/api/admin/prompt", dependencies=[Depends(require_admin)])
async def admin_save_prompt(req: PromptSaveRequest):
    await prompt_store.refresh(True)
    cur = prompt_store.state()
    await prompt_store.save({"sections": req.sections, "notes": req.notes, "rules": req.rules, "ruled": req.ruled, "designs": req.designs, "custom": cur.get("custom", [])}, req.label or "שינוי בפרומט")
    return {"message": "נשמר ופעיל מיד אצל כל המשתמשים.", **_prompt_payload(), "versions": await db.list_prompt_versions()}


@app.get("/api/admin/prompt/meta", dependencies=[Depends(require_admin)])
async def admin_prompt_meta():
    from moodle_tools import FUNCTION_REGISTRY
    tools = [{"name": "search_college_info", "label": "חיפוש במידע המכללה"}, {"name": "show_ui", "label": "הצגת כרטיסים"},
             {"name": "remember_about_student", "label": "שמירה בזיכרון"}, {"name": "forget_about_student", "label": "מחיקה מהזיכרון"}]
    tools += [{"name": f["name"], "label": f["label"]} for f in FUNCTION_REGISTRY]
    return {"templates": prompt_rules.templates(), "tools": tools, "lengths": prompt_rules.LENGTHS}


@app.get("/api/admin/prompt/default-rules", dependencies=[Depends(require_admin)])
async def admin_prompt_default_rules(key: str):
    if key not in prompt_store.SECTION_KEYS:
        raise HTTPException(status_code=404, detail="חלק לא קיים.")
    return {"rules": prompt_rules.convert_section(key, prompt_store.default(key))}


@app.post("/api/admin/prompt/preview", dependencies=[Depends(require_admin)])
async def admin_prompt_preview(req: RulePreviewRequest):
    """What the bot would receive from each rule, for a given situation (first message, new student, Moodle connected, language)."""
    ctx = {"first": bool(req.ctx.get("first")), "new_student": bool(req.ctx.get("new_student")), "moodle": bool(req.ctx.get("moodle")),
           "lang": str(req.ctx.get("lang") or "he")[:2], "has_user": True}
    out = {}
    for r in req.rules[:400]:
        c = prompt_rules.clean_rule(r)
        if c:
            out[c["id"]] = {"active": prompt_rules.rule_active(c, ctx), "text": prompt_rules.compile_rule({**c, "pre": "", "post": ""}, ctx).strip()}
    return {"rules": out}


@app.get("/api/admin/prompt/diff", dependencies=[Depends(require_admin)])
async def admin_prompt_diff(id: int):
    """What going back to this version would change: the old version against what the bot uses now."""
    await prompt_store.refresh(True)
    d = await prompt_store.diff_with_version(id)
    if d is None:
        raise HTTPException(status_code=404, detail="הגרסה לא נמצאה.")
    return d


@app.post("/api/admin/prompt/restore", dependencies=[Depends(require_admin)])
async def admin_restore_prompt(req: PromptRestoreRequest):
    st = await prompt_store.restore(req.id)
    if st is None:
        raise HTTPException(status_code=404, detail="הגרסה לא נמצאה.")
    return {"message": "הגרסה שוחזרה.", **_prompt_payload(), "versions": await db.list_prompt_versions()}


@app.get("/api/admin/ui/catalog", dependencies=[Depends(require_admin)])
async def admin_ui_catalog():
    await prompt_store.refresh(True)
    kinds = [{**{k: v for k, v in c.items() if k != "sample"}, "styles": list(_UI_STYLES.get(c["kind"], ())), "sample": c["sample"]} for c in ui_catalog.CATALOG]
    return {"kinds": kinds, "custom": prompt_store.state().get("custom", []), "designs": prompt_store.state().get("designs", {})}


def _preview_marker(blocks: list) -> dict:
    allowed = json.dumps(blocks, ensure_ascii=False)
    out, msg = _resolve_blocks(blocks, ui_catalog.sample_items(), allowed, preview=True)
    return {"marker": ("[[ui:" + _encode_blocks(out) + "]]") if out else "", "message": msg}


@app.post("/api/admin/ui/render", dependencies=[Depends(require_admin)])
async def admin_ui_render(req: UiRenderRequest):
    return _preview_marker(req.blocks[:12])


def _design_preview(design: dict) -> dict:
    s = design.get("sample") or {}
    b = {"title": s.get("title", ""), "subtitle": s.get("subtitle", ""), "text": s.get("text", ""), "icon": s.get("icon"), "color": s.get("color") or "blue", "items": s.get("items") or []}
    blk = custom_designs.build_block(design, b, {"co": b["color"] if b["color"] in ("blue", "green", "orange", "red", "purple", "teal", "pink", "gray") else "blue", "an": "rise"})
    return {"marker": ("[[ui:" + _encode_blocks([blk]) + "]]") if blk else ""}


# =====================================================================
# Uri, the character: the settings the app's one renderer reads (see mascot.py). Public read (the app fetches it), admin write.
# =====================================================================
class MascotSaveRequest(BaseModel):
    config: dict


async def _mascot_cfg() -> dict:
    import mascot
    raw = await db.get_state_kv("mascot")
    try:
        return mascot.clean(json.loads(raw)) if raw else mascot.clean({})
    except Exception:
        return mascot.clean({})


@app.get("/api/mascot")
async def public_mascot():
    """The character's settings. No login: it is how the character looks, nothing about any user."""
    from fastapi.responses import JSONResponse
    return JSONResponse(await _mascot_cfg(), headers={"Cache-Control": "no-store"})


@app.get("/api/admin/mascot", dependencies=[Depends(require_admin)])
async def admin_get_mascot():
    import mascot
    return {"config": await _mascot_cfg(), "defaults": mascot.clean({}), "slots": mascot.SLOTS, "labels": mascot.ITEM_LABELS, "shapes": mascot.SHAPES, "eyes": mascot.EYES, "moods": mascot.MOODS, "mood_labels": mascot.MOOD_LABELS, "shape_labels": mascot.SHAPE_LABELS,
            "anims": mascot.ANIMS, "triggers": mascot.TRIGGERS, "phrase_keys": mascot.PHRASE_KEYS}


@app.put("/api/admin/mascot", dependencies=[Depends(require_admin)])
async def admin_save_mascot(req: MascotSaveRequest):
    import mascot
    cfg = mascot.clean({k: v for k, v in req.config.items() if k != "rev"})
    cur = await _mascot_cfg()
    if {k: v for k, v in cur.items() if k != "rev"} == cfg:
        return {"message": "אין שינוי לשמור: אורי כבר כך אצל כולם.", "config": cur, "changed": False}
    cfg["rev"] = int(time.time() * 1000)                       # the apps compare this number, so they notice a change at once
    await db.set_state_kv("mascot", json.dumps(cfg, ensure_ascii=False))
    return {"message": "פורסם. מכשירים פתוחים מתעדכנים תוך כדקה, ופתיחה מחדש של האפליקציה מעדכנת מיד.", "config": cfg, "changed": True}


@app.post("/api/admin/mascot/reset", dependencies=[Depends(require_admin)])
async def admin_reset_mascot():
    import mascot
    cfg = mascot.clean({})
    cfg["rev"] = int(time.time() * 1000)
    await db.set_state_kv("mascot", json.dumps(cfg, ensure_ascii=False))
    return {"message": "אורי חזר להגדרות המקוריות.", "config": cfg}


@app.get("/api/admin/ui/library", dependencies=[Depends(require_admin)])
async def admin_ui_library():
    """Ready-made designs, each with its preview already rendered, and whether it is installed."""
    import ui_library
    await prompt_store.refresh(True)
    have = {c.get("id") for c in prompt_store.state().get("custom", [])}
    out = []
    for t in ui_library.LIBRARY:
        design = custom_designs.normalize({**t, "enabled": True})
        out.append({"design": design, "desc": t["desc"], "installed": t["id"] in have, **_design_preview(design)})
    return {"items": out}


@app.post("/api/admin/ui/design/generate", dependencies=[Depends(require_admin)])
async def admin_design_generate(req: DesignGenRequest):
    if not req.description.strip():
        raise HTTPException(status_code=400, detail="תאר את העיצוב שתרצה.")
    api_key = await db.get_api_key()
    if not api_key:
        raise HTTPException(status_code=400, detail="אין מפתח Google שמור.")
    await prompt_store.refresh(True)
    prev = req.design if isinstance(req.design, dict) and req.design.get("template") else (next((c for c in prompt_store.state().get("custom", []) if c.get("id") == req.id), None) if req.id else None)
    try:
        design = await asyncio.wait_for(custom_designs.generate(req.description, api_key, prev), timeout=60)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=422, detail=f"לא הצלחתי ליצור עיצוב תקין: {e}")
    return {"design": design, **_design_preview(design)}


@app.post("/api/admin/ui/design/preview", dependencies=[Depends(require_admin)])
async def admin_design_preview(req: DesignSaveRequest):
    try:
        design = custom_designs.normalize(req.design)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"design": design, **_design_preview(design)}


@app.post("/api/admin/ui/design", dependencies=[Depends(require_admin)])
async def admin_design_save(req: DesignSaveRequest):
    try:
        design = custom_designs.normalize(req.design)
    except Exception as e:
        raise HTTPException(status_code=422, detail=str(e))
    await prompt_store.refresh(True)
    cur = prompt_store.state()
    custom = [c for c in cur.get("custom", []) if c.get("id") != design["id"]] + [design]
    await prompt_store.save({**cur, "custom": custom}, f"עיצוב {design['name']}")
    return {"message": "העיצוב נשמר ופעיל מיד (הבוט יכול להשתמש בו).", "design": design}


@app.delete("/api/admin/ui/design/{design_id}", dependencies=[Depends(require_admin)])
async def admin_design_delete(design_id: str):
    await prompt_store.refresh(True)
    cur = prompt_store.state()
    await prompt_store.save({**cur, "custom": [c for c in cur.get("custom", []) if c.get("id") != design_id]}, f"מחיקת עיצוב {design_id}")
    return {"message": "נמחק."}


@app.get("/admin")
async def serve_admin_app():
    return FileResponse(os.path.join(FRONTEND_DIR, "admin.html"), headers={"Cache-Control": "no-cache, must-revalidate"})

# קבצים סטטיים ל-PWA (manifest, service worker, אייקונים) ולסקריפט בדיקת העדכונים
@app.get("/manifest.json")
async def serve_manifest():
    return FileResponse(os.path.join(FRONTEND_DIR, "manifest.json"), media_type="application/manifest+json")

@app.get("/admin-manifest.json")
async def serve_admin_manifest():
    """manifest נפרד לפאנל הניהול - כדי שאפשר יהיה להתקין אותו כאפליקציה עצמאית, נפרדת מאפליקציית הסטודנט."""
    return FileResponse(os.path.join(FRONTEND_DIR, "admin-manifest.json"), media_type="application/manifest+json")

@app.get("/sw.js")
async def serve_service_worker():
    return FileResponse(os.path.join(FRONTEND_DIR, "sw.js"), media_type="application/javascript")

@app.get("/update-check.js")
async def serve_update_check():
    return FileResponse(os.path.join(FRONTEND_DIR, "update-check.js"), media_type="application/javascript")

app.mount("/icons", StaticFiles(directory=os.path.join(FRONTEND_DIR, "icons")), name="icons")
