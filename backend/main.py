import sys
try:
    # מונע קריסה בהדפסת עברית/אימוג'י לקונסול של Windows (cp1255/cp1252 לא תומכים ב-UTF-8)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import os
import json
import asyncio
import httpx
from io import BytesIO
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from pypdf import PdfReader

from db import db
from auth import admin_auth, require_admin, hash_password, student_auth, require_student
from gemini_diagnostics import run_full_diagnostics, test_connectivity
from push import send_push_to_subscription, VAPID_PUBLIC_KEY
from rag_engine import (
    chunk_text,
    get_embedding,
    categorize_and_summarize,
    stream_gemini_answer,
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

# הגדרת CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


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
    question: str


class AdminLoginRequest(BaseModel):
    password: str


class StudentSignupRequest(BaseModel):
    username: str
    password: str


class StudentLoginRequest(BaseModel):
    username: str
    password: str


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


async def _stream_chat_response(conversation_id: str, question: str) -> StreamingResponse:
    """
    ליבת הצ'אט המשותפת: שולפת מידע רלוונטי (pgvector), שומרת את שאלת הסטודנט,
    מזרימה תשובה עם הקשר (סיכום + הודעות אחרונות), שומרת אותה, ודוחסת הקשר ישן לסיכום
    כשהשיחה מתארכת. אין יותר הגבלת מכסה יומית - סטודנט יכול לשאול כמה שאלות שהוא רוצה.
    """
    api_key = await db.get_api_key()

    # שליפת המידע הרלוונטי ממאגר הידע של המכללה - חיפוש וקטורי שרץ בתוך ה-DB עצמו (pgvector)
    question_embedding = await get_embedding(question, api_key) if api_key else []
    relevant_chunks = await db.search_similar_chunks(question_embedding, top_k=3)

    await db.add_message(conversation_id, "user", question)

    async def response_generator():
        full_answer = ""

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

        async for token in stream_gemini_answer(
            question, relevant_chunks, api_key, bot_identity, catalog,
            conversation_summary, history_for_prompt
        ):
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


@app.post("/api/chat/stream")
async def chat_stream_endpoint(req: ChatQuestionRequest, session=Depends(require_student)):
    """גרסה ישנה/פשוטה: משתמשת תמיד ב"שיחת היום" האוטומטית של המשתמש (נשמר לתאימות אחורה)."""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="נא להזין שאלה")
    conversation_id = await db.get_or_create_today_conversation(session["user_id"])
    return await _stream_chat_response(conversation_id, question)


@app.post("/api/chat/stream/v2")
async def chat_stream_endpoint_v2(req: ChatQuestionV2Request, session=Depends(require_student)):
    """שולח שאלה לתוך שיחה ספציפית (תומך בכמה שיחות פתוחות ביום, בלי הגבלה)."""
    question = req.question.strip()
    if not question:
        raise HTTPException(status_code=400, detail="נא להזין שאלה")
    owner_id = await db.get_conversation_owner(req.conversation_id)
    if owner_id != session["user_id"]:
        raise HTTPException(status_code=404, detail="השיחה לא נמצאה")
    return await _stream_chat_response(req.conversation_id, question)


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

@app.get("/sw.js")
async def serve_service_worker():
    return FileResponse(os.path.join(FRONTEND_DIR, "sw.js"), media_type="application/javascript")

@app.get("/update-check.js")
async def serve_update_check():
    return FileResponse(os.path.join(FRONTEND_DIR, "update-check.js"), media_type="application/javascript")

app.mount("/icons", StaticFiles(directory=os.path.join(FRONTEND_DIR, "icons")), name="icons")
