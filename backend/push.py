import os
import json
from typing import Dict

from pywebpush import webpush, WebPushException

VAPID_PRIVATE_KEY = os.environ.get("VAPID_PRIVATE_KEY", "").replace("\\n", "\n")
VAPID_PUBLIC_KEY = os.environ.get("VAPID_PUBLIC_KEY", "")
VAPID_SUBJECT = os.environ.get("VAPID_SUBJECT", "mailto:admin@example.com")


def send_push_to_subscription(subscription: Dict[str, str], payload: dict) -> bool:
    """שולח התראת Push אמיתית לדפדפן/טלפון של מנוי אחד. מחזיר False אם המנוי כבר לא תקף (למחיקה)."""
    if not VAPID_PRIVATE_KEY:
        return False
    try:
        webpush(
            subscription_info={
                "endpoint": subscription["endpoint"],
                "keys": {"p256dh": subscription["p256dh"], "auth": subscription["auth"]},
            },
            data=json.dumps(payload, ensure_ascii=False),
            vapid_private_key=VAPID_PRIVATE_KEY,
            vapid_claims={"sub": VAPID_SUBJECT},
        )
        return True
    except WebPushException:
        return False
    except Exception:
        return False
