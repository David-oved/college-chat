"""
Uri, the face of the app's AI part, as DATA. The picture itself is drawn by ONE renderer in the app (chat.js); this module holds the settings that renderer reads:
body shape and colour, eyes, cheeks, the outfit worn by default, outfits that switch on by date (holidays, exam season), which items the students may put on him,
which ones they unlock by playing, how alive he is (blinking, looking at the finger, bouncing) and what he says when he is poked.
The admin changes it in the admin app; the app fetches it (small, public, cached) and every place Uri appears (floating button, chat avatars, welcome screen,
the guided tour, the demo video, the play corner) is redrawn from it, with no software update.
New drawing parts (a new accessory shape) are the only thing that needs an app update; everything in this file is chosen from what the app can already draw.
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
MOODS = ("happy", "grin", "wow", "think", "sleep", "love")

DEFAULT: Dict[str, Any] = {
    "shape": "bubble", "body": "#ffffff", "cheek": "#ff8fb1", "eyes": "big", "cheeks": True, "spark": True,
    "outfit": {"hat": "", "face": "", "neck": ""},
    "seasons": [],
    "wardrobe": True,
    "available": ["gradcap", "glasses", "sunglasses", "bow", "headphones", "partyhat", "crown", "sprout", "ears", "scarf"],
    "unlocks": {"sunglasses": 10, "crown": 25},
    "behavior": {"blink": True, "look": True, "bounce": True, "react": True, "play": True, "sleepy": True},
    "phrases": ["הי, זה דגדג!", "אני כאן אם צריך עזרה", "עוד פעם? אני לא מתלונן", "יש שאלה? אני מוכן", "אל תשכח ללמוד, וגם לנוח", "מה נשמע?", "בא לך לשאול משהו?", "תלבישו לי משהו יפה"],
}
_HEX = re.compile(r"^#[0-9a-fA-F]{6}$")
_MD = re.compile(r"^(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$")


def _outfit(o: Any) -> Dict[str, str]:
    o = o if isinstance(o, dict) else {}
    return {slot: (o.get(slot) if o.get(slot) in items else "") for slot, items in SLOTS.items()}


def clean(cfg: Any) -> Dict[str, Any]:
    """Whatever arrives (admin form, an old saved version) becomes a complete, safe configuration; unknown values fall back to the defaults."""
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
    ph = [str(x).strip()[:70] for x in (c.get("phrases") if isinstance(c.get("phrases"), list) else d["phrases"]) if str(x).strip()][:14]
    out["phrases"] = ph or d["phrases"]
    return out
