import sys
try:
    # מונע קריסה בהדפסת עברית/אימוג'י לקונסול של Windows (cp1255/cp1252 לא תומכים ב-UTF-8)
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

import os
from io import BytesIO
from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from typing import Optional
from pypdf import PdfReader

from db import db
from auth import admin_auth, require_admin, hash_password
from gemini_diagnostics import run_full_diagnostics, test_connectivity
from rag_engine import (
    chunk_text,
    get_embedding,
    categorize_and_summarize,
    stream_gemini_answer,
    stream_knowledge_draft
)

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
    user_id: str
    question: str


class AdminLoginRequest(BaseModel):
    password: str


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
    return {"chunks": await db.get_all_chunks()}


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


async def _save_chunks_with_ai_categorization(chunks: list, api_key: str) -> int:
    """
    עבור כל chunk: יוצר embedding, מסווג אותו לקטגוריה (ע"י AI, מול רשימת הקטגוריות
    הקיימות), שומר אותו ב-DB, ומעדכן את הקטלוג (ה"מפה") שתמיד מוצגת למודל הצ'אט.
    """
    existing_categories = await db.get_categories()
    saved = 0
    for c in chunks:
        embedding = await get_embedding(f"{c['title']}: {c['text']}", api_key) if api_key else []

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
    return saved


@app.post("/api/admin/knowledge/text", dependencies=[Depends(require_admin)])
async def upload_knowledge_text(req: AddKnowledgeTextRequest):
    """העלאת טקסט חופשי על ידי המנהל: חיתוך ל-Chunks, יצירת וקטורים (Embeddings) וסיווג ע"י AI"""
    chunks = chunk_text(req.text, title=req.title)
    if not chunks:
        raise HTTPException(status_code=400, detail="הטקסט שהוזן ריק")

    api_key = await db.get_api_key()
    saved = await _save_chunks_with_ai_categorization(chunks, api_key)
    return {
        "message": f"המידע עובד בהצלחה ונשמר ב-{saved} מקטעים (Chunks)!",
        "added_chunks": saved
    }


@app.post("/api/admin/knowledge/pdf", dependencies=[Depends(require_admin)])
async def upload_knowledge_pdf(title: str = Form(...), file: UploadFile = File(...)):
    """העלאת קובץ PDF או TXT על ידי המנהל, חילוץ הטקסט, חיתוך ויצירת וקטורים"""
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
    saved = await _save_chunks_with_ai_categorization(chunks, api_key)
    return {
        "message": f"הקובץ '{file.filename}' עובד ונחתך ל-{saved} מקטעים!",
        "added_chunks": saved
    }


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


@app.post("/api/chat/stream")
async def chat_stream_endpoint(req: ChatQuestionRequest):
    """
    הנתיב המרכזי של הצ'אט:
    1. בודק אם לסטודנט נשארה מכסה יומית.
    2. בודק אם יש מפתח API בשרת (אם עוד לא הוזן מפתח, מחזיר תשובת הדגמה חכמה מה-RAG!).
    3. שולף את הנהלים הרלוונטיים של המכללה.
    4. מעדכן את המכסה ומזרים את התשובה מילה-אחר-מילה (Streaming).
    """
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="נא להזין שאלה")

    # 1. בדיקת מכסה יומית (שומר הסף)
    quota = await db.get_user_quota_status(req.user_id)
    if not quota["can_ask"]:
        raise HTTPException(
            status_code=429,
            detail=f"סיימת את מכסת השאלות היומית שלך ({quota['daily_limit']} שאלות). המכסה תתאפס אוטומטית מחר!"
        )

    api_key = await db.get_api_key()

    # 2. שליפת המידע הרלוונטי ממאגר הידע של המכללה - חיפוש וקטורי שרץ בתוך ה-DB עצמו (pgvector)
    question_embedding = await get_embedding(req.question, api_key) if api_key else []
    relevant_chunks = await db.search_similar_chunks(question_embedding, top_k=3)

    # 3. הורדת שאלה אחת מהמכסה של הסטודנט
    await db.increment_user_usage(req.user_id)

    # 4. הזרמת התשובה (Streaming)
    async def response_generator():
        # אם המנהל עדיין לא הזין מפתח Gemini API, נציג מצב הדגמה שמראה שה-RAG עובד!
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
            return

        # כשיש מפתח API - פונים ל-Gemini דרך התור ומזרימים בזמן אמת
        bot_identity = await db.get_bot_identity()
        catalog = await db.get_catalog()
        async for token in stream_gemini_answer(req.question, relevant_chunks, api_key, bot_identity, catalog):
            yield token

    return StreamingResponse(response_generator(), media_type="text/plain; charset=utf-8")


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
