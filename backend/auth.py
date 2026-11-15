import secrets
import hashlib
import hmac
from datetime import datetime, timedelta
from typing import Optional
from fastapi import Header, HTTPException

SESSION_TTL_HOURS = 12


def hash_password(password: str) -> str:
    return hashlib.sha256(password.encode("utf-8")).hexdigest()


class AdminAuth:
    """
    ניהול התחברות מנהל. סיסמת המנהל (מגובבת) נשמרת בטבלת system_config ב-DB
    (לא בקובץ מקומי), כך שהיא שרידה גם אחרי deploy מחדש או מעבר מכונה.
    טוקני ההתחברות עצמם נשארים בזיכרון התהליך - זה תקין, כי ממילא התחברות
    חדשה נדרשת בכל הפעלה מחדש של השרת.
    """

    def __init__(self):
        self.sessions: dict[str, datetime] = {}

    async def ensure_password_exists(self, db) -> Optional[str]:
        """
        בהפעלה הראשונה (כשאין עדיין hash שמור ב-DB), מייצר סיסמה אקראית,
        שומר את ה-hash שלה, ומחזיר את הסיסמה הגלויה (להדפסה חד-פעמית לקונסול).
        """
        existing_hash = await db.get_admin_password_hash()
        if existing_hash:
            return None
        generated_password = secrets.token_urlsafe(9)
        await db.set_admin_password_hash(hash_password(generated_password))
        return generated_password

    async def verify_password(self, db, password: str) -> bool:
        if not password:
            return False
        existing_hash = await db.get_admin_password_hash()
        return hmac.compare_digest(hash_password(password), existing_hash or "")

    def create_session(self) -> str:
        token = secrets.token_urlsafe(32)
        self.sessions[token] = datetime.utcnow() + timedelta(hours=SESSION_TTL_HOURS)
        return token

    def validate_token(self, token: str) -> bool:
        expiry = self.sessions.get(token)
        if not expiry:
            return False
        if datetime.utcnow() > expiry:
            del self.sessions[token]
            return False
        return True


admin_auth = AdminAuth()


async def require_admin(authorization: Optional[str] = Header(None)):
    """Dependency שמגינה על כל נתיבי הניהול - דורשת טוקן תקף שהתקבל מ-/api/admin/login"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="נדרשת התחברות לפאנל הניהול")
    token = authorization[len("Bearer "):]
    if not admin_auth.validate_token(token):
        raise HTTPException(status_code=401, detail="ההתחברות פגה או שאינה תקפה, יש להתחבר מחדש")


class StudentAuth:
    """התחברות אמיתית לסטודנטים: שם משתמש + סיסמה, חשבון משל עצמם ב-DB (app_users)."""

    def __init__(self):
        self.sessions: dict[str, dict] = {}  # token -> {"user_id":..., "expiry":...}

    async def signup(self, db, username: str, password: str) -> Optional[str]:
        username = (username or "").strip()
        if not username or not password or len(password) < 3:
            return None
        user_id = await db.create_user(username, hash_password(password))
        return user_id

    async def login(self, db, username: str, password: str) -> Optional[dict]:
        user = await db.get_user_by_username((username or "").strip())
        if not user:
            return None
        if not hmac.compare_digest(hash_password(password), user["password_hash"]):
            return None
        token = secrets.token_urlsafe(32)
        self.sessions[token] = {
            "user_id": user["id"],
            "username": user["username"],
            "expiry": datetime.utcnow() + timedelta(days=30),
        }
        return {"token": token, "user_id": user["id"], "username": user["username"]}

    def validate_token(self, token: str) -> Optional[dict]:
        session = self.sessions.get(token)
        if not session:
            return None
        if datetime.utcnow() > session["expiry"]:
            del self.sessions[token]
            return None
        return session


student_auth = StudentAuth()


async def require_student(authorization: Optional[str] = Header(None)) -> dict:
    """Dependency שמגינה על נתיבי הצ'אט - דורשת טוקן תקף שהתקבל מ-/api/login"""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="נדרשת התחברות")
    token = authorization[len("Bearer "):]
    session = student_auth.validate_token(token)
    if not session:
        raise HTTPException(status_code=401, detail="ההתחברות פגה או שאינה תקפה, יש להתחבר מחדש")
    return session
