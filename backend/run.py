"""
נקודת הכניסה להרצת השרת. קובע את מדיניות ה-event loop של Windows *לפני* ש-uvicorn
מקים את הלולאה שלו - כי psycopg (מצב אסינכרוני) לא תומך ב-ProactorEventLoop
שמוגדר כברירת מחדל ב-Windows.
"""
import sys
import os
import asyncio

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import uvicorn

if __name__ == "__main__":
    uvicorn.run("main:app", host="127.0.0.1", port=8000)
