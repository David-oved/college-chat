"""תאריך עברי ותיאור זמן יחסי (בעוד/לפני) בעברית - כדי שהמודל לא יצטרך לחשב לבד אם מועד כבר עבר ובכמה."""
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

ISRAEL_TZ = ZoneInfo("Asia/Jerusalem")
_HEBREW_EPOCH = -1373427        # היום הקבוע (R.D.) של התחלת הלוח העברי
_MONTHS = {1: "ניסן", 2: "אייר", 3: "סיוון", 4: "תמוז", 5: "אב", 6: "אלול", 7: "תשרי", 8: "חשוון", 9: "כסלו", 10: "טבת", 11: "שבט", 12: "אדר", 13: "אדר ב׳"}
_WEEKDAYS = ["ב'", "ג'", "ד'", "ה'", "ו'", "ש'", "א'"]      # לפי date.weekday(): שני=0


def _leap(y: int) -> bool:
    return (7 * y + 1) % 19 < 7


def _elapsed_days(y: int) -> int:
    months = (235 * y - 234) // 19
    parts = 12084 + 13753 * months
    day = months * 29 + parts // 25920
    if (3 * (day + 1)) % 7 < 3:
        day += 1
    return day


def _new_year(y: int) -> int:
    ny0, ny1, ny2 = _elapsed_days(y - 1), _elapsed_days(y), _elapsed_days(y + 1)
    corr = 2 if ny2 - ny1 == 356 else (1 if ny1 - ny0 == 382 else 0)
    return _HEBREW_EPOCH + ny1 + corr


def _year_days(y: int) -> int:
    return _new_year(y + 1) - _new_year(y)


def _month_days(y: int, m: int) -> int:
    if m in (2, 4, 6, 10, 13):
        return 29
    if m == 12:
        return 30 if _leap(y) else 29
    if m == 8:
        return 30 if _year_days(y) % 10 == 5 else 29
    if m == 9:
        return 29 if _year_days(y) % 10 == 3 else 30
    return 30


def _fixed(y: int, m: int, d: int) -> int:
    last = 13 if _leap(y) else 12
    total = _new_year(y) + d - 1
    if m < 7:
        for mm in range(7, last + 1):
            total += _month_days(y, mm)
        for mm in range(1, m):
            total += _month_days(y, mm)
    else:
        for mm in range(7, m):
            total += _month_days(y, mm)
    return total


def from_gregorian(d: date):
    """(שנה, חודש 1-13, יום) עבריים."""
    rd = d.toordinal()
    year = int((rd - _HEBREW_EPOCH) / (35975351 / 98496)) + 1
    while rd >= _new_year(year + 1):
        year += 1
    while rd < _new_year(year):
        year -= 1
    last = 13 if _leap(year) else 12
    month = 6
    for m in list(range(7, last + 1)) + list(range(1, 7)):       # השנה מתחילה בתשרי (7) ומסתיימת באלול (6)
        if rd <= _fixed(year, m, _month_days(year, m)):
            month = m
            break
    day = rd - _fixed(year, month, 1) + 1
    return year, month, day


def _gematria(n: int) -> str:
    ones = ["", "א", "ב", "ג", "ד", "ה", "ו", "ז", "ח", "ט"]
    tens = ["", "י", "כ", "ל", "מ", "נ", "ס", "ע", "פ", "צ"]
    hundreds = ["", "ק", "ר", "ש", "ת"]
    s = ""
    while n >= 400:
        s += "ת"
        n -= 400
    s += hundreds[n // 100]
    n %= 100
    if n == 15:
        s += "טו"
    elif n == 16:
        s += "טז"
    else:
        s += tens[n // 10] + ones[n % 10]
    return s


def _with_quotes(s: str) -> str:
    return s + "׳" if len(s) == 1 else s[:-1] + "״" + s[-1]


def hebrew_date_str(d: date) -> str:
    y, m, day = from_gregorian(d)
    name = _MONTHS[m]
    if m == 12 and _leap(y):
        name = "אדר א׳"
    return f"{_with_quotes(_gematria(day))} ב{name} {_with_quotes(_gematria(y % 1000))}"


def weekday_he(d: date) -> str:
    return _WEEKDAYS[d.weekday()]


def _span(secs: int) -> str:
    secs = abs(int(secs))
    if secs < 3600:
        m = max(1, secs // 60)
        return f"{m} דקות"
    if secs < 86400:
        h = round(secs / 3600)
        return "שעה" if h == 1 else f"{h} שעות"
    dd = round(secs / 86400)
    return "יום" if dd == 1 else f"{dd} ימים"


def describe_ts(ts, now=None) -> str:
    """"יום ג' 17/11/2026 21:00 (ח׳ בכסלו תשפ״ז) - בעוד 5 שעות" או "... - עבר המועד לפני 3 ימים". שעון ישראל."""
    try:
        ts = int(ts)
    except Exception:
        return "ללא מועד"
    if not ts:
        return "ללא מועד"
    dt = datetime.fromtimestamp(ts, ISRAEL_TZ)
    now = int(now if now is not None else time.time())
    diff = ts - now
    base = f"יום {weekday_he(dt.date())} {dt.strftime('%d/%m/%Y %H:%M')} ({hebrew_date_str(dt.date())})"
    if diff < 0:
        return base + f" - עבר המועד לפני {_span(diff)}"
    return base + f" - בעוד {_span(diff)}"
