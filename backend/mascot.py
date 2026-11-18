"""
Uri, the face of the app's AI part, as DATA. The picture and the movement are drawn by ONE renderer in the app (chat.js); this module holds the settings that renderer reads:
the body (shape, colour, eyes), what he wears (by default and by date: only the admin dresses him), the speech bubble's look, a few quiet animations and WHEN each may play
(opening the chat, waiting for an answer, an answer arriving, an error, now and then), how often he says a line and WHAT he says, and a few look settings (eye colour, outline,
glow, the floating button's colours and size, the small tag).
It is a serious app: no games, no wardrobe for students. The admin changes everything here in the admin app; the app fetches it (small, public, never cached) and every place Uri appears
is redrawn from it, with no software update. New drawing parts and new animation SHAPES are the only things that need an app update.
"""
import re
from typing import Any, Dict, List

VERSION = 2          # settings saved by the first (playful) version carry no version: they are dropped, so everything starts clean

SLOTS: Dict[str, List[str]] = {
    "hat": ["gradcap", "partyhat", "bow", "headphones"],
    "face": ["glasses", "sunglasses"],
    "neck": ["scarf"],
}
ITEM_LABELS = {"gradcap": "כובע בוגרים", "partyhat": "כובע חגיגי", "bow": "פפיון", "headphones": "אוזניות", "glasses": "משקפיים", "sunglasses": "משקפי שמש", "scarf": "צעיף"}
ALL_ITEMS = [i for items in SLOTS.values() for i in items]
SHAPES = ("bubble", "blob", "circle", "square")
SHAPE_LABELS = {"bubble": "בועת שיחה", "blob": "ביצה רכה", "circle": "עיגול", "square": "ריבוע מעוגל"}
EYES = ("oval", "big", "happy")
BUBBLE_SHAPES = ("round", "square", "pill")
MOODS = ("happy", "think", "wow", "wink")
MOOD_LABELS = {"happy": "רגיל", "think": "חושב", "wow": "מופתע", "wink": "קורץ"}

# the movements the app can play, with their names in the admin (the movement itself is in the app)
ANIMS: Dict[str, str] = {
    "jump": "קפיצה קטנה", "nod": "הנהון", "wiggle": "ניענוע עדין", "tilt": "הטיית ראש", "pulse": "פעימה", "zoom": "התקרבות",
    "shake": "רעד", "lookaround": "מסתכל סביב", "float": "ריחוף", "wink": "קריצה",
}
# the moments an animation (and a line) can be tied to
TRIGGERS: Dict[str, str] = {
    "open": "פותחים את הצ'אט", "think": "ממתינים לתשובה", "done": "התשובה הגיעה", "error": "שגיאה", "idle": "מדי פעם, כשאף אחד לא עושה כלום",
}
# the categories of lines (the opening greeting changes with the hour)
PHRASE_KEYS: Dict[str, str] = {
    "open": "בפתיחת הצ'אט (כללי)", "open_morning": "בפתיחה בבוקר (5 עד 11)", "open_noon": "בפתיחה בצהריים (11 עד 17)", "open_evening": "בפתיחה בערב (17 עד 22)",
    "open_night": "בפתיחה בלילה (22 עד 5)", "think": "בזמן שממתינים לתשובה", "done": "כשהתשובה הגיעה", "error": "כשיש שגיאה", "idle": "מדי פעם, מעצמו",
}
LOOK_COLORS = ("eye", "outline", "aura", "fab1", "fab2")
BUBBLE_COLORS = ("bg", "text", "border")

DEFAULT: Dict[str, Any] = {
    "v": VERSION,
    "shape": "bubble", "body": "#ffffff", "eyes": "oval", "spark": True,
    "outfit": {"hat": "", "face": "", "neck": ""},
    "seasons": [],
    "behavior": {"blink": True, "look": True, "react": True, "idle": True},
    "anim": {"enabled": list(ANIMS), "speed": 1.0},
    "triggers": {
        "open": ["nod", "wink", "jump"], "think": ["lookaround", "tilt"], "done": ["nod", "jump", "pulse"], "error": ["shake"], "idle": ["float", "wink", "lookaround"],
    },
    "talk": {"open": 35, "think": 25, "done": 15, "error": 100, "idle": 8},
    "idle": {"min": 40, "max": 90},
    "phrases": {
        "open": ["היי, במה אפשר לעזור?", "אני מקשיב"],
        "open_morning": ["בוקר טוב, במה אפשר לעזור?", "בוקר אור"],
        "open_noon": ["צהריים טובים, אני כאן", "במה אפשר לעזור?"],
        "open_evening": ["ערב טוב, במה אפשר לעזור?", "אני כאן אם צריך"],
        "open_night": ["מאוחר, אבל אני כאן", "במה אפשר לעזור?"],
        "think": ["רגע, בודק", "עוד שנייה", "מחפש את התשובה"],
        "done": ["הנה", "מקווה שזה עוזר", "יש עוד משהו?"],
        "error": ["משהו השתבש, נסה שוב", "תקלה קטנה, נסה שוב בעוד רגע"],
        "idle": ["אני כאן אם צריך", "יש שאלה על הלימודים?"],
    },
    "look": {"eye": "", "outline": "", "outlineW": 0, "aura": "", "auraSize": 0, "fab1": "", "fab2": "", "fabScale": 1.0, "tag": "AI"},
    "bubble": {"shape": "round", "bg": "", "text": "", "border": ""},
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


def _color(v: Any) -> str:
    return v if isinstance(v, str) and _HEX.match(v) else ""


def clean(cfg: Any) -> Dict[str, Any]:
    """Whatever arrives (admin form, an older saved version) becomes a complete, safe configuration; unknown values fall back to the defaults.
    Settings saved before version 2 are ignored completely."""
    c = cfg if isinstance(cfg, dict) else {}
    if c.get("v") != VERSION:
        c = {}
    d = DEFAULT
    out: Dict[str, Any] = {
        "v": VERSION,
        "shape": c.get("shape") if c.get("shape") in SHAPES else d["shape"],
        "body": c["body"] if isinstance(c.get("body"), str) and _HEX.match(c["body"]) else d["body"],
        "eyes": c.get("eyes") if c.get("eyes") in EYES else d["eyes"],
        "spark": c.get("spark") is not False,
        "outfit": _outfit(c.get("outfit")),
    }
    seasons = []
    for s in (c.get("seasons") or [])[:12] if isinstance(c.get("seasons"), list) else []:
        if isinstance(s, dict) and _MD.match(str(s.get("from") or "")) and _MD.match(str(s.get("to") or "")):
            seasons.append({"from": s["from"], "to": s["to"], "label": str(s.get("label") or "")[:30], "outfit": _outfit(s.get("outfit"))})
    out["seasons"] = seasons
    b = c.get("behavior") if isinstance(c.get("behavior"), dict) else {}
    out["behavior"] = {k: b.get(k) is not False for k in d["behavior"]}
    a = c.get("anim") if isinstance(c.get("anim"), dict) else {}
    out["anim"] = {"enabled": [x for x in (a.get("enabled") if isinstance(a.get("enabled"), list) else d["anim"]["enabled"]) if x in ANIMS], "speed": round(_num(a.get("speed"), 0.5, 2.0, 1.0), 2)}
    t = c.get("triggers") if isinstance(c.get("triggers"), dict) else {}
    out["triggers"] = {k: [x for x in (t.get(k) if isinstance(t.get(k), list) else d["triggers"][k]) if x in ANIMS][:10] for k in TRIGGERS}
    ta = c.get("talk") if isinstance(c.get("talk"), dict) else {}
    out["talk"] = {k: int(_num(ta.get(k), 0, 100, d["talk"][k])) for k in TRIGGERS}
    i = c.get("idle") if isinstance(c.get("idle"), dict) else {}
    mn = int(_num(i.get("min"), 10, 600, d["idle"]["min"]))
    out["idle"] = {"min": mn, "max": max(mn, int(_num(i.get("max"), 10, 1200, d["idle"]["max"])))}
    ph = c.get("phrases") if isinstance(c.get("phrases"), dict) else {}
    out["phrases"] = {k: _lines(ph.get(k), d["phrases"][k]) for k in PHRASE_KEYS}
    lk = c.get("look") if isinstance(c.get("look"), dict) else {}
    look: Dict[str, Any] = {k: _color(lk.get(k)) for k in LOOK_COLORS}
    look["outlineW"] = round(_num(lk.get("outlineW"), 0, 3, 0), 1)
    look["auraSize"] = int(_num(lk.get("auraSize"), 0, 24, 0))
    look["fabScale"] = round(_num(lk.get("fabScale"), 0.8, 1.5, 1.0), 2)
    look["tag"] = str(lk.get("tag") if "tag" in lk else d["look"]["tag"])[:4]
    out["look"] = look
    bb = c.get("bubble") if isinstance(c.get("bubble"), dict) else {}
    out["bubble"] = {"shape": bb.get("shape") if bb.get("shape") in BUBBLE_SHAPES else d["bubble"]["shape"], **{k: _color(bb.get(k)) for k in BUBBLE_COLORS}}
    if isinstance(c.get("rev"), int):
        out["rev"] = c["rev"]
    return out
