"""Offline tests for Uri's settings (mascot.py). Run: python test_mascot.py"""
import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import mascot as m

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    if not cond:
        fails.append(name)


d = m.clean({})
check("defaults: version 3 and a valid default animation", d["v"] == 3 and m.valid_anim(d["intro"]["fixed"]))
check("defaults are stable (clean of clean is the same)", m.clean(d) == d)
check("every ready-made name is a valid combination", all(m.valid_anim(p[0]) and p[1] for p in m.PRESETS), [p for p in m.PRESETS if not m.valid_anim(p[0])])
check("ready-made ids are unique", len({p[0] for p in m.PRESETS}) == len(m.PRESETS))
check("dozens of ready-made animations", len(m.PRESETS) >= 36, len(m.PRESETS))
check("the default animation is a wink", m.DEFAULT_INTRO_ID.split("-")[1] == "wink")
check("a wink is among the faces", "wink" in m.FACES and "winkl" in m.FACES and "double" in m.FACES)
check("many more combinations than ready-made names", len(m.ENTRIES) * len(m.FACES) * len(m.AMBIENTS) > 500)

x = m.clean({"v": 3, "intro": {"fixed": "bogus", "mode": "weird", "pool": ["pop-wink-none", "bad", "zoom-wow-snow", "pop-wink-none"], "min": 99, "bg": "red", "text": {"he": ["שלום", "", "  "], "xx": ["a"]}}})
check("an unknown animation falls back", x["intro"]["fixed"] == m.DEFAULT_INTRO_ID)
check("an unknown mode falls back", x["intro"]["mode"] == "fixed")
check("the pool keeps only valid ids, without repeats", x["intro"]["pool"] == ["pop-wink-none", "zoom-wow-snow"], x["intro"]["pool"])
check("the time is limited", x["intro"]["min"] == 6.0)
check("a bad colour is dropped", x["intro"]["bg"] == "")
check("opening lines: blanks removed, unknown languages dropped", x["intro"]["text"] == {"he": ["שלום"], "en": [], "fr": []}, x["intro"]["text"])

t = m.clean({"v": 3, "themes": [{"label": "חורף", "from": "12-01", "to": "02-28", "body": "#dbeeff", "outfit": {"neck": "scarf", "hat": "nope"}, "intro": "rise-wink-snow", "text": {"he": ["חורף"]}},
                                  {"label": "bad", "from": "13-01", "to": "02-28"}, "junk", None]})
check("a theme is kept, a bad one dropped", len(t["themes"]) == 1 and t["themes"][0]["label"] == "חורף")
check("a theme's outfit is checked against the real items", t["themes"][0]["outfit"] == {"hat": "", "face": "", "neck": "scarf"})
check("a theme keeps its animation and colour", t["themes"][0]["intro"] == "rise-wink-snow" and t["themes"][0]["body"] == "#dbeeff")
check("the theme templates are all valid", all(m.valid_anim(x["intro"]) and m.clean({"v": 3, "themes": [x]})["themes"] for x in m.THEME_TEMPLATES))

old = m.clean({"v": 2, "body": "#ffe3d1", "spark": False, "outfit": {"hat": "gradcap"}, "look": {"tag": "Uri"}, "seasons": [{"label": "מבחנים", "from": "01-01", "to": "01-31", "outfit": {"hat": "gradcap"}}],
               "anim": {"enabled": ["jump"]}, "phrases": {"open": ["x"]}})
check("version 2 keeps colour, star, tag, outfit and seasons", old["body"] == "#ffe3d1" and old["spark"] is False and old["tag"] == "Uri" and old["outfit"]["hat"] == "gradcap" and old["themes"][0]["label"] == "מבחנים", old)
check("version 2 movements and phrases are gone", "anim" not in old and "phrases" not in old and "triggers" not in old)
check("settings of no version are ignored", m.clean({"body": "#000000"})["body"] == "#ffffff")
check("junk in, defaults out", m.clean("junk") == d and m.clean(None) == d and m.clean([1, 2]) == d)
check("rev is kept", m.clean({"v": 3, "rev": 5})["rev"] == 5)
check("the tag is limited to 4 characters", len(m.clean({"v": 3, "tag": "abcdefgh"})["tag"]) == 4)

print("\n" + ("ALL PASSED" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
