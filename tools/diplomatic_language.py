"""Language-neutral periods and bounded operative wording, independent of consent."""
import re

WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
         "seven": 7, "eight": 8, "nine": 9, "ten": 10,
         "один": 1, "одна": 1, "одно": 1, "два": 2, "две": 2, "три": 3, "четыре": 4,
         "пять": 5, "шесть": 6, "семь": 7, "восемь": 8, "девять": 9, "десять": 10,
         "одного": 1, "двух": 2, "трёх": 3, "трех": 3, "четырёх": 4, "четырех": 4, "пяти": 5,
         "un": 1, "une": 1, "deux": 2, "trois": 3, "quatre": 4, "cinq": 5,
         "sept": 7, "huit": 8, "neuf": 9, "dix": 10}
ADJECTIVES = {"одно": 1, "двух": 2, "трех": 3, "трёх": 3, "четырех": 4,
              "четырёх": 4, "пяти": 5, "шести": 6, "семи": 7, "восьми": 8, "девяти": 9, "десяти": 10}
EN = {n: word for word, n in list(WORDS.items())[:10]}


def period(value):
    """Return (amount, unit) only for an entire recognized period, never guess."""
    text = str(value or "").strip().lower().replace("-", " ")
    text = re.sub(r"^(?:на|сроком на|for)\s+", "", text)
    numeric_adjective = re.fullmatch(r"(\d+)\s*летн(?:ий|яя|ее|ие|его|ей|ем|им|их|ими|ему|юю)", text)
    if numeric_adjective and 1 <= int(numeric_adjective[1]) <= 100:
        return int(numeric_adjective[1]), "year"
    for stem, amount in ADJECTIVES.items():
        if re.fullmatch(stem + r"летн(?:ий|яя|ее|ие|его|ей|ем|им|их|ими|ему|юю)", text):
            return amount, "year"
    match = re.fullmatch(r"(\d+|" + "|".join(WORDS) + r")\s+(years?|months?|days?|лет|год(?:а|ов)?|месяц(?:а|ев)?|день|дня|дней|ans?|années?|mois|jours?)", text)
    if not match:
        return None
    amount = int(match[1]) if match[1].isdigit() else WORDS[match[1]]
    if not 1 <= amount <= 36500:
        return None
    unit = match[2]
    unit = "year" if re.fullmatch(r"years?|лет|год(?:а|ов)?|ans?|années?", unit) else "month" if re.fullmatch(r"months?|месяц(?:а|ев)?|mois", unit) else "day"
    return amount, unit


def canonical_period(value):
    parsed = period(value)
    if parsed is None:
        return value
    amount, unit = parsed
    return f"{EN.get(amount, str(amount))} {unit}" + ("s" if amount != 1 else "")


def localized_period(value, language="en"):
    parsed = period(value)
    if parsed is None:
        return str(value or "")
    amount, unit = parsed
    if language == "ru":
        forms = {"year": ("год", "года", "лет"), "month": ("месяц", "месяца", "месяцев"), "day": ("день", "дня", "дней")}[unit]
        index = 2 if 11 <= amount % 100 <= 14 else 0 if amount % 10 == 1 else 1 if amount % 10 in (2, 3, 4) else 2
        return f"{amount} {forms[index]}"
    if language == "fr":
        word = {"year": "an", "month": "mois", "day": "jour"}[unit]
        return f"{amount} {word}" + ("s" if amount > 1 and unit != "month" else "")
    return canonical_period(value)


def localized_country(tag, original, language):
    from hoi4_country_names import load_country_names
    # The tag is authoritative; a Russian input must not make an English reply
    # use an inflected Russian alias as its official country identity.
    if tag:
        return load_country_names(language).get(tag, tag)
    return original
