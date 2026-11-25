"""
Regression tests of the card validator (run: py -3.13 backend/test_ui_blocks.py). Each case is a bug that was found by testing the bot against the live server.
"""
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import ui_blocks as u

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name, extra if not cond else "")
    if not cond:
        fails.append(name)


def kinds(blocks, allowed="", items=None):
    out, msg = u.resolve_blocks(blocks, items or {}, allowed)
    return [b["k"] for b in out], out, msg


# 1. a card made of buttons only was shown as the whole answer (directions with an empty location and routes)
k, out, msg = kinds([{"type": "location", "title": "קמפוס לב"}, {"type": "routes", "items": []}, {"type": "buttons", "items": [{"label": "עוד"}]}])
check("buttons-only answer is refused", k == [] and "location" in msg and "routes" in msg, msg)

# 2. steps that came with only a detail vanished
k, out, _ = kinds([{"type": "steps", "items": [{"detail": "להיכנס ללב-נט"}, {"text": "ללחוץ על טפסים"}]}])
check("steps with only a detail are kept", k == ["steps"] and len(out[0]["it"]) == 2)

# 3. "שלב 1: ..." printed the number twice
k, out, _ = kinds([{"type": "steps", "items": [{"title": "שלב 1: כניסה"}, {"title": "שלב 2 - בחירה"}]}])
check("step numbers are not repeated in the label", [x["l"] for x in out[0]["it"]] == ["כניסה", "בחירה"])

# 4. the model writes &quot; for a quote
k, out, _ = kinds([{"type": "routes", "items": [{"label": "6", "title": "x", "detail": "תחנת שח&quot;ל"}]}])
check("html entities are decoded", out[0]["it"][0]["d"] == 'תחנת שח"ל')

# 5. the bus badge is a small circle
k, out, _ = kinds([{"type": "routes", "items": [{"label": "קו 39", "title": "x"}]}])
check("bus badge without the word", out[0]["it"][0]["b"] == "39")

# 6. three amounts or a range in giant type broke on a phone
k, out, _ = kinds([{"type": "highlight", "items": [{"label": "a", "value": "8,100 עד 14,300 ש\"ח"}, {"label": "b", "value": "7,530"}, {"label": "c", "value": "8,800"}]}])
check("three amounts become a price list", k == ["price"] and out[0]["s"] == "rows")
k, out, _ = kinds([{"type": "highlight", "items": [{"label": "ליום איחור", "value": "6 ש\"ח"}]}])
check("one short number stays a big number", k == ["highlight"])

# 7. a table of hours becomes an hours block, a table with other columns is left alone
k, out, _ = kinds([{"type": "table", "columns": ["a"], "items": [{"label": "מזכירות", "cells": ["א'-ה'", "9:00-12:00"]}]}])
check("hours table becomes hours", k == ["hours"])

# 8. a long title keeps its words (no dangling "of the")
t, s = u._fit_title("מדור שכר לימוד והנהלת חשבונות של המכללה האקדמית לב בירושלים", "")
check("long title cut at a word, full text in the subtitle", not t.rstrip("…").endswith(" של") and "בירושלים" in s, t)

# 9. a title of 40 characters is fine (the app wraps to two lines)
check("40-character title is untouched", u._fit_title("מדור שכר לימוד והנהלת חשבונות של המכללה", "")[0] == "מדור שכר לימוד והנהלת חשבונות של המכללה")

# 9b. a comparison whose row name was written as the first cell: the data must not slide one column over
k, out, _ = kinds([{"type": "compare", "style": "cards", "columns": ["לב", "טל"], "items": [{"cells": ["קהל יעד", "סטודנטים", "סטודנטיות"]}, {"cells": ["תשלום", "המידע אינו מפורט בטקסט", "קבוע"]}]}])
check("compare: first cell becomes the row name", out[0]["it"][0] == {"l": "קהל יעד", "c": ["סטודנטים", "סטודנטיות"]}, str(out))
check("compare: 'not in the text' is not shown as a cell", out[0]["it"][1]["c"][0] == "לא צוין")

# 9c. a two-field "table" is a price list / facts / timeline on a phone
check("2-field table of prices -> price list", kinds([{"type": "compare", "items": [{"cells": ["בירן", "8,100"]}, {"cells": ["גליקמן", "8,650"]}]}])[0] == ["price"])
check("2-field table of dates -> timeline", kinds([{"type": "table", "columns": ["תאריך", "אירוע"], "items": [{"label": "1/1", "cells": ["תחילת סמסטר"]}]}])[0] == ["timeline"])
check("2-field table of text -> facts", kinds([{"type": "table", "columns": ["מסלול", "משך"], "items": [{"label": "מדעי המחשב", "cells": ["3 שנים"]}]}])[0] == ["facts"])
check("real comparison stays a comparison", kinds([{"type": "compare", "columns": ["a", "b"], "items": [{"label": "משך", "cells": ["3", "4"]}]}])[0] == ["compare"])

# 9d. the display call written as text must be recovered, never shown as raw JSON
import rag_engine as rag
leak = (
    'בדקתי לך:\n\n[[show_ui:[\n'
    ' {"type":"highlight","items":[{"value":"7,530 ש\\"ח","label":"החל מ-"}]},\n'
    ' {"type":"facts","items":[{"label":"א","value":"ב"}]}\n]]]'
)
check("leaked show_ui text is detected", bool(rag._LEAK.search(leak)) and not rag._LEAK.search("שאלה על show_ui בלי סוגריים"))
check("leaked show_ui JSON becomes cards", [b["k"] for b in rag._parse_leaked_ui(leak)] == ["highlight", "facts"])

# 9e. two neighbouring link cards of one link each are one card; a card with its own title stays separate
k, out, _ = kinds([{"type": "links", "items": [{"label": "א", "url": "https://www.jct.ac.il/a"}]}, {"type": "links", "items": [{"label": "ב", "url": "https://www.jct.ac.il/b"}]}])
check("neighbouring untitled cards of one kind merge", k == ["links"] and len(out[0]["it"]) == 2, str(k))
k, out, _ = kinds([{"type": "links", "items": [{"label": "א", "url": "https://www.jct.ac.il/a"}]}, {"type": "links", "title": "עוד", "items": [{"label": "ב", "url": "https://www.jct.ac.il/b"}]}])
check("a card with its own title is not merged", k == ["links", "links"])

# 9f. a long fact value is not cut with dots: it moves to the detail line whole
long_v = "על בסיס מקום פנוי, בכניסה לקמפוס מול ביתן השומר ובחניון התחתון"
k, out, _ = kinds([{"type": "facts", "items": [{"label": "תנאים", "value": long_v}]}])
check("long fact value goes to the detail, whole", out[0]["it"][0].get("d") == long_v and not out[0]["it"][0].get("v"), str(out))

# 10. the validator never crashes on malformed input (13% of random runs crashed before)
random.seed(3)
JUNK = [None, "", 0, 5, 3.5, True, [], {}, "<b>x</b>", "א" * 300, "08:00-17:00", "א'-ה'", ["x", None], {"a": 1}, "קו 6", "[[ui:abc]]"]


def junk():
    return random.choice(JUNK)


out, _m = u.resolve_blocks([{"type": "facts", "items": [{"label": "שכר לימוד", "value": "12,000 ש״ח"}]},
                            {"type": "buttons", "items": [{"label": "איך נרשמים?"}, {"label": "איך נרשמים"}, {"label": "איך נרשמים ?"}, {"label": "כמה זה עולה"}]}], {}, "")
_btn = [b for b in out if b.get("k") == "buttons"]
check("buttons: the same follow-up question three times is one button", len(_btn) == 1 and [r["l"] for r in _btn[0]["it"]] == ["איך נרשמים?", "כמה זה עולה"], str(out))

errors = 0
for _ in range(2000):
    blocks = []
    for _ in range(random.randint(0, 8)):
        b = {"type": random.choice(list(u.STYLES) + ["", None, 5])}
        for f in ("style", "title", "subtitle", "text", "tone", "color", "icon"):
            if random.random() < .5:
                b[f] = junk()
        b["columns"] = junk()
        b["ids"] = junk()
        b["items"] = random.choice([junk(), [random.choice([{f: junk() for f in ("label", "value", "detail", "cells", "days", "time", "url", "kind")}, junk()]) for _ in range(random.randint(0, 9))]])
        blocks.append(b)
    try:
        out, msg = u.resolve_blocks(random.choice([blocks, junk()]), {}, "")
        u.encode_blocks(out)
    except Exception:
        errors += 1
check("fuzz: 2000 malformed inputs, no crash", errors == 0, str(errors))

print("\n" + ("ALL PASSED" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
