import time
from typing import Dict, Any
import httpx

# ערכי מכסה חינמית ידועים ומתועדים על ידי Google (עשויים להשתנות - אינם נשלפים מה-API בזמן אמת,
# כי Google לא חושפת את המכסה בפועל של מפתח ספציפי דרך ה-API).
KNOWN_FREE_TIER_REFERENCE = {
    "gemini-2.0-flash": {"rpm": 15, "rpd": 1500, "tpm": "1,000,000"},
    "gemini-2.0-flash-lite": {"rpm": 30, "rpd": 1500, "tpm": "1,000,000"},
    "gemini-1.5-flash": {"rpm": 15, "rpd": 1500, "tpm": "1,000,000"},
    "gemini-1.5-flash-8b": {"rpm": 15, "rpd": 1500, "tpm": "1,000,000"},
    "gemini-1.5-pro": {"rpm": 2, "rpd": 50, "tpm": "32,000"},
    "text-embedding-004": {"rpm": 1500, "rpd": "ללא הגבלה ידועה", "tpm": "—"},
}

RATE_LIMITS_DOCS_URL = "https://ai.google.dev/gemini-api/docs/rate-limits"


def classify_api_key(api_key: str) -> Dict[str, Any]:
    """מנסה לאפיין את פורמט המפתח. אין API של גוגל שחושף 'למי שייך' מפתח - זו הערכה לפי מבנה הטקסט בלבד."""
    key = (api_key or "").strip()
    if not key:
        return {"type": "ריק", "format_ok": False, "note": "לא הוזן מפתח."}

    masked = f"{key[:6]}...{key[-4:]}" if len(key) > 10 else "***"

    if key.startswith("AIzaSy") and 35 <= len(key) <= 45:
        return {
            "type": "Gemini API Key (Google AI Studio)",
            "format_ok": True,
            "masked": masked,
            "note": "פורמט תקין של מפתח Gemini API רגיל שהונפק דרך Google AI Studio / Google Cloud.",
        }

    return {
        "type": "פורמט לא סטנדרטי",
        "format_ok": False,
        "masked": masked,
        "note": (
            "מפתחות Gemini API 'קלאסיים' מתחילים ב-'AIzaSy', אבל גוגל מנפיקה גם מפתחות/טוקנים "
            "בפורמטים אחרים שעובדים לא פחות טוב. אי אפשר לקבוע רק לפי הצורה הטקסטואלית אם המפתח "
            "תקין - הבדיקה בפועל (מודלים + קריאת חיבור) שמופיעה למטה היא המקור האמין."
        ),
    }


async def list_available_models(api_key: str) -> Dict[str, Any]:
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.get(url)
        if resp.status_code != 200:
            return {"success": False, "status_code": resp.status_code, "error": resp.text[:800]}

        data = resp.json()
        models = []
        for m in data.get("models", []):
            name = m.get("name", "").replace("models/", "")
            methods = m.get("supportedGenerationMethods", [])
            models.append({
                "name": name,
                "display_name": m.get("displayName", name),
                "supports_generate_content": "generateContent" in methods,
                "input_token_limit": m.get("inputTokenLimit"),
                "output_token_limit": m.get("outputTokenLimit"),
                "free_tier_reference": KNOWN_FREE_TIER_REFERENCE.get(name),
            })
        return {"success": True, "count": len(models), "models": models}
    except Exception as e:
        return {
            "success": False,
            "status_code": None,
            "error": f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__} (ככל הנראה פסק זמן בתקשורת מול Google)",
        }


_NOT_CHAT = ("image", "tts", "live", "audio", "embedding", "robotics", "computer", "vision", "veo", "imagen", "lyria", "transcribe", "omni", "aqa", "banana")


async def list_chat_models(api_key: str) -> Dict[str, Any]:
    """The models of THIS key that can chat (text in, text out), for the admin's model picker."""
    res = await list_available_models(api_key)
    if not res.get("success"):
        return res
    out = []
    for m in res["models"]:
        n = m["name"]
        if not m["supports_generate_content"] or not n.startswith("gemini") or any(x in n.lower() for x in _NOT_CHAT):
            continue
        out.append({"name": n, "display_name": m["display_name"], "input_token_limit": m.get("input_token_limit"), "output_token_limit": m.get("output_token_limit")})
    out.sort(key=lambda x: (0 if "lite" in x["name"] else 1, x["name"]))
    return {"success": True, "models": out}


async def test_connectivity(api_key: str, model: str = "gemini-flash-lite-latest") -> Dict[str, Any]:
    """שולח בקשה אמיתית וקטנה למודל כדי לבדוק שהמפתח בפועל מגיב."""
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
    payload = {
        "contents": [{"role": "user", "parts": [{"text": "זוהי בדיקת תקינות בלבד. השב במילה אחת: תקין"}]}],
        "generationConfig": {"maxOutputTokens": 10, "temperature": 0},
    }
    start = time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(url, json=payload)
        latency_ms = round((time.monotonic() - start) * 1000)

        if resp.status_code == 200:
            data = resp.json()
            text = ""
            try:
                text = data["candidates"][0]["content"]["parts"][0]["text"].strip()
            except Exception:
                pass
            return {
                "success": True,
                "status_code": 200,
                "latency_ms": latency_ms,
                "model_used": model,
                "sample_reply": text,
            }

        return {
            "success": False,
            "status_code": resp.status_code,
            "latency_ms": latency_ms,
            "model_used": model,
            "error": resp.text[:800],
        }
    except Exception as e:
        return {
            "success": False,
            "status_code": None,
            "latency_ms": round((time.monotonic() - start) * 1000),
            "model_used": model,
            "error": f"{type(e).__name__}: {e}" if str(e) else f"{type(e).__name__} (ככל הנראה פסק זמן בתקשורת מול Google)",
        }


async def run_full_diagnostics(api_key: str) -> Dict[str, Any]:
    key = (api_key or "").strip()
    if not key:
        return {"error": "לא הוזן מפתח API לבדיקה."}

    key_format = classify_api_key(key)
    models_result = await list_available_models(key)

    test_model = "gemini-flash-lite-latest"
    if models_result.get("success") and models_result.get("models"):
        gc_models = [m["name"] for m in models_result["models"] if m["supports_generate_content"]]
        if gc_models and test_model not in gc_models:
            test_model = gc_models[0]

    connectivity = await test_connectivity(key, test_model)

    return {
        "key_format": key_format,
        "models": models_result,
        "connectivity_test": connectivity,
        "disclaimer": (
            "מספרי המכסה החינמית (RPM/RPD/TPM) מוצגים כהפניה ידועה ומתועדת ועשויים להשתנות - "
            f"Google אינה חושפת את המכסה בפועל של מפתח ספציפי דרך ה-API. לנתונים עדכניים: {RATE_LIMITS_DOCS_URL}"
        ),
    }
