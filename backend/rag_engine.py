import asyncio
import json
import re
from typing import List, Dict, Any, AsyncGenerator, Optional
import httpx

from db import db

# =====================================================================
# מנגנון תורים (Queue) למניעת חסימות Rate-Limit ב-API החינמי של גוגל
# מאפשר עד 2 בקשות מקבילות ל-Gemini בו-זמנית; השאר ממתינים בתור מסודר
# =====================================================================
gemini_queue_semaphore = asyncio.Semaphore(2)

# =====================================================================
# System Prompt קשיח למניעת הזיות (Anti-Hallucination)
# =====================================================================
def build_system_prompt(bot_identity: str = "", catalog: Optional[List[Dict[str, str]]] = None) -> str:
    """
    בונה את ה-System Prompt. מידע הזהות של הבוט (מי הוא, מטעם מי, מה הוא יודע לעשות)
    מוזרק תמיד, בלי קשר לתוצאות ה-RAG - כדי שהבוט ידע לענות על "מי אתה" בעקביות,
    ולא רק כשהמידע הזה במקרה עלה בחיפוש הדמיון. אותו דבר לגבי הקטלוג (ה"מפה") -
    רשימת הנושאים הקיימים במאגר הידע, שמתעדכנת ע"י AI בכל הוספת מידע ומוצגת תמיד למודל.
    """
    identity_block = ""
    if bot_identity and bot_identity.strip():
        identity_block = f"""
=== מידע בסיסי על עצמך (זמין תמיד, לא תלוי בשאלה) ===
{bot_identity.strip()}
========================================================
"""

    catalog_block = ""
    if catalog:
        catalog_lines = "\n".join(f"- {c['category']}: {c['summary']}" for c in catalog)
        catalog_block = f"""
=== מפת הנושאים הקיימים במאגר הידע (לידיעתך - לא להציג כמו שהוא לסטודנט) ===
{catalog_lines}
=============================================================================
"""

    return f"""אתה העוזר האקדמי הרשמי והחכם של המכללה.
תפקידך לענות על שאלות של סטודנטים ("למי פונים כש...", "מה עושים כש...") בצורה אדיבה, ברורה ותמציתית בעברית.
{identity_block}{catalog_block}
חוקי ברזל שאסור לך להפר בשום מצב:
1. על שאלות הנוגעות לנהלי המכללה - ענה אך ורק על סמך "מידע המכללה הרלוונטי" המצורף למטה. על שאלות הנוגעות אליך עצמך (מי אתה, מה שמך, מטעם מי אתה פועל, מה אתה יודע לעשות) - ענה אך ורק על סמך "מידע בסיסי על עצמך" שמופיע למעלה, אם סופק.
2. אל תמציא שום מידע, מספרי טלפון, שמות, חדרים, שעות פעילות או נהלים שאינם כתובים במפורש במידע המצורף!
3. אם התשובה לשאלת הסטודנט אינה מופיעה באף אחד מהמקורות המצורפים, אמור בנימוס: "אין לי מידע רשמי על כך בנהלי המכללה המעודכנים במערכת. מומלץ לפנות למזכירות הסטודנטים לבירור."
4. שמור על תשובות מסודרות ונעימות לקריאה (השתמש בנקודות במידת הצורך).
"""


def chunk_text(text: str, title: str = "מסמך מכללה", chunk_size: int = 600, overlap: int = 100) -> List[Dict[str, Any]]:
    """
    חותך טקסט ארוך שהמנהל מעלה למקטעים (Chunks) עם חפיפה (Overlap),
    כדי שמשפטים והקשרים לא ייקטעו באמצע.
    """
    cleaned = text.strip()
    if not cleaned:
        return []

    chunks = []
    start = 0
    part = 1
    while start < len(cleaned):
        end = min(start + chunk_size, len(cleaned))
        # ננסה לחתוך בסוף משפט או שורה חדשה אם אפשר
        if end < len(cleaned):
            last_period = max(cleaned.rfind(".", start, end), cleaned.rfind("\n", start, end))
            if last_period > start + (chunk_size // 2):
                end = last_period + 1

        chunk_str = cleaned[start:end].strip()
        if chunk_str:
            chunks.append({
                "title": f"{title} (חלק {part})" if len(cleaned) > chunk_size else title,
                "text": chunk_str,
            })
            part += 1

        if end >= len(cleaned):
            break
        start = max(start + 1, end - overlap)

    return chunks


async def get_embedding(text: str, api_key: str) -> List[float]:
    """
    הופך טקסט לוקטור (Embedding) באמצעות ה-API של Gemini.
    מבקש embedding מוקטן ל-768 מימדים (output_dimensionality) במקום 3072 המלאים -
    זה בתוך המגבלה שבה pgvector עדיין יודע לבנות אינדקס HNSW מהיר (המגבלה היא 2000).
    אם אין מפתח או שיש תקלה זמנית, מחזיר רשימה ריקה.
    """
    if not api_key:
        return []

    url = f"https://generativelanguage.googleapis.com/v1beta/models/gemini-embedding-001:embedContent?key={api_key}"
    payload = {
        "model": "models/gemini-embedding-001",
        "content": {"parts": [{"text": text}]},
        "outputDimensionality": 768,
    }
    try:
        await db.increment_api_usage("embedding")
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code == 200:
                data = resp.json()
                return data.get("embedding", {}).get("values", [])
    except Exception:
        pass
    return []


# "gemini-flash-latest" וכו' הם כינויים (aliases) שגוגל מצמידה תמיד למודל הנוכחי,
# כך שלא צריך לעדכן שם מודל קשיח בקוד כל פעם שגוגל משיקה גרסה חדשה.
# ה-"lite" מודל ראשון בכוונה: בבדיקה בפועל (ראה שיחה) התגלה שלמפתח חינמי יש מכסה יומית
# נמוכה מאוד (20 בקשות/יום) על המודל הכבד (gemini-flash-latest), בעוד שהגרסה ה-lite
# עדיין הייתה זמינה - אז היא סיכוי טוב יותר להצלחה בפועל במכסה חינמית.
DEFAULT_MODELS_TO_TRY = ["gemini-flash-lite-latest", "gemini-flash-latest", "gemini-pro-latest"]


async def _stream_from_gemini(
    full_prompt: str,
    api_key: str,
    models_to_try: List[str] = None,
    max_output_tokens: int = 600,
    call_type: str = "other",
) -> AsyncGenerator[str, None]:
    """
    ליבת ההזרמה מול Gemini: מנגנון תורים (Semaphore) + Retry אוטומטי לשגיאות 429/5xx,
    ומזרים את התשובה טוקן-טוקן בזמן אמת. משותף לכל קריאה שצריכה תשובת AI מוזרמת
    (גם תשובת הצ'אט לסטודנט וגם ניסוח AI למנהל).
    """
    models_to_try = models_to_try or DEFAULT_MODELS_TO_TRY
    payload = {
        "contents": [{"role": "user", "parts": [{"text": full_prompt}]}],
        "generationConfig": {
            "temperature": 0.1,  # טמפרטורה נמוכה מאוד למניעת המצאות והזיות!
            "maxOutputTokens": max_output_tokens,
        },
    }

    # כניסה לתור (Semaphore) כדי למנוע הצפה של ה-API החינמי
    async with gemini_queue_semaphore:
        max_retries = 3
        for attempt in range(max_retries):
            for model_name in models_to_try:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:streamGenerateContent?alt=sse&key={api_key}"
                try:
                    await db.increment_api_usage(call_type)
                    async with httpx.AsyncClient(timeout=30.0) as client:
                        async with client.stream("POST", url, json=payload) as response:
                            if response.status_code == 200:
                                async for line in response.aiter_lines():
                                    if line.startswith("data: "):
                                        data_str = line[6:].strip()
                                        if not data_str or data_str == "[DONE]":
                                            continue
                                        try:
                                            data_obj = json.loads(data_str)
                                            candidates = data_obj.get("candidates", [])
                                            if candidates:
                                                parts = candidates[0].get("content", {}).get("parts", [])
                                                for part in parts:
                                                    text_piece = part.get("text", "")
                                                    if text_piece:
                                                        yield text_piece
                                        except Exception:
                                            continue
                                return  # סיימנו להזרים בהצלחה!

                            elif response.status_code == 429 or response.status_code >= 500:
                                # עומס (429) או שגיאת שרת זמנית של גוגל (5xx) - נמתין מעט וננסה שוב
                                break
                            elif response.status_code in (400, 403):
                                err_body = await response.aread()
                                err_msg = err_body.decode("utf-8", errors="ignore")
                                if "API_KEY_INVALID" in err_msg or response.status_code == 403:
                                    yield "⚠️ שגיאה: מפתח ה-API של Gemini שהוזן בפאנל הניהול אינו תקין. אנא בדוק אותו במסך המנהל."
                                    return
                except Exception:
                    continue

            # אם קיבלנו עומס, נחכה לפני ניסיון חוזר
            if attempt < max_retries - 1:
                await asyncio.sleep(2 * (attempt + 1))

        yield "⚠️ המערכת בעומס רגעי מול שרתי ה-AI או שמפתח ה-API אינו זמין. אנא נסה שוב בעוד מספר שניות."


async def stream_gemini_answer(
    question: str,
    context_chunks: List[Dict[str, Any]],
    api_key: str,
    bot_identity: str = "",
    catalog: Optional[List[Dict[str, str]]] = None,
) -> AsyncGenerator[str, None]:
    """בונה את הפרומפט לתשובת צ'אט לסטודנט (עם ה-RAG שנשלף + זהות הבוט + מפת הנושאים) ומזרים אותו מ-Gemini."""
    # הרכבת ההקשר מתוך מסמכי המכללה שנשלפו
    if context_chunks:
        context_text = "\n\n---\n\n".join(
            [f"מקור: {c.get('title', 'נהלי מכללה')}\nתוכן: {c['text']}" for c in context_chunks]
        )
    else:
        context_text = "לא נמצאו מסמכים במאגר המכללה."

    full_prompt = f"""{build_system_prompt(bot_identity, catalog)}

=== מידע המכללה הרלוונטי ===
{context_text}
=============================

שאלת הסטודנט: {question}
תשובה (בהתבסס אך ורק על המידע המצורף למעלה):"""

    async for token in _stream_from_gemini(full_prompt, api_key, call_type="chat_answer"):
        yield token


CATEGORIZE_SYSTEM_PROMPT = """את/ה מסווג/ת מידע חדש למאגר ידע של צ'אטבוט מכללה.

קטגוריות קיימות במאגר: {categories}

מקטע חדש לסיווג:
כותרת: {title}
תוכן: {text}

המשימה שלך:
1. בחר/י קטגוריה מתאימה למקטע - קטגוריה קיימת מהרשימה אם היא מתאימה באמת, אחרת קטגוריה חדשה קצרה (מילה או שתיים, בעברית).
2. כתוב/כתבי תקציר שורה אחת שמתאר באופן כללי מה סוג המידע שנמצא בקטגוריה הזו (לא רק על המקטע הזה הספציפי).

החזר/י אך ורק בפורמט הזה, בלי שום טקסט נוסף לפני או אחרי:
קטגוריה: <קטגוריה>
תקציר: <תקציר שורה אחת>"""


async def categorize_and_summarize(
    title: str, text: str, api_key: str, existing_categories: List[str]
) -> Dict[str, str]:
    """
    מסווג מקטע ידע חדש לקטגוריה (קיימת או חדשה) וכותב תקציר קצר לקטגוריה הזו.
    זו ה"מפה" (קטלוג) שתמיד מוצגת למודל הצ'אט - מתעדכנת ע"י AI בכל הוספת מידע,
    ולא ידנית ע"י המנהל.
    """
    categories_text = ", ".join(existing_categories) if existing_categories else "(אין עדיין קטגוריות במאגר)"
    prompt = CATEGORIZE_SYSTEM_PROMPT.format(categories=categories_text, title=title, text=text)

    full_response = ""
    async for token in _stream_from_gemini(prompt, api_key, max_output_tokens=150, call_type="categorize"):
        full_response += token

    category_match = re.search(r"קטגוריה:\s*(.+)", full_response)
    summary_match = re.search(r"תקציר:\s*(.+)", full_response)

    category = category_match.group(1).strip() if category_match else "כללי"
    summary = summary_match.group(1).strip() if summary_match else f"מידע בנושא {title}"
    return {"category": category, "summary": summary}


KNOWLEDGE_DRAFT_SYSTEM_PROMPT = """אתה עוזר למנהל מערכת שמכין מידע רשמי למאגר הידע של צ'אטבוט המכללה.
המנהל ייתן לך תיאור חופשי, הערה, או טקסט גולמי בשפה חופשית. המשימה שלך:
1. לנסח ממנו "נוהל" קצר, ברור ומסודר שמתאים להיות מוצג לסטודנטים.
2. לשמור בדיוק על העובדות שנמסרו (טלפונים, חדרים, שעות, שמות, תאריכים) - בלי להמציא שום פרט שלא ניתן לך ובלי להשלים מספרים/שעות שלא צוינו.
3. אם משהו חסר או לא ברור - השאר את זה מנוסח בדיוק כפי שנמסר, אל תנחש ואל תמציא.

החזר את התשובה בפורמט הבא בדיוק, בלי שום הקדמה או הסבר נוסף:
כותרת: <כותרת קצרה וברורה לנוהל>
תוכן: <הנוסח המלא של הנוהל, בעברית ברורה ומסודרת>"""


async def stream_knowledge_draft(instruction: str, api_key: str) -> AsyncGenerator[str, None]:
    """
    מנסח עם Gemini טיוטת 'נוהל' (כותרת + תוכן) ממידע חופשי שהמנהל כותב.
    זו טיוטה בלבד להצגה ולאישור המנהל - השמירה בפועל למאגר הידע נעשית
    דרך אותו נתיב הוספת-טקסט הרגיל, רק אחרי שהמנהל בדק ואישר את הניסוח.
    """
    full_prompt = f"{KNOWLEDGE_DRAFT_SYSTEM_PROMPT}\n\nתיאור המנהל:\n{instruction.strip()}"
    async for token in _stream_from_gemini(full_prompt, api_key, max_output_tokens=500, call_type="ai_draft"):
        yield token
