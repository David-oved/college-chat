"""The catalog the admin gallery shows: every block kind with a Hebrew name, a one-line explanation, and a realistic sample (in the format the AI sends to show_ui)."""
import time
from typing import Any, Dict, List

_I = lambda *a: [dict(zip(("label", "value", "detail"), x)) for x in a]

CATALOG: List[Dict[str, Any]] = [
    {"kind": "hero", "name": "פתיחה בולטת", "desc": "כרטיס צבעוני גדול בראש תשובה על נושא עם שם ומספר מפתח.",
     "sample": {"title": "קמפוס לב", "subtitle": "גבעת מרדכי, ירושלים", "text": "הקמפוס המרכזי של המרכז האקדמי לב", "icon": "school", "color": "blue",
                "items": [{"value": "8,100 ש\"ח", "label": "מחיר לשנה"}, {"value": "3", "label": "מסלולים"}]}},
    {"kind": "highlight", "name": "מספר גדול", "desc": "סכום, תאריך או מספר אחד שהוא התשובה.", "sample": {"color": "orange", "anim": "pop", "items": [{"value": "6 ש\"ח", "label": "קנס ליום איחור", "detail": "עד 30 יום"}]}},
    {"kind": "price", "name": "מחירים", "desc": "כמה סכומים עם תיאור.", "sample": {"title": "עלויות", "icon": "money", "color": "orange", "items": _I(("שכר לימוד", "8,100 ש\"ח", "לשנה"), ("מעונות", "1,900 ש\"ח", "לחודש"), ("דמי רישום", "300 ש\"ח"))}},
    {"kind": "facts", "name": "עובדות", "desc": "זוגות של שם וערך.", "sample": {"title": "פרטים", "color": "teal", "items": _I(("תשלומים", "עד 10"), ("הנחה", "5% בתשלום מראש"), ("מועד אחרון", "1 בספטמבר"))}},
    {"kind": "stats", "name": "מספרים", "desc": "עד ארבעה מספרים בשורה.", "sample": {"color": "purple", "items": _I(("מטלות השבוע", "4"), ("ממוצע", "88"), ("קורסים", "6"))}},
    {"kind": "progress", "name": "פסי התקדמות", "desc": "אחוזים (0-100).", "sample": {"title": "התקדמות בתואר", "color": "green", "items": _I(("נקודות זכות", "62"), ("קורסי חובה", "80"))}},
    {"kind": "callout", "name": "הדגשה", "desc": "הערה חשובה, אזהרה או טיפ בתיבה צבעונית.", "sample": {"tone": "warn", "title": "שים לב", "text": "ההגשה נסגרת ביום חמישי בחצות ואין הארכות."}},
    {"kind": "banner", "name": "פס הודעה", "desc": "פס קצר בראש תשובה.", "sample": {"tone": "tip", "text": "עדיף לפנות למזכירות במייל, התשובה מהירה יותר."}},
    {"kind": "summary", "name": "סיכום", "desc": "כותרת, טקסט קצר ונקודות.", "sample": {"title": "בקצרה", "text": "ערעור על ציון מוגש תוך 14 יום.", "items": [{"label": "מגישים בפורטל"}, {"label": "מצרפים נימוק"}]}},
    {"kind": "heading", "name": "כותרת מקטע", "desc": "מפרידה בין חלקים בתשובה ארוכה.", "sample": {"title": "מה צריך להכין", "text": "לפני שמגיעים"}},
    {"kind": "steps", "name": "שלבים", "desc": "תהליך צעד אחר צעד.", "sample": {"title": "איך נרשמים", "color": "blue", "items": [{"title": "נכנסים לפורטל", "detail": "עם שם משתמש של המכללה"}, {"title": "בוחרים קורסים"}, {"title": "מאשרים ומשלמים", "detail": "עד סוף השבוע"}]}},
    {"kind": "timeline", "name": "ציר תאריכים", "desc": "אירועים לפי תאריך.", "sample": {"title": "לוח שנה", "items": [{"date": "1.10", "title": "פתיחת שנה", "detail": "טקס פתיחה"}, {"date": "15.1", "title": "סיום סמסטר א"}, {"date": "1.3", "title": "תחילת סמסטר ב"}]}},
    {"kind": "checklist", "name": "רשימת סימון", "desc": "מה להביא או להכין. הסטודנט מסמן.", "sample": {"title": "מה להביא", "color": "green", "items": [{"label": "תעודת זהות"}, {"label": "אישור לימודים", "detail": "מהמזכירות"}, {"label": "צילום ביטוח"}]}},
    {"kind": "list", "name": "רשימה", "desc": "נקודות, מספרים או כרטיסים.", "sample": {"title": "אפשרויות", "icon": "check", "items": [{"label": "מעונות", "detail": "ליד הקמפוס"}, {"label": "דירה בשכירות"}, {"label": "מלגת מגורים"}]}},
    {"kind": "tags", "name": "תגיות", "desc": "מילות מפתח קצרות.", "sample": {"color": "pink", "items": [{"label": "מלגות"}, {"label": "רישום"}, {"label": "מעונות"}]}},
    {"kind": "contact", "name": "איש קשר", "desc": "כרטיס עם חיוג, מייל ווואטסאפ.", "sample": {"title": "מזכירות סטודנטים", "subtitle": "קומה 1", "color": "teal",
                "items": [{"kind": "phone", "value": "02-6751111"}, {"kind": "email", "value": "info@jct.ac.il"}]}},
    {"kind": "hours", "name": "שעות פעילות", "desc": "שעות לפי ימים, עם 'פתוח עכשיו'.", "sample": {"title": "מזכירות", "text": "בערבי חג נסגר מוקדם", "items": [{"days": "א'-ה'", "time": "8:15-16:00"}, {"days": "ו'", "time": "8:15-12:00"}]}},
    {"kind": "location", "name": "מיקום", "desc": "כתובת עם ניווט.", "sample": {"title": "קמפוס לב", "subtitle": "בניין ראשי", "text": "הרב קוק 21, ירושלים", "color": "green"}},
    {"kind": "routes", "name": "קווים ונתיבים", "desc": "תג צבעוני גדול לכל קו או אפשרות.", "sample": {"title": "קווי אוטובוס", "icon": "bus", "color": "blue", "items": [{"label": "6", "title": "מגבעת שאול", "detail": "ירידה בשח\"ל"}, {"label": "5", "title": "מבית הכרם", "detail": "ירידה בגבעת מרדכי"}]}},
    {"kind": "links", "name": "קישורים", "desc": "כפתורי קישור לאתרים של המכללה.", "sample": {"title": "קישורים", "items": [{"label": "הפורטל", "detail": "רישום וציונים", "url": "https://www.jct.ac.il"}]}},
    {"kind": "compare", "name": "השוואה", "desc": "טבלה שמשווה אפשרויות.", "sample": {"title": "מסלולים", "columns": ["יום", "ערב"], "items": [{"label": "שעות", "cells": ["8-14", "17-21"]}, {"label": "מחיר", "cells": ["8,100", "7,200"]}]}},
    {"kind": "table", "name": "טבלה", "desc": "טבלה רגילה.", "sample": {"columns": ["קורס", "ציון"], "items": [{"label": "מבוא", "cells": ["מבוא", "90"]}, {"label": "אלגברה", "cells": ["אלגברה", "84"]}]}},
    {"kind": "accordion", "name": "שאלות ותשובות", "desc": "קטעים מתקפלים.", "sample": {"title": "שאלות נפוצות", "items": [{"q": "איך מגישים ערעור?", "a": "דרך הפורטל בתוך 14 יום."}, {"q": "כמה זה עולה?", "a": "אין עלות."}]}},
    {"kind": "tabs", "name": "לשוניות", "desc": "מעבר בין כמה אפשרויות באותו מקום.", "sample": {"items": [{"title": "קמפוס לב", "text": "הרב קוק 21"}, {"title": "קמפוס טל", "text": "פרוג 3"}]}},
    {"kind": "quote", "name": "ציטוט / הגדרה", "desc": "ציטוט או הסבר של מושג.", "sample": {"text": "נקודת זכות היא יחידת מדידה של עומס לימודים.", "subtitle": "נקודת זכות"}},
    {"kind": "buttons", "name": "כפתורי המשך", "desc": "כפתורים ששולחים הודעה בשם הסטודנט.", "sample": {"items": [{"label": "איך מגישים מלגה"}, {"label": "מה שעות המזכירות"}]}},
    {"kind": "deadlines", "name": "מועדי הגשה (Moodle)", "desc": "מטלות ומבחנים של הסטודנט.", "sample": {"ids": ["d1", "d2", "d3"]}, "moodle": True},
    {"kind": "grades", "name": "ציונים (Moodle)", "desc": "ציונים של הסטודנט.", "sample": {"ids": ["g1", "g2"]}, "moodle": True},
    {"kind": "courses", "name": "קורסים (Moodle)", "desc": "הקורסים של הסטודנט.", "sample": {"ids": ["c1", "c2", "c3"]}, "moodle": True},
]


def sample_items() -> Dict[str, Dict[str, Any]]:
    now = int(time.time())
    return {
        "d1": {"k": "d", "n": "הגשת תרגיל 4", "c": "אלגברה לינארית", "t": now + 2 * 86400},
        "d2": {"k": "d", "n": "מבחן אמצע", "c": "מבוא לתכנות", "t": now + 6 * 86400},
        "d3": {"k": "d", "n": "פרויקט גמר", "c": "בסיסי נתונים", "t": now - 3600, "o": 1},
        "g1": {"k": "g", "n": "מבוא לתכנות", "v": "91"},
        "g2": {"k": "g", "n": "אלגברה לינארית", "v": "84"},
        "c1": {"k": "c", "n": "מבוא לתכנות"}, "c2": {"k": "c", "n": "אלגברה לינארית"}, "c3": {"k": "c", "n": "בסיסי נתונים"},
    }
