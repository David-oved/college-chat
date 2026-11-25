"""Offline tests: the language of a question, the engine's messages in it, Hebrew names in cards, and the reader of a display call written as text. Run: python test_langmsg.py"""
import io
import sys

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import langmsg as L
import leakparse as P

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    if not cond:
        fails.append(name)


for q, want in [("איפה הספרייה", "he"), ("Как добраться до кампуса?", "ru"), ("كم تكلفة المعهد؟", "ar"), ("Combien coûte l'inscription ?", "fr"), ("Où se trouve la bibliothèque ?", "fr"),
                ("¿Cuánto cuesta la residencia?", "es"), ("Wann ist die Bibliothek geöffnet?", "de"), ("How much does a dorm cost", "en"), ("Tuition?", "en"), ("12345", "he"),
                ("איך מגיעים\n\n[הסטודנט לחץ]", "he"), ("hello\n\n[Как]", "en")]:
    check("language of: " + q.split("\n")[0], L.lang_of(q) == want, L.lang_of(q))
for key in L.MSG:
    for lg in ("he", "en", "fr", "es", "de", "ru", "ar"):
        check("message %s in %s" % (key, lg), bool(L.MSG[key].get(lg)))
check("a Spanish question gets a Spanish message", "No pude" in L.msg("no_answer", "¿Cuánto cuesta?"))
check("a Russian question gets a Russian message", "карточках" in L.msg("no_cards", "Сколько стоит?"))
check("a Hebrew question gets a Hebrew message", L.msg("no_answer", "כמה עולה") == L.MSG["no_answer"]["he"])

blocks = [{"k": "list", "s": "cards", "ti": "ספריות המרכז האקדמי לב", "it": [{"l": "קמפוס לב", "d": "גבעת מרדכי, ירושלים"}, {"l": "קמפוס טל", "v": "8,100 ש\"ח"}]}]
en = L.localize_names(blocks, "en")
check("campus names are written in Latin letters", "Lev Academic Center" in en[0]["ti"], en[0]["ti"])
check("Lev and Tal become Lev and Tal", en[0]["it"][0]["l"] == "קמפוס Lev" and en[0]["it"][1]["l"] == "קמפוס Tal", en[0]["it"])
check("the city and the currency", "Givat Mordechai, Jerusalem" in en[0]["it"][0]["d"] and "₪" in en[0]["it"][1]["v"], en[0]["it"])
check("Arabic gets Arabic letters", "ليف" in L.localize_names(blocks, "ar")[0]["it"][0]["l"])
check("Hebrew questions are untouched", L.localize_names(blocks, "he") == blocks)
check("a word that only contains the letters is not changed", L.names_in("בלב", "en") == "בלב" and L.names_in("טלפון", "en") == "טלפון")
check("the fields that are not text are not touched", L.localize_names([{"k": "list", "ic": "לב", "co": "טל"}], "en") == [{"k": "list", "ic": "לב", "co": "טל"}])
check("Hebrew ratio of Hebrew cards is high", L.hebrew_ratio(blocks) > 0.9)
check("Hebrew ratio of English cards is zero", L.hebrew_ratio([{"k": "x", "ti": "Library hours"}]) == 0)
check("Hebrew ratio of a mix is in between", 0.1 < L.hebrew_ratio([{"k": "x", "ti": "Library שעות פתיחה"}]) < 0.9)

orig = [{"k": "price", "s": "rows", "co": "blue", "ti": "מחירים", "it": [{"l": "חדר", "v": "8,100"}, {"l": "מייל", "v": "info@jct.ac.il"}], "a": [{"t": "phone", "v": "02-6751111", "l": "מזכירות"}]}]
tr = [{"k": "PRICE", "s": "x", "co": "red", "ti": "Prices", "it": [{"l": "Room", "v": "9,999"}, {"l": "Mail", "v": "other@x.com"}], "a": [{"t": "טלפון", "v": "000", "l": "Secretariat"}]}]
m = L.merge_translation(orig, tr)
check("translation: text replaced, structure and protected fields kept", m[0]["k"] == "price" and m[0]["co"] == "blue" and m[0]["ti"] == "Prices" and m[0]["it"][0]["l"] == "Room", m)
check("translation: numbers, emails, phones and kinds keep the original", m[0]["it"][0]["v"] == "8,100" and m[0]["it"][1]["v"] == "info@jct.ac.il" and m[0]["a"][0]["v"] == "02-6751111" and m[0]["a"][0]["t"] == "phone", m)
check("translation: an action label is translated", m[0]["a"][0]["l"] == "Secretariat")
check("translation: a different shape keeps the original", L.merge_translation(orig, [{"k": "x"}, {"k": "y"}]) == orig and L.merge_translation(orig, "junk") == orig)
check("translation: accordion answers are text", L.merge_translation([{"q": "שאלה", "a": "תשובה"}], [{"q": "Question", "a": "Answer"}]) == [{"q": "Question", "a": "Answer"}])

t1 = '''show_ui(
  blocks: [
    location(title: "Bibliothèque du campus Lev", subtitle: "Technologique", text: "Campus Lev, Jérusalem", color: "blue", icon: "school"),
    callout(tone: "tip", text: "Le système comprend une bibliothèque."),
    buttons(items: [{"label": "Horaires"}, {"label": "Prix"}])
  ]
)'''
r = P.parse_call(t1)
check("call with name: value arguments", [b["type"] for b in r] == ["location", "callout", "buttons"] and r[0]["title"].startswith("Bibliothèque") and r[2]["items"][1]["label"] == "Prix", r)
t2 = '''show_ui(
  type="compare", title="Стоимость (תשפ״ז)", color="blue",
  items=[ {"label": "Кампус Лев", "cells": ["Биран: 8,100 ₪", "Гликман: 8,650 ₪"]}, {"label": "Таль", "cells": ["7,530 ₪"]} ]
)ל'''
r = P.parse_call(t2)
check("call with name=value arguments is one block", len(r) == 1 and r[0]["type"] == "compare" and len(r[0]["items"]) == 2 and r[0]["items"][0]["cells"][1] == "Гликман: 8,650 ₪", r)
check("escaped quotes and newlines", P.parse_call('show_ui(type="callout", text="a \\"b\\"\\nc")')[0]["text"] == 'a "b"\nc')
check("numbers and booleans", P.parse_call('show_ui(type="progress", items=[{"label": "x", "value": 70.5, "on": true}])')[0]["items"][0] == {"label": "x", "value": 70.5, "on": True})
check("text without a call gives nothing", P.parse_call("שאלה על show_ui בלי סוגריים") == [] and P.parse_call("") == [])
check("a broken call gives nothing, no crash", P.parse_call('show_ui(blocks: [location(title: "x"') == [] and P.parse_call('show_ui(type="a", title=') == [])
check("deep nesting does not crash", P.parse_call("show_ui(" + "a(" * 3000 + ")" * 3000 + ")") == [])
import rag_engine as rag
check("the engine reads a call with name: value arguments as cards", [b.get("k") for b in rag._parse_leaked_ui(t1)] == ["location", "callout", "buttons"])
check("the engine reads a call with name=value arguments as cards", [b.get("k") for b in rag._parse_leaked_ui(t2)] == ["compare"])
import random
random.seed(7)
junk = ['show_ui(', ')', '[', ']', '{', '}', ',', ':', '=', '"x', "'y'", 'type', 'blocks', ' ', '\n', '12', '-', 'a(', '\\']
errs = 0
for _ in range(3000):
    try:
        P.parse_call("".join(random.choice(junk) for _ in range(random.randint(1, 30))))
    except Exception:
        errs += 1
check("fuzz: 3000 broken inputs, no crash", errs == 0, errs)

print("\n" + ("ALL PASSED" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
