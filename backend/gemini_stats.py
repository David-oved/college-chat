"""מדדי Gemini חיים בזיכרון השרת: כמה קריאות בדקה, כמה נדחו ב-429 בשעה האחרונה, ומה הסטטוס האחרון לכל מודל.
(Google לא חושפת API שמחזיר "כמה מכסה נשארה" - הסימן החי האמין הוא תגובות 429 והקצב בפועל.)"""
import time
from collections import deque
from typing import Any, Dict

_events = deque(maxlen=5000)       # (ts, model, status)
_last: Dict[str, Dict[str, Any]] = {}


def record(model: str, status: int):
    now = time.time()
    _events.append((now, model, int(status)))
    _last[model] = {"status": int(status), "at": now}


def snapshot() -> Dict[str, Any]:
    now = time.time()
    per_model: Dict[str, Dict[str, Any]] = {}
    last_min = last_hour = limited_hour = errors_hour = 0
    for ts, model, st in _events:
        age = now - ts
        if age > 3600:
            continue
        m = per_model.setdefault(model, {"ok": 0, "rate_limited": 0, "errors": 0})
        last_hour += 1
        if age <= 60:
            last_min += 1
        if st == 200:
            m["ok"] += 1
        elif st == 429:
            m["rate_limited"] += 1
            limited_hour += 1
        else:
            m["errors"] += 1
            errors_hour += 1
    for model, info in _last.items():
        d = per_model.setdefault(model, {"ok": 0, "rate_limited": 0, "errors": 0})
        d["last_status"] = info["status"]
        d["last_seconds_ago"] = int(now - info["at"])
    if limited_hour and limited_hour >= max(3, last_hour // 5):
        health = "limited"
    elif errors_hour > max(3, last_hour // 3):
        health = "degraded"
    else:
        health = "ok"
    return {"requests_last_minute": last_min, "requests_last_hour": last_hour, "rate_limited_last_hour": limited_hour,
            "errors_last_hour": errors_hour, "health": health, "per_model": per_model}
