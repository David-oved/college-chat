"""
ערכת רכיבי תצוגה שה-AI מרכיב בעצמו: הוא קורא ל-show_ui ומחליט איזה מידע להציג, באיזה סוג תצוגה, סגנון, צבע ואנימציה.
- נתוני Moodle (מטלות, ציונים, קורסים) לא עוברים דרך המודל: הכלים נותנים לכל פריט מזהה (d1, g2, c3...) והמודל רק מצביע - כך אי אפשר שימציא תאריך.
- מידע מהמאגר (טלפונים, מיילים, שעות, נהלים) המודל כותב בעצמו, אבל השרת מוודא שכל מייל, טלפון וקישור שהוא מצרף באמת מופיעים בחומר שעמד לרשותו
  בתשובה הזו (מאגר הידע, תוצאות הכלים, השאלה) - מה שלא נמצא שם נזרק, כך שלא יוצג כפתור חיוג למספר מומצא.
- השרת מאמת ומנקה הכול (סוגים וסגנונות מותרים, אורכים, צבעים, אייקונים), והאפליקציה מציירת.
"""
import base64
import json
import re
from typing import Any, Dict, List, Tuple

import custom_designs
import prompt_store

# kind -> allowed styles (first one is the default)
STYLES = {
    "deadlines": ("tickets", "timeline", "focus"),
    "grades": ("gauges", "list"),
    "courses": ("chips", "list"),
    "contact": ("card", "row", "big"),
    "hours": ("table", "week", "compact"),
    "steps": ("numbered", "timeline", "cards"),
    "facts": ("list", "grid", "chips"),
    "highlight": ("big", "pill", "row"),
    "list": ("bullets", "icons", "numbers", "cards"),
    "links": ("list", "chips", "buttons"),
    "timeline": ("vertical", "chips"),
    "price": ("tags", "rows", "big"),
    "summary": ("card", "plain", "glass"),
    "heading": ("plain", "accent", "centered"),
    "banner": ("bar", "card"),
    "table": ("plain", "striped"),
    "compare": ("table", "cards"),
    "tags": ("chips",),
    "checklist": ("boxes",),
    "accordion": ("faq",),
    "tabs": ("tabs",),
    "location": ("card",),
    "quote": ("quote", "definition"),
    "callout": ("box",),
    "stats": ("row",),
    "progress": ("bars",),
    "buttons": ("quick",),
    "hero": ("gradient", "soft"),
    "routes": ("badges", "pills"),
}
ITEM_KINDS = {"deadlines": "d", "grades": "g", "courses": "c"}
TONES = ("info", "warn", "success", "tip", "danger")
COLORS = ("blue", "green", "orange", "red", "purple", "teal", "pink", "gray")
ANIMS = ("fade", "rise", "pop", "slide", "glow", "none")
ICONS = ("phone", "mail", "clock", "pin", "book", "money", "warning", "info", "user", "calendar", "file", "link", "key", "shield", "heart",
         "star", "school", "tool", "chat", "check", "bolt", "globe", "id", "food", "home", "bus", "wifi", "print", "lock", "bell", "gift", "flag")
ACTION_KINDS = ("email", "phone", "whatsapp", "link", "address", "text")

_MAX_ITEMS = {"contact": 6, "hours": 10, "steps": 10, "facts": 10, "highlight": 4, "list": 12, "links": 8, "timeline": 10, "price": 8,
              "summary": 8, "tags": 14, "checklist": 12, "accordion": 8, "tabs": 5, "compare": 8, "table": 12, "quote": 1,
              "stats": 4, "progress": 8, "buttons": 4, "hero": 3, "routes": 10}

UI_ADDENDUM = """

=== תצוגה חזותית (show_ui) - אתה המעצב ===
כלל ברזל, בלי יוצא מן הכלל, לא משנה מה קורה: כל תשובה שמכילה יותר משלוש שורות טקסט - מוצגת ויזואלית עם show_ui, אף פעם לא כקיר טקסט. וגם תשובה קצרה יותר אבל מורכבת (רשימה, כמה פרטים, מספרים, תאריכים או שעות, סכומים, שלבים, איש קשר, השוואה, תנאים) - ויזואלית. טקסט רגיל מותר רק לתשובה פשוטה של עד שלוש שורות קצרות (תודה, אישור, כן/לא, שאלת הבהרה, סירוב מנומס, הודעת שגיאה). אם התחלת לחשוב על תשובה ארוכה בטקסט - עצור וקרא ל-show_ui. השרת בודק כל תשובה, ותשובה ארוכה או מורכבת שנכתבה בטקסט מומרת אוטומטית לתצוגה ויזואלית (זה איטי ופחות טוב), אז חסוך את זה וקרא ישר ל-show_ui.
הסטודנטים לא אוהבים לקרוא. אתה בוחר סוג תצוגה, סגנון, צבע, אייקון ואנימציה לכל בלוק, וכמה בלוקים שרוצים בתשובה אחת (בדרך כלל 2 עד 5). תהיה יצירתי, קצר ובהיר: מעט מילים, הרבה מבנה; בכל בלוק עד שורה או שתיים. אחרי show_ui התשובה נגמרת: הכרטיסים הם התשובה, אל תכתוב אחריהם כלום ואל תחזור בטקסט על מה שמוצג. אם צריך משפט פתיחה - הוא חייב להיות לפני הקריאה ל-show_ui, קצר מאוד.

איך מרכיבים תשובה טובה (מתכונים - הצע שילוב, לא בלוק בודד):
1. התשובה עצמה קודם: בלוק ראשון נותן את התשובה במבט אחד (highlight או price או banner או callout), ורק אחריו הפרטים. הסטודנט צריך להבין מה התשובה לפני שקרא משהו.
2. שאלה על איש קשר / מי אחראי / איך פונים: contact (עם פעולות חיוג, מייל, וואטסאפ) + hours אם יש שעות קבלה + callout קצר עם טיפ (למשל "עדיף לפנות במייל").
3. שעות פעילות: hours (style week) + callout אם יש חריג (חגים, ימי שישי).
4. תהליך, "איך עושים X" / רישום / ערעור / בקשה: banner או callout קצר (מה זה ובשביל מה) + steps (style timeline או numbered) + callout tone=warn על המלכודת או המועד + contact אם יש למי לפנות.
5. עלויות ותשלומים / שכר לימוד / מלגות: highlight או price (הסכום הגדול) + facts (מה כלול / תנאים) + callout (מה חשוב לדעת) .
6. השוואה בין אפשרויות (מסלולים, מעונות, קמפוסים): compare (style cards בטלפון) או tabs - לא פסקאות.
7. מה להביא / להכין / רשימת מסמכים: checklist.
8. תאריכים ולוח שנה: timeline (style chips או vertical) + highlight לתאריך הקרוב.
9. כמה שאלות-משנה או הרבה מידע: accordion (שאלות ותשובות מתקפלות) או tabs. נהלים ומדיניות: summary + steps + callout.
10. מיקום והגעה: location + steps עם הוראות הגעה + hours.
10b. מבחנים של הסטודנט: highlight עם ספירה לאחור למבחן הקרוב + deadlines (ids מהכלי get_my_exams) + callout קצר על מה שחשוב (נסגר היום, ניסיון אחד, ניסיון פתוח) + buttons להמשך.
11. מידע אישי מ-Moodle: deadlines / grades / courses לפי ids, ומעליהם highlight או stats (למשל כמה מטלות השבוע) ו-callout אדום על איחור.
12. כשהמידע ארוך: heading בין הקבוצות (בקצרה) ו-summary של שורה אחת בסוף. בסוף תשובה עשירה אפשר buttons עם 2-3 המשכים טבעיים ("עוד פרטים", "איך נרשמים").
שלמות, בלי יוצא מן הכלל: אין כרטיס ריק. בכל שורה ב-facts / price / stats / progress / highlight חייב להיות value אמיתי (או detail ב-facts) מהחומר שקיבלת. אם אין לך מידע על משהו - אל תכלול אותו בכלל (אל תשלח שורה עם label בלבד, אל תכתוב "חניה" בלי לומר מה). כל בלוק מכיל תוכן ממשי, ואם הוא לא - מחק אותו. לפני ששולחים: עבור על הבלוקים ובדוק שכל פרט נכון ושנמצא בחומר.
הסטנדרט לעיצוב - ככה נראית תשובה יפה ומושכת: "איך מגיעים לקמפוס לב?" -> [location(title:"קמפוס לב", subtitle:"גבעת מרדכי, ירושלים", text:"כתובת מלאה מהחומר", color:green), routes(style:badges, title:"קווי אוטובוס", icon:bus, color:blue, items:[{label:"6", title:"מגבעת שאול", detail:"ירידה: שח"ל/הלר"},{label:"5", title:"מבית הכרם", detail:"ירידה: גבעת מרדכי/בייט"},{label:"39", title:"מהר חוצבים", detail:"ירידה: גבעת מרדכי/בייט"}]), banner(tone:tip, color:purple, icon:info, text:"טיפ קצר ושימושי אחד מהחומר")]. שים לב: מעט מילים, אייקון וצבע בכל בלוק, מבנה במקום פסקאות, וקצר: כל שורה בתוך בלוק עד כמה מילים. תמיד בנה כך - ולעולם לא רשימה ארוכה אחת של נקודות.
פתיחה בולטת: כשיש לתשובה נושא ברור (קמפוס, שירות, מלגה, מסלול), פתח ב-hero או ב-location או ב-highlight ורק אחר כך פרטים. בקשת מעבר בין כמה אפשרויות (קווים, קמפוסים, מסלולים) -> routes.
אנימציה וצבע: anim=rise כברירת מחדל, pop למספר גדול (highlight), glow לדבר דחוף או חשוב מאוד, slide לצעדים. צבע לפי משמעות. אל תחזור על אותו סוג בלוק כשיש סוג מתאים יותר (רשימת bullets היא הברירה האחרונה, לא הראשונה). בלי טקסט כפול: מה שבכרטיס לא חוזר בטקסט.
דוגמה: "כמה עולה שכר לימוד ואיך משלמים?" -> blocks: [highlight(items:[{value:"...",label:"שכר לימוד לשנה"}], anim:pop, color:blue), facts(items:[{label:"תשלומים",value:"..."},{label:"הנחה",value:"..."}]), steps(style:timeline, items:[{title:"שלב 1"},{title:"שלב 2"}]), callout(tone:warn, text:"שים לב: ..."), buttons(items:[{label:"איך מגישים בקשה למלגה"}])].
דוגמה: "מי אחראי על הרישום ומתי פתוח?" -> blocks: [contact(title:"...", subtitle:"...", items:[{kind:phone,value:"..."},{kind:email,value:"..."}], color:teal), hours(style:week, items:[...]), callout(tone:tip, text:"...")].

מידע מ-Moodle (מטלות, ציונים, קורסים): שלוף עם הכלי המתאים; כל פריט בתוצאה מגיע עם מזהה בסוגריים כמו [d3]. בבלוק מצביעים על ids בלבד.
- deadlines (ids של d): style tickets / timeline / focus (המטלה הדחופה גדולה). grades (ids של g): gauges / list. courses (ids של c): chips / list.

מידע מהמאגר ומהכלים (אתה כותב את התוכן, קצר): כל מייל, טלפון וקישור חייבים להיות בדיוק כפי שהופיעו בחומר שקיבלת, אחרת השרת ימחק אותם. אל תמציא פרטים.
- contact: איש קשר או מדור. title=שם, subtitle=תפקיד, text=שורה נוספת. items=פעולות {kind: email|phone|whatsapp|link|address, value, label?} - הסטודנט לוחץ ומתקשר/שולח מייל/פותח וואטסאפ או מעתיק. style card/row/big.
- hours: שעות פעילות. items {days, time, label?}, למשל days="א'-ה'" time="8:15-22:00". text=הערה. האפליקציה מחשבת "פתוח עכשיו". style table / week (שבוע עם היום מודגש) / compact.
- steps: תהליך צעד אחר צעד. items {title, detail?}. style numbered / timeline / cards.
- facts: זוגות שם-ערך. items {label, value, icon?}. style list / grid / chips.
- highlight: מספר או סכום גדול. items {value, label, detail?} (עד 4). style big / pill / row. מתאים ל"6 ש"ח ליום איחור".
- price: סכומים. items {label, value, detail?}. style tags / rows / big.
- callout: הדגשה. text, tone info/warn/success/tip/danger, title?, icon?. banner: פס הודעה (text, tone, icon?, style bar/card).
- list: רשימה. items {label, detail?, icon?}. style bullets / icons / numbers / cards.
- checklist: מה להביא או להכין (הסטודנט מסמן). items {label, detail?}.
- accordion: שאלות ותשובות מתקפלות. items {q, a} (עד 8).
- tabs: לשוניות (למשל קמפוס לב / טל / לוסטיג). items {title, text} (עד 5).
- compare / table: טבלה. columns=[כותרות], items {label (כותרת שורה), cells:[...]}. compare style table/cards.
- links: קישורים ללחיצה. items {label, detail?, url, icon?}. style list / chips / buttons.
- timeline: ציר תאריכים כללי (לא מטלות). items {date, title, detail?}. style vertical / chips.
- location: כתובת עם ניווט (Waze, מפות, העתקה). title, text=כתובת, subtitle?.
- quote: ציטוט או הגדרה: text, subtitle=מקור/מונח. style quote / definition.
- summary: סיכום קצר: title, text, items {label} כנקודות. style card / plain / glass.
- heading: כותרת מקטע: title, text=כותרת משנה. style plain / accent / centered. tags: תגיות קצרות: items {label}.
- hero: פתיחה בולטת ומרשימה לתשובה (כרטיס צבעוני מלא עם אייקון גדול). title=הנושא, subtitle=שורת משנה, text=פרט קצר, icon, color, items עד 3 מספרים מרכזיים {value, label} (למשל "8,100 ש"ח" / "מחיר לשנה"). style gradient (ברירת מחדל, בולט מאוד) / soft (עדין). מתאים לשאלה על מקום, שירות, מסלול או נושא שיש לו שם ומספר מפתח. בלוק אחד, בראש התשובה.
- routes: קווים, אפשרויות או נתיבים עם תג צבעוני גדול לכל שורה: items {label=התג (מספר קו, "א'", "1"), title, detail?}. style badges / pills. מתאים לקווי אוטובוס (label="6", title="מגבעת שאול", detail="לרדת בתחנת שח"ל/הלר"), לקמפוסים, למסלולי לימוד, לשלבים ממוספרים צבעוניים.
- stats (מספרים), progress (פסי אחוזים 0-100), buttons (כפתורי המשך ששולחים הודעה בשם הסטודנט; items {label, value?}), steps עם items {label} פשוט.
לכל בלוק אפשר: color (blue/green/orange/red/purple/teal/pink/gray), icon (phone/mail/clock/pin/book/money/warning/info/user/calendar/file/link/key/shield/heart/star/school/tool/chat/check/bolt/globe/id/food/home/bus/wifi/print/lock/bell/gift/flag), anim (fade/rise/pop/slide/glow - כניסה מונפשת, none לבלי), title.
עיצוב: בחר צבע לפי משמעות (אדום לאזהרה, ירוק להצלחה, כתום לתשלום/דחיפות, כחול למידע, סגול לטיפ) ושלב בלוקים: למשל banner קצר, ואחריו contact, ואחריו steps. מהירות: אם צריך כמה כלים - קרא להם כולם באותו צעד במקביל. אל תשתמש בשם של אדם או במספר שלא מופיעים בחומר.
"""

# The long instruction is kept in sections the admin can edit (prompt_store); UI_ADDENDUM stays the built-in whole.
def _split_ui(full: str) -> Dict[str, str]:
    marks = [("ui_recipes", "איך מרכיבים תשובה טובה"), ("ui_standard", "הסטנדרט לעיצוב"), ("ui_moodle", "מידע מ-Moodle (מטלות"), ("ui_reference", "מידע מהמאגר ומהכלים")]
    out: Dict[str, str] = {}
    cur, buf = "ui_iron", []
    for ln in full.splitlines(keepends=True):
        for key, m in marks:
            if ln.startswith(m):
                out[cur] = "".join(buf)
                cur, buf = key, []
                break
        buf.append(ln)
    out[cur] = "".join(buf)
    return out


UI_PARTS = _split_ui(UI_ADDENDUM)
for _k, _v in UI_PARTS.items():
    prompt_store.register(_k, _v)
UI_KEYS = ("ui_iron", "ui_recipes", "ui_standard", "ui_moodle", "ui_reference")


def ui_text() -> str:
    """The display instructions as the admin set them (sections switched off are left out), plus the admin's design controls."""
    return "".join(prompt_store.text(k) for k in UI_KEYS) + prompt_store.design_text()


_ITEM = {
    "type": "OBJECT",
    "properties": {
        "label": {"type": "STRING", "description": "תווית / שם"},
        "value": {"type": "STRING", "description": "ערך (מספר, מייל, טלפון, טקסט)"},
        "detail": {"type": "STRING", "description": "פירוט קצר"},
        "title": {"type": "STRING"},
        "text": {"type": "STRING"},
        "kind": {"type": "STRING", "description": "לפעולת contact: email | phone | whatsapp | link | address"},
        "url": {"type": "STRING", "description": "קישור https"},
        "icon": {"type": "STRING"},
        "q": {"type": "STRING", "description": "שאלה (accordion)"},
        "a": {"type": "STRING", "description": "תשובה (accordion)"},
        "days": {"type": "STRING", "description": "ימים, למשל א'-ה'"},
        "time": {"type": "STRING", "description": "שעות, למשל 8:15-22:00"},
        "date": {"type": "STRING"},
        "cells": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "תאי שורה בטבלה"},
    },
}

UI_TOOLS = [{"functionDeclarations": [{
    "name": "show_ui",
    "description": "מציג לסטודנט רכיבים חזותיים (כרטיסי איש קשר עם חיוג ומייל, שעות פעילות, שלבים, טבלאות, כרטיסי מטלות ועוד) במקום טקסט. מטלות/ציונים/קורסים לפי מזהים שהכלים החזירו; מידע אחר ישירות.",
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "blocks": {
                "type": "ARRAY",
                "description": "רשימת הבלוקים להצגה, לפי הסדר",
                "items": {
                    "type": "OBJECT",
                    "properties": {
                        "type": {"type": "STRING", "description": "deadlines grades courses contact hours steps facts highlight price callout banner list checklist accordion tabs compare table links timeline location quote summary heading tags stats progress buttons hero routes"},
                        "style": {"type": "STRING", "description": "סגנון לפי סוג הבלוק (ראה הנחיות)"},
                        "title": {"type": "STRING", "description": "כותרת (או שם איש קשר)"},
                        "subtitle": {"type": "STRING", "description": "כותרת משנה (או תפקיד)"},
                        "ids": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "מזהי פריטי Moodle: d1, g2, c3"},
                        "text": {"type": "STRING", "description": "טקסט הבלוק (callout, banner, quote, summary, location=כתובת, הערה לשעות)"},
                        "tone": {"type": "STRING", "description": "info | warn | success | tip | danger"},
                        "color": {"type": "STRING", "description": "blue green orange red purple teal pink gray"},
                        "icon": {"type": "STRING", "description": "שם אייקון מהרשימה"},
                        "anim": {"type": "STRING", "description": "fade rise pop slide glow none"},
                        "columns": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "כותרות עמודות לטבלה/השוואה"},
                        "items": {"type": "ARRAY", "items": _ITEM, "description": "פריטי הבלוק"},
                    },
                    "required": ["type"],
                },
            }
        },
        "required": ["blocks"],
    },
}]}]


# the small models now and then slip an Arabic letter into a Hebrew word ("מחيري"): in text that is clearly Hebrew those letters are put back
_AR2HE = str.maketrans("يىكلمنهورتبدسعفصقشجحخطزا", "ייכלמנהורתבדסעפצקשגחכטזא")
_HEB = re.compile(r"[א-ת]")
_ARB = re.compile(r"[؀-ۿ]")


def _s(v: Any, n: int) -> str:
    t = " ".join(str(v if v is not None else "").split())
    if _ARB.search(t) and len(_HEB.findall(t)) > len(_ARB.findall(t)):
        t = t.translate(_AR2HE)
    return t[:n]


def _digits(s: str) -> str:
    return re.sub(r"\D", "", s or "")


_EMAIL = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}$")
_TRUSTED_HOSTS = ("jct.ac.il",)


def _verified(kind: str, value: str, allowed: str) -> bool:
    """מייל/טלפון/קישור חייבים להופיע בחומר שהיה למודל. (בלי חומר מקור - לא מאשרים כלום.)"""
    allowed_l = (allowed or "").lower()
    v = value.strip()
    if kind == "email":
        return bool(_EMAIL.match(v)) and v.lower() in allowed_l
    if kind in ("phone", "whatsapp"):
        d = _digits(v)
        if len(d) < 3:
            return False
        if v.startswith("*") and v[1:].isdigit():       # שלוחה כמו *3239
            return v in allowed
        return d in _digits(allowed) if len(d) >= 6 else (v in allowed)
    if kind == "link":
        m = re.match(r"^https://([A-Za-z0-9.\-]+)(/[^\s]*)?$", v)
        if not m:
            return False
        host = m.group(1).lower()
        return v.lower() in allowed_l or any(host == h or host.endswith("." + h) for h in _TRUSTED_HOSTS)
    return True


def _clean_actions(raw: Any, allowed: str) -> List[Dict[str, str]]:
    out: List[Dict[str, str]] = []
    for x in (raw or [])[:6]:
        if not isinstance(x, dict):
            continue
        kind = _s(x.get("kind"), 10).lower()
        value = _s(x.get("value") or x.get("url"), 160)
        if kind not in ACTION_KINDS or not value:
            continue
        if kind == "text":
            out.append({"t": "text", "v": value, "l": _s(x.get("label"), 30)})
            continue
        if kind == "address":
            out.append({"t": "address", "v": value, "l": _s(x.get("label"), 30)})
            continue
        if _verified(kind, value, allowed):
            out.append({"t": kind, "v": value, "l": _s(x.get("label"), 30)})
    return out


def _common(b: Dict[str, Any]) -> Dict[str, Any]:
    d: Dict[str, Any] = {}
    color = _s(b.get("color"), 8).lower()
    icon = _s(b.get("icon"), 12).lower()
    anim = _s(b.get("anim"), 8).lower()
    if color in COLORS:
        d["co"] = color
    if icon in ICONS:
        d["ic"] = icon
    if anim in ANIMS:
        d["an"] = anim
    return d


def _icon(x: Any) -> str:
    i = _s(x, 12).lower()
    return i if i in ICONS else ""


def resolve_blocks(blocks: Any, items: Dict[str, Dict[str, Any]], allowed: str = "", preview: bool = False) -> Tuple[List[Dict[str, Any]], str]:
    """מאמת את מה שהמודל ביקש. מחזיר (בלוקים נקיים, הודעה למודל)."""
    out: List[Dict[str, Any]] = []
    problems: List[str] = []
    dropped = 0
    if not isinstance(blocks, list):
        return [], "blocks חייב להיות רשימה."
    for b in blocks[:10]:
        if not isinstance(b, dict):
            continue
        kind = _s(b.get("type"), 14).lower()
        title = _s(b.get("title"), 70)
        sub = _s(b.get("subtitle"), 90)
        text = _s(b.get("text"), 360)
        raw_items = [x for x in (b.get("items") or []) if isinstance(x, dict)]
        raw_items = raw_items[: _MAX_ITEMS.get(kind, 8)]
        style = _s(b.get("style"), 12).lower()
        if kind in STYLES and style not in STYLES[kind]:
            style = STYLES[kind][0]
        base = {"k": kind, "s": style, "ti": title, **_common(b)}
        if sub:
            base["su"] = sub

        # ---------------- the admin's controls: a switched-off kind is not shown, a custom design (made in the admin app) is filled from its template
        if kind in prompt_store.disabled_kinds() and not preview:
            problems.append(f"סוג הבלוק {kind} כבוי על ידי המנהל - השתמש בסוג אחר.")
            continue
        if kind.startswith("x_"):
            design = prompt_store.custom_design(kind)
            blk = custom_designs.build_block(design, {"title": title, "subtitle": sub, "text": text, "icon": b.get("icon"), "color": b.get("color"), "items": raw_items},
                                             _common(b)) if design else None
            if blk:
                out.append(blk)
            elif not design:
                problems.append(f"סוג בלוק לא מוכר: {kind}")
            continue
        # ---------------- Moodle items by id
        if kind in ITEM_KINDS:
            want = ITEM_KINDS[kind]
            ids = [str(i) for i in (b.get("ids") or []) if isinstance(i, (str, int))][:24]
            rows = []
            for i in ids:
                it = items.get(i)
                if it and it.get("k") == want:
                    rows.append({k: v for k, v in it.items() if k in ("n", "c", "t", "v", "d", "u", "o", "g")})
            if not rows:
                problems.append(f"בלוק {kind}: לא נמצאו פריטים לפי ה-ids שנשלחו (השתמש רק במזהים שהכלים החזירו).")
                continue
            if kind == "deadlines":
                rows.sort(key=lambda r: r.get("t", 0))
            out.append({**base, "it": rows})
        # ---------------- contact
        elif kind == "contact":
            acts = _clean_actions(b.get("items"), allowed)
            asked = len([x for x in raw_items])
            dropped += max(0, asked - len(acts))
            if not acts and not text:
                continue                                       # a name alone is not a contact card: nothing to call, write or open
            out.append({**base, "tx": text, "it": acts})
        # ---------------- hours
        elif kind == "hours":
            rows = [{"d": _s(x.get("days"), 30), "h": _s(x.get("time"), 40), "l": _s(x.get("label"), 40)} for x in raw_items if _s(x.get("time"), 40)]
            if rows:
                out.append({**base, "tx": text, "it": rows})
        # ---------------- list-like with label/detail/value/icon
        elif kind in ("steps", "facts", "list", "highlight", "price", "tags", "checklist", "stats", "progress", "buttons", "summary"):
            its = []
            for x in raw_items:
                label = _s(x.get("label") or x.get("title"), 80)
                if not label and kind != "highlight":
                    continue
                row = {"l": label}
                v = _s(x.get("value"), 120 if kind == "buttons" else 40)
                d = _s(x.get("detail") or x.get("text"), 220)
                if v:
                    row["v"] = v
                if d:
                    row["d"] = d
                ic = _icon(x.get("icon"))
                if ic:
                    row["ic"] = ic
                its.append(row)
            # never an empty-looking card: rows of a value-type block must carry their value (facts may carry a detail instead)
            if kind in ("price", "stats", "progress", "highlight"):
                its = [r for r in its if r.get("v")]
            elif kind == "facts":
                full = [r for r in its if r.get("v") or r.get("d")]
                if full:
                    its = full
                else:                                        # labels only: it is a plain list, not a table of facts
                    base = {**base, "k": "list", "s": "bullets"}
            if its or (kind == "summary" and text):
                out.append({**base, "tx": text, "it": its})
            elif kind != "summary":
                problems.append(f"בלוק {kind} נזרק: לשורות חסרים ערכים.")
        # ---------------- hero (a bold opening card) and routes (lines / options with a coloured badge)
        elif kind == "hero":
            its = [{"v": _s(x.get("value"), 24), "l": _s(x.get("label"), 40)} for x in raw_items if _s(x.get("value"), 24)]
            if title:
                out.append({**base, "tx": text, "it": its})
        elif kind == "routes":
            its = []
            for x in raw_items:
                badge = _s(x.get("label"), 10)
                name = _s(x.get("title") or x.get("text"), 70)
                if not badge:                                   # "קו 6 מגבעת שאול" -> badge "6", title "מגבעת שאול"
                    m = re.match(r"^(?:קו\s*)?(\d{1,3}[א-ת]?)\s*[-:–,]?\s*(.*)$", name)
                    if m and m.group(2):
                        badge, name = m.group(1), m.group(2)
                det = _s(x.get("detail"), 160)
                if name or det:
                    its.append({"b": badge, "l": name or det, "d": det if name else ""})
            if its:
                out.append({**base, "it": its})
        # ---------------- links
        elif kind == "links":
            its = []
            for x in raw_items:
                url = _s(x.get("url") or x.get("value"), 220)
                if url and _verified("link", url, allowed):
                    its.append({"l": _s(x.get("label"), 60) or url, "u": url, "d": _s(x.get("detail"), 120), "ic": _icon(x.get("icon"))})
                else:
                    dropped += 1
            if its:
                out.append({**base, "it": its})
        # ---------------- timeline of dates
        elif kind == "timeline":
            its = [{"dt": _s(x.get("date"), 40), "l": _s(x.get("title") or x.get("label"), 80), "d": _s(x.get("detail"), 160)} for x in raw_items if _s(x.get("title") or x.get("label"), 80)]
            if its:
                out.append({**base, "it": its})
        # ---------------- accordion / tabs
        elif kind == "accordion":
            its = [{"q": _s(x.get("q"), 140), "a": _s(x.get("a"), 500)} for x in raw_items if _s(x.get("q"), 140) and _s(x.get("a"), 500)]
            if its:
                out.append({**base, "it": its})
        elif kind == "tabs":
            its = [{"l": _s(x.get("title") or x.get("label"), 30), "tx": _s(x.get("text"), 600)} for x in raw_items if _s(x.get("title") or x.get("label"), 30) and _s(x.get("text"), 600)]
            if its:
                out.append({**base, "it": its})
        # ---------------- tables
        elif kind in ("compare", "table"):
            cols = [_s(c, 30) for c in (b.get("columns") or [])[:4]]
            rows = []
            for x in raw_items:
                cells = [_s(c, 80) for c in (x.get("cells") or [])[:4]]
                if cells or _s(x.get("label"), 50):
                    rows.append({"l": _s(x.get("label"), 50), "c": cells})
            if rows:
                out.append({**base, "col": cols, "it": rows})
        # ---------------- location / quote / callout / banner / heading
        elif kind == "location":
            if text:
                out.append({**base, "tx": text})
        elif kind == "quote":
            if text:
                out.append({**base, "tx": text})
        elif kind in ("callout", "banner"):
            if text:
                tone = _s(b.get("tone"), 10).lower()
                out.append({**base, "tone": tone if tone in TONES else "info", "tx": text})
        elif kind == "heading":
            if title:
                out.append({**base, "tx": text})
        else:
            problems.append(f"סוג בלוק לא מוכר: {kind}")
    if not out:
        return [], "לא הוצג כלום. " + " ".join(problems) + " תקן והצג שוב, או כתוב תשובה בטקסט."
    msg = "הוצג לסטודנט בהצלחה. כתוב לכל היותר משפט קצר אחד (או כלום), בלי לחזור על מה שמוצג."
    if dropped:
        msg += f" (הערה: {dropped} פעולות/קישורים הוסרו כי הפרטים שלהם לא מופיעים בחומר שעמד לרשותך - אל תמציא פרטי קשר.)"
    if problems:
        msg += " (הערות: " + " ".join(problems) + ")"
    return out, msg


def encode_blocks(blocks: List[Dict[str, Any]]) -> str:
    raw = json.dumps({"b": blocks}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")
