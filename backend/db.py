import os
import sys
import asyncio
from datetime import date
from typing import Dict, List, Any, Optional

if sys.platform == "win32":
    # ה-ProactorEventLoop המוגדר כברירת מחדל ב-Windows לא נתמך ע"י psycopg במצב אסינכרוני
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

from dotenv import load_dotenv
from psycopg_pool import AsyncConnectionPool
from psycopg.rows import dict_row

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

DATABASE_URL = os.environ.get("DATABASE_URL", "")

_pool: Optional[AsyncConnectionPool] = None


def _get_pool() -> AsyncConnectionPool:
    global _pool
    if _pool is None:
        if not DATABASE_URL:
            raise RuntimeError(
                "DATABASE_URL לא מוגדר. צור קובץ backend/.env עם שורה: DATABASE_URL=postgresql://..."
            )
        _pool = AsyncConnectionPool(DATABASE_URL, min_size=1, max_size=5, open=False)
    return _pool


def _vector_literal(embedding: Optional[List[float]]) -> Optional[str]:
    """
    ממיר list[float] לפורמט הטקסטואלי של pgvector (למשל '[0.12,0.34,...]'),
    כדי לשלוח אותו כמחרוזת רגילה ולבצע cast מפורש ל-::vector ב-SQL - פשוט
    ואמין יותר מהסתמכות על מתאם טיפוסים אוטומטי.
    """
    if not embedding:
        return None
    return "[" + ",".join(repr(float(x)) for x in embedding) + "]"


class Database:
    """
    שכבת גישה אסינכרונית ל-Postgres (Supabase) - מחליפה את אחסון ה-JSON המקומי.
    החיפוש הוקטורי (RAG) רץ בתוך ה-DB עצמו באמצעות pgvector, לא בלולאת Python.
    """

    async def init(self):
        pool = _get_pool()
        if pool.closed:
            await pool.open()

    async def close(self):
        if _pool is not None and not _pool.closed:
            await _pool.close()

    # ----------------------------------------------------------------
    # הגדרות מערכת (system_config - שורה יחידה)
    # ----------------------------------------------------------------
    async def get_config_status(self) -> dict:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "select gemini_api_key, daily_quota_limit, bot_identity_info from system_config where id = 1;"
                )
                row = await cur.fetchone() or {}
                await cur.execute("select count(*) as cnt from knowledge_chunks;")
                count_row = await cur.fetchone()

        key = row.get("gemini_api_key") or ""
        masked_key = f"...{key[-4:]}" if len(key) > 4 else ("מוגדר" if key else "לא הוגדר")
        return {
            "has_api_key": bool(key),
            "masked_api_key": masked_key,
            "daily_quota_limit": row.get("daily_quota_limit", 10),
            "total_chunks": count_row["cnt"] if count_row else 0,
            "bot_identity_info": row.get("bot_identity_info") or "",
        }

    async def get_api_key(self) -> str:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select gemini_api_key from system_config where id = 1;")
                row = await cur.fetchone()
        return (row[0] if row else "") or ""

    async def get_bot_identity(self) -> str:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select bot_identity_info from system_config where id = 1;")
                row = await cur.fetchone()
        return (row[0] if row else "") or ""

    async def update_config(
        self,
        api_key: Optional[str] = None,
        daily_limit: Optional[int] = None,
        bot_identity_info: Optional[str] = None,
    ):
        updates, params = [], []
        if api_key is not None and api_key.strip() != "":
            updates.append("gemini_api_key = %s")
            params.append(api_key.strip())
        if daily_limit is not None and daily_limit >= 0:
            updates.append("daily_quota_limit = %s")
            params.append(daily_limit)
        if bot_identity_info is not None:
            updates.append("bot_identity_info = %s")
            params.append(bot_identity_info.strip())
        if not updates:
            return

        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"update system_config set {', '.join(updates)} where id = 1;", params)

    async def get_admin_password_hash(self) -> str:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select admin_password_hash from system_config where id = 1;")
                row = await cur.fetchone()
        return (row[0] if row else "") or ""

    async def set_admin_password_hash(self, password_hash: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("update system_config set admin_password_hash = %s where id = 1;", (password_hash,))

    # ----------------------------------------------------------------
    # מכסות סטודנטים (user_quotas)
    # ----------------------------------------------------------------
    async def get_user_quota_status(self, user_id: str) -> dict:
        today = date.today()
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("select daily_quota_limit from system_config where id = 1;")
                cfg = await cur.fetchone()
                daily_limit = (cfg or {}).get("daily_quota_limit", 10)

                await cur.execute(
                    "select used_today, last_reset_date from user_quotas where user_id = %s;", (user_id,)
                )
                row = await cur.fetchone()

                if not row or row["last_reset_date"] != today:
                    await cur.execute(
                        """
                        insert into user_quotas (user_id, used_today, last_reset_date)
                        values (%s, 0, %s)
                        on conflict (user_id) do update set used_today = 0, last_reset_date = excluded.last_reset_date;
                        """,
                        (user_id, today),
                    )
                    used = 0
                else:
                    used = row["used_today"]

        remaining = max(0, daily_limit - used)
        return {
            "user_id": user_id,
            "used_today": used,
            "daily_limit": daily_limit,
            "remaining": remaining,
            "can_ask": remaining > 0,
        }

    async def increment_user_usage(self, user_id: str) -> dict:
        await self.get_user_quota_status(user_id)  # מוודא שיש רשומה עדכנית להיום
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("update user_quotas set used_today = used_today + 1 where user_id = %s;", (user_id,))
        return await self.get_user_quota_status(user_id)

    async def reset_user_quota(self, user_id: str) -> dict:
        today = date.today()
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    insert into user_quotas (user_id, used_today, last_reset_date)
                    values (%s, 0, %s)
                    on conflict (user_id) do update set used_today = 0, last_reset_date = excluded.last_reset_date;
                    """,
                    (user_id, today),
                )
        return await self.get_user_quota_status(user_id)

    # ----------------------------------------------------------------
    # מאגר הידע (knowledge_chunks) + חיפוש וקטורי עם pgvector
    # ----------------------------------------------------------------
    async def add_knowledge_chunk(
        self, title: str, text: str, category: str, embedding: Optional[List[float]]
    ) -> str:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    insert into knowledge_chunks (title, text, category, embedding)
                    values (%s, %s, %s, %s::vector)
                    returning id;
                    """,
                    (title, text, category, _vector_literal(embedding)),
                )
                row = await cur.fetchone()
        return str(row[0])

    async def get_all_chunks(self) -> List[Dict[str, Any]]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    select id, title, text, category, (embedding is not null) as has_embedding
                    from knowledge_chunks
                    order by created_at desc;
                    """
                )
                rows = await cur.fetchall()
        return [
            {
                "id": str(r["id"]),
                "title": r["title"],
                "text": r["text"],
                "category": r["category"],
                "has_embedding": r["has_embedding"],
            }
            for r in rows
        ]

    async def chunk_text_exists(self, text: str) -> bool:
        """האם מקטע עם בדיוק אותו טקסט כבר קיים במאגר (למניעת כפילויות בהעלאה חוזרת)."""
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select 1 from knowledge_chunks where text = %s limit 1;", (text,))
                return await cur.fetchone() is not None

    async def get_chunks_without_embedding(self) -> List[Dict[str, Any]]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "select id, title, text from knowledge_chunks where embedding is null order by created_at;"
                )
                rows = await cur.fetchall()
        return [{"id": str(r["id"]), "title": r["title"], "text": r["text"]} for r in rows]

    async def set_chunk_embedding(self, chunk_id: str, embedding: List[float]):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update knowledge_chunks set embedding = %s::vector where id = %s;",
                    (_vector_literal(embedding), chunk_id),
                )

    async def delete_chunk(self, chunk_id: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("delete from knowledge_chunks where id = %s;", (chunk_id,))

    async def search_similar_chunks(self, query_embedding: Optional[List[float]], top_k: int = 3) -> List[Dict[str, Any]]:
        """חיפוש הדמיון עצמו רץ בתוך Postgres (אופרטור <=> של pgvector), לא בלולאת Python."""
        vec = _vector_literal(query_embedding)
        if not vec:
            return []
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    select id, title, text, category, 1 - (embedding <=> %s::vector) as similarity
                    from knowledge_chunks
                    where embedding is not null
                    order by embedding <=> %s::vector
                    limit %s;
                    """,
                    (vec, vec, top_k),
                )
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------
    # קטלוג/מפת נושאים (knowledge_catalog) - מתעדכן ע"י AI בכל הוספה
    # ----------------------------------------------------------------
    async def get_catalog(self) -> List[Dict[str, Any]]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("select category, summary, chunk_count from knowledge_catalog order by category;")
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_categories(self) -> List[str]:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select category from knowledge_catalog order by category;")
                rows = await cur.fetchall()
        return [r[0] for r in rows]

    # ----------------------------------------------------------------
    # מדדי שימוש (api_usage_daily) - כמה פעמים קראנו בפועל ל-Gemini היום
    # ----------------------------------------------------------------
    async def increment_api_usage(self, call_type: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    insert into api_usage_daily (usage_date, call_type, count)
                    values (current_date, %s, 1)
                    on conflict (usage_date, call_type) do update
                      set count = api_usage_daily.count + 1;
                    """,
                    (call_type,),
                )

    async def get_today_usage(self) -> Dict[str, Any]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "select call_type, count from api_usage_daily where usage_date = current_date;"
                )
                rows = await cur.fetchall()
        by_type = {r["call_type"]: r["count"] for r in rows}
        by_type["total"] = sum(by_type.values())
        return by_type

    async def get_student_usage_summary(self) -> Dict[str, Any]:
        """
        שימוש אמיתי של סטודנטים היום - נספר ישירות מטבלת ה-messages (לא ממנגנון מכסה ישן
        שכבר לא פעיל מאז שהשאלות היומיות הפכו ללא מוגבלות). כל שורה היא משתמש אמיתי מ-app_users
        עם כמה שאלות הוא שאל בפועל היום ומתי שלח הודעה אחרונה.
        """
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    select u.id as user_id, u.username,
                           count(m.id) filter (where m.role = 'user') as questions_today,
                           max(m.created_at) as last_message_at
                    from app_users u
                    left join conversations c on c.user_id = u.id
                    left join messages m
                      on m.conversation_id = c.id and m.created_at::date = current_date
                    group by u.id, u.username
                    order by questions_today desc, u.username;
                    """
                )
                rows = await cur.fetchall()

        students = [
            {
                "user_id": str(r["user_id"]),
                "username": r["username"],
                "questions_today": r["questions_today"] or 0,
                "last_message_at": r["last_message_at"],
            }
            for r in rows
        ]
        return {
            "students": students,
            "active_students_today": sum(1 for s in students if s["questions_today"] > 0),
            "total_questions_today": sum(s["questions_today"] for s in students),
        }

    async def upsert_catalog_entry(self, category: str, summary: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    insert into knowledge_catalog (category, summary, chunk_count, updated_at)
                    values (%s, %s, 1, now())
                    on conflict (category) do update
                      set summary = excluded.summary,
                          chunk_count = knowledge_catalog.chunk_count + 1,
                          updated_at = now();
                    """,
                    (category, summary),
                )

    # ----------------------------------------------------------------
    # משתמשי אפליקציה (app_users) - התחברות אמיתית בשם משתמש + סיסמה
    # ----------------------------------------------------------------
    async def create_user(self, username: str, password_hash: str) -> Optional[str]:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                try:
                    await cur.execute(
                        "insert into app_users (username, password_hash) values (%s, %s) returning id;",
                        (username, password_hash),
                    )
                    row = await cur.fetchone()
                    return str(row[0])
                except Exception:
                    return None

    async def get_user_by_username(self, username: str) -> Optional[Dict[str, Any]]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "select id, username, password_hash from app_users where username = %s;", (username,)
                )
                row = await cur.fetchone()
        if not row:
            return None
        row = dict(row)
        row["id"] = str(row["id"])  # psycopg מחזיר UUID כאובייקט - ממירים למחרוזת לעקביות בהשוואות
        return row

    async def get_user_by_id(self, user_id: str) -> Optional[Dict[str, Any]]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("select id, username from app_users where id = %s;", (user_id,))
                row = await cur.fetchone()
        if not row:
            return None
        row = dict(row)
        row["id"] = str(row["id"])
        return row

    async def get_all_users_with_counts(self) -> List[Dict[str, Any]]:
        """שורה אחת לכל משתמש - לדשבורד הניהול: כמה שיחות וכמה הודעות יש לו בסה"כ (JOIN יחיד, לא לולאת N+1)."""
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    select u.id, u.username, u.created_at,
                           count(distinct c.id) as conversation_count,
                           count(m.id) as message_count,
                           max(m.created_at) as last_message_at
                    from app_users u
                    left join conversations c on c.user_id = u.id
                    left join messages m on m.conversation_id = c.id
                    group by u.id, u.username, u.created_at
                    order by u.created_at desc;
                    """
                )
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_user_activity(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        כל השיחות וההודעות בפועל של משתמש ספציפי - בשביל שהמנהל יוכל לראות בדיוק
        מה כל משתמש שאל ומתי, ולא רק מספרים מצטברים.
        """
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "select id, username, created_at from app_users where id = %s;", (user_id,)
                )
                user = await cur.fetchone()
                if not user:
                    return None

                await cur.execute(
                    """
                    select c.id as conversation_id, c.title, c.created_at as conversation_created_at,
                           m.id as message_id, m.role, m.content, m.created_at as message_created_at
                    from conversations c
                    join messages m on m.conversation_id = c.id
                    where c.user_id = %s
                    order by c.created_at desc, m.created_at asc;
                    """,
                    (user_id,),
                )
                rows = await cur.fetchall()

        conversations: Dict[str, Dict[str, Any]] = {}
        order: List[str] = []
        for r in rows:
            cid = str(r["conversation_id"])
            if cid not in conversations:
                conversations[cid] = {
                    "conversation_id": cid,
                    "title": r["title"],
                    "created_at": r["conversation_created_at"],
                    "messages": [],
                }
                order.append(cid)
            conversations[cid]["messages"].append({
                "role": r["role"],
                "content": r["content"],
                "created_at": r["message_created_at"],
            })

        return {
            "user_id": str(user["id"]),
            "username": user["username"],
            "created_at": user["created_at"],
            "conversations": [conversations[cid] for cid in order],
        }

    # ----------------------------------------------------------------
    # שיחות והודעות (conversations/messages) - היסטוריה אמיתית + הקשר
    # ----------------------------------------------------------------
    async def get_or_create_today_conversation(self, user_id: str) -> str:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    select id from conversations
                    where user_id = %s and created_at::date = current_date
                    order by created_at desc limit 1;
                    """,
                    (user_id,),
                )
                row = await cur.fetchone()
                if row:
                    return str(row["id"])

                await cur.execute(
                    "insert into conversations (user_id) values (%s) returning id;", (user_id,)
                )
                new_row = await cur.fetchone()
        return str(new_row["id"])

    async def create_conversation(self, user_id: str, title: str = "שיחה חדשה") -> Dict[str, Any]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "insert into conversations (user_id, title) values (%s, %s) returning id, title, created_at;",
                    (user_id, title),
                )
                row = await cur.fetchone()
        return dict(row)

    async def get_user_conversations(self, user_id: str) -> List[Dict[str, Any]]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    select id, title, created_at, updated_at
                    from conversations
                    where user_id = %s
                    order by coalesce(updated_at, created_at) desc;
                    """,
                    (user_id,),
                )
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_conversation_owner(self, conversation_id: str) -> Optional[str]:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select user_id from conversations where id = %s;", (conversation_id,))
                row = await cur.fetchone()
        return str(row[0]) if row else None

    async def get_all_messages(self, conversation_id: str) -> List[Dict[str, Any]]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "select role, content, created_at from messages where conversation_id = %s order by created_at asc;",
                    (conversation_id,),
                )
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def touch_conversation(self, conversation_id: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("update conversations set updated_at = now() where id = %s;", (conversation_id,))

    async def add_message(self, conversation_id: str, role: str, content: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "insert into messages (conversation_id, role, content) values (%s, %s, %s);",
                    (conversation_id, role, content),
                )
                await cur.execute(
                    "update conversations set updated_at = now() where id = %s;", (conversation_id,)
                )

    async def get_recent_messages(self, conversation_id: str, limit: int = 12) -> List[Dict[str, str]]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    select role, content from messages
                    where conversation_id = %s
                    order by created_at desc
                    limit %s;
                    """,
                    (conversation_id, limit),
                )
                rows = await cur.fetchall()
        return [dict(r) for r in reversed(rows)]

    async def count_messages(self, conversation_id: str) -> int:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select count(*) from messages where conversation_id = %s;", (conversation_id,))
                row = await cur.fetchone()
        return row[0] if row else 0

    async def get_conversation_summary(self, conversation_id: str) -> str:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select summary from conversations where id = %s;", (conversation_id,))
                row = await cur.fetchone()
        return (row[0] if row else "") or ""

    async def update_conversation_summary(self, conversation_id: str, summary: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update conversations set summary = %s where id = %s;", (summary, conversation_id)
                )

    async def get_old_messages_to_summarize(self, conversation_id: str, keep_recent: int = 12) -> List[Dict[str, str]]:
        """מחזיר הודעות ישנות (מעבר ל-keep_recent האחרונות) שעדיין לא סוכמו - לדחיסה לתוך summary."""
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    select role, content from messages
                    where conversation_id = %s
                    order by created_at asc
                    offset greatest((select count(*) from messages where conversation_id = %s) - %s - 6, 0)
                    limit 6;
                    """,
                    (conversation_id, conversation_id, keep_recent),
                )
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------
    # התראות Push (עדכון גרסה דרך version.json ב-GitHub)
    # ----------------------------------------------------------------
    async def save_push_subscription(self, user_id: str, endpoint: str, p256dh: str, auth: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    insert into push_subscriptions (user_id, endpoint, p256dh, auth)
                    values (%s, %s, %s, %s)
                    on conflict (endpoint) do update set user_id = excluded.user_id, p256dh = excluded.p256dh, auth = excluded.auth;
                    """,
                    (user_id, endpoint, p256dh, auth),
                )

    async def remove_push_subscription(self, endpoint: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("delete from push_subscriptions where endpoint = %s;", (endpoint,))

    async def get_all_push_subscriptions(self) -> List[Dict[str, Any]]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("select endpoint, p256dh, auth from push_subscriptions;")
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def get_last_seen_version(self) -> str:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select last_seen_version from system_config where id = 1;")
                row = await cur.fetchone()
        return (row[0] if row else "") or ""

    async def set_last_seen_version(self, version: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("update system_config set last_seen_version = %s where id = 1;", (version,))


db = Database()
