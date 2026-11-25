"""
ערכת רכיבי תצוגה שה-AI מרכיב בעצמו: הוא קורא ל-show_ui ומחליט איזה מידע להציג, באיזה סוג תצוגה, סגנון, צבע ואנימציה.
- נתוני Moodle (מטלות, ציונים, קורסים) לא עוברים דרך המודל: הכלים נותנים לכל פריט מזהה (d1, g2, c3...) והמודל רק מצביע - כך אי אפשר שימציא תאריך.
- מידע מהמאגר (טלפונים, מיילים, שעות, נהלים) המודל כותב בעצמו, אבל השרת מוודא שכל מייל, טלפון וקישור שהוא מצרף באמת מופיעים בחומר שעמד לרשותו
  בתשובה הזו (מאגר הידע, תוצאות הכלים, השאלה) - מה שלא נמצא שם נזרק, כך שלא יוצג כפתור חיוג למספר מומצא.
- השרת מאמת ומנקה הכול (סוגים וסגנונות מותרים, אורכים, צבעים, אייקונים), והאפליקציה מציירת.
"""
import base64
import html
import json
import re
from typing import Any, Dict, List, Optional, Tuple

import custom_designs
import prompt_store

# kind -> allowed styles (first one is the default)
STYLES = {
    "deadlines": ("tickets", "timeline", "focus"),
    "grades": ("gauges", "list"),
    "courses": ("chips", "list"),
    "contact": ("card", "row", "big"),
    "hours": ("agenda", "table", "week", "bars"),
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

_MAX_ITEMS = {"contact": 6, "hours": 16, "steps": 10, "facts": 10, "highlight": 4, "list": 12, "links": 8, "timeline": 10, "price": 8,
              "summary": 8, "tags": 14, "checklist": 12, "accordion": 8, "tabs": 5, "compare": 8, "table": 12, "quote": 1,
              "stats": 4, "progress": 8, "buttons": 4, "hero": 3, "routes": 10}

UI_ADDENDUM = """

=== תצוגה חזותית (show_ui) - אתה המעצב ===
כלל ברזל, בלי יוצא מן הכלל, לא משנה מה קורה: כל תשובה שמכילה יותר משלוש שורות טקסט - מוצגת ויזואלית עם show_ui, אף פעם לא כקיר טקסט. וגם תשובה קצרה יותר אבל מורכבת (רשימה, כמה פרטים, מספרים, תאריכים או שעות, סכומים, שלבים, איש קשר, השוואה, תנאים) - ויזואלית. טקסט רגיל מותר רק לתשובה פשוטה של עד שלוש שורות קצרות (תודה, אישור, כן/לא, שאלת הבהרה, סירוב מנומס, הודעת שגיאה). אם התחלת לחשוב על תשובה ארוכה בטקסט - עצור וקרא ל-show_ui. השרת בודק כל תשובה, ותשובה ארוכה או מורכבת שנכתבה בטקסט מומרת אוטומטית לתצוגה ויזואלית (זה איטי ופחות טוב), אז חסוך את זה וקרא ישר ל-show_ui.
הסטודנטים לא אוהבים לקרוא. אתה בוחר סוג תצוגה, סגנון, צבע, אייקון ואנימציה לכל בלוק, וכמה בלוקים שרוצים בתשובה אחת (בדרך כלל 2 עד 5). תהיה יצירתי, קצר ובהיר: מעט מילים, הרבה מבנה; בכל בלוק עד שורה או שתיים. אחרי show_ui התשובה נגמרת: הכרטיסים הם התשובה, אל תכתוב אחריהם כלום ואל תחזור בטקסט על מה שמוצג. אם צריך משפט פתיחה - הוא חייב להיות לפני הקריאה ל-show_ui, קצר מאוד.

איך מרכיבים תשובה טובה (מתכונים - הצע שילוב, לא בלוק בודד):
1. התשובה עצמה קודם: בלוק ראשון נותן את התשובה במבט אחד (highlight או price או banner או callout), ורק אחריו הפרטים. הסטודנט צריך להבין מה התשובה לפני שקרא משהו.
2. שאלה על איש קשר / מי אחראי / איך פונים: contact (עם פעולות חיוג, מייל, וואטסאפ) + hours אם יש שעות קבלה + callout קצר עם טיפ (למשל "עדיף לפנות במייל").
3. שעות פעילות: hours (style agenda, ברירת המחדל) + callout אם יש חריג (חגים, ימי שישי).
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
דוגמה: "מי אחראי על הרישום ומתי פתוח?" -> blocks: [contact(title:"...", subtitle:"...", items:[{kind:phone,value:"..."},{kind:email,value:"..."}], color:teal), hours(style:agenda, items:[...]), callout(tone:tip, text:"...")].

מידע מ-Moodle (מטלות, ציונים, קורסים): שלוף עם הכלי המתאים; כל פריט בתוצאה מגיע עם מזהה בסוגריים כמו [d3]. בבלוק מצביעים על ids בלבד.
- deadlines (ids של d): style tickets / timeline / focus (המטלה הדחופה גדולה). grades (ids של g): gauges / list. courses (ids של c): chips / list.

מידע מהמאגר ומהכלים (אתה כותב את התוכן, קצר): כל מייל, טלפון וקישור חייבים להיות בדיוק כפי שהופיעו בחומר שקיבלת, אחרת השרת ימחק אותם. אל תמציא פרטים.
- contact: איש קשר או מדור. title=שם, subtitle=תפקיד, text=שורה נוספת. items=פעולות {kind: email|phone|whatsapp|link|address, value, label?} - הסטודנט לוחץ ומתקשר/שולח מייל/פותח וואטסאפ או מעתיק. style card/row/big.
- hours: שעות פעילות. items {days, time, label?}, למשל days="א'-ה'" time="8:15-22:00". text=הערה. האפליקציה מחשבת "פתוח עכשיו". style agenda (ברירת המחדל: רשימה מסודרת, שורה לכל קבוצת ימים והטווחים מתחתיה, נקודה צבעונית לסוג השירות, ומסנן לפי סוג שירות כשיש כמה; בטלפון ובמחשב) / table (שורות עם תגיות) / week (שבעה ריבועי ימים, רק כשהשעות פשוטות מאוד) / bars (פס זמן). השתמש ב-agenda כמעט תמיד. סוג השירות (טלפוני, קהל, זום) או שם המדור/המשרד נכתב ב-label של כל טווח ולא בתוך time; כמה מדורים עם שעות שונות (מזכירות, חשבונות, דיקן) = בלוק hours אחד עם label=שם המדור בכל פריט (האפליקציה מציגה כל מדור בכרטיס משלו, עם "פתוח עכשיו" משלו). שעות לעולם לא נכתבות ב-table או compare או בטקסט; כמה טווחים באותו יום = כמה פריטים עם אותם days. כמה משרדים עם אותן שעות - בלוק אחד, לא כרטיס לכל משרד.
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


UI_FIT = """

=== אורך ותוכן בתוך הבלוקים (שלא יצא מהמסך) ===
המסך בטלפון צר: בכותרת ובשורה נכנסות כמה מילים בלבד, וכל מה שארוך יותר נחתך או נשבר לכמה שורות.
- title של בלוק: עד 3-4 מילים (עד 30 תווים). שם של נושא, לא משפט ולא רשימה. את הפרטים כותבים ב-subtitle, ב-text או בשורות.
- label של שורה: שם קצר, עד 3-4 מילים (עד 30 תווים). הפירוט בא ב-detail או ב-value, לא בתוך ה-label.
- value ב-highlight / stats / hero / price: מספר או סכום קצר בלבד, עד 12 תווים ("8,100 ש״ח", "עד 50%", "14 יום"). טווח, תנאי או הסבר - ב-label או ב-detail.
- כפתורים (buttons): עד 4 מילים. text של callout או banner: עד שני משפטים קצרים.
- הרבה מידע (יותר מ-6 שורות, או כמה נושאים בשאלה אחת): אל תדחוס הכול לבלוק אחד ואל תכתוב את זה בכותרת. פצל לבלוקים לפי נושא, כל אחד עם title או heading קצר; accordion או tabs כשכל פריט הוא פסקה, compare או table להשוואות, list או facts בקבוצות של עד 6-8 שורות. עדיף 4-6 בלוקים מסודרים מבלוק אחד גדול.
- אל תקצר על חשבון העובדות: מה שלא נכנס לכותרת עובר לפירוט, לא נמחק. (השרת מקצר כותרת ארוכה ומעביר את הנוסח המלא לשורת המשנה, אבל עדיף לכתוב נכון מההתחלה.)
"""

UI_CHOOSE = """

=== איזה כרטיס מתי: בחירה לפי צורת המידע ===
בחר את סוג הכרטיס לפי הצורה של המידע שיש לך, לא לפי ההרגל:
- מספר או תאריך אחד שהוא התשובה (מחיר, מועד, ספירה): highlight או hero, ורק אחריו הפרטים.
- כמה זוגות של שם וערך על דבר אחד (תנאים, מה כלול, פרטי מעונות): facts.
- כמה אפשרויות ולכל אחת כמה מאפיינים (מסלולים, קמפוסים, מעונות): compare עד 3 עמודות ו-4 שורות, או tabs כשיש הרבה טקסט לכל אחת. אל תבנה טבלה רחבה: בטלפון כל שורה בה הופכת לכרטיס, וטקסט ארוך בתא נראה רע.
- שלבים בסדר קבוע: steps. תאריכים לאורך זמן: timeline. שעות פעילות: תמיד hours. שאלות ותשובות או כמה נושאים עם פסקה לכל אחד: accordion. רשימה קצרה של פריטים (פקולטות, מסמכים): list, ואם הסטודנט צריך להכין או להביא: checklist. הגעה: location ואחריו routes. פנייה לאדם או מדור: contact.
- table רק כשבאמת יש 2 עד 3 עמודות קצרות (עד שלוש מילים בתא).
- תשובה של משפט או שניים: טקסט קצר בלי כרטיסים. callout רק כשיש משהו שחשוב להדגיש (מועד אחרון, מלכודת), לא כדי לחזור על מה שכבר כתוב.
- שאלה עמומה (למשל "כמה עולים המעונות" בלי לציין קמפוס): ברירת המחדל היא לענות על כל האפשרויות בקצרה (price או compare), לא לשאול חזרה. רק אם אי אפשר: משפט הבהרה אחד בטקסט ו-buttons עם האפשרויות. אף פעם לא שאלה בתוך כרטיס facts, וכפתורים לא נושאים תשובה בתוך ה-value שלהם.
- שאלה אחת פשוטה ("כמה עולה שנה", "מתי הספרייה פתוחה"): עד 3 כרטיסים, הראשון הוא התשובה. הרבה כרטיסים נועדו לשאלה עם כמה נושאים, לא לשאלה אחת. מידע משני (נתונים ישנים, תכניות מיוחדות) - שורה אחת ב-callout, לא כרטיס משלו.
- כתובת או מיקום (גם של כמה קמפוסים): location לכל מקום, עם הכתובת עצמה ב-text. לא list של שמות בלבד.
- אל תוסיף כרטיס שלא מוסיף מידע: לא callout שחוזר על הכרטיס שלפניו, ולא buttons כשאין המשך טבעי.
- כמה נושאים בשאלה אחת: כרטיס או שניים לכל נושא, עם heading קצר ביניהם. עדיף לתת את החשוב ביותר ולהציע המשך בכפתור מאשר לדחוס הכול.
- כתוב עברית תקנית וקרא את הטקסט לפני השליחה: אין אותיות ערביות בתוך מילים בעברית, ואין שגיאות כתיב.
"""

UI_PARTS = _split_ui(UI_ADDENDUM)
UI_PARTS["ui_fit"] = UI_FIT
UI_PARTS["ui_choose"] = UI_CHOOSE
for _k, _v in UI_PARTS.items():
    prompt_store.register(_k, _v)
UI_KEYS = ("ui_iron", "ui_recipes", "ui_standard", "ui_moodle", "ui_reference", "ui_fit", "ui_choose")


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
    t = " ".join(html.unescape(str(v if v is not None else "")).split())          # (the model sometimes writes &quot; for a quote: the app escapes text itself, so it would show as is)
    if _ARB.search(t) and len(_HEB.findall(t)) > len(_ARB.findall(t)):
        t = t.translate(_AR2HE)
    if len(t) <= n:
        return t
    cut = t[: n - 1]
    sp = cut.rfind(" ")
    return (cut[:sp] if sp >= int(n * 0.55) else cut).rstrip(" ,;:-.") + "…"          # never a word cut in half


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


_RANGE_SPLIT = re.compile(r"\s*(?:,|;|\s\+\s|\sו-?(?=\d))\s*")


def _hours_rows(raw_items: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """Opening-hours rows. When the model writes the kind of service inside the time ("10:00-12:00 (טלפוני), 13:00-15:00 (קהל)"), each range becomes its own
    row with the note as its label, so the app can show the note next to the time instead of cutting it into the hours."""
    rows: List[Dict[str, str]] = []
    for x in raw_items:
        days, label, raw = _s(x.get("days"), 30), _s(x.get("label"), 40), _s(x.get("time"), 90)
        if not raw:
            continue
        parts = [p for p in _RANGE_SPLIT.split(raw) if p.strip()] or [raw]
        if any("(" in p for p in parts):
            for p in parts:
                m = re.search(r"\(([^)]{1,30})\)", p)
                clean = (p.replace(m.group(0), "") if m else p).strip()
                if clean:
                    rows.append({"d": days, "h": _s(clean, 40), "l": _s(m.group(1), 30) if m else label})
        else:
            rows.append({"d": days, "h": _s(raw, 40), "l": label})
    return rows[:16]


_SPLITTABLE = ("facts", "list", "steps", "price", "timeline", "checklist", "links", "table", "compare", "accordion", "progress", "tags", "routes")


def _expand(blocks: List[Any]) -> List[Any]:
    """More rows than a block shows are not thrown away: they continue in the next block of the same kind (up to three blocks)."""
    out: List[Any] = []
    for b in blocks:
        kind = _s(b.get("type"), 14).lower() if isinstance(b, dict) else ""
        its = b.get("items") if isinstance(b, dict) else None
        mx = _MAX_ITEMS.get(kind, 8)
        if kind in _SPLITTABLE and isinstance(its, list) and len(its) > mx:
            for i in range(0, min(len(its), mx * 3), mx):
                nb = dict(b)
                nb["items"] = its[i:i + mx]
                if i:
                    nb["title"], nb["subtitle"], nb["text"] = "", "", ""
                out.append(nb)
        else:
            out.append(b)
    return out


_DANGLING = ("של", "ו", "ב", "ל", "את", "עם", "על", "או", "מן", "אל", "כל", "ה")


def _fit_title(title: str, sub: str, limit: int = 52):
    """A title is a name, not a sentence: when it is really long it is shortened at a word (never leaving "of the…" hanging), and the full words move to the subtitle
    (nothing is lost). 52 characters: the app wraps a title to two lines, so a name of 40 characters is fine on a phone."""
    if len(title) <= limit:
        return title, sub
    short = _s(title, limit)
    words = short.rstrip("…").split()
    while len(words) > 2 and words[-1] in _DANGLING:
        words.pop()
    short = " ".join(words) + "…"
    return short, sub or _s(title, 90)


_DATE_LIKE = re.compile(r"^\d{1,2}[./]\d{1,2}(?:[./]\d{2,4})?$")
_PRICE_LIKE = re.compile(r"^[\d,.\-–\s]+(?:ש\"ח|ש״ח|שח|₪|נק\"ז|%)?$")
_NO_INFO = re.compile(r"אינו מפורט|אינה מפורטת|לא מפורט|אין מידע|לא נמצא מידע|לא צוין בטקסט|המידע חסר")
_TIME_RANGE = re.compile(r"\d{1,2}[:.]\d{2}\s*[-–]\s*\d{1,2}[:.]\d{2}")
_DAYS_CELL = re.compile(r"(?:^|[\s,\-–])[אבגדהוש]['׳\"](?=$|[\s,\-–])|\bימים?\b|\bיום\b")


def _table_as_hours(rows: List[Dict[str, Any]]) -> Optional[List[Dict[str, str]]]:
    """Opening hours written as a table (department, days, hours) break in the middle of the times on a phone. When a table is nothing but that, it becomes an hours
    block (the app lays it out by department, with the times never broken). A table that has other columns as well (a phone number...) is left alone."""
    out: List[Dict[str, str]] = []
    for x in rows:
        parts = [p for p in ([_s(x.get("label"), 60)] + [_s(c, 90) for c in (x.get("cells") or [])]) if p]
        time = next((p for p in parts if _TIME_RANGE.search(p)), "")
        if not time:
            continue
        days = next((p for p in parts if p is not time and len(p) <= 40 and _DAYS_CELL.search(p)), "")
        rest = [p for p in parts if p is not time and p is not days]
        if len(rest) > 1:
            return None
        out.append({"days": days, "time": time, "label": rest[0] if rest else ""})
    return out if rows and len(out) == len(rows) and any(o["days"] for o in out) else None


def _icon(x: Any) -> str:
    i = _s(x, 12).lower()
    return i if i in ICONS else ""


def _as_list(v: Any) -> List[Any]:
    if isinstance(v, list):
        return v
    if isinstance(v, dict):
        return [v]
    return []


def _normalize(blocks: Any) -> Any:
    """The model's arguments are not always the shape the schema promises (a number where a list belongs, a cell that is a list...). Fuzzing the validator with
    malformed input crashed it in 13% of the runs, which would have failed the whole turn: everything is put in shape here, before the real work."""
    if not isinstance(blocks, list):
        return blocks
    out = []
    for b in blocks:
        if not isinstance(b, dict):
            continue
        nb = dict(b)
        nb["items"] = [dict(x, cells=[c for c in _as_list(x.get("cells")) if isinstance(c, (str, int, float))]) if "cells" in x else dict(x)
                       for x in _as_list(b.get("items")) if isinstance(x, dict)]
        nb["columns"] = [c for c in _as_list(b.get("columns")) if isinstance(c, (str, int, float))]
        nb["ids"] = [c for c in _as_list(b.get("ids")) if isinstance(c, (str, int))]
        out.append(nb)
    return out


def resolve_blocks(blocks: Any, items: Dict[str, Dict[str, Any]], allowed: str = "", preview: bool = False) -> Tuple[List[Dict[str, Any]], str]:
    try:
        return _resolve_blocks(_normalize(blocks), items, allowed, preview)
    except Exception as e:                                          # never let a strange argument fail the answer: the model is told and sends it again
        print(f"[ui] resolve failed: {type(e).__name__}: {e}")
        return [], "הבלוקים לא היו במבנה תקין. שלח שוב רשימה של בלוקים עם type ו-items כרשימה של אובייקטים."


def _resolve_blocks(blocks: Any, items: Dict[str, Dict[str, Any]], allowed: str = "", preview: bool = False) -> Tuple[List[Dict[str, Any]], str]:
    """מאמת את מה שהמודל ביקש. מחזיר (בלוקים נקיים, הודעה למודל)."""
    out: List[Dict[str, Any]] = []
    problems: List[str] = []
    dropped = 0
    if not isinstance(blocks, list):
        return [], "blocks חייב להיות רשימה."
    for b in _expand(blocks[:10])[:14]:
        if not isinstance(b, dict):
            continue
        kind = _s(b.get("type"), 14).lower()
        title = _s(b.get("title"), 70)
        sub = _s(b.get("subtitle"), 90)
        if kind not in ("callout", "banner", "quote"):
            title, sub = _fit_title(title, sub)
        text = _s(b.get("text"), 360)
        raw_items = [x for x in (b.get("items") or []) if isinstance(x, dict)]
        raw_items = raw_items[: _MAX_ITEMS.get(kind, 8)]
        if kind in ("table", "compare"):
            as_hours = _table_as_hours(raw_items)
            if as_hours:
                kind, raw_items = "hours", as_hours
        style = _s(b.get("style"), 12).lower()
        if kind in STYLES and style not in STYLES[kind]:
            style = STYLES[kind][0]
        if kind in STYLES and not preview:
            if kind == "hours" and style in ("table", "week") and not prompt_store.is_fixed(kind):
                style = "agenda"                                   # the agenda list is the standard hours design; table/week stay for answers saved earlier and for a style the admin fixes
            style = prompt_store.apply_style(kind, style, STYLES[kind])            # the admin may fix the style of a kind, or take some styles off the bot's menu
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
            if kind == "courses":                                        # the same course twice in a list is noise
                seen_n, uniq = set(), []
                for r in rows:
                    if r.get("n") not in seen_n:
                        seen_n.add(r.get("n"))
                        uniq.append(r)
                rows = uniq
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
            rows = _hours_rows(raw_items)
            if rows:
                out.append({**base, "tx": text, "it": rows})
        # ---------------- list-like with label/detail/value/icon
        elif kind in ("steps", "facts", "list", "highlight", "price", "tags", "checklist", "stats", "progress", "buttons", "summary"):
            its = []
            for x in raw_items:
                label = _s(x.get("label") or x.get("title") or x.get("q"), 160)
                if not label and kind in ("steps", "list", "checklist", "facts", "tags", "buttons"):
                    label = _s(x.get("text") or x.get("detail") or x.get("a"), 160)          # (found by testing: steps that came with only a detail vanished and the answer promised steps it did not show)
                    if label:
                        x = {**x, "detail": "", "text": ""}
                if not label and kind != "highlight":
                    continue
                if kind in ("steps", "checklist"):                              # the card numbers its own steps: "שלב 1: כניסה" would show "1" twice
                    label = re.sub(r"^(?:שלב|צעד|step)\s*\d+\s*[:.\-–]\s*", "", label, flags=re.I) or label
                v = _s(x.get("value"), 120 if kind == "buttons" else 40)
                d = _s(x.get("detail") or x.get("text"), 220)
                if kind == "facts" and len(" ".join(str(x.get("value") or "").split())) > 40:
                    # (found by testing: a long value was cut with "…" and the rest of the fact was lost.) A long value is a sentence: it goes to the detail line, whole.
                    d = _s((str(x.get("value")) + " " + d).strip() if d else x.get("value"), 260)
                    v = ""
                if kind not in ("buttons", "tags", "summary") and len(label) > 44:          # a label is a name: the long sentence becomes the detail
                    d = d or _s(label, 220)
                    label = _s(label, 40)
                if kind == "buttons":                                           # the same follow-up question twice (or three times) is one button
                    key = re.sub(r"[\W_]+", "", (v or label).lower())
                    if any(re.sub(r"[\W_]+", "", (r.get("v") or r["l"]).lower()) == key for r in its):
                        continue
                row = {"l": label[:80]}
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
                # a big-number card is for ONE or TWO short numbers. Three amounts, or a range ("8,100 עד 14,300 ש״ח"), set in 32px type breaks on a phone: it becomes a price list
                if kind == "highlight" and its and (len(its) > 2 or any(len(r["v"]) > 14 for r in its)) and not prompt_store.is_fixed("highlight"):
                    base = {**base, "k": "price", "s": "rows"}
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
                badge = re.sub(r"^קו\s*(?=\d)", "", _s(x.get("label"), 10))          # the badge is a small circle: "קו 6" is just "6" (found by testing)
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
                cells = [_s(c, 80) for c in (x.get("cells") or [])[:5]]
                rl = _s(x.get("label"), 50)
                # (found by testing: the model often writes the row's name as the first cell. With two columns and three cells the data slid one column to the right,
                # so the dorm of campus A was shown under campus B.) A row with one cell more than the columns has its first cell as the row name.
                if cols and not rl and len(cells) == len(cols) + 1:
                    rl, cells = cells[0], cells[1:]
                cells = [("לא צוין" if _NO_INFO.search(c) else c) for c in cells[:4]]          # the bot's "not in the text I got" must not reach the student as a cell
                if cells or rl:
                    rows.append({"l": rl, "c": cells})
            if rows and not cols and all(not r["l"] for r in rows) and len({len(r["c"]) for r in rows}) == 1 and len(rows[0]["c"]) >= 3 and all(len(r["c"][0]) <= 30 for r in rows):
                rows = [{"l": r["c"][0], "c": r["c"][1:]} for r in rows]                 # no column headers and three or more cells: the first cell is the row's name
            # A "table" of just two fields per row (a name and a value) is not a table: on a phone it is a price list, a list of facts, or a timeline of dates.
            pairs = [((r["l"], r["c"][-1]) if r["l"] and len(r["c"]) == 1 else (r["c"][0], r["c"][1]) if not r["l"] and len(r["c"]) == 2 else None) for r in rows]
            if rows and all(pairs) and len(cols) <= 2 and all(len(v) <= 36 and 0 < len(k) <= 60 for k, v in pairs):
                if all(_DATE_LIKE.match(k) for k, _ in pairs):
                    out.append({**base, "k": "timeline", "s": "vertical", "it": [{"dt": k, "l": v, "d": ""} for k, v in pairs]})
                elif all(_PRICE_LIKE.match(v) for _, v in pairs):
                    out.append({**base, "k": "price", "s": "rows", "it": [{"l": k, "v": v} for k, v in pairs]})
                else:
                    out.append({**base, "k": "facts", "s": "list", "it": [{"l": k, "v": v} for k, v in pairs]})
                continue
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
    # (Found by testing: two link cards of one link each, one after the other.) Neighbouring cards of the same kind and style with no title of their own are one card.
    merged: List[Dict[str, Any]] = []
    for blk in out:
        prev = merged[-1] if merged else None
        if (prev and blk.get("k") == prev.get("k") and blk.get("s") == prev.get("s") and blk.get("k") in ("links", "facts", "list", "price", "tags", "checklist")
                and not blk.get("ti") and not blk.get("su") and not blk.get("tx") and len(prev.get("it", [])) + len(blk.get("it", [])) <= _MAX_ITEMS.get(blk["k"], 8)):
            prev["it"] = prev["it"] + blk["it"]
        else:
            merged.append(blk)
    out = merged
    # (Found by testing: a directions answer whose location / routes blocks came out empty was shown as a lone row of buttons - the student got no answer at all.)
    # Buttons and headings alone are not an answer, and every kind the model asked for that did not make it is named, with what it was missing.
    if not preview:
        shown_k = {x.get("k") for x in out}
        asked_k = list(dict.fromkeys(_s(b.get("type"), 14).lower() for b in blocks[:10] if isinstance(b, dict)))
        converted = {"table": "hours", "compare": "hours", "facts": "list"}              # a block that became another kind is not a missing one
        missing = [k for k in asked_k if k and k in STYLES and k not in shown_k and k not in ITEM_KINDS and converted.get(k) not in shown_k
                   and k not in prompt_store.disabled_kinds() and not any(k in p for p in problems)]
        need = {"location": "text = הכתובת", "contact": "פעולות (טלפון/מייל) שמופיעים בחומר, או text", "routes": "items עם label ו-title", "hours": "items עם days ו-time",
                "links": "url מהחומר", "table": "columns ו-items עם cells", "compare": "columns ו-items עם cells", "facts": "label ו-value לכל שורה", "price": "label ו-value לכל שורה"}
        if missing:
            problems.append("הבלוקים הבאים לא הוצגו כי היו ריקים או לא תקינים: " + ", ".join(k + (f" (חסר: {need[k]})" if k in need else "") for k in missing) + ".")
        if out and all(x.get("k") in ("buttons", "heading") for x in out):
            return [], "לא הוצג תוכן: נשארו רק כפתורים או כותרת. " + " ".join(problems) + " שלח שוב בלוקים עם תוכן ממשי מהחומר (או כתוב את התשובה בטקסט)."
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
