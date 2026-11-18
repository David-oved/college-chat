"""Ready-made designs the admin can install with one click (they become editable custom designs, see custom_designs.py). Each one is a small HTML template with
scoped CSS made of the app's own colour variables (var(--bc) is the colour the bot picks), so they look right in light and dark and on phone and desktop."""
from typing import Any, Dict, List

LIBRARY: List[Dict[str, Any]] = [
    {
        "id": "x_promo", "name": "הטבה או הצעה מיוחדת", "desc": "כותרת עם אייקון, מספר גדול ורשימת תנאים עם וי.",
        "when": "כשיש הטבה, הנחה, מלגה או הצעה מיוחדת עם סכום או אחוז מרכזי ותנאים: text הוא הסכום או האחוז, items הם התנאים (label=התנאי, detail=פירוט).",
        "template": '<div class="hd"><span class="ic">{{icon}}</span><div><h3>{{title}}</h3><small>{{subtitle}}</small></div></div><div class="big">{{text}}</div>'
                    '<ul class="ls">{{#items}}<li><i data-ic="check"></i><span><b>{{label}}</b><small>{{detail}}</small></span></li>{{/items}}</ul>',
        "css": ".hd{display:flex;gap:10px;align-items:center}.ic{display:grid;place-items:center;width:38px;height:38px;border-radius:11px;background:var(--bc);color:#fff;flex:none}"
               "h3{margin:0;font-size:16px;font-weight:850}.hd small{color:var(--muted);font-size:12.5px}.big{margin:12px 0 8px;font-size:30px;font-weight:900;color:var(--bc);letter-spacing:-.02em}"
               ".ls{list-style:none;margin:0;padding:0;display:grid;gap:8px}.ls li{display:flex;gap:9px;align-items:flex-start}.ls li svg{color:var(--bc);margin-top:2px}"
               ".ls b{display:block;font-size:14px}.ls small{display:block;color:var(--muted);font-size:12.5px}",
        "sample": {"title": "מלגת הצטיינות", "subtitle": "לשנת הלימודים הקרובה", "text": "עד 50% הנחה", "icon": "gift", "color": "orange",
                   "items": [{"label": "ממוצע 90 ומעלה", "detail": "בשנה הקודמת"}, {"label": "הגשת בקשה עד 1 בספטמבר", "detail": "דרך הפורטל"}]},
    },
    {
        "id": "x_notice", "name": "הודעה חשובה", "desc": "פס צבעוני בצד, כותרת, טקסט ותגיות קצרות.",
        "when": "כשיש הודעה חשובה או שינוי שחייבים לשים לב אליו (שינוי מועד, סגירה, עדכון נוהל): title=הנושא, text=ההסבר, items=תגיות קצרות כמו תאריך או קורס (label).",
        "template": '<div class="top"><span class="ic">{{icon}}</span><h3>{{title}}</h3></div><p>{{text}}</p><div>{{#items}}<span class="tag">{{label}}</span>{{/items}}</div>',
        "css": "&{border-inline-start:5px solid var(--bc)}.top{display:flex;gap:8px;align-items:center}.ic{color:var(--bc);display:grid}h3{margin:0;font-size:15.5px;font-weight:850}"
               "p{margin:7px 0 9px;line-height:1.55;font-size:14px}.tag{display:inline-block;margin:0 0 4px 6px;padding:2px 11px;border-radius:99px;font-size:12px;font-weight:800;background:color-mix(in srgb,var(--bc) 16%,var(--card))}",
        "sample": {"title": "המבחן הוקדם", "text": "מבחן הגמר באלגברה יתקיים ב-15 בינואר ולא ב-22 בו, באותה שעה ובאותו חדר.", "icon": "warning", "color": "red",
                   "items": [{"label": "15.1"}, {"label": "אלגברה לינארית"}]},
    },
    {
        "id": "x_person", "name": "כרטיס איש קשר או מרצה", "desc": "תמונה עגולה, שם ותפקיד, ושורות פרטים מסודרות.",
        "when": "כשמציגים מרצה, איש צוות או בעל תפקיד עם כמה פרטים (חדר, שעות קבלה, מייל, תחום): title=שם, subtitle=תפקיד, items=שורות פרטים (label=שם הפרט, value=הערך).",
        "template": '<div class="hd"><span class="av">{{icon}}</span><div><h3>{{title}}</h3><small>{{subtitle}}</small></div></div><p class="tx">{{text}}</p>'
                    '<div class="rows">{{#items}}<div class="r"><span>{{label}}</span><b>{{value}}</b></div>{{/items}}</div>',
        "css": ".hd{display:flex;gap:12px;align-items:center}.av{display:grid;place-items:center;width:46px;height:46px;border-radius:50%;background:color-mix(in srgb,var(--bc) 18%,var(--card));color:var(--bc);flex:none}"
               "h3{margin:0;font-size:16px;font-weight:850}.hd small{color:var(--muted);font-size:12.5px}.tx{margin:8px 0 0;font-size:13.5px;line-height:1.5}"
               ".rows{margin-top:10px;border-top:1px solid var(--line)}.r{display:flex;justify-content:space-between;gap:12px;padding:8px 0;border-bottom:1px dashed var(--line);font-size:13.5px}"
               ".r span{color:var(--muted)}.r b{font-weight:750;text-align:left}",
        "sample": {"title": "ד\"ר דנה כהן", "subtitle": "מרצה בכירה, מדעי המחשב", "icon": "user", "color": "teal",
                   "items": [{"label": "חדר", "value": "ב-214"}, {"label": "שעות קבלה", "value": "ג' 14:00-16:00"}, {"label": "מייל", "value": "dana@jct.ac.il"}]},
    },
    {
        "id": "x_steps3", "name": "שלבים בכרטיסים", "desc": "כרטיס לכל שלב עם מספר גדול, מסודרים בשורה (בטלפון נשברים לשורות).",
        "when": "כשיש תהליך קצר של 2 עד 5 שלבים שכדאי לראות במבט אחד (רישום, ערעור, בקשה): title=שם התהליך, items=השלבים (label=השלב, detail=פירוט קצר).",
        "template": '<h3>{{title}}</h3><div class="g">{{#items}}<div class="s"><span class="n"></span><b>{{label}}</b><small>{{detail}}</small></div>{{/items}}</div>',
        "css": "h3{margin:0 0 10px;font-size:15.5px;font-weight:850}.g{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:8px;counter-reset:st}"
               ".s{counter-increment:st;padding:11px;border-radius:12px;background:color-mix(in srgb,var(--bc) 9%,var(--card));border:1px solid color-mix(in srgb,var(--bc) 24%,var(--line))}"
               ".n::before{content:counter(st);display:grid;place-items:center;width:26px;height:26px;margin-bottom:7px;border-radius:50%;background:var(--bc);color:#fff;font-weight:850;font-size:13px}"
               ".s b{display:block;font-size:14px}.s small{display:block;margin-top:2px;color:var(--muted);font-size:12.5px}",
        "sample": {"title": "איך מגישים ערעור", "color": "purple", "items": [{"label": "נכנסים לפורטל", "detail": "עם שם משתמש של המכללה"}, {"label": "ממלאים טופס", "detail": "ומצרפים נימוק"}, {"label": "ממתינים לתשובה", "detail": "עד 14 יום"}]},
    },
]
