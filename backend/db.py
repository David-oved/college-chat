import os
import re
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
        _pool = AsyncConnectionPool(DATABASE_URL, min_size=4, max_size=8, open=False)
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
        # מיגרציה עדינה ואידמפוטנטית: עמודת שם פרטי למשתמשים (מגיע מ-Moodle בחיבור). אם ההרשאות
        # ב-DB לא מאפשרות DDL - לא מפילים את השרת, פשוט ממשיכים בלי שם פרטי שמור.
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("alter table app_users add column if not exists display_name text;")
        except Exception as e:
            print(f"[db] could not ensure app_users.display_name: {e}")
        # the models the admin chose for the chat (comma separated; the first is the main one; empty = the built-in order)
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("alter table system_config add column if not exists gemini_models text;")
        except Exception as e:
            print(f"[db] could not ensure system_config.gemini_models: {e}")
        # other AI providers (OpenAI, xAI Grok): their keys, their chosen models, and which provider the bot uses ("google" by default)
        for col in ("ai_provider", "openai_api_key", "xai_api_key", "openai_models", "xai_models", "groq_api_key", "groq_models"):
            try:
                async with pool.connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(f"alter table system_config add column if not exists {col} text;")
            except Exception as e:
                print(f"[db] could not ensure system_config.{col}: {e}")
        # the admin's control over the prompt: one JSON state (sections, notes, designs) plus a history of versions
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("create table if not exists prompt_state (k text primary key, v text not null);")
                    await cur.execute("create table if not exists prompt_versions (id bigserial primary key, created_at timestamptz not null default now(), label text, snapshot text not null);")
        except Exception as e:
            print(f"[db] could not ensure the prompt tables: {e}")
        # the student's own AI key/model (developers): one encrypted text on the account, and their own usage per day and model (kept 7 days)
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("alter table app_users add column if not exists ai_cfg text;")
                    await cur.execute("""create table if not exists user_ai_usage (
                        user_id uuid not null references app_users(id) on delete cascade, usage_date date not null, model text not null,
                        calls int not null default 0, fails int not null default 0, primary key (user_id, usage_date, model));""")
        except Exception as e:
            print(f"[db] could not ensure the personal AI tables: {e}")
        # זיכרון הבוט לכל משתמש: עובדות שהסטודנט סיפר (למשל "עזבתי את הקורס X") - נשמרות בשרת, לכל משתמש לחוד
        try:
            async with pool.connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        """
                        create table if not exists user_memories (
                            id uuid primary key default gen_random_uuid(),
                            user_id uuid not null references app_users(id) on delete cascade,
                            content text not null,
                            kind text not null default 'fact',
                            course text,
                            created_at timestamptz not null default now()
                        );
                        """
                    )
                    await cur.execute("create index if not exists user_memories_user_idx on user_memories (user_id, created_at);")
        except Exception as e:
            print(f"[db] could not ensure user_memories: {e}")

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

    async def _get_api_key_raw(self) -> str:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select gemini_api_key from system_config where id = 1;")
                row = await cur.fetchone()
        return (row[0] if row else "") or ""

    async def _get_bot_identity_raw(self) -> str:
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

    async def _get_gemini_models_raw(self) -> Optional[List[str]]:
        """None = the built-in order. Otherwise the admin's list: the first is the main model, the rest are the fallbacks in order."""
        try:
            async with _get_pool().connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("select gemini_models from system_config where id = 1;")
                    row = await cur.fetchone()
        except Exception:
            return None
        raw = (row[0] if row else None) or ""
        names = [n.strip() for n in raw.split(",") if n.strip()]
        return names or None

    async def set_gemini_models(self, names: Optional[List[str]]):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("update system_config set gemini_models = %s where id = 1;", (",".join(names) if names else None,))
        self.invalidate_config_cache()

    # ---- the student's own AI key (encrypted with a key derived from the server secret; never shown again, only masked)
    @staticmethod
    def _fernet():
        import base64 as _b, hashlib as _h
        from cryptography.fernet import Fernet
        from auth import _session_secret
        return Fernet(_b.urlsafe_b64encode(_h.sha256(b"user-ai-key-v1:" + _session_secret()).digest()))

    async def get_state_kv(self, k: str) -> Optional[str]:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select v from prompt_state where k = %s;", (k,))
                row = await cur.fetchone()
        return row[0] if row else None

    async def set_state_kv(self, k: str, v: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("insert into prompt_state(k, v) values (%s, %s) on conflict (k) do update set v = excluded.v;", (k, v))

    async def get_prompt_state(self) -> Optional[str]:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select v from prompt_state where k = 'main';")
                row = await cur.fetchone()
        return row[0] if row else None

    async def set_prompt_state(self, raw: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("insert into prompt_state(k, v) values ('main', %s) on conflict (k) do update set v = excluded.v;", (raw,))

    async def add_prompt_version(self, label: str, raw: str):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("insert into prompt_versions(label, snapshot) values (%s, %s);", (label[:120], raw))
                await cur.execute("delete from prompt_versions where id not in (select id from prompt_versions order by id desc limit 40);")

    async def list_prompt_versions(self) -> List[Dict[str, Any]]:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select id, created_at, label from prompt_versions order by id desc limit 40;")
                rows = await cur.fetchall()
        return [{"id": r[0], "at": r[1].isoformat(), "label": r[2] or ""} for r in rows]

    async def get_prompt_version(self, vid: int) -> Optional[str]:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select snapshot from prompt_versions where id = %s;", (vid,))
                row = await cur.fetchone()
        return row[0] if row else None

    async def get_user_ai(self, user_id: str) -> Optional[Dict[str, Any]]:
        """{"mode": "personal"|"public", "kind", "key", "models": []} or None."""
        try:
            async with _get_pool().connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("select ai_cfg from app_users where id = %s;", (user_id,))
                    row = await cur.fetchone()
            raw = row[0] if row else None
            if not raw:
                return None
            import json as _j
            cfg = _j.loads(self._fernet().decrypt(raw.encode()).decode())
            return cfg if isinstance(cfg, dict) and cfg.get("kind") and cfg.get("key") else None
        except Exception:
            return None

    async def set_user_ai(self, user_id: str, cfg: Optional[Dict[str, Any]]):
        import json as _j
        token = self._fernet().encrypt(_j.dumps(cfg, ensure_ascii=False).encode()).decode() if cfg else None
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("update app_users set ai_cfg = %s where id = %s;", (token, user_id))
                if not cfg:
                    await cur.execute("delete from user_ai_usage where user_id = %s;", (user_id,))

    def bump_user_ai(self, user_id: str, model: str, fail: bool = False):
        import asyncio as _a

        async def _run():
            try:
                async with _get_pool().connection() as conn:
                    async with conn.cursor() as cur:
                        await cur.execute(
                            f"insert into user_ai_usage (user_id, usage_date, model, calls, fails) values (%s, current_date, %s, {0 if fail else 1}, {1 if fail else 0}) "
                            f"on conflict (user_id, usage_date, model) do update set calls = user_ai_usage.calls + {0 if fail else 1}, fails = user_ai_usage.fails + {1 if fail else 0};",
                            (user_id, model[:120]))
            except Exception:
                pass
        try:
            _a.get_running_loop().create_task(_run())
        except RuntimeError:
            pass

    async def get_user_ai_usage(self, user_id: str) -> List[Dict[str, Any]]:
        """This student's own calls on their own key, the last 7 days (older rows are removed)."""
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("delete from user_ai_usage where user_id = %s and usage_date < current_date - 6;", (user_id,))
                await cur.execute("select usage_date::text as day, model, calls, fails from user_ai_usage where user_id = %s order by usage_date desc, model;", (user_id,))
                return await cur.fetchall()

    async def _get_ai_provider_cfg_raw(self) -> Dict[str, Any]:
        out = {"active": "google", "openai_key": "", "xai_key": "", "groq_key": "", "openai_models": [], "xai_models": [], "groq_models": []}
        try:
            async with _get_pool().connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("select ai_provider, openai_api_key, xai_api_key, openai_models, xai_models, groq_api_key, groq_models from system_config where id = 1;")
                    row = await cur.fetchone()
        except Exception:
            return out
        if row:
            out["active"] = row[0] if row[0] in ("google", "openai", "xai", "groq") else "google"
            out["groq_key"] = row[5] or ""
            out["groq_models"] = [n for n in (row[6] or "").split(",") if n.strip()]
            out["openai_key"], out["xai_key"] = row[1] or "", row[2] or ""
            out["openai_models"] = [n for n in (row[3] or "").split(",") if n.strip()]
            out["xai_models"] = [n for n in (row[4] or "").split(",") if n.strip()]
        return out

    async def get_ai_provider_cfg(self) -> Dict[str, Any]:
        return await self._cfg("get_ai_provider_cfg")

    async def set_ai_provider_value(self, column: str, value: Optional[str]):
        if column not in ("ai_provider", "openai_api_key", "xai_api_key", "openai_models", "xai_models", "groq_api_key", "groq_models"):
            raise ValueError("bad column")
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(f"update system_config set {column} = %s where id = 1;", (value or None,))
        self.invalidate_config_cache()

    async def _get_moodle_enabled_functions_raw(self) -> Optional[List[str]]:
        """None = כל הפונקציות דלוקות (ברירת מחדל). רשימה (אפילו ריקה) = בדיוק מה שהמנהל בחר."""
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("select moodle_enabled_functions from system_config where id = 1;")
                row = await cur.fetchone()
        raw = row[0] if row else None
        if raw is None:
            return None
        return [n for n in raw.split(",") if n]

    async def set_moodle_enabled_functions(self, names: List[str]):
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update system_config set moodle_enabled_functions = %s where id = 1;", (",".join(names),)
                )

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
        await self.recount_catalog()

    async def update_chunk(self, chunk_id: str, title: str, text: str, category: str, embedding: Optional[List[float]]) -> bool:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update knowledge_chunks set title = %s, text = %s, category = %s, embedding = %s::vector where id = %s;",
                    (title, text, category, _vector_literal(embedding), chunk_id),
                )
                ok = cur.rowcount > 0
        await self.recount_catalog()
        return ok

    async def recount_catalog(self):
        """ספירות הקטלוג (המפה שהבוט רואה) חייבות להתאים למה שבאמת שמור - אחרת הבוט "יודע" על קטעים שלא קיימים."""
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("update knowledge_catalog k set chunk_count = (select count(*) from knowledge_chunks c where c.category = k.category);")
                await cur.execute("delete from knowledge_catalog where chunk_count = 0;")
        self.invalidate_config_cache()

    async def get_usage_range(self, days: int = 7) -> List[Dict[str, Any]]:
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    "select usage_date::text as day, call_type, count from api_usage_daily where usage_date > current_date - %s::int order by usage_date;",
                    (int(days),),
                )
                return [dict(r) for r in await cur.fetchall()]

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

    async def keyword_search_chunks(self, query: str, top_k: int = 3) -> List[Dict[str, Any]]:
        """The fallback when the embedding service is down: a plain word search (title matches count triple). Hebrew one-letter prefixes (ב ל ה ו מ ש כ) are also tried off."""
        words = []
        for w in re.findall(r"[\w'\"׳״-]{3,}", str(query or "").lower()):
            w = w.strip("'\"-")
            for v in {w, w[1:] if len(w) > 3 and w[0] in "בלהומשכ" else w}:
                if len(v) >= 3 and v not in words:
                    words.append(v)
        words = [w.replace("\\", "").replace("%", "").replace("_", "") for w in words][:12]
        if not words:
            return []
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute(
                    """
                    select id, title, text, category, score::float / 10 as similarity from (
                      select c.id, c.title, c.text, c.category,
                             (select count(*) from unnest(%s::text[]) t where c.title ilike '%%' || t || '%%') * 3
                           + (select count(*) from unnest(%s::text[]) t where c.text ilike '%%' || t || '%%') as score
                      from knowledge_chunks c
                    ) s where score > 0 order by score desc limit %s;
                    """,
                    (words, words, top_k),
                )
                rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ----------------------------------------------------------------
    # קטלוג/מפת נושאים (knowledge_catalog) - מתעדכן ע"י AI בכל הוספה
    # ----------------------------------------------------------------
    async def _get_catalog_raw(self) -> List[Dict[str, Any]]:
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
    # ----------------------------------------------------------------
    # מטמון קצר להגדרות שכמעט לא משתנות (מפתח, זהות הבוט, קטלוג, כלים דלוקים): כל שליפה כזו עלתה סבב מלא ל-DB (~300ms)
    # בכל הודעה. שינוי של המנהל מתעדכן תוך _CFG_TTL שניות.
    # ----------------------------------------------------------------
    _CFG_TTL = 45.0
    _cfg_cache: Dict[str, Any] = {}

    async def _cfg(self, name: str):
        import time as _t
        hit = self._cfg_cache.get(name)
        if hit and hit[0] > _t.monotonic():
            return hit[1]
        value = await getattr(self, f"_{name}_raw")()
        self._cfg_cache[name] = (_t.monotonic() + self._CFG_TTL, value)
        return value

    def invalidate_config_cache(self):
        self._cfg_cache.clear()

    async def get_api_key(self) -> str:
        return await self._cfg("get_api_key")

    async def get_bot_identity(self) -> str:
        return await self._cfg("get_bot_identity")

    async def get_moodle_enabled_functions(self) -> Optional[List[str]]:
        return await self._cfg("get_moodle_enabled_functions")

    async def get_gemini_models(self) -> Optional[List[str]]:
        return await self._cfg("get_gemini_models")

    async def get_catalog(self) -> List[Dict[str, Any]]:
        return await self._cfg("get_catalog")

    def bump_api_usage(self, call_type: str):
        """סופר קריאה ל-Gemini ברקע - לא מחכים לזה ולא מעכבים את התשובה בסבב DB."""
        import asyncio as _a

        async def _run():
            try:
                await self.increment_api_usage(call_type)
            except Exception:
                pass

        try:
            _a.get_running_loop().create_task(_run())
        except RuntimeError:
            pass

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
        by_type["total"] = sum(v for k, v in by_type.items() if not k.startswith(("tool:", "ui:", "model:", "win:")))      # רק קריאות Gemini, לא מוני הכלים והתצוגות
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

    async def get_user_display_name(self, user_id: str) -> Optional[str]:
        """השם הפרטי השמור של המשתמש (או None אם אין / העמודה עוד לא קיימת)."""
        try:
            async with _get_pool().connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute("select display_name from app_users where id = %s;", (user_id,))
                    row = await cur.fetchone()
            return row[0] if row and row[0] else None
        except Exception:
            return None

    async def set_user_display_name(self, user_id: str, display_name: str):
        try:
            async with _get_pool().connection() as conn:
                async with conn.cursor() as cur:
                    await cur.execute(
                        "update app_users set display_name = %s where id = %s and display_name is distinct from %s;",
                        (display_name, user_id, display_name),
                    )
        except Exception as e:
            print(f"[db] could not save display_name: {e}")

    async def set_conversation_title_if_default(self, conversation_id: str, title: str):
        """נותן לשיחה כותרת מהשאלה הראשונה (רק אם עדיין יש לה את כותרת ברירת המחדל)."""
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "update conversations set title = %s where id = %s and title = 'שיחה חדשה';",
                    (title, conversation_id),
                )

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

    async def get_user_profile_summary(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        'פאנל שקיפות' למשתמש עצמו - בדיוק מה שמור עליו אצלנו ב-DB כרגע (לא יותר מזה):
        שורת app_users שלו + כמה שיחות/הודעות יש לו בפועל.
        """
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
                    where u.id = %s
                    group by u.id, u.username, u.created_at;
                    """,
                    (user_id,),
                )
                row = await cur.fetchone()
        if not row:
            return None
        row = dict(row)
        row["id"] = str(row["id"])
        return row

    async def get_user_activity(self, user_id: str) -> Optional[Dict[str, Any]]:
        """
        שימוש של משתמש - מספרים בלבד. פרטיות: תוכן ההודעות והכותרות (שנגזרות מהשאלה הראשונה) שייכים למשתמש בלבד ולא נשלפים למנהל.
        """
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("select id, username, created_at from app_users where id = %s;", (user_id,))
                user = await cur.fetchone()
                if not user:
                    return None
                await cur.execute(
                    """
                    select count(distinct c.id) as conversations,
                           count(*) filter (where m.role = 'user') as questions,
                           max(m.created_at) as last_active
                    from conversations c join messages m on m.conversation_id = c.id
                    where c.user_id = %s;
                    """,
                    (user_id,),
                )
                tot = await cur.fetchone()
                await cur.execute(
                    """
                    select (m.created_at at time zone 'Asia/Jerusalem')::date as day, count(*) as questions
                    from conversations c join messages m on m.conversation_id = c.id
                    where c.user_id = %s and m.role = 'user' and m.created_at > now() - interval '30 days'
                    group by 1 order by 1 desc;
                    """,
                    (user_id,),
                )
                days = await cur.fetchall()
        return {
            "user_id": str(user["id"]),
            "username": user["username"],
            "created_at": user["created_at"],
            "conversations": tot["conversations"] or 0,
            "questions_total": tot["questions"] or 0,
            "last_active": tot["last_active"],
            "questions_by_day": [{"day": str(d["day"]), "questions": d["questions"]} for d in days],
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

    # ----------------------------------------------------------------
    # זיכרון הבוט על כל משתמש (user_memories)
    # ----------------------------------------------------------------
    MAX_MEMORIES_PER_USER = 40

    async def list_memories(self, user_id: str) -> List[Dict[str, Any]]:
        try:
            async with _get_pool().connection() as conn:
                async with conn.cursor(row_factory=dict_row) as cur:
                    await cur.execute(
                        "select id, content, kind, course, created_at from user_memories where user_id = %s order by created_at asc;",
                        (user_id,),
                    )
                    rows = await cur.fetchall()
            return [{**dict(r), "id": str(r["id"])} for r in rows]
        except Exception as e:
            print(f"[db] list_memories failed: {e}")
            return []

    async def add_memory(self, user_id: str, content: str, kind: str = "fact", course: Optional[str] = None) -> Optional[Dict[str, Any]]:
        """שומר זיכרון חדש. מחזיר None אם זה כפול (אותו תוכן בדיוק) או אם הזיכרון מלא."""
        async with _get_pool().connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cur:
                await cur.execute("select count(*) as n from user_memories where user_id = %s;", (user_id,))
                if (await cur.fetchone())["n"] >= self.MAX_MEMORIES_PER_USER:
                    return None
                await cur.execute(
                    "select 1 from user_memories where user_id = %s and lower(content) = lower(%s);", (user_id, content)
                )
                if await cur.fetchone():
                    return None
                await cur.execute(
                    "insert into user_memories (user_id, content, kind, course) values (%s, %s, %s, %s) returning id, content, kind, course, created_at;",
                    (user_id, content, kind, course),
                )
                row = await cur.fetchone()
        return {**dict(row), "id": str(row["id"])}

    async def delete_memory(self, user_id: str, memory_id: str) -> bool:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("delete from user_memories where id = %s and user_id = %s returning id;", (memory_id, user_id))
                return (await cur.fetchone()) is not None

    async def delete_all_memories(self, user_id: str) -> int:
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute("delete from user_memories where user_id = %s;", (user_id,))
                return cur.rowcount or 0

    async def delete_conversation(self, conversation_id: str, user_id: str) -> bool:
        """מוחק שיחה אחת של המשתמש (וכל ההודעות שלה). מחזיר False אם השיחה לא קיימת או לא שלו."""
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "delete from messages where conversation_id in (select id from conversations where id = %s and user_id = %s);",
                    (conversation_id, user_id),
                )
                await cur.execute(
                    "delete from conversations where id = %s and user_id = %s returning id;", (conversation_id, user_id)
                )
                row = await cur.fetchone()
        return row is not None

    async def delete_all_conversations(self, user_id: str) -> int:
        """מוחק את כל השיחות וההודעות של המשתמש. מחזיר כמה שיחות נמחקו."""
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "delete from messages where conversation_id in (select id from conversations where user_id = %s);", (user_id,)
                )
                await cur.execute("delete from conversations where user_id = %s;", (user_id,))
                return cur.rowcount or 0

    async def delete_user_everything(self, user_id: str) -> Dict[str, int]:
        """Erases the student completely, as if they had never opened the chat: messages, conversations, memories, notification
        subscriptions, every other table that holds a user_id, and finally the account row itself. Cannot be undone."""
        counts: Dict[str, int] = {}
        async with _get_pool().connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    "delete from messages where conversation_id in (select id from conversations where user_id = %s);", (user_id,)
                )
                counts["messages"] = cur.rowcount or 0
                await cur.execute(
                    "select table_name from information_schema.columns where table_schema = 'public' and column_name = 'user_id' "
                    "and table_name not in ('app_users', 'messages') order by (table_name = 'conversations');"
                )
                tables = [r[0] for r in await cur.fetchall()]
                for t in tables:                                     # conversations is last (its messages are already gone)
                    if not t.replace("_", "").isalnum():
                        continue
                    await cur.execute(f'delete from "{t}" where user_id = %s;', (user_id,))
                    counts[t] = cur.rowcount or 0
                await cur.execute("delete from app_users where id = %s;", (user_id,))
                counts["account"] = cur.rowcount or 0
        return counts

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
