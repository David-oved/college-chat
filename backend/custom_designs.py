"""
Designs the admin creates (with AI, in free language) without a software update.
A design is a small HTML template + scoped CSS. The server sanitizes both (allow-listed tags and attributes, no scripts, no links, no
external resources, no fixed positioning), fills the template with escaped values, and sends the finished HTML as a block of kind "x".
The app only needs ONE generic renderer for this ("x"), so any number of new designs can be added from the admin app.
What is NOT possible this way: a design that needs new JavaScript behaviour in the app (a new interaction). That one needs an app update.
"""
import html
import json
import re
from html.parser import HTMLParser
from typing import Any, Dict, List, Optional

ALLOWED_TAGS = {"div", "span", "b", "i", "em", "strong", "small", "p", "ul", "ol", "li", "br", "hr", "h3", "h4", "table", "thead", "tbody", "tr", "th", "td"}
VOID = {"br", "hr"}
SKIP_CONTENT = {"script", "style", "iframe", "object", "embed", "svg", "math", "template", "noscript"}
_CLASS = re.compile(r"[^A-Za-z0-9_\- ]")
_ICON = re.compile(r"[^a-z0-9{} ]")
FIELDS = ("title", "subtitle", "text", "icon", "color")
ITEM_FIELDS = ("label", "value", "detail", "title", "text", "icon")
MAX_HTML, MAX_CSS = 6000, 4000


class _Clean(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: List[str] = []
        self.stack: List[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in SKIP_CONTENT:
            self.skip += 1
            return
        if self.skip or tag not in ALLOWED_TAGS:
            return
        keep = []
        for k, v in attrs:
            v = v or ""
            if k == "class":
                keep.append(f'class="{_CLASS.sub("", v)[:80]}"')
            elif k == "data-ic" and tag in ("i", "span"):
                keep.append(f'data-ic="{_ICON.sub("", v.lower())[:30]}"')
        self.out.append(f"<{tag}{(' ' + ' '.join(keep)) if keep else ''}>")
        if tag not in VOID:
            self.stack.append(tag)

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in VOID and tag in ALLOWED_TAGS and not self.skip and self.stack and self.stack[-1] == tag:
            self.out.append(f"</{self.stack.pop()}>")

    def handle_endtag(self, tag):
        if tag in SKIP_CONTENT:
            self.skip = max(0, self.skip - 1)
            return
        if self.skip or tag not in ALLOWED_TAGS or tag in VOID or tag not in self.stack:
            return
        while self.stack:
            t = self.stack.pop()
            self.out.append(f"</{t}>")
            if t == tag:
                break

    def handle_data(self, data):
        if not self.skip:
            self.out.append(html.escape(data, quote=False))

    def result(self) -> str:
        while self.stack:
            self.out.append(f"</{self.stack.pop()}>")
        return "".join(self.out)


def sanitize_template(tpl: str) -> str:
    p = _Clean()
    p.feed(str(tpl or "")[:MAX_HTML])
    p.close()
    return p.result()


_BAD_CSS = re.compile(r"@|url\s*\(|expression|javascript|behavior|<|>|\\|position\s*:\s*fixed|z-index|-moz-binding", re.I)


def scope_css(css: str, cls: str) -> str:
    """Every selector is prefixed with the design's own class, so a design can never restyle the rest of the app."""
    css = re.sub(r"/\*.*?\*/", "", str(css or "")[:MAX_CSS], flags=re.S)
    if _BAD_CSS.search(css):
        raise ValueError("ה-CSS מכיל דבר אסור (@, url, position:fixed, z-index או תו חשוד)")
    rules = []
    for rule in css.split("}"):
        if "{" not in rule:
            continue
        sel, decl = rule.split("{", 1)
        sels = []
        for x in sel.split(","):
            x = x.strip()
            if not x:
                continue
            sels.append(f".{cls}" if x in ("&", ":root", "root") else f".{cls} {x}")
        if sels and decl.strip():
            rules.append(",".join(sels) + "{" + decl.strip() + "}")
    return "".join(rules)


def slug_ok(s: str) -> bool:
    return bool(re.fullmatch(r"x_[a-z0-9]{2,16}", s or ""))


def _e(v: Any, n: int = 200) -> str:
    return html.escape(" ".join(str(v or "").split())[:n], quote=False)


def _icon_html(name: Any) -> str:
    n = re.sub(r"[^a-z]", "", str(name or "").lower())[:12]
    return f'<i data-ic="{n}"></i>' if n else ""


def _fill(tpl: str, d: Dict[str, Any]) -> str:
    def rep(m):
        k = m.group(1)
        if k == "icon":
            return _icon_html(d.get("icon"))
        return _e(d.get(k), 300)
    return re.sub(r"\{\{\s*(\w+)\s*\}\}", rep, tpl)


def render(design: Dict[str, Any], b: Dict[str, Any]) -> str:
    """Fills the (already sanitized) template with the model's values. Values are escaped; the template is the only source of tags."""
    tpl = design["template"]
    items = [x for x in (b.get("items") or []) if isinstance(x, dict)][:12]

    def loop(m):
        body = m.group(1)
        rows = [_fill(body, {k: x.get(k) for k in ITEM_FIELDS}) for x in items if any(x.get(k) for k in ITEM_FIELDS)]
        return "".join(rows)
    t = re.sub(r"\{\{#items\}\}(.*?)\{\{/items\}\}", loop, tpl, flags=re.S)
    return _fill(t, {k: b.get(k) for k in FIELDS})


def build_block(design: Dict[str, Any], b: Dict[str, Any], common: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    h = render(design, b)
    if not re.sub(r"<[^>]+>", "", h).strip():
        return None                                              # nothing to show: no empty card
    cls = "aiX_" + design["id"][2:]
    return {"k": "x", "id": design["id"], "cl": cls, "h": h, "css": scope_css(design.get("css", ""), cls), "ti": "", **common}


def normalize(raw: Dict[str, Any]) -> Dict[str, Any]:
    """What gets stored for a design: validated id/name/description, sanitized template, CSS that was checked."""
    did = str(raw.get("id") or "")
    if not slug_ok(did):
        raise ValueError("מזהה לא תקין")
    tpl = sanitize_template(raw.get("template"))
    if not re.sub(r"<[^>]+>", "", tpl).strip():
        raise ValueError("התבנית ריקה")
    css = re.sub(r"/\*.*?\*/", "", str(raw.get("css") or ""), flags=re.S).strip()
    scope_css(css, "t")                                          # raises if the CSS is not acceptable
    sample = raw.get("sample") if isinstance(raw.get("sample"), dict) else {}
    return {
        "id": did, "name": str(raw.get("name") or did)[:60], "when": str(raw.get("when") or "")[:400],
        "template": tpl, "css": css, "enabled": raw.get("enabled") is not False,
        "sample": {k: sample.get(k) for k in ("title", "subtitle", "text", "icon", "color", "items") if k in sample},
    }


GEN_PROMPT = """אתה מעצב ממשק. המנהל של אפליקציית צ'אט למכללה מתאר עיצוב חדש לכרטיס שהבוט יציג לסטודנטים. החזר אך ורק JSON תקין (בלי הסבר) במבנה:
{"name": "שם קצר בעברית", "slug": "אותיות לועזיות קטנות ומספרים בלבד, 2 עד 12 תווים", "when": "משפט אחד: מתי הבוט צריך להשתמש בעיצוב הזה",
 "template": "HTML של הכרטיס", "css": "CSS של הכרטיס", "sample": {"title": "...", "subtitle": "...", "text": "...", "icon": "star", "color": "blue", "items": [{"label": "...", "value": "...", "detail": "..."}]}}
כללי התבנית: תגיות מותרות בלבד: div span b i em strong small p ul ol li br hr h3 h4 table thead tbody tr th td. מותר רק class ו-data-ic. בלי style, בלי script, בלי קישורים או תמונות.
מקומות למילוי: {{title}} {{subtitle}} {{text}} {{icon}} (מצייר אייקון; או <i data-ic="star"></i> קבוע). לרשימת פריטים: {{#items}}...{{label}} {{value}} {{detail}}...{{/items}} (הקטע בין התגיות חוזר לכל פריט; בתוכו אפשר גם {{title}} {{text}} {{icon}}).
אייקונים מותרים: phone mail clock pin book money warning info user calendar file link key shield heart star school tool chat check bolt globe id food home bus wifi print lock bell gift flag.
כללי CSS: בלי @, url(), position:fixed, z-index. כתוב סלקטורים רגילים לפי ה-class שלך (למשל .row, .big); אפשר "&" לעטיפה החיצונית. הצבע של הכרטיס זמין כמשתנה var(--bc) (הצבע שהבוט בוחר), ועוד: var(--text) var(--muted) var(--line) var(--card). השתמש בהם כדי שזה יתאים למצב כהה ובהיר. הממשק מימין לשמאל (RTL). עיצוב נקי, פינות מעוגלות, ריווח נעים, מתאים לטלפון (רוחב ~340px).
"""


async def generate(description: str, api_key: str, previous: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    from rag_engine import _generate_once
    prompt = GEN_PROMPT + "\nתיאור המנהל:\n" + description.strip()[:1500]
    if previous:
        prompt += "\n\nזה העיצוב הנוכחי (עדכן אותו לפי התיאור למעלה, ושמור על ה-slug):\n" + json.dumps(
            {k: previous.get(k) for k in ("name", "id", "when", "template", "css")}, ensure_ascii=False)[:6000]
    content = await _generate_once(
        [{"role": "user", "parts": [{"text": prompt}]}], api_key, max_output_tokens=3000, call_type="design_gen",
        gen_extra={"responseMimeType": "application/json"})
    txt = "".join(p.get("text", "") for p in (content or {}).get("parts", []) if isinstance(p, dict))
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ValueError("ה-AI לא החזיר עיצוב. נסה לנסח אחרת.")
    raw = json.loads(m.group(0))
    slug = re.sub(r"[^a-z0-9]", "", str(raw.get("slug") or "").lower())[:12] or "new"
    raw["id"] = (previous or {}).get("id") or ("x_" + (slug if len(slug) >= 2 else slug + "x"))
    return normalize(raw)
