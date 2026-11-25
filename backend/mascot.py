"""
Uri, the face of the app's AI part, as DATA. ONE renderer in the app (chat.js) draws him; this module holds the settings it reads. Version 3 is deliberately small:
no jumping or movements any more, only what a serious app needs -
  * the face: its colour, the small star, the small tag next to it;
  * what he wears by default, and THEMES: a period of the year (winter, summer, exams, a holiday) with its own outfit, face colour, opening animation and opening line;
  * the OPENING ANIMATION of the chat: how the face arrives (entry), what it does (face), what floats around it (ambient), the background, how long it stays, and the line of text.
    The three parts combine freely (10 x 9 x 11 = 990 animations); ready-made names are only shortcuts to a combination. An animation is the id "entry-face-ambient".
The admin changes everything here in the admin app, the app fetches it (small, public, never cached), and nothing needs an app update: the app already knows every entry, face and
ambient listed below. A NEW entry, face or ambient is the one thing that needs an app update, so this list is generous.
"""
import re
from typing import Any, Dict, List

VERSION = 3

SLOTS: Dict[str, List[str]] = {
    "hat": ["gradcap", "partyhat", "bow", "headphones"],
    "face": ["glasses", "sunglasses"],
    "neck": ["scarf"],
}
ITEM_LABELS = {"gradcap": "כובע בוגרים", "partyhat": "כובע חגיגי", "bow": "פפיון", "headphones": "אוזניות", "glasses": "משקפיים", "sunglasses": "משקפי שמש", "scarf": "צעיף"}

# how the face ARRIVES in the opening animation
ENTRIES: Dict[str, str] = {
    "pop": "קפיצת כניסה", "spin": "סיבוב", "drop": "נפילה ונחיתה", "slide": "החלקה מלמטה", "flip": "היפוך",
    "zoom": "התקרבות מטושטשת", "stretch": "מתיחה ומעיכה", "swing": "נדנוד", "rise": "עלייה מתוך הברק", "shake": "רעידה",
}
# what the face DOES once it is there
FACES: Dict[str, str] = {
    "wink": "קריצה", "winkl": "קריצה שמאלית", "double": "קריצה כפולה", "smile": "חיוך רחב", "wow": "הפתעה",
    "think": "מחשבה", "nod": "הנהון", "sleepy": "מתעורר", "love": "עיניים אוהבות",
}
# what floats around it
AMBIENTS: Dict[str, str] = {
    "none": "בלי כלום", "sparkle": "ניצנוצים", "rings": "גלים", "orbit": "כוכבי לכת", "confetti": "קונפטי", "stars": "כוכבים",
    "snow": "שלג", "leaves": "עלים", "hearts": "לבבות", "bubbles": "בועות", "rays": "קרניים",
}
# ready-made names for a combination (id = entry-face-ambient)
PRESETS: List[List[str]] = [
    ["pop-wink-sparkle", "קריצה קלאסית"], ["spin-wink-sparkle", "קריצה בסיבוב"], ["drop-wink-rings", "נחיתה וקריצה"], ["slide-wink-none", "החלקה וקריצה"],
    ["flip-wink-stars", "היפוך וקריצה"], ["pop-double-sparkle", "קריצה כפולה"], ["pop-winkl-sparkle", "קורץ לשמאל"], ["pop-wink-confetti", "קריצה בקונפטי"],
    ["rise-wink-hearts", "קריצה עם לבבות"], ["pop-smile-sparkle", "חיוך רחב"], ["rise-smile-snow", "חיוך ושלג"], ["rise-wink-snow", "חורף: קריצה בשלג"],
    ["swing-wink-leaves", "סתיו: קריצה בעלים"], ["slide-wink-bubbles", "קיץ: בועות"], ["zoom-smile-rays", "קיץ: שמש"], ["pop-wink-stars", "מבחנים: בהצלחה"],
    ["zoom-think-orbit", "מבחנים: ריכוז"], ["shake-wow-sparkle", "הפתעה"], ["drop-wow-confetti", "הפתעה בקונפטי"], ["slide-think-none", "חושב ומחייך"],
    ["rise-sleepy-rings", "מתעורר"], ["pop-nod-none", "הנהון נעים"], ["pop-love-hearts", "אוהב"], ["drop-wow-rings", "נפילה והפתעה"],
    ["stretch-wink-sparkle", "מתיחה עליזה"], ["stretch-smile-bubbles", "מתיחה וחיוך"], ["swing-smile-rings", "נדנוד שקט"], ["swing-wink-stars", "נדנוד וקריצה"],
    ["zoom-wink-rays", "זום וקריצה"], ["zoom-smile-confetti", "זום חגיגי"], ["shake-wink-none", "רעידה וקריצה"], ["flip-smile-confetti", "היפוך חגיגי"],
    ["flip-think-orbit", "היפוך וחשיבה"], ["rise-sleepy-stars", "ירח וכוכבים"], ["pop-smile-rays", "קרניים"], ["rise-wink-bubbles", "בועות וקריצה"],
    ["drop-wow-hearts", "לבבות בהפתעה"], ["rise-double-snow", "שלג וקריצה כפולה"], ["swing-nod-leaves", "עלים והנהון"], ["pop-smile-none", "מינימלי"],
    ["zoom-wink-none", "קורץ מהחושך"], ["pop-smile-rings", "גלים וחיוך"],
]
DEFAULT_INTRO_ID = "pop-wink-sparkle"
LANGS = ("he", "en", "fr")
ALL_ITEMS = [i for items in SLOTS.values() for i in items]

DEFAULT_TEXT: Dict[str, List[str]] = {"he": [], "en": [], "fr": []}

# themes the admin can add with one click (the admin edits everything afterwards)
THEME_TEMPLATES: List[Dict[str, Any]] = [
    {"label": "חורף", "from": "12-01", "to": "02-28", "body": "#dbeeff", "outfit": {"hat": "", "face": "", "neck": "scarf"}, "intro": "rise-wink-snow", "text": {"he": ["חורף נעים, במה אפשר לעזור?"], "en": ["Cosy winter, how can I help?"], "fr": ["Bel hiver, comment puis-je aider ?"]}},
    {"label": "קיץ", "from": "06-15", "to": "08-31", "body": "#fff4c2", "outfit": {"hat": "", "face": "sunglasses", "neck": ""}, "intro": "zoom-smile-rays", "text": {"he": ["קיץ טוב, במה אפשר לעזור?"], "en": ["Happy summer, how can I help?"], "fr": ["Bon été, comment puis-je aider ?"]}},
    {"label": "תקופת מבחנים", "from": "01-10", "to": "02-15", "body": "", "outfit": {"hat": "gradcap", "face": "glasses", "neck": ""}, "intro": "pop-wink-stars", "text": {"he": ["בהצלחה במבחנים!"], "en": ["Good luck with your exams!"], "fr": ["Bonne chance pour les examens !"]}},
    {"label": "חגים", "from": "09-01", "to": "10-31", "body": "#ffe3d1", "outfit": {"hat": "partyhat", "face": "", "neck": ""}, "intro": "swing-wink-leaves", "text": {"he": ["חג שמח!"], "en": ["Happy holidays!"], "fr": ["Joyeuses fêtes !"]}},
]

DEFAULT: Dict[str, Any] = {
    "v": VERSION,
    "body": "#ffffff", "spark": True, "tag": "AI",
    "outfit": {"hat": "", "face": "", "neck": ""},
    "themes": [],
    "intro": {"enabled": True, "mode": "fixed", "fixed": DEFAULT_INTRO_ID, "pool": [], "bg": "", "min": 1.6, "text": DEFAULT_TEXT},
}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_MD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def valid_anim(x: Any) -> bool:
    p = str(x or "").split("-")
    return len(p) == 3 and p[0] in ENTRIES and p[1] in FACES and p[2] in AMBIENTS


def _anim(x: Any, fallback: str = "") -> str:
    return str(x) if valid_anim(x) else fallback


def _color(v: Any) -> str:
    return v if isinstance(v, str) and _HEX.match(v) else ""


def _outfit(o: Any) -> Dict[str, str]:
    o = o if isinstance(o, dict) else {}
    return {slot: (o.get(slot) if o.get(slot) in items else "") for slot, items in SLOTS.items()}


def _num(v: Any, lo: float, hi: float, dflt: float) -> float:
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return dflt


def _lines(raw: Any) -> List[str]:
    return [str(x).strip()[:80] for x in (raw if isinstance(raw, list) else []) if str(x).strip()][:10]


def _texts(raw: Any) -> Dict[str, List[str]]:
    r = raw if isinstance(raw, dict) else {}
    return {lang: _lines(r.get(lang)) for lang in LANGS}


def clean(cfg: Any) -> Dict[str, Any]:
    """Whatever arrives (the admin form, an older saved version) becomes a complete, safe configuration; unknown values fall back to the defaults.
    Version 2 settings (the first, bigger version) keep their colour, star, tag, outfit and seasons; everything else of theirs (movements, phrases, bubble) is gone on purpose."""
    c = cfg if isinstance(cfg, dict) else {}
    old = c.get("v") == 2
    if c.get("v") != VERSION and not old:
        c = {}
    d = DEFAULT
    out: Dict[str, Any] = {
        "v": VERSION,
        "body": _color(c.get("body")) or d["body"],
        "spark": c.get("spark") is not False,
        "outfit": _outfit(c.get("outfit")),
    }
    tag = c.get("tag")
    if old and isinstance(c.get("look"), dict):
        tag = c["look"].get("tag", tag)
    out["tag"] = str(tag if tag is not None else d["tag"])[:4]
    themes = []
    raw_themes = c.get("themes") if isinstance(c.get("themes"), list) else (c.get("seasons") if old and isinstance(c.get("seasons"), list) else [])
    for s in raw_themes[:12]:
        if isinstance(s, dict) and _MD.match(str(s.get("from") or "")) and _MD.match(str(s.get("to") or "")):
            themes.append({
                "label": str(s.get("label") or "")[:30], "from": s["from"], "to": s["to"], "outfit": _outfit(s.get("outfit")), "body": _color(s.get("body")),
                "intro": _anim(s.get("intro")), "text": _texts(s.get("text")), "enabled": s.get("enabled") is not False,
            })
    out["themes"] = themes
    i = c.get("intro") if isinstance(c.get("intro"), dict) else {}
    pool = [x for x in (i.get("pool") if isinstance(i.get("pool"), list) else []) if valid_anim(x)]
    out["intro"] = {
        "enabled": i.get("enabled") is not False,
        "mode": i.get("mode") if i.get("mode") in ("fixed", "random") else "fixed",
        "fixed": _anim(i.get("fixed"), DEFAULT_INTRO_ID),
        "pool": list(dict.fromkeys(pool))[:60],
        "bg": _color(i.get("bg")),
        "min": round(_num(i.get("min"), 1.2, 6.0, d["intro"]["min"]), 1),
        "text": _texts(i.get("text")),
    }
    if isinstance(c.get("rev"), int):
        out["rev"] = c["rev"]
    return out


def catalog() -> Dict[str, Any]:
    """What the admin app needs to draw its controls."""
    return {"entries": ENTRIES, "faces": FACES, "ambients": AMBIENTS, "presets": PRESETS, "slots": SLOTS, "labels": ITEM_LABELS, "themes": THEME_TEMPLATES, "default_intro": DEFAULT_INTRO_ID}
