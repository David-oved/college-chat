import asyncio
import json
import re
from datetime import datetime
from zoneinfo import ZoneInfo
from typing import List, Dict, Any, AsyncGenerator, Optional
import httpx
from http_client import shared_client
import gemini_stats

from db import db

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")

# =====================================================================
# מנגנון תורים (Queue) למניעת חסימות Rate-Limit ב-API החינמי של גוגל
# מאפשר עד 2 בקשות מקבילות ל-Gemini בו-זמנית; השאר ממתינים בתור מסודר
# =====================================================================
gemini_queue_semaphore = asyncio.Semaphore(8)

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
5. אל תשתמש באימוג'ים או בסמלי אימוג'י בתשובות - כתוב טקסט נקי בלבד (האפליקציה מציגה אייקונים משלה).
6. פתח ישר בתוכן התשובה. אל תפתח ב"בדקתי", "בדקתי לך", "בדקתי במאגר" או בתיאור של מה שעשית - הסטודנט רואה בעצמו שבדקת. אם אין מידע, אמור זאת במשפט ישיר וקצר.
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
    # עד 3 ניסיונות: שגיאות זמניות (429 = עומס/מכסה, 5xx = תקלה בצד גוגל) מנוסות שוב
    # עם השהיה גדלה, במקום להיכשל בשקט ולשמור מקטע בלי וקטור.
    for attempt in range(3):
        try:
            db.bump_api_usage("embedding")
            async with shared_client(15.0) as client:
                resp = await client.post(url, json=payload)
            gemini_stats.record("embedding", resp.status_code)
            if resp.status_code == 200:
                return resp.json().get("embedding", {}).get("values", [])
            if resp.status_code not in (429, 500, 502, 503, 504):
                break  # שגיאה קבועה (מפתח לא תקין וכד') - אין טעם לנסות שוב
        except Exception:
            pass
        if attempt < 2:
            await asyncio.sleep(2 * (attempt + 1))
    return []


# "gemini-flash-latest" וכו' הם כינויים (aliases) שגוגל מצמידה תמיד למודל הנוכחי,
# כך שלא צריך לעדכן שם מודל קשיח בקוד כל פעם שגוגל משיקה גרסה חדשה.
# ה-"lite" מודל ראשון בכוונה: בבדיקה בפועל (ראה שיחה) התגלה שלמפתח חינמי יש מכסה יומית
# נמוכה מאוד (20 בקשות/יום) על המודל הכבד (gemini-flash-latest), בעוד שהגרסה ה-lite
# עדיין הייתה זמינה - אז היא סיכוי טוב יותר להצלחה בפועל במכסה חינמית.
DEFAULT_MODELS_TO_TRY = ["gemini-flash-lite-latest", "gemini-flash-latest", "gemini-pro-latest"]


async def _stream_from_gemini(
    contents,
    api_key: str,
    models_to_try: List[str] = None,
    max_output_tokens: int = 600,
    call_type: str = "other",
) -> AsyncGenerator[str, None]:
    """
    ליבת ההזרמה מול Gemini: מנגנון תורים (Semaphore) + Retry אוטומטי לשגיאות 429/5xx,
    ומזרים את התשובה טוקן-טוקן בזמן אמת. משותף לכל קריאה שצריכה תשובת AI מוזרמת
    (גם תשובת הצ'אט לסטודנט וגם ניסוח AI למנהל).
    `contents` יכול להיות מחרוזת (פרומפט שטוח, הדרך הרגילה) או כבר רשימת turns מוכנה
    (למשל כש-Moodle function calling צריך לשלוח גם את תור ה-functionCall/functionResponse).
    """
    models_to_try = models_to_try or DEFAULT_MODELS_TO_TRY
    turns = contents if isinstance(contents, list) else [{"role": "user", "parts": [{"text": contents}]}]
    payload = {
        "contents": turns,
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
                    db.bump_api_usage(call_type)
                    async with shared_client(30.0) as client:
                        async with client.stream("POST", url, json=payload) as response:
                            gemini_stats.record(model_name, response.status_code)
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
                                    yield "שגיאה: מפתח ה-API של Gemini שהוזן בפאנל הניהול אינו תקין. אנא בדוק אותו במסך המנהל."
                                    return
                except Exception:
                    continue

            # אם קיבלנו עומס, נחכה לפני ניסיון חוזר
            if attempt < max_retries - 1:
                await asyncio.sleep(2 * (attempt + 1))

        yield "המערכת בעומס רגעי מול שרתי ה-AI או שמפתח ה-API אינו זמין. אנא נסה שוב בעוד מספר שניות."


async def _stream_gemini_events(contents, api_key: str, tools=None, max_output_tokens: int = 800, call_type: str = "chat_answer"):
    """
    קריאה מוזרמת אחת ל-Gemini, עם כלים אם ביקשו: מזרים ("text", קטע) ברגע שמגיע, ובסוף ("calls", [functionCall...]) אם המודל ביקש כלים,
    או ("failed", None) אם כל הניסיונות נכשלו בלי שיצא כלום. זה מחליף את הסבב "החלטה" הנפרד: שאלה רגילה מתחילה להיענות מיד,
    ורק כשצריך כלי יש סבב שני. כמו ההזרמה הרגילה: ניסיון חוזר על 429/5xx והחלפת מודל, אבל בלי כפילות אחרי שכבר יצא טקסט.
    """
    turns = contents if isinstance(contents, list) else [{"role": "user", "parts": [{"text": contents}]}]
    payload = {"contents": turns, "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_output_tokens}}
    if tools:
        payload["tools"] = tools
    async with gemini_queue_semaphore:
        for attempt in range(3):
            for model_name in DEFAULT_MODELS_TO_TRY:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:streamGenerateContent?alt=sse&key={api_key}"
                emitted = False
                calls = []
                try:
                    db.bump_api_usage(call_type)
                    async with shared_client(30.0) as client:
                        async with client.stream("POST", url, json=payload) as response:
                            gemini_stats.record(model_name, response.status_code)
                            if response.status_code == 200:
                                async for line in response.aiter_lines():
                                    if not line.startswith("data: "):
                                        continue
                                    data_str = line[6:].strip()
                                    if not data_str or data_str == "[DONE]":
                                        continue
                                    try:
                                        cand = json.loads(data_str).get("candidates", [])
                                    except Exception:
                                        continue
                                    if not cand:
                                        continue
                                    for part in cand[0].get("content", {}).get("parts", []):
                                        if not isinstance(part, dict):
                                            continue
                                        if part.get("functionCall"):
                                            calls.append(part)
                                        elif part.get("text"):
                                            emitted = True
                                            yield ("text", part["text"])
                                if calls:
                                    yield ("calls", calls)
                                return
                            if response.status_code in (400, 403):
                                err = (await response.aread()).decode("utf-8", errors="ignore")
                                if "API_KEY_INVALID" in err or response.status_code == 403:
                                    yield ("text", "שגיאה: מפתח ה-API של Gemini שהוזן בפאנל הניהול אינו תקין. אנא בדוק אותו במסך המנהל.")
                                    return
                                print(f"[chat] Gemini {response.status_code} with {model_name}: {err[:300]}")
                            # 429 / 5xx / שגיאת סכמה: מנסים את המודל הבא מיד
                except Exception as e:
                    print(f"[chat] Gemini stream error with {model_name}: {type(e).__name__}: {e}")
                    if emitted:
                        return                      # כבר יצא טקסט - לא מתחילים מחדש על מודל אחר (היה כפול)
                    continue
            if attempt < 2:
                await asyncio.sleep(1.2 * (attempt + 1))
    yield ("failed", None)


async def _generate_once(
    contents: List[dict],
    api_key: str,
    tools: Optional[List[dict]] = None,
    max_output_tokens: int = 300,
    call_type: str = "other",
) -> Optional[dict]:
    """
    קריאה בודדת (לא מוזרמת) ל-Gemini - משמשת לסיבוב ה"החלטה" של function calling
    (האם ובאיזה כלי Moodle להשתמש). בכוונה בלי ה-retry/backoff המלא של ההזרמה -
    זו קריאה קצרה וזולה, ואם היא נכשלת פשוט חוזרים לנתיב הרגיל בלי כלים.
    """
    payload = {
        "contents": contents,
        "generationConfig": {"temperature": 0.1, "maxOutputTokens": max_output_tokens},
    }
    if tools:
        payload["tools"] = tools

    async with gemini_queue_semaphore:
        for model_name in DEFAULT_MODELS_TO_TRY:
            url = f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent?key={api_key}"
            try:
                db.bump_api_usage(call_type)
                async with shared_client(30.0) as client:
                    resp = await client.post(url, json=payload)
                if resp.status_code == 200:
                    candidates = resp.json().get("candidates", [])
                    return candidates[0].get("content") if candidates else None
                if resp.status_code not in (429, 500, 502, 503, 504):
                    # לא מנסים שוב (שגיאה קבועה, כמו 400 בבעיית סכמה) - אבל מדפיסים כדי שלא
                    # ייפול בשקט לנתיב-בלי-כלים בלי שום עקבה למה (זה בדיוק מה שקרה בבדיקה בפועל)
                    print(f"[moodle-tools] Gemini decision call נכשל ({resp.status_code}): {resp.text[:500]}")
                    return None
            except Exception as e:
                print(f"[moodle-tools] Gemini decision call החריג עם {model_name}: {type(e).__name__}: {e}")
                continue
    return None


_HEBREW_WEEKDAYS = {0: "שני", 1: "שלישי", 2: "רביעי", 3: "חמישי", 4: "שישי", 5: "שבת", 6: "ראשון"}


def _current_israel_time_block() -> str:
    now = datetime.now(ISRAEL_TZ)
    day_name = _HEBREW_WEEKDAYS[now.weekday()]
    return f"יום {day_name}, {now.strftime('%d/%m/%Y %H:%M')} (שעון ישראל)"


def _build_chat_user_turn(
    question: str,
    context_chunks: List[Dict[str, Any]],
    bot_identity: str,
    catalog: Optional[List[Dict[str, str]]],
    conversation_summary: str,
    recent_messages: Optional[List[Dict[str, str]]],
    username: str = "",
    extra_system_instructions: str = "",
) -> str:
    """מרכיב את תור הפתיחה (system prompt + הקשר RAG + היסטוריה + השאלה) - משותף לנתיב הרגיל ולנתיב Moodle."""
    if context_chunks:
        context_text = "\n\n---\n\n".join(
            [f"מקור: {c.get('title', 'נהלי מכללה')}\nתוכן: {c['text']}" for c in context_chunks]
        )
    else:
        context_text = "לא נמצאו מסמכים במאגר המכללה."

    # מידע בסיסי אמיתי שתמיד זמין למודל - לא תלוי בשליפת RAG - כדי שיוכל לחשב בעצמו
    # דברים כמו "האם המטלה הזו באיחור" (צריך גם תאריך נוכחי וגם תאריך הגשה, לא רק אחד מהם).
    basic_info_block = f"""
=== מידע בסיסי בזמן אמת (עובדתי ומדויק - אפשר להסתמך עליו לחישובים) ===
שם המשתמש המחובר כרגע: {username or "לא ידוע"}
תאריך ושעה נוכחיים: {_current_israel_time_block()}
=========================================================================
"""

    history_block = ""
    if conversation_summary:
        history_block += f"\n=== תקציר השיחה עד כה ===\n{conversation_summary}\n"
    if recent_messages:
        turns = "\n".join(
            f"{'סטודנט' if m['role'] == 'user' else 'בוט'}: {m['content']}" for m in recent_messages
        )
        history_block += f"\n=== הודעות אחרונות בשיחה ===\n{turns}\n"

    return f"""{build_system_prompt(bot_identity, catalog)}{extra_system_instructions}
{basic_info_block}{history_block}
=== מידע המכללה הרלוונטי לשאלה הנוכחית ===
{context_text}
=============================

שאלת הסטודנט (הנוכחית): {question}
תשובה (בהתבסס אך ורק על המידע המצורף למעלה, ותוך המשכיות עם השיחה אם רלוונטי):"""


RICH_ADDENDUM = """

=== תגובות עשירות באפליקציה ===
אפשר לעצב: **טקסט מודגש** להדגשה (שמות קורסים, תאריכים, מילים חשובות), רשימות עם - או 1. וכותרת קצרה עם ## . קצר ומסודר עדיף על קיר טקסט.
סקר ותגובות מהירות: כל סימן בשורה משלו, בסוף התשובה, לכל היותר אחד בהודעה, בלי אימוג'י, ובלי התווים | או ] בתוך הטקסטים:
[[poll:multi|השאלה|אפשרות 1|אפשרות 2|אפשרות 3]]   - בחירה מרובה (2 עד 8 אפשרויות)
[[poll:single|השאלה|אפשרות 1|אפשרות 2]]   - בחירה אחת
[[quick|כן|לא]]   - כפתורי תשובה מהירה (2 עד 4)
הסטודנט מקיש, ותשובתו מגיעה אליך כהודעה חדשה שמתחילה ב"בחרתי: " (או "I chose: "). כתוב לפני הסקר משפט קצר כמו "סמן את מה שנכון" ואל תחזור על האפשרויות בטקסט.
מתי להשתמש: כשצריך לברר משהו שקל לענות עליו בלחיצה. למשל, אחרי שהצגת את רשימת הקורסים של הסטודנט אתה יכול ביוזמתך לשאול ב-poll:multi אילו מהם באמת שלו (כולל אפשרות "כולם" אם מתאים).
כשהתשובה מגיעה, עדכן את הזיכרון: כל קורס שלא סומן הוא קורס שהסטודנט לא לומד - שמור אותו עם remember_about_student ו-kind="left_course" (קריאה אחת לכל קורס, בלי לשמור את מה שסומן). אשר בקצרה מה עודכן.
אל תציף בסקרים: רק כשזה באמת חוסך הקלדה או מבהיר משהו, לא בכל תשובה ולא על מידע רגיש.
"""

MOODLE_SYSTEM_ADDENDUM = """

כלל קריטי: אם אתה קורא לכלי, אל תכתוב שום טקסט לפני הקריאה (בלי "רגע אבדוק", בלי הקדמה) - קרא לכלי מיד. את התשובה כותבים רק אחרי שהתוצאה חזרה.

=== מידע אישי מ-Moodle (קורסים/מטלות/ציונים/יומן) ===
לסטודנט הזה יש חיבור פעיל ל-Moodle. אם השאלה נוגעת למידע אישי שלו (הקורסים שלו, מטלות,
ציונים, או אירועים ביומן) - ואך ורק במקרה הזה - תוכל להשתמש בכלים (functions) שסופקו לך
כדי לשלוף את המידע האמיתי והעדכני. אל תמציא קורסים/ציונים/מטלות בעצמך; אם הכלי מחזיר
שאין מידע או שיש שגיאה - העבר את זה לסטודנט בנימוס בדיוק כפי שהתקבל. לשאלות נהלים רגילות
(לא אישיות) המשך כרגיל על בסיס מידע המכללה בלבד, בלי להשתמש בכלים.

=== שליחת קבצים ומסמכים - בחוכמה ובאחריות ===
כשהסטודנט מבקש קובץ, מסמך, דף תרגיל, פתרון, הרצאה/מצגת, מבחן קודם, סילבוס או סיכום מאחד הקורסים שלו - השתמש בכלי get_course_files.
1. תמיד ציין מאיזה קורס (course_name) ומה בדיוק מחפשים: שים ב-search את מה שהסטודנט ביקש במילים שלו (נושא, מספר תרגיל, שבוע; בלי מילות נימוס),
   וב-kind את הסוג אם ברור. אם לא ברור באיזה קורס מדובר והסטודנט בכמה קורסים - שאל אותו קודם, אל תנחש.
2. הכלי עצמו מדרג לפי התאמה ומחזיר רק את הטובים, עם הסבר על כל קובץ (סוג, מאיזו פעילות ונושא, מועד, תיאור). קרא את ההסברים ובחר בעצמך מה באמת עונה
   על הבקשה - בדרך כלל קובץ אחד עד שלושה. הסבר בשורה קצרה מה כל קובץ שאתה שולח. "תרגיל 3" אינו "תרגיל 13"; פתרון נשלח רק אם ביקשו פתרון.
   אם כמה קבצים מתאימים באותה מידה ואי אפשר לדעת למה הסטודנט התכוון - שאל אותו איזה מהם, במקום לשלוח את כולם.
3. כדי לשלוח קובץ - העתק את הסמן [[file:קוד|שם|גודל]] *בדיוק כפי שהוא* מהתוצאה, בשורה משלו; האפליקציה הופכת אותו לכרטיס הורדה. לעולם אל תמציא סמנים
   או קודים, אל תכתוב כתובות URL של קבצים, ואל תכתוב טוקנים.
4. בקשות גורפות או לא הגיוניות - לא נענות כמו שהן: "תביא לי את כל הקבצים", "את כל מה שיש לי במודל", "את כל הקורס", קבצים מכל הקורסים בבת אחת.
   הסבר בנימוס שלשלוח הכל זה לא מועיל ולא אפשרי, ושאל מה הוא צריך בפועל (איזה קורס, איזה סוג - תרגיל/הרצאה/מבחן - ואיזה מספר או נושא). אפשר להציע לו
   את הסקירה שהכלי מחזיר (כמה קבצים מכל סוג) כדי שיבחר. אל תפעיל את הכלי שוב ושוב כדי "לאסוף הכל".
   בקשות שהן בכלל לא קשורות למה שקיים (קבצים של סטודנטים אחרים, מבחנים סודיים שעוד לא פורסמו, עקיפת הרשאות) - סרב בנימוס והצע אלטרנטיבה חוקית.
5. אם הכלי מחזיר שאין התאמה טובה - אל תשלח קבצים אקראיים "לפחות משהו". אמור שלא מצאת, ציין מה כן יש (לפי הסקירה) ושאל מה הוא מחפש.
6. אתה לא קורא את תוכן הקבצים - רק שולח אותם ומתאר אותם לפי שם/נושא/תיאור. אם הוא מבקש הסבר על התוכן עצמו, אמור לו זאת בכנות."""


# ---------------------------------------------------------------------
# סטטוס חי: אירועים קטנים בתוך זרם התשובה ("מחפש קבצים בקורס X...") - רק ללקוח שביקש (status=true), ולא נשמרים ב-DB
# ---------------------------------------------------------------------
STATUS_OPEN, STATUS_CLOSE = "\x01", "\x02"


def status_event(**kw) -> str:
    return STATUS_OPEN + json.dumps(kw, ensure_ascii=False, separators=(",", ":")) + STATUS_CLOSE


MAX_TOOL_CALLS_PER_TURN = 4
MAX_FILE_SEARCHES_PER_TURN = 2
MAX_TOOL_ROUNDS = 3


async def stream_chat_with_tools(
    question: str,
    context_chunks: List[Dict[str, Any]],
    api_key: str,
    bot_identity: str = "",
    catalog: Optional[List[Dict[str, str]]] = None,
    conversation_summary: str = "",
    recent_messages: Optional[List[Dict[str, str]]] = None,
    username: str = "",
    moodle_token: Optional[str] = None,
    moodle_user_id: Any = None,
    enabled_functions: Optional[set] = None,
    memories: Optional[List[Dict[str, Any]]] = None,
    user_id: Optional[str] = None,
    emit_status: bool = False,
) -> AsyncGenerator[str, None]:
    """
    מנוע הצ'אט המאוחד: כלי Moodle (אם יש חיבור) + כלי זיכרון (אם יש משתמש) בסיבוב החלטה אחד, ואז תשובה מוזרמת.
    זיכרון הסטודנט מוזרם לפרומפט בכל הודעה, וקורסים שעזב מוסתרים מתוצאות Moodle. עם emit_status גם מזרים אירועי סטטוס.
    כל כשל בנתיב הכלים נופל חזרה לתשובה רגילה בלי כלים - הצ'אט לא נשבר.
    """
    from moodle_tools import get_enabled_tools, execute_moodle_function, set_hidden_courses
    from memory import (MEMORY_ADDENDUM, MEMORY_TOOLS, MEMORY_TOOL_NAMES, build_memory_block, left_courses, run_memory_tool)

    memories = memories or []
    set_hidden_courses(left_courses(memories))

    moodle_tools = get_enabled_tools(enabled_functions) if (moodle_token and moodle_user_id) else []
    moodle_names = {d["name"] for grp in moodle_tools for d in grp.get("functionDeclarations", [])}
    declarations = [d for grp in moodle_tools for d in grp.get("functionDeclarations", [])]
    if user_id:
        declarations = declarations + MEMORY_TOOLS[0]["functionDeclarations"]
    tools = [{"functionDeclarations": declarations}] if declarations else []

    extra = RICH_ADDENDUM
    if moodle_names:
        extra += MOODLE_SYSTEM_ADDENDUM
    if user_id:
        extra += MEMORY_ADDENDUM + build_memory_block(memories)

    user_turn_text = _build_chat_user_turn(
        question, context_chunks, bot_identity, catalog, conversation_summary, recent_messages,
        username=username, extra_system_instructions=extra,
    )

    async def final_answer(contents):
        if emit_status:
            yield status_event(s="write")
        async for token in _stream_from_gemini(contents, api_key, call_type="chat_answer"):
            yield token

    contents = [{"role": "user", "parts": [{"text": user_turn_text}]}]
    if not tools:
        async for token in final_answer(user_turn_text):
            yield token
        return

    # קריאה מוזרמת אחת עם הכלים: שאלה רגילה מתחילה להיענות מיד; רק אם המודל מבקש כלי יש סבב שני אחרי ביצוע הכלים
    calls: List[Dict[str, Any]] = []
    emitted_text = False
    failed = False
    if emit_status:
        yield status_event(s="think")
    async for kind, value in _stream_gemini_events(contents, api_key, tools=tools, max_output_tokens=800):
        if kind == "text":
            if emit_status and not emitted_text:
                yield status_event(s="write")
            emitted_text = True
            yield value
        elif kind == "calls":
            calls = value[:MAX_TOOL_CALLS_PER_TURN]
        else:
            failed = True
    if failed and not emitted_text:
        # נתיב הכלים נכשל (למשל סכמה) - עונים בלי כלים במקום להיכשל
        async for token in final_answer(user_turn_text):
            yield token
        return
    if not calls:
        return

    # עד MAX_TOOL_ROUNDS סבבי כלים: למשל "מה הציונים שלי" דורש get_my_courses ואז כלי הציונים עם מזהה הקורס.
    # בסבב האחרון לא מציעים כלים, כדי שתמיד תצא תשובה.
    file_searches = 0
    round_no = 0
    while calls:
        round_no += 1
        kept = []
        for p in calls:
            if p["functionCall"].get("name") == "get_course_files":
                file_searches += 1
                if file_searches > MAX_FILE_SEARCHES_PER_TURN:
                    continue
            kept.append(p)
        calls = kept or calls[:1]

        if emit_status:
            for p in calls:
                fc = p["functionCall"]
                a = fc.get("args") or {}
                yield status_event(
                    s="tool", name=fc.get("name", ""),
                    arg=str(a.get("course_name") or a.get("quiz_name") or a.get("fact") or "")[:80],
                    detail=str(a.get("search") or a.get("assignment_name") or a.get("kind") or "")[:80],
                )

        async def run(p):
            fc = p["functionCall"]
            name, args = fc.get("name", ""), fc.get("args") or {}
            if name in MEMORY_TOOL_NAMES and user_id:
                return await run_memory_tool(name, args, user_id, memories)
            if name in moodle_names:
                return await execute_moodle_function(name, args, moodle_token, moodle_user_id), None
            return f"הכלי {name} לא זמין.", None

        outcomes = await asyncio.gather(*[run(p) for p in calls])
        if emit_status:
            for _, event in outcomes:
                if event:
                    yield status_event(**event)

        contents.append({"role": "model", "parts": calls})
        contents.append({
            "role": "user",
            "parts": [{"functionResponse": {"name": p["functionCall"].get("name", ""), "response": {"result": text}}} for p, (text, _) in zip(calls, outcomes)],
        })

        if round_no >= MAX_TOOL_ROUNDS:
            break
        calls = []
        got_text = False
        failed = False
        async for kind, value in _stream_gemini_events(contents, api_key, tools=tools, max_output_tokens=800):
            if kind == "text":
                if emit_status and not got_text:
                    yield status_event(s="write")
                got_text = True
                yield value
            elif kind == "calls":
                calls = value[:MAX_TOOL_CALLS_PER_TURN]
            else:
                failed = True
        if got_text:
            return
        if not calls and not failed:
            # המודל החזיר ריק: מבקשים ממנו תשובה סופית בלי כלים
            break
        if failed:
            break

    answered = False
    async for token in final_answer(contents):
        answered = True
        yield token
    if not answered:
        yield "לא הצלחתי להשלים את הבקשה כרגע. נסה שוב בעוד רגע."


async def stream_gemini_answer_with_moodle_tools(
    question: str,
    context_chunks: List[Dict[str, Any]],
    api_key: str,
    moodle_token: str,
    moodle_user_id: Any,
    bot_identity: str = "",
    catalog: Optional[List[Dict[str, str]]] = None,
    conversation_summary: str = "",
    recent_messages: Optional[List[Dict[str, str]]] = None,
    username: str = "",
    enabled_functions: Optional[set] = None,
) -> AsyncGenerator[str, None]:
    """
    כמו stream_gemini_answer, אבל עם אפשרות ל-Gemini לבצע function calling מול Moodle
    (קורסים/מטלות/ציונים/יומן) כשהשאלה אישית. סיבוב ראשון (לא מוזרם) נותן ל-Gemini
    הזדמנות להחליט אם להשתמש בכלי אחד או יותר (שאלה שנוגעת בכמה נושאים בבת אחת - למשל
    גם ציונים וגם מועד הגשה - מטופלת בכל הקריאות המקבילות שהמודל מבקש, לא רק הראשונה);
    אם כן - מבצעים את כל הקריאות האמיתיות ל-Moodle ומזינים את כל התוצאות בחזרה לסיבוב
    שני (מוזרם) שמנסח את התשובה הסופית לסטודנט.
    אם משהו בנתיב הזה נכשל מכל סיבה - נופלים חזרה לנתיב הרגיל (בלי כלים), בלי לשבור את הצ'אט.
    `enabled_functions` - סט שמות הפונקציות שהמנהל השאיר דלוקות (None = הכל דלוק).
    """
    from moodle_tools import get_enabled_tools, execute_moodle_function

    tools = get_enabled_tools(enabled_functions)
    if not tools:
        # המנהל כיבה את כל הפונקציות - אין טעם לפנות ל-Moodle בכלל, נתיב רגיל
        async for token in stream_gemini_answer(
            question, context_chunks, api_key, bot_identity, catalog, conversation_summary,
            recent_messages, username,
        ):
            yield token
        return

    user_turn_text = _build_chat_user_turn(
        question, context_chunks, bot_identity, catalog, conversation_summary, recent_messages,
        username=username, extra_system_instructions=MOODLE_SYSTEM_ADDENDUM,
    )
    contents = [{"role": "user", "parts": [{"text": user_turn_text}]}]

    try:
        decision_content = await _generate_once(
            contents, api_key, tools=tools, max_output_tokens=400, call_type="chat_answer"
        )
    except Exception:
        decision_content = None

    parts = (decision_content or {}).get("parts", [])
    function_call_parts = [p for p in parts if isinstance(p, dict) and "functionCall" in p]

    if not function_call_parts:
        # גמיני לא ביקש כלי (שאלה לא אישית, או שלא זיהה צורך) - נתיב רגיל, בלי לבזבז את הסבב שכבר קרה
        async for token in stream_gemini_answer(
            question, context_chunks, api_key, bot_identity, catalog, conversation_summary,
            recent_messages, username,
        ):
            yield token
        return

    # מבצעים את כל הקריאות שהמודל ביקש (יכול להיות יותר מאחת לשאלה מרובת-נושאים), במקביל
    results = await asyncio.gather(*[
        execute_moodle_function(p["functionCall"].get("name", ""), p["functionCall"].get("args") or {}, moodle_token, moodle_user_id)
        for p in function_call_parts
    ])

    contents.append({"role": "model", "parts": function_call_parts})
    contents.append({
        "role": "user",
        "parts": [
            {"functionResponse": {"name": p["functionCall"].get("name", ""), "response": {"result": result}}}
            for p, result in zip(function_call_parts, results)
        ],
    })

    async for token in _stream_from_gemini(contents, api_key, call_type="chat_answer"):
        yield token


async def stream_gemini_answer(
    question: str,
    context_chunks: List[Dict[str, Any]],
    api_key: str,
    bot_identity: str = "",
    catalog: Optional[List[Dict[str, str]]] = None,
    conversation_summary: str = "",
    recent_messages: Optional[List[Dict[str, str]]] = None,
    username: str = "",
) -> AsyncGenerator[str, None]:
    """בונה את הפרומפט לתשובת צ'אט לסטודנט (עם ה-RAG שנשלף + זהות הבוט + מפת הנושאים + הקשר השיחה) ומזרים אותו מ-Gemini."""
    full_prompt = _build_chat_user_turn(
        question, context_chunks, bot_identity, catalog, conversation_summary, recent_messages, username=username
    )
    async for token in _stream_from_gemini(full_prompt, api_key, call_type="chat_answer"):
        yield token


async def summarize_conversation(old_summary: str, messages: List[Dict[str, str]], api_key: str) -> str:
    """
    מכווץ חלק ישן של השיחה לתוך סיכום קצר אחד (2-4 משפטים), כדי שלא יישלח למודל
    טקסט ארוך מדי ככל שהשיחה מתארכת - משלב את הסיכום הקודם עם ההודעות החדשות.
    """
    if not messages:
        return old_summary

    convo_text = "\n".join(
        f"{'סטודנט' if m['role'] == 'user' else 'בוט'}: {m['content']}" for m in messages
    )
    prompt = f"""סכם בקצרה (2-4 משפטים, בעברית) את קטע השיחה הבא בין סטודנט לעוזר AI של מכללה,
כדי לשמר את ההקשר החשוב להמשך השיחה (מה הסטודנט שאל/רצה, מה כבר נענה).
אם יש סיכום קודם - שלב אותו עם המידע החדש לכדי סיכום מעודכן אחד, לא שני סיכומים נפרדים.

סיכום קודם: {old_summary or "(אין סיכום קודם)"}

קטע השיחה החדש:
{convo_text}

החזר אך ורק את הסיכום המעודכן, בלי הקדמות או כותרות."""

    full_response = ""
    async for token in _stream_from_gemini(prompt, api_key, max_output_tokens=200, call_type="summarize"):
        full_response += token
    return full_response.strip()


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
