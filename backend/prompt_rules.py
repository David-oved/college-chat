"""
The rule builder behind the bot's prompt.
A prompt section is a list of RULES; a rule is a row of CUBES ("כאשר ..." / "ואז ..." / "או ..." / show a design / ask a poll / call a tool ...).
The bot still receives plain text: compile_rule() turns the cubes back into sentences.

Converting the existing prompts is lossless by construction: every piece of the original text becomes a cube that keeps its exact original characters
(`raw`), and compiling unchanged cubes returns those characters as they were. A cube the admin edits drops its `raw` and is written from its fields.

Conditions that the server can check (first message, new student, Moodle connected, language, once per student) are evaluated here and decide whether
the rule is included at all. Conditions about the question itself ("כאשר שואלים על מחיר") are written into the text and the model decides.
"""
import re
import uuid
from typing import Any, Dict, List, Optional

JOIN_WORDS = {"": "", "and": "וגם", "then": "ואז", "or": "או", "else": "אחרת", "except": "חוץ מ"}
MACHINE_WHEN = ("first_message", "new_student", "moodle", "no_moodle", "lang", "once")
CUBE_TYPES = ("when", "say", "design", "poll", "quick", "tool", "length", "tone", "remember", "dont", "must", "example", "info", "text")
LEADS = {"when": "כאשר", "dont": "אל", "must": "חובה:", "example": "דוגמה:", "tone": "השתמש בטון", "remember": "שמור בזיכרון:"}
LENGTHS = {
    "short": "ענה בקצרה, עד שלוש שורות, בטקסט רגיל",
    "long": "ענה בהרחבה ובפירוט",
    "visual": "הצג את התשובה ויזואלית עם show_ui ולא כטקסט",
}
_STR = lambda v, n=600: str(v or "")[:n]


def _id() -> str:
    return uuid.uuid4().hex[:8]


# ------------------------------------------------------------------ conversion of existing text
_NUM = r"(?:\d+[a-z]?[.)]\s*)?"
_WHEN = re.compile(r"^\s*" + _NUM + r"(כש|כאשר|אם |בשאלה|שאלה על|שאלה אישית|מתי |במקרה|בקשת|בקשות|כשהסטודנט)")
_DONT = re.compile(r"^\s*" + _NUM + r"(אל |אסור|אין |לעולם|בלי |אף פעם)|אסור|אל תכתוב|אל תשתמש|אל תציג|אל תשלח|אל תמציא|אל תחזור|אל תענה|אל תקרא")
_MUST = re.compile(r"חובה|חייב|כלל ברזל|כלל קריטי|בלי יוצא מן הכלל|תמיד ")
_EXAMPLE = re.compile(r"^\s*" + _NUM + r"(דוגמה|למשל|הסטנדרט)|->")
_TOOLS = re.compile(r"קרא ל|הכלי|\bget_\w+|search_college_info|remember_about_student|forget_about_student")
_DESIGN = re.compile(r"show_ui|\b(highlight|hero|callout|banner|contact|steps|facts|checklist|accordion|compare|routes|timeline|location)\b")
_POLL = re.compile(r"poll:|\[\[quick|סקר")


def _classify(s: str, first: bool) -> str:
    if first and _WHEN.match(s):
        return "when"
    if _DONT.search(s):
        return "dont"
    if _EXAMPLE.search(s):
        return "example"
    if _MUST.search(s):
        return "must"
    if _POLL.search(s):
        return "poll"
    if _TOOLS.search(s):
        return "tool"
    if _DESIGN.search(s):
        return "design"
    return "info"


def _split_sentences(line: str) -> List[str]:
    """Cuts a line into pieces that tile it exactly (nothing lost): after . ! ? followed by a space, never inside brackets or quotes."""
    cuts, depth, quote = [], 0, False
    for i, ch in enumerate(line):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        elif ch == '"':
            quote = not quote
        elif ch in ".!?" and depth == 0 and not quote and i + 1 < len(line) and line[i + 1] == " " and not re.fullmatch(r"\s*\d+", line[:i + 1]):
            cuts.append(i + 1)
    pieces, prev = [], 0
    for c in cuts + [len(line)]:
        pieces.append(line[prev:c])
        prev = c
    out: List[str] = []
    for p in pieces:
        if out and len(p.strip()) < 14:
            out[-1] += p                                   # too small to be a cube of its own
        else:
            out.append(p)
    if len(out) > 1 and len(out[0].strip()) < 14:
        out[1] = out[0] + out[1]
        out = out[1:]
    return out


def convert_line(line: str) -> List[Dict[str, Any]]:
    pieces = _split_sentences(line)
    first = pieces[0]
    # "כשהסטודנט שואל X - ענה Y": the opening clause becomes a condition cube, the rest an action
    if _WHEN.match(first):
        m = re.search(r"\s[-–]\s|:\s|,\s", first[12:140])
        if m:
            at = 12 + m.start()
            pieces[0:1] = [first[:at], first[at:]]
    cubes = []
    for i, p in enumerate(pieces):
        t = _classify(p, i == 0)
        cubes.append({"id": _id(), "t": t, "lead": "", "text": p.strip(), "join": "", "raw": p, "p": {"kind": "topic"} if t == "when" else {}})
    return cubes


def convert_section(key: str, text: str) -> List[Dict[str, Any]]:
    rules: List[Dict[str, Any]] = []
    prev_end = 0
    for i, m in enumerate(re.finditer(r"[^\n]+", text or "")):
        line = m.group()
        if not line.strip():
            continue
        cubes = convert_line(line)
        title = re.sub(r"^\s*(\d+[a-z]?[.)]\s*)?", "", line.strip())
        title = (title[:46].rsplit(" ", 1)[0] if len(title) > 46 else title).strip(" :-–.,")
        rules.append({"id": f"{key}-{len(rules) + 1}", "section": key, "title": title, "enabled": True, "pre": text[prev_end:m.start()], "post": "", "cubes": cubes})
        prev_end = m.end()
    if rules:
        rules[-1]["post"] = text[prev_end:]
    return rules


# ------------------------------------------------------------------ compile
def _design_text(p: Dict[str, Any]) -> str:
    s = f"הצג עם show_ui בלוק מסוג {_STR(p.get('kind'), 20)}"
    if p.get("style"):
        s += f" בסגנון {_STR(p['style'], 20)}"
    if p.get("color"):
        s += f" בצבע {_STR(p['color'], 10)}"
    if p.get("anim"):
        s += f" עם אנימציה {_STR(p['anim'], 10)}"
    if p.get("note"):
        s += f" ({_STR(p['note'], 200)})"
    return s


def cube_text(c: Dict[str, Any]) -> str:
    """The sentence a cube stands for (used when the cube was written or edited in the builder)."""
    t, p, txt = c.get("t"), c.get("p") or {}, _STR(c.get("text"))
    lead = c.get("lead")
    lead = LEADS.get(t, "") if lead in (None, "") and not c.get("raw") and t in LEADS and not _starts_with_lead(txt) else (lead or "")
    if t == "design":
        return _design_text(p) + (f" - {txt}" if txt else "")
    if t == "poll":
        opts = [_STR(o, 60) for o in (p.get("options") or []) if str(o).strip()][:12]
        mode = "single" if p.get("mode") == "single" else "multi"
        q = _STR(p.get("q"), 160)
        return (f"שלח בסוף התשובה סקר ({'בחירה אחת' if mode == 'single' else 'בחירה מרובה'}) בפורמט [[poll:{mode}|{q}|" + "|".join(opts) + "]]" if opts and q else txt) + (f" - {txt}" if opts and q and txt else "")
    if t == "quick":
        opts = [_STR(o, 40) for o in (p.get("options") or []) if str(o).strip()][:4]
        return ("הוסף כפתורי תשובה מהירה בפורמט [[quick|" + "|".join(opts) + "]]" if len(opts) >= 2 else txt) + (f" - {txt}" if len(opts) >= 2 and txt else "")
    if t == "tool":
        return f"קרא לכלי {_STR(p.get('name'), 60)}" + (f" ({txt})" if txt else "")
    if t == "length":
        return LENGTHS.get(p.get("mode"), "") + (f" ({txt})" if txt else "")
    return (lead + " " if lead else "") + txt


def _starts_with_lead(txt: str) -> bool:
    return bool(re.match(r"^\s*(כש|כאשר|אם |אז |אל |חובה|דוגמה|שמור)", txt))


def machine_ok(c: Dict[str, Any], ctx: Optional[Dict[str, Any]]) -> bool:
    if ctx is None:
        return True
    k, p = (c.get("p") or {}).get("kind"), c.get("p") or {}
    if k == "first_message":
        return bool(ctx.get("first"))
    if k == "new_student":
        return bool(ctx.get("new_student"))
    if k == "moodle":
        return bool(ctx.get("moodle"))
    if k == "no_moodle":
        return not ctx.get("moodle")
    if k == "lang":
        return str(ctx.get("lang") or "he")[:2] == p.get("lang", "he")
    if k == "once":
        return bool(ctx.get("has_user"))
    return True


def rule_active(rule: Dict[str, Any], ctx: Optional[Dict[str, Any]]) -> bool:
    """The server-checkable conditions of the rule, combined left to right with the cubes' own connectors (and / or)."""
    if not rule.get("enabled", True):
        return False
    res: Optional[bool] = None
    for c in rule.get("cubes", []):
        if c.get("t") == "when" and (c.get("p") or {}).get("kind") in MACHINE_WHEN:
            v = machine_ok(c, ctx)
            res = v if res is None else ((res or v) if c.get("join") == "or" else (res and v))
    return True if res is None else res


def compile_rule(rule: Dict[str, Any], ctx: Optional[Dict[str, Any]] = None) -> str:
    if not rule_active(rule, ctx):
        return ""
    parts: List[str] = []
    once = False
    generated = False
    for c in rule.get("cubes", []):
        if c.get("t") == "when" and (c.get("p") or {}).get("kind") in MACHINE_WHEN:
            once = once or (c.get("p") or {}).get("kind") == "once"
            continue                                       # already decided by the server; the bot is not told about it
        if c.get("raw") is not None:
            parts.append(c["raw"])
            continue
        generated = True
        txt = cube_text(c).strip()
        if not txt:
            continue
        word = JOIN_WORDS.get(c.get("join") or "", "")
        if parts:
            parts.append((", " + word + " " if word else " ") + txt)
        else:
            parts.append(txt)
    body = "".join(parts)
    if once:
        tag = (rule.get("title") or rule.get("id") or "").strip()
        body += (f" (בצע זאת פעם אחת בלבד לכל סטודנט: אם בזיכרון שלך כבר יש עובדה שמתחילה ב-\"בוצע: {tag}\" - אל תבצע שוב. "
                 f"אחרי שביצעת, שמור עם remember_about_student, kind=fact, את הנוסח \"בוצע: {tag}\".)")
    if not body.strip():
        return ""
    if generated and body.rstrip()[-1] not in ".!?:)\"]":
        body = body.rstrip() + "."
    return (rule.get("pre") if rule.get("pre") is not None else "\n") + body + (rule.get("post") or "")


def compile_rules(rules: List[Dict[str, Any]], ctx: Optional[Dict[str, Any]] = None) -> str:
    return "".join(compile_rule(r, ctx) for r in rules)


# ------------------------------------------------------------------ validation of what the admin sends
def clean_rule(r: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(r, dict):
        return None
    cubes = []
    for c in (r.get("cubes") or [])[:40]:
        if not isinstance(c, dict) or c.get("t") not in CUBE_TYPES:
            continue
        p = c.get("p") if isinstance(c.get("p"), dict) else {}
        pp: Dict[str, Any] = {}
        for k in ("kind", "style", "color", "anim", "mode", "q", "name", "lang", "note"):
            if p.get(k) is not None:
                pp[k] = _STR(p[k], 200)
        if isinstance(p.get("options"), list):
            pp["options"] = [_STR(o, 80) for o in p["options"][:12]]
        cubes.append({
            "id": _STR(c.get("id") or _id(), 12), "t": c["t"], "lead": _STR(c.get("lead"), 30), "text": _STR(c.get("text"), 4000),
            "join": c.get("join") if c.get("join") in JOIN_WORDS else "", "raw": c["raw"] if isinstance(c.get("raw"), str) and len(c["raw"]) <= 5000 else None, "p": pp,
        })
    if not cubes:
        return None
    return {
        "id": _STR(r.get("id") or _id(), 24), "section": _STR(r.get("section") or "mine", 20), "title": _STR(r.get("title"), 80),
        "enabled": r.get("enabled") is not False, "pre": r["pre"][:20] if isinstance(r.get("pre"), str) else "\n",
        "post": r["post"][:20] if isinstance(r.get("post"), str) else "", "cubes": cubes,
    }


def note_to_rule(n: Dict[str, Any]) -> Dict[str, Any]:
    """An older-style note (text + conditions) becomes a rule: condition cubes, then the text."""
    w = n.get("when") or {}
    cubes: List[Dict[str, Any]] = []

    def cond(kind, **p):
        cubes.append({"id": _id(), "t": "when", "lead": "", "text": "", "join": "and" if cubes else "", "raw": None, "p": {"kind": kind, **p}})
    if w.get("scope") == "first_message":
        cond("first_message")
    if w.get("scope") == "new_student":
        cond("new_student")
    if w.get("moodle") == "yes":
        cond("moodle")
    if w.get("moodle") == "no":
        cond("no_moodle")
    if w.get("lang"):
        cond("lang", lang=w["lang"])
    if w.get("once"):
        cond("once")
    cubes.append({"id": _id(), "t": "say", "lead": "", "text": str(n.get("text") or ""), "join": "then" if cubes else "", "raw": None, "p": {}})
    return {"id": str(n.get("id") or _id()), "section": "mine", "title": str(n.get("title") or ""), "enabled": n.get("enabled") is not False, "pre": "\n", "post": "", "cubes": cubes}


# ------------------------------------------------------------------ ready-made rules the admin can drop in
def _t(t, text="", join="", **p):
    return {"id": _id(), "t": t, "lead": "", "text": text, "join": join, "raw": None, "p": p}


def templates() -> List[Dict[str, Any]]:
    return [
        {"name": "שאלה על מחיר או שכר לימוד", "desc": "מספר גדול, פרטים והערה חשובה", "rule": {"title": "שאלות מחיר", "cubes": [
            _t("when", "שואלים על מחיר, שכר לימוד או תשלום", kind="topic"), _t("design", "", "then", kind="highlight", style="big", color="orange", anim="pop"),
            _t("design", "פרטי מה כלול ותנאים", "then", kind="facts"), _t("design", "מה חשוב לדעת", "then", kind="callout", color="orange")]}},
        {"name": "הודעה ראשונה: סקר קורסים", "desc": "בפעם הראשונה בשיחה, פעם אחת לכל סטודנט", "rule": {"title": "סקר קורסים", "cubes": [
            _t("when", kind="first_message"), _t("when", "", "and", kind="moodle"), _t("when", "", "and", kind="once"),
            _t("poll", "", "then", kind="", mode="multi", q="אילו מהקורסים האלה אתה לומד עכשיו?", options=["כולם"])]}},
        {"name": "סטודנט חדש: קבלת פנים", "desc": "פתיחה חמה כשעוד אין זיכרון", "rule": {"title": "קבלת פנים", "cubes": [
            _t("when", kind="new_student"), _t("say", "הצג את עצמך בשם אורי במשפט אחד חם, ואז הצע בכפתורים 3 דברים שאפשר לשאול", "then"), _t("length", "", "and", mode="short")]}},
        {"name": "איש קשר: כרטיס ושעות", "desc": "איש קשר עם חיוג ושעות קבלה", "rule": {"title": "מי אחראי / איך פונים", "cubes": [
            _t("when", "שואלים למי פונים, מי אחראי או איך יוצרים קשר", kind="topic"), _t("design", "", "then", kind="contact", color="teal"),
            _t("design", "אם יש שעות קבלה", "and", kind="hours", style="week"), _t("design", "טיפ קצר", "and", kind="callout", color="purple")]}},
        {"name": "אין מידע: הפניה בנימוס", "desc": "כשהחיפוש לא החזיר כלום", "rule": {"title": "אין מידע", "cubes": [
            _t("when", "החיפוש לא מצא מידע על הנושא", kind="topic"), _t("say", "אמור בקצרה שאין לך מידע רשמי על כך והפנה למזכירות", "then"), _t("dont", "תמציא מידע", "and")]}},
        {"name": "אנגלית: ענה באנגלית", "desc": "כשהאפליקציה באנגלית", "rule": {"title": "אנגלית", "cubes": [
            _t("when", kind="lang", lang="en"), _t("say", "ענה באנגלית, כולל תוכן הכרטיסים", "then")]}},
    ]
