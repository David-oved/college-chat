"""
Uri, the face of the app's AI part, as DATA. The picture and the movement are drawn by ONE renderer in the app (chat.js); this module holds the settings that renderer reads:
body shape and colour, eyes, cheeks, outfits (default, by date, what the students may wear and unlock), how alive he is, WHICH animation he plays WHEN (tap, opening the chat,
sending, waiting for an answer, an answer arriving, an error, idling), how often he talks and WHAT he says in each situation, and a few look settings (eye colour, outline, glow,
the floating button's colours and size, the small tag).
The admin changes all of it in the admin app; the app fetches it (small, public, cached) and every place Uri appears is redrawn from it, with no software update.
New drawing parts and new animation SHAPES are the only things that need an app update; everything in this file is chosen from what the app can already draw.
"""
import re
from typing import Any, Dict, List

SLOTS: Dict[str, List[str]] = {
    "hat": ["gradcap", "partyhat", "crown", "sprout", "bow", "headphones", "ears"],
    "face": ["glasses", "sunglasses"],
    "neck": ["scarf"],
}
ITEM_LABELS = {
    "gradcap": "כובע בוגרים", "partyhat": "כובע מסיבה", "crown": "כתר", "sprout": "נבט", "bow": "פפיון", "headphones": "אוזניות", "ears": "אוזני חתול",
    "glasses": "משקפיים", "sunglasses": "משקפי שמש", "scarf": "צעיף",
}
ALL_ITEMS = [i for items in SLOTS.values() for i in items]
SHAPES = ("bubble", "blob")
EYES = ("oval", "big", "happy")
MOODS = ("happy", "grin", "wow", "think", "love", "sleep", "dizzy")

# every animation the app can play, with its name in the admin (the movement itself is in the app)
ANIMS: Dict[str, str] = {
    "jump": "קפיצה", "bounce": "קפיצות ברצף", "backflip": "סלטה", "spin": "סיבוב", "squish": "מעיכה", "wiggle": "ניענוע", "shake": "רעד", "nod": "הנהון", "no": "לא לא",
    "pulse": "פעימת לב", "zoom": "קפיצת התקרבות", "peekaboo": "קוקו", "tilt": "הטיית ראש", "wobble": "נדנוד", "dizzy": "סחרחורת", "blush": "מבוכה", "kiss": "נשיקה",
    "laugh": "צחוק", "sneeze": "התעטשות", "yawn": "פיהוק", "sparkle": "נצנוץ", "confetti": "קונפטי", "tada": "טה-דה", "glitch": "גליץ'", "float": "ריחוף",
    "lookaround": "מסתכל סביב", "wink": "קריצה", "rainbow": "קשת צבעים", "party": "מסיבה",
}
# the moments an animation (and a line) can be tied to
TRIGGERS: Dict[str, str] = {
    "tap": "נוגעים בו", "open": "פותחים את הצ'אט", "send": "שולחים הודעה", "think": "ממתינים לתשובה", "done": "התשובה הגיעה", "error": "שגיאה", "idle": "כשאף אחד לא עושה כלום", "party": "מסיבה (הרבה נגיעות מהירות)",
}
# the extra categories of lines (the chat's opening greeting changes with the hour)
PHRASE_KEYS: Dict[str, str] = {
    "tap": "כשנוגעים בו", "open": "בפתיחת הצ'אט (כללי)", "open_morning": "בפתיחה בבוקר (5 עד 11)", "open_noon": "בפתיחה בצהריים (11 עד 17)", "open_evening": "בפתיחה בערב (17 עד 22)",
    "open_night": "בפתיחה בלילה (22 עד 5)", "send": "אחרי ששולחים הודעה", "think": "בזמן שממתינים לתשובה", "done": "כשהתשובה הגיעה", "error": "כשיש שגיאה", "idle": "סתם, מעצמו", "party": "במסיבה",
}
LOOK_COLORS = ("eye", "outline", "aura", "fab1", "fab2")

_ALL_ANIMS = list(ANIMS)
DEFAULT: Dict[str, Any] = {
    "shape": "bubble", "body": "#ffffff", "cheek": "#ff8fb1", "eyes": "big", "cheeks": True, "spark": True,
    "outfit": {"hat": "", "face": "", "neck": ""},
    "seasons": [],
    "wardrobe": True,
    "available": ["gradcap", "glasses", "sunglasses", "bow", "headphones", "partyhat", "crown", "sprout", "ears", "scarf"],
    "unlocks": {"sunglasses": 10, "crown": 25},
    "behavior": {"blink": True, "look": True, "bounce": True, "react": True, "play": True, "sleepy": True},
    "anim": {"enabled": _ALL_ANIMS, "speed": 1.0, "particles": True, "amount": 6},
    "triggers": {
        "tap": ["jump", "squish", "wiggle", "blush", "spin", "zoom", "tilt", "wink", "kiss", "laugh", "nod", "backflip", "pulse", "bounce"],
        "open": ["peekaboo", "jump", "zoom", "wiggle"], "send": ["nod", "wink", "tilt"], "think": ["lookaround", "tilt", "wobble", "float"],
        "done": ["jump", "sparkle", "nod", "laugh", "pulse"], "error": ["shake", "dizzy", "no"],
        "idle": ["jump", "wiggle", "squish", "lookaround", "yawn", "float", "wink", "glitch", "sneeze", "rainbow"], "party": ["party"],
    },
    "talk": {"tap": 100, "open": 80, "send": 10, "think": 55, "done": 25, "error": 100, "idle": 60, "party": 100},
    "idle": {"min": 18, "max": 40},
    "phrases": {
        "tap": ["הי, זה דגדג!", "אני כאן אם צריך עזרה", "עוד פעם? אני לא מתלונן", "יש שאלה? אני מוכן", "אל תשכח ללמוד, וגם לנוח", "מה נשמע?", "בא לך לשאול משהו?", "תלבישו לי משהו יפה", "הו, איזה פינוק"],
        "open": ["היי! באת לשאול משהו?", "שמח לראות אותך", "אני מקשיב"],
        "open_morning": ["בוקר טוב! מוכן לשאלות", "בוקר אור, מה בתוכנית היום?", "קמת מוקדם, כל הכבוד"],
        "open_noon": ["צהריים טובים! איך היום עובר?", "הפסקת צהריים? אני כאן"],
        "open_evening": ["ערב טוב! עוד יום מאחוריך", "ערב נעים, במה אפשר לעזור?"],
        "open_night": ["מאוחר... אל תשכח לישון", "ערנות לילית? גם אני כאן"],
        "send": ["קיבלתי!", "רגע, בודק", "שאלה טובה"],
        "think": ["עוד רגע...", "אני על זה", "מחפש את התשובה", "כמעט שם"],
        "done": ["הנה!", "מקווה שזה עוזר", "יש עוד שאלה?", "בכיף"],
        "error": ["אופס, משהו השתבש", "נסה שוב? אני כאן", "תקלה קטנה, זה יסתדר"],
        "idle": ["אני כאן אם צריך", "הכול בסדר?", "אפשר לגעת בי, אני לא נושך", "יש מבחן קרוב? אפשר לשאול אותי", "מה חדש?"],
        "party": ["מסיבה!", "יאללה, מרקידים!", "וואו כמה אנרגיה"],
    },
    "look": {"eye": "", "outline": "", "outlineW": 0, "aura": "", "auraSize": 0, "fab1": "", "fab2": "", "fabScale": 1.0, "tag": "AI"},
}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_MD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def _outfit(o: Any) -> Dict[str, str]:
    o = o if isinstance(o, dict) else {}
    return {slot: (o.get(slot) if o.get(slot) in items else "") for slot, items in SLOTS.items()}


def _num(v: Any, lo: float, hi: float, dflt: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return dflt


def _lines(raw: Any, fallback: List[str]) -> List[str]:
    out = [str(x).strip()[:70] for x in (raw if isinstance(raw, list) else []) if str(x).strip()][:14]
    return out or list(fallback)


def clean(cfg: Any) -> Dict[str, Any]:
    """Whatever arrives (admin form, an older saved version) becomes a complete, safe configuration; unknown values fall back to the defaults."""
    c = cfg if isinstance(cfg, dict) else {}
    d = DEFAULT
    out: Dict[str, Any] = {
        "shape": c.get("shape") if c.get("shape") in SHAPES else d["shape"],
        "body": c["body"] if isinstance(c.get("body"), str) and _HEX.match(c["body"]) else d["body"],
        "cheek": c["cheek"] if isinstance(c.get("cheek"), str) and _HEX.match(c["cheek"]) else d["cheek"],
        "eyes": c.get("eyes") if c.get("eyes") in EYES else d["eyes"],
        "cheeks": c.get("cheeks") is not False, "spark": c.get("spark") is not False,
        "outfit": _outfit(c.get("outfit")),
        "wardrobe": c.get("wardrobe") is not False,
        "available": [i for i in (c.get("available") if isinstance(c.get("available"), list) else d["available"]) if i in ALL_ITEMS],
    }
    seasons = []
    for s in (c.get("seasons") or [])[:12] if isinstance(c.get("seasons"), list) else []:
        if isinstance(s, dict) and _MD.match(str(s.get("from") or "")) and _MD.match(str(s.get("to") or "")):
            seasons.append({"from": s["from"], "to": s["to"], "label": str(s.get("label") or "")[:30], "outfit": _outfit(s.get("outfit"))})
    out["seasons"] = seasons
    un = c.get("unlocks") if isinstance(c.get("unlocks"), dict) else d["unlocks"]
    out["unlocks"] = {k: max(1, min(500, int(v))) for k, v in un.items() if k in ALL_ITEMS and str(v).lstrip("-").isdigit()}
    b = c.get("behavior") if isinstance(c.get("behavior"), dict) else {}
    out["behavior"] = {k: b.get(k) is not False for k in d["behavior"]}
    # animations: which are allowed, global speed and particles
    a = c.get("anim") if isinstance(c.get("anim"), dict) else {}
    out["anim"] = {
        "enabled": [x for x in (a.get("enabled") if isinstance(a.get("enabled"), list) else d["anim"]["enabled"]) if x in ANIMS],
        "speed": round(_num(a.get("speed"), 0.5, 2.0, 1.0), 2), "particles": a.get("particles") is not False, "amount": int(_num(a.get("amount"), 2, 14, 6)),
    }
    # which animations each moment may pick from (the app picks one at random)
    t = c.get("triggers") if isinstance(c.get("triggers"), dict) else {}
    out["triggers"] = {k: [x for x in (t.get(k) if isinstance(t.get(k), list) else d["triggers"][k]) if x in ANIMS][:30] for k in TRIGGERS}
    ta = c.get("talk") if isinstance(c.get("talk"), dict) else {}
    out["talk"] = {k: int(_num(ta.get(k), 0, 100, d["talk"][k])) for k in TRIGGERS}
    i = c.get("idle") if isinstance(c.get("idle"), dict) else {}
    mn = int(_num(i.get("min"), 6, 600, d["idle"]["min"]))
    out["idle"] = {"min": mn, "max": max(mn, int(_num(i.get("max"), 6, 1200, d["idle"]["max"])))}
    # lines: the older single list becomes the "tap" lines
    ph = c.get("phrases")
    if isinstance(ph, list):
        ph = {"tap": ph}
    ph = ph if isinstance(ph, dict) else {}
    out["phrases"] = {k: _lines(ph.get(k), d["phrases"][k] if k in d["phrases"] else []) if k in ph or k in d["phrases"] else [] for k in PHRASE_KEYS}
    # extra look settings (empty colour = the app's own)
    lk = c.get("look") if isinstance(c.get("look"), dict) else {}
    look: Dict[str, Any] = {k: (lk[k] if isinstance(lk.get(k), str) and _HEX.match(lk[k]) else "") for k in LOOK_COLORS}
    look["outlineW"] = round(_num(lk.get("outlineW"), 0, 3, 0), 1)
    look["auraSize"] = int(_num(lk.get("auraSize"), 0, 24, 0))
    look["fabScale"] = round(_num(lk.get("fabScale"), 0.8, 1.5, 1.0), 2)
    look["tag"] = str(lk.get("tag") if "tag" in lk else d["look"]["tag"])[:4]
    out["look"] = look
    return out
