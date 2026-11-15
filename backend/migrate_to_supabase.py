"""
סקריפט חד-פעמי: מעביר את הנתונים מהקבצים המקומיים (mock_data.json + admin_auth.json)
ל-Postgres (Supabase). מריצים פעם אחת ידנית: py -3.13 migrate_to_supabase.py
"""
import asyncio
import json
import os

from db import db
from rag_engine import get_embedding, categorize_and_summarize

MOCK_DATA_PATH = os.path.join(os.path.dirname(__file__), "mock_data.json")
ADMIN_AUTH_PATH = os.path.join(os.path.dirname(__file__), "admin_auth.json")


async def main():
    await db.init()

    mock_data = {}
    if os.path.exists(MOCK_DATA_PATH):
        with open(MOCK_DATA_PATH, "r", encoding="utf-8") as f:
            mock_data = json.load(f)

    system_config = mock_data.get("system_config", {})
    api_key = system_config.get("gemini_api_key", "")
    daily_limit = system_config.get("daily_quota_limit", 10)
    bot_identity = system_config.get("bot_identity_info", "")

    print("מעביר הגדרות מערכת...")
    await db.update_config(api_key=api_key, daily_limit=daily_limit, bot_identity_info=bot_identity)

    if os.path.exists(ADMIN_AUTH_PATH):
        with open(ADMIN_AUTH_PATH, "r", encoding="utf-8") as f:
            admin_auth_data = json.load(f)
        password_hash = admin_auth_data.get("password_hash")
        if password_hash:
            await db.set_admin_password_hash(password_hash)
            print("סיסמת המנהל הקיימת הועברה בהצלחה.")

    user_quotas = mock_data.get("user_quotas", {})
    for user_id in user_quotas:
        await db.reset_user_quota(user_id)  # נרשם מחדש, מתאפס ל-0 (מכסה יומית ממילא מתאפסת כל יום)
    print(f"נרשמו {len(user_quotas)} משתמשים (מכסה מאופסת להיום).")

    chunks = mock_data.get("knowledge_chunks", [])
    print(f"מעביר {len(chunks)} מקטעי ידע (embeddings חדשים ב-768 מימדים + סיווג AI לקטגוריות)...")

    existing_categories = []
    for i, c in enumerate(chunks, 1):
        title = c.get("title", "ללא כותרת")
        text = c["text"]
        embedding = await get_embedding(f"{title}: {text}", api_key) if api_key else []

        if api_key:
            classification = await categorize_and_summarize(title, text, api_key, existing_categories)
            category, summary = classification["category"], classification["summary"]
            if category not in existing_categories:
                existing_categories.append(category)
        else:
            category, summary = "כללי", "מידע כללי (טרם סווג)"

        await db.add_knowledge_chunk(title, text, category, embedding or None)
        await db.upsert_catalog_entry(category, summary)
        print(f"  [{i}/{len(chunks)}] '{title}' -> קטגוריה: {category}")

    print("\nההעברה הושלמה בהצלחה!")
    await db.close()


if __name__ == "__main__":
    asyncio.run(main())
