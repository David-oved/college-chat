"""Offline regression tests for the pure parts of the knowledge search (no database, no network).
Run: python test_search.py"""
import io
import sys

if hasattr(sys.stdout, "buffer"):                        # the Windows console code page cannot print Russian / Arabic
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

import rag_engine as r

fails = []


def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else " " + str(extra)))
    if not cond:
        fails.append(name)


# ---- follow-up questions are searched together with the question before them
check("follow-up: short Hebrew", r.contextual_query("ומה לגבי ההרשמה אליהם?", "מה המחירים של מעונות רווקים בקמפוס לב") is not None)
check("follow-up: English", "dorms" in (r.contextual_query("and how much does it cost?", "Tell me about the dorms at Lev campus") or ""))
check("follow-up: French", r.contextual_query("et quel est le prix ?", "Parle-moi des résidences") is not None)
check("follow-up: Russian", r.contextual_query("а сколько это стоит", "Расскажи про общежитие") is not None)
check("follow-up: pronoun in a longer question", r.contextual_query("מתי היא פתוחה בימי שישי בערב", "איפה הספרייה") is not None)
check("follow-up: a full new question is NOT mixed with the old one", r.contextual_query("מה ההבדל בין מדעי המחשב להנדסת תוכנה מבחינת נקודות זכות ומשך", "מה המחירים של המעונות") is None)
check("follow-up: nothing before it", r.contextual_query("ומה העלות", "") is None)
check("follow-up: the card marker of the previous message is cut", "[" not in (r.contextual_query("ואיך נרשמים", "שעות הספרייה\n\n[הסטודנט לחץ על פריט]") or ""))
check("follow-up: same text twice -> None", r.contextual_query("שעות הספרייה", "שעות הספרייה") is None)

# ---- merging result lists
a = [{"id": 1, "title": "a"}, {"id": 2, "title": "b"}]
b = [{"id": 2, "title": "b"}, {"id": 3, "title": "c"}, {"id": 4, "title": "d"}]
m = r.merge_chunk_lists(a, b, limit=5)
check("merge: best of each first, no duplicates", [x["id"] for x in m] == [1, 2, 3, 4], m)
check("merge: the limit", len(r.merge_chunk_lists(a, b, limit=2)) == 2)
check("merge: empty lists", r.merge_chunk_lists([], []) == [])

# ---- several topics in one question, in other languages
t = r.split_topics("How do I get to Lev campus and what is the dorm policy?")
check("topics: English two questions", len(t) == 2, t)
t = r.split_topics("Comment venir au campus et quelles sont les règles des résidences ?")
check("topics: French two questions", len(t) == 2, t)
t = r.split_topics("Как добраться до кампуса и какие правила в общежитии?")
check("topics: Russian two questions", len(t) == 2, t)
t = r.split_topics("How much does tuition cost and where can I park")
check("topics: English without a question mark", len(t) == 2, t)
check("topics: one English question stays whole", r.split_topics("How do I register for the second exam date?") == ["How do I register for the second exam date?"])
check("topics: 'and' inside one topic stays whole", len(r.split_topics("What are the library opening hours and rules")) == 1)
check("topics: the student's own data is not searched", r.split_topics("What are my grades and how much is tuition") == ["What are my grades and how much is tuition"], r.split_topics("What are my grades and how much is tuition"))
check("topics: Hebrew is as before", len(r.split_topics("איך מגיעים לקמפוס ומה מדיניות המעונות")) == 2)

# ---- greetings and thanks in every language are not searched
for w in ["היי", "תודה רבה", "thanks a lot", "Hello!", "merci beaucoup", "Привет", "спасибо большое", "شكرا", "مرحبا!", "السلام عليكم", "bonsoir", "good morning"]:
    check("smalltalk: " + w, r._is_smalltalk(w))
for w in ["מה שעות הספרייה", "thanks for the library hours, but when does it close", "спасибо, а где парковка", "Hello, how do I register?"]:
    check("not smalltalk: " + w, not r._is_smalltalk(w))

print("\n" + ("ALL PASSED" if not fails else "FAILED: " + ", ".join(fails)))
sys.exit(1 if fails else 0)
