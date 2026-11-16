import sys
import time
import base64
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
from io import BytesIO
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from typing import Optional, List
from pypdf import PdfReader

from db import db
from auth import admin_auth, require_admin, hash_password, student_auth, require_student
from gemini_diagnostics import run_full_diagnostics, test_connectivity
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
async def admin_login(req: AdminLoginRequest):
    """התחברות לפאנל הניהול. הסיסמה נוצרת אוטומטית בהפעלה הראשונה של השרת ומוצגת בקונסול."""
    if not await admin_auth.verify_password(db, req.password):
        raise HTTPException(status_code=401, detail="סיסמה שגויה")
    token = admin_auth.create_session()
    return {"token": token}


@app.post("/api/admin/change-password", dependencies=[Depends(require_admin)])
async def change_admin_password(req: ChangePasswordRequest):
    """מאפשר למנהל המחובר לשנות את סיסמת הכניסה לפאנל (נשמר כ-hash ב-DB)."""
    if not req.new_password or len(req.new_password) < 3:
        raise HTTPException(status_code=400, detail="הסיסמה חייבת להכיל לפחות 3 תווים")
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
async def student_signup(req: StudentSignupRequest):
    """יצירת חשבון סטודנט חדש - שם משתמש + סיסמה, נשמר ב-DB (app_users)."""
    user_id = await student_auth.signup(db, req.username, req.password)
    if not user_id:
        raise HTTPException(status_code=400, detail="שם המשתמש כבר תפוס, או שהסיסמה קצרה מדי (מינימום 3 תווים)")
    login_result = await student_auth.login(db, req.username, req.password)
    return login_result


@app.post("/api/login")
async def student_login(req: StudentLoginRequest):
    """התחברות לחשבון סטודנט קיים."""
    result = await student_auth.login(db, req.username, req.password)
    if not result:
        raise HTTPException(status_code=401, detail="שם משתמש או סיסמה שגויים")
    return result


TEST_LOGIN_PASSWORD = "1234"
TEST_LOGIN_USERNAME = "test_user"


@app.post("/api/test-login")
async def test_login(req: TestLoginRequest):
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
        async with httpx.AsyncClient(timeout=15) as client:
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
    if not user:
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
    from rag_engine import _build_chat_user_turn, MOODLE_SYSTEM_ADDENDUM
    from moodle_tools import MOODLE_FUNCTION_NAMES

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
        extra_system_instructions=MOODLE_SYSTEM_ADDENDUM if enabled_functions else "",
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


@app.get("/api/admin/knowledge", dependencies=[Depends(require_admin)])
async def list_knowledge():
    """מחזיר את כל קטעי המידע של המכללה השמורים במאגר"""
    return {"chunks": await db.get_all_chunks(), "catalog": await db.get_catalog()}


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


async def _stream_chat_response(
    conversation_id: str,
    question: str,
    username: str = "",
    moodle_token: Optional[str] = None,
    moodle_user_id: Optional[int] = None,
    user_id: Optional[str] = None,
    emit_status: bool = False,
) -> StreamingResponse:
    """
    ליבת הצ'אט המשותפת: שומרת את שאלת הסטודנט, שולפת מידע רלוונטי (pgvector), טוענת את הזיכרון של הסטודנט, ומזרימה תשובה
    עם הקשר (סיכום + הודעות אחרונות); שומרת אותה, ודוחסת הקשר ישן לסיכום כשהשיחה מתארכת. אין הגבלת מכסה יומית.
    אם יש חיבור Moodle פעיל (moodle_token+moodle_user_id מגיעים מה-frontend, לא נשמרים אצלנו) - גמיני מקבל כלי Moodle,
    ואם יש user_id - גם כלי זיכרון. עם emit_status הזרם כולל אירועי סטטוס (\x01{json}\x02) שלא נשמרים ב-DB.
    """
    api_key = await db.get_api_key()

    await db.add_message(conversation_id, "user", question)
    # כותרת לשיחה (להיסטוריה): השורה הראשונה של השאלה הראשונה, קצרה
    await db.set_conversation_title_if_default(conversation_id, (question.splitlines() or [question])[0][:48])

    async def response_generator():
        full_answer = ""

        if emit_status:
            yield status_event(s="kb")
        # שליפת המידע הרלוונטי ממאגר הידע של המכללה - חיפוש וקטורי שרץ בתוך ה-DB עצמו (pgvector)
        try:
            question_embedding = await get_embedding(question, api_key) if api_key else []
            relevant_chunks = await db.search_similar_chunks(question_embedding, top_k=3)
        except Exception as e:
            print(f"[chat] knowledge search failed: {type(e).__name__}: {e}")
            relevant_chunks = []

        if not api_key:
            import asyncio
            demo_intro = "💡 **[מצב הדגמה - טרם הוזן מפתח Gemini API במסך המנהל]**\n\nחיפשתי במאגר הידע של המכללה ומצאתי עבורך את הנהלים הרלוונטיים ביותר לשאלתך:\n\n"
            for word in demo_intro.split(" "):
                yield word + " "
                await asyncio.sleep(0.03)

            if relevant_chunks:
                top = relevant_chunks[0]
                text_out = f"📌 **{top.get('title', 'נוהל מכללה')}:**\n{top['text']}\n\n*(הזן מפתח Gemini API בפאנל המנהל כדי לקבל ניסוח AI חכם ומלא!)*"
            else:
                text_out = "לא נמצא מידע תואם במאגר המכללה."

            for word in text_out.split(" "):
                yield word + " "
                await asyncio.sleep(0.03)
            await db.add_message(conversation_id, "assistant", text_out)
            return

        bot_identity = await db.get_bot_identity()
        catalog = await db.get_catalog()
        conversation_summary = await db.get_conversation_summary(conversation_id)
        recent_messages = await db.get_recent_messages(conversation_id, limit=KEEP_RECENT_MESSAGES)
        # ההודעה האחרונה ברשימה היא השאלה הנוכחית עצמה - לא צריך לשלוח אותה פעמיים
        history_for_prompt = recent_messages[:-1] if recent_messages else []
        memories = await db.list_memories(user_id) if user_id else []
        enabled_functions = await db.get_moodle_enabled_functions() if (moodle_token and moodle_user_id) else None

        answer_stream = stream_chat_with_tools(
            question, relevant_chunks, api_key, bot_identity, catalog, conversation_summary, history_for_prompt,
            username=username, moodle_token=moodle_token, moodle_user_id=moodle_user_id,
            enabled_functions=set(enabled_functions) if enabled_functions is not None else None,
            memories=memories, user_id=user_id, emit_status=emit_status,
        )

        async for token in answer_stream:
            if token.startswith(STATUS_OPEN):      # אירוע סטטוס: עובר ללקוח, אבל לא חלק מהתשובה שנשמרת
                if emit_status:
                    yield token
                continue
            full_answer += token
            yield token

        await db.add_message(conversation_id, "assistant", full_answer)

        # דחיסת הקשר: אם השיחה התארכה, נסכם את ההודעות הישנות כדי שלא יישלח טקסט ענק למודל בהמשך
        total_messages = await db.count_messages(conversation_id)
        if total_messages > SUMMARIZE_AFTER_MESSAGES:
            old_messages = await db.get_old_messages_to_summarize(conversation_id, keep_recent=KEEP_RECENT_MESSAGES)
            if old_messages:
                old_summary = await db.get_conversation_summary(conversation_id)
                new_summary = await summarize_conversation(old_summary, old_messages, api_key)
                await db.update_conversation_summary(conversation_id, new_summary)

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
    owner_id = await db.get_conversation_owner(req.conversation_id)
    if owner_id != session["user_id"]:
        raise HTTPException(status_code=404, detail="השיחה לא נמצאה")

    # כלי Moodle: הטוקן מגיע מהמכשיר בכל בקשה ולא נשמר. את ה-userid לא סומכים על הלקוח - מאמתים מול
    # Moodle עצמו שהטוקן שייך בדיוק למשתמש של הסשן הזה, ורק אז מפעילים את הכלים עבורו.
    moodle_wstoken = None
    moodle_uid = None
    own_moodle_id = session["username"][len("moodle_"):] if session["username"].startswith("moodle_") else None
    if req.moodle_token and own_moodle_id:
        try:
            moodle_wstoken, info = await _verify_moodle_token(req.moodle_token)
        except HTTPException as e:
            moodle_wstoken = None
            if e.status_code == 401:
                raise HTTPException(status_code=403, detail="החיבור ל-Moodle פג - יש להתחבר מחדש באפליקציה")
            # Moodle לא זמין כרגע (502): ממשיכים לענות בלי כלי Moodle במקום להיכשל
        else:
            if str(info.get("userid")) != own_moodle_id:
                raise HTTPException(status_code=403, detail="הטוקן שייך למשתמש אחר")
            moodle_uid = int(info["userid"])
    return await _stream_chat_response(
        req.conversation_id, question, session.get("display_name") or session["username"], moodle_wstoken, moodle_uid,
        user_id=session["user_id"], emit_status=bool(req.status),
    )


# =====================================================================
# 3. הגשת מסכי הווב (אפליקציית הסטודנט + פאנל המנהל)
# =====================================================================
FRONTEND_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "frontend")

@app.get("/")
async def serve_student_app():
    return FileResponse(os.path.join(FRONTEND_DIR, "student.html"))

@app.get("/admin")
async def serve_admin_app():
    return FileResponse(os.path.join(FRONTEND_DIR, "admin.html"))

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
