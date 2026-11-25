"""
The language of the student's question, and what follows from it: the engine's own messages ("I could not answer, try again") in that language, the campus names written in
that language's letters, and a check for cards that came out in Hebrew although the question was not. Pure functions, no network (tested in test_langmsg.py).
Found by testing: a French, Russian or Spanish question was answered with a Hebrew error line, and cards for an Arabic question had Hebrew names in the middle of Arabic text.
"""
import re
from typing import Any, Dict, List

_HE = re.compile(r"[א-ת]")
_AR = re.compile(r"[؀-ۿ]")
_RU = re.compile(r"[Ѐ-ӿ]")
_LAT = re.compile(r"[A-Za-zÀ-ÿ]")
_FR = re.compile(r"\b(?:combien|comment|bonjour|merci|quand|pourquoi|est-ce|quel(?:le)?s?|je|les|des|du|où|pas|pour|vous|est|bibliothèque|inscription)\b|[àâçéèêëîïôûù]", re.I)
_ES = re.compile(r"[¿¡ñ]|\b(?:cuánto|cuanto|dónde|donde|cómo|cuándo|qué|quiero|necesito|hola|gracias|los|las|una|por favor|está|cuesta|residencia|biblioteca)\b", re.I)
_DE = re.compile(r"[äöüß]|\b(?:wie|wann|wo|ich|ist|die|der|das|und|nicht|bitte|danke|kostet|bibliothek|geöffnet)\b", re.I)


def lang_of(text: Any) -> str:
    """he / ar / ru / fr / es / de / en (anything else Latin, or nothing to go by, is en)."""
    q = str(text or "").split("\n\n[")[0]
    if _HE.search(q):
        return "he"
    if _AR.search(q):
        return "ar"
    if _RU.search(q):
        return "ru"
    if not _LAT.search(q):
        return "he"
    score = {"fr": len(_FR.findall(q)), "es": len(_ES.findall(q)), "de": len(_DE.findall(q))}
    best = max(score, key=lambda k: score[k])
    return best if score[best] > 0 else "en"


MSG: Dict[str, Dict[str, str]] = {
    "no_answer": {
        "he": "לא הצלחתי לענות כרגע. אפשר לנסות שוב?", "en": "I couldn't answer just now. Could you try again?", "fr": "Je n'ai pas pu répondre pour le moment. Peux-tu réessayer ?",
        "es": "No pude responder en este momento. ¿Puedes intentarlo de nuevo?", "de": "Ich konnte gerade nicht antworten. Kannst du es noch einmal versuchen?",
        "ru": "Сейчас я не смог ответить. Попробуйте ещё раз?", "ar": "لم أتمكن من الإجابة الآن. هل يمكنك المحاولة مرة أخرى؟",
    },
    "no_request": {
        "he": "לא הצלחתי להשלים את הבקשה כרגע. נסה שוב בעוד רגע.", "en": "I couldn't complete the request just now. Please try again in a moment.", "fr": "Je n'ai pas pu terminer la demande. Réessaie dans un instant.",
        "es": "No pude completar la solicitud. Inténtalo de nuevo en un momento.", "de": "Ich konnte die Anfrage gerade nicht abschließen. Bitte versuche es gleich noch einmal.",
        "ru": "Сейчас не удалось выполнить запрос. Попробуйте ещё раз через минуту.", "ar": "لم أتمكن من إكمال الطلب الآن. حاول مرة أخرى بعد قليل.",
    },
    "no_cards": {
        "he": "לא הצלחתי לסדר את התשובה בכרטיסים. אפשר לשאול שוב במילים אחרות?", "en": "I couldn't lay the answer out in cards. Could you ask again in other words?",
        "fr": "Je n'ai pas pu présenter la réponse en cartes. Peux-tu reformuler ta question ?", "es": "No pude presentar la respuesta en tarjetas. ¿Puedes preguntar con otras palabras?",
        "de": "Ich konnte die Antwort nicht in Karten darstellen. Kannst du es mit anderen Worten fragen?", "ru": "Не удалось оформить ответ в карточках. Можете спросить другими словами?",
        "ar": "لم أتمكن من ترتيب الإجابة في بطاقات. هل يمكنك السؤال بكلمات أخرى؟",
    },
    "no_file": {
        "he": "לא הצלחתי למשוך את הקובץ כרגע. אפשר לנסות שוב בעוד רגע, או לפתוח אותו ישירות ב-Moodle.", "en": "I couldn't fetch the file just now. Try again in a moment, or open it directly in Moodle.",
        "fr": "Je n'ai pas pu récupérer le fichier. Réessaie dans un instant ou ouvre-le directement dans Moodle.", "es": "No pude obtener el archivo. Inténtalo de nuevo en un momento o ábrelo directamente en Moodle.",
        "de": "Ich konnte die Datei gerade nicht abrufen. Versuche es gleich noch einmal oder öffne sie direkt in Moodle.", "ru": "Не удалось получить файл. Попробуйте ещё раз или откройте его прямо в Moodle.",
        "ar": "لم أتمكن من جلب الملف الآن. حاول مرة أخرى بعد قليل أو افتحه مباشرة في Moodle.",
    },
    "stalled": {
        "he": "התשובה התעכבה יותר מדי ולא הצלחתי להשלים אותה. נסה לשלוח שוב.", "en": "The answer took too long and I couldn't finish it. Please send it again.",
        "fr": "La réponse a pris trop de temps. Renvoie ta question.", "es": "La respuesta tardó demasiado y no pude completarla. Envíala de nuevo.",
        "de": "Die Antwort hat zu lange gedauert. Bitte sende die Frage noch einmal.", "ru": "Ответ занял слишком много времени. Отправьте вопрос ещё раз.",
        "ar": "استغرقت الإجابة وقتًا طويلًا ولم أتمكن من إكمالها. أرسل السؤال مرة أخرى.",
    },
}


def msg(key: str, text: Any) -> str:
    """The engine's message `key` in the language of the student's question."""
    d = MSG.get(key) or {}
    return d.get(lang_of(text)) or d.get("en") or d.get("he") or ""


# Names that stay Hebrew in the material but must not appear in Hebrew letters inside a card in another language. (the campus names, the city, the system, the year)
_NAMES = [
    (r"מרכז אקדמי לב", {"en": "Lev Academic Center", "ru": "Академический центр Лев", "ar": "المركز الأكاديمي ليف"}),
    (r"המרכז האקדמי לב", {"en": "the Lev Academic Center", "ru": "Академический центр Лев", "ar": "المركز الأكاديمي ليف"}),
    (r"גבעת מרדכי", {"en": "Givat Mordechai", "ru": "Гиват-Мордехай", "ar": "جفعات مردخاي"}),
    (r"ירושלים", {"en": "Jerusalem", "ru": "Иерусалим", "ar": "القدس"}),
    (r"רמת גן", {"en": "Ramat Gan", "ru": "Рамат-Ган", "ar": "رمات غان"}),
    (r"לב-?נט", {"en": "Levnet", "ru": "Левнет", "ar": "ليفنت"}),
    (r"לוסטיג", {"en": "Lustig", "ru": "Люстиг", "ar": "لوستيغ"}),
    (r"תבונה", {"en": "Tavuna", "ru": "Твуна", "ar": "تفونا"}),
    (r"מבח\"ר|מבחר", {"en": "Mahar", "ru": "Махар", "ar": "مهار"}),
    (r"תשפ[\"״]ז", {"en": "5787", "ru": "5787", "ar": "5787"}),
    (r"תשפ[\"״]ו", {"en": "5786", "ru": "5786", "ar": "5786"}),
    (r"ש[\"״]ח", {"en": "₪", "ru": "₪", "ar": "₪"}),
    (r"(?<![א-ת])טל(?![א-ת])", {"en": "Tal", "ru": "Таль", "ar": "طال"}),
    (r"(?<![א-ת])לב(?![א-ת])", {"en": "Lev", "ru": "Лев", "ar": "ليف"}),
]
_LATIN = ("en", "fr", "es", "de")
_SKIP_KEYS = {"k", "s", "co", "ic", "an", "id", "cl", "css", "h", "u", "kind", "url"}
_KIND_TOKENS = {"phone", "email", "address", "whatsapp", "link", "text", "tel", "mail"}


def _walk(o: Any, fn) -> Any:
    if isinstance(o, str):
        return fn(o)
    if isinstance(o, list):
        return [_walk(x, fn) for x in o]
    if isinstance(o, dict):
        return {k: (v if k in _SKIP_KEYS else _walk(v, fn)) for k, v in o.items()}
    return o


def names_in(text: str, lang: str) -> str:
    """The names above written in the letters of `lang` (Latin languages use the English spelling)."""
    key = "en" if lang in _LATIN else lang
    if key == "he":
        return text
    for pat, tr in _NAMES:
        if key in tr:
            text = re.sub(pat, tr[key], text)
    return text


def localize_names(blocks: List[Dict[str, Any]], lang: str) -> List[Dict[str, Any]]:
    return _walk(blocks, lambda s: names_in(s, lang)) if lang != "he" else blocks


def hebrew_ratio(blocks: Any) -> float:
    """The share of Hebrew among the letters of every card text (0 to 1)."""
    he = other = 0

    def count(s: str) -> str:
        nonlocal he, other
        he += len(_HE.findall(s))
        other += len(re.findall(r"[A-Za-zÀ-ÿЀ-ӿ؀-ۿ]", s))
        return s
    _walk(blocks, count)
    return he / (he + other) if (he + other) else 0.0


def merge_translation(orig: Any, tr: Any) -> Any:
    """The translated text on the ORIGINAL structure: a translated string replaces an original one only at the same place, and never in a protected field (kind, colour, icon, links,
    actions, times, numbers). Anything that does not line up keeps the original."""
    if isinstance(orig, str):
        if not isinstance(tr, str) or not tr.strip():
            return orig
        if orig in _KIND_TOKENS or re.fullmatch(r"[\d\s:.,\-–/+%₪$€()]*", orig) or re.search(r"@|https?://|\d{2,3}-\d{6,7}", orig):
            return orig
        return tr
    if isinstance(orig, list):
        if not isinstance(tr, list) or len(tr) != len(orig):
            return orig
        return [merge_translation(a, b) for a, b in zip(orig, tr)]
    if isinstance(orig, dict):
        if not isinstance(tr, dict):
            return orig
        return {k: (v if k in _SKIP_KEYS else merge_translation(v, tr.get(k))) for k, v in orig.items()}
    return orig
