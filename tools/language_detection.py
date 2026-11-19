"""Language detection and validation for the installed HOI4 localization set."""

import os
from pathlib import Path
import re


# The locale directories shipped with the installed HOI4 1.19.3 build.
HOI_LANGUAGE_LOCALES = {
    "l_english": "en",
    "l_french": "fr",
    "l_german": "de",
    "l_japanese": "ja",
    "l_korean": "ko",
    "l_polish": "pl",
    "l_russian": "ru",
    "l_simp_chinese": "zh",
    "l_spanish": "es",
    "l_braz_por": "pt",
}
LANGUAGE_NAMES = {
    "en": "English", "fr": "French", "de": "German", "ja": "Japanese",
    "ko": "Korean", "pl": "Polish", "ru": "Russian", "uk": "Ukrainian",
    "zh": "Simplified Chinese", "es": "Spanish", "pt": "Brazilian Portuguese",
}
# Ukrainian remains available as a chat-language extension, although it is not
# one of the localization directories shipped with this HOI4 installation.
SUPPORTED_LANGUAGES = frozenset((*LANGUAGE_NAMES.keys(),))

_WORD_RE = re.compile(r"[a-zA-ZÀ-ÖØ-öø-ÿĀ-žąćęłńóśźżА-Яа-яЁёІіЇїЄєҐґ]+", re.IGNORECASE)
_WORDS = {
    "en": {"the", "and", "we", "you", "our", "your", "with", "for", "this", "that",
           "offer", "propose", "proposal", "terms", "conditions", "agree", "discuss",
           "because", "what", "how", "would", "could", "from", "will", "not", "hello",
           "offers", "proposes", "pact", "aggression", "years", "guarantees", "independence"},
    "fr": {"le", "la", "les", "un", "une", "des", "nous", "vous", "notre", "votre",
           "avec", "pour", "mais", "propose", "proposition", "conditions", "garanties",
           "accord", "discuter", "territoire", "quelles", "comment", "pourquoi", "que",
           "laissez", "sont", "dans", "et", "est", "oui", "merci", "bonjour"},
    "de": {"der", "die", "das", "ein", "eine", "und", "wir", "sie", "unser", "mit",
           "für", "aber", "nicht", "vorschlag", "bedingungen", "garantien", "zustimmen",
           "diskutieren", "warum", "welche", "sind", "ist", "dass", "ja", "danke", "hallo"},
    "pl": {"i", "oraz", "że", "jest", "są", "nie", "my", "wy", "nasze", "wasze",
           "dla", "proponujemy", "propozycja", "warunki", "gwarancje", "umowa", "zgadzamy",
           "omówić", "dlaczego", "jakie", "tak", "dziękuję", "cześć"},
    "es": {"el", "la", "los", "las", "un", "una", "y", "que", "nosotros", "ustedes",
           "nuestro", "con", "para", "pero", "propuesta", "condiciones", "garantías",
           "acuerdo", "discutir", "porqué", "por", "cuáles", "estamos", "sí", "gracias", "hola"},
    "pt": {"o", "a", "os", "as", "um", "uma", "e", "que", "nós", "vocês", "nosso",
           "com", "para", "mas", "proposta", "condições", "garantias", "acordo", "discutir",
           "porquê", "quais", "estamos", "sim", "obrigado", "obrigada", "olá"},
    "ru": {"и", "в", "не", "что", "это", "мы", "вы", "наши", "ваши", "для", "как",
           "предлагаю", "предложение", "условия", "гарантии", "соглашение", "обсудить",
           "почему", "уточните", "переговоры", "готовы", "стороны", "да", "спасибо", "здравствуйте"},
    "uk": {"і", "та", "що", "це", "ми", "ви", "наші", "ваші", "для", "як", "але",
           "пропоную", "пропозиція", "умови", "гарантії", "угода", "обговорити", "чому",
           "уточніть", "переговори", "готові", "сторони", "так", "дякую", "привіт"},
}
_LATIN_LANGUAGES = ("en", "fr", "de", "pl", "es", "pt")
_CYRILLIC_LANGUAGES = ("ru", "uk")
_LANGUAGE_ALIASES = {
    **HOI_LANGUAGE_LOCALES,
    "english": "en", "french": "fr", "german": "de", "japanese": "ja",
    "korean": "ko", "polish": "pl", "russian": "ru", "ukrainian": "uk",
    "simplified_chinese": "zh", "chinese": "zh", "spanish": "es",
    "braz_por": "pt", "portuguese": "pt", "pt_br": "pt", "zh_cn": "zh",
}


def normalize_language(value):
    """Normalize a chat language code, HOI4 locale ID, or readable name."""
    if not isinstance(value, str):
        return None
    value = value.strip().casefold().replace("-", "_")
    if value in SUPPORTED_LANGUAGES:
        return value
    return _LANGUAGE_ALIASES.get(value)


def configured_game_language(settings_path=None, environ=None):
    """Return explicit EYES_CHAT_LANGUAGE or the locale in HOI4 settings.txt."""
    env = os.environ if environ is None else environ
    override = env.get("EYES_CHAT_LANGUAGE", "auto")
    if str(override).strip().casefold() not in {"", "auto"}:
        return normalize_language(override)
    if settings_path is None:
        settings_path = (
            Path.home() / "Documents" / "Paradox Interactive"
            / "Hearts of Iron IV" / "settings.txt"
        )
    try:
        settings = Path(settings_path).read_text(encoding="utf-8-sig")
    except OSError:
        return None
    match = re.search(r'^\s*language\s*=\s*["\']?([A-Za-z_]+)', settings, re.MULTILINE)
    return normalize_language(match.group(1)) if match else None


def language_name(language):
    return LANGUAGE_NAMES.get(normalize_language(language), str(language))


def _script_counts(text):
    return {
        "cyrillic": len(re.findall(r"[А-Яа-яЁёІіЇїЄєҐґ]", text)),
        "latin": len(re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿĀ-žąćęłńóśźż]", text)),
        "kana": len(re.findall(r"[ぁ-ゖァ-ヺー]", text)),
        "hangul": len(re.findall(r"[가-힣㄰-㆏]", text)),
        "han": len(re.findall(r"[一-鿿㐀-䶿]", text)),
    }


def _scores(words, text):
    scores = {language: sum(word in vocabulary for word in words)
              for language, vocabulary in _WORDS.items()}
    lowered = text.casefold()
    marks = {
        "fr": "àâçéèêëîïôùûüÿœ",
        "de": "äöüß",
        "pl": "ąćęłńóśźż",
        "es": "áéíóúñ¿¡",
        "pt": "ãõáéíóúç",
    }
    for language, characters in marks.items():
        scores[language] += sum(lowered.count(char) for char in characters) * 2
    return scores


def detect_language(text, fallback=None):
    """Detect supported languages; use fallback for short or genuinely ambiguous text."""
    fallback = normalize_language(fallback)
    if not isinstance(text, str) or not text.strip():
        return fallback
    words = [word.casefold() for word in _WORD_RE.findall(text)]
    counts = _script_counts(text)
    if counts["hangul"] >= 2:
        return "ko"
    if counts["kana"] >= 2:
        return "ja"
    if counts["han"] >= 2 and counts["kana"] == 0:
        if fallback in {"ja", "zh"}:
            return fallback
        return "zh"
    if not words:
        return fallback

    scores = _scores(words, text)
    if counts["cyrillic"] >= 2:
        if counts["cyrillic"] >= counts["latin"]:
            ru_uk = sorted(((scores[code], code) for code in _CYRILLIC_LANGUAGES), reverse=True)
            if ru_uk[0][0] == ru_uk[1][0]:
                return fallback if fallback in _CYRILLIC_LANGUAGES else "ru"
            return ru_uk[0][1]
    latin_ranked = sorted(((scores[code], code) for code in _LATIN_LANGUAGES), reverse=True)
    cyrillic_ranked = sorted(((scores[code], code) for code in _CYRILLIC_LANGUAGES), reverse=True)
    if counts["cyrillic"] >= 2 and cyrillic_ranked[0][0] > latin_ranked[0][0]:
        if cyrillic_ranked[0][0] == cyrillic_ranked[1][0]:
            return fallback if fallback in _CYRILLIC_LANGUAGES else "ru"
        return cyrillic_ranked[0][1]
    best_score, best_language = latin_ranked[0]
    second_score = latin_ranked[1][0]
    if best_score == 0 or best_score == second_score:
        # Clear language-specific diacritics disambiguate short Latin-script text.
        distinctive = [code for code, chars in {
            "fr": "àâçéèêëîïôùûüÿœ", "de": "äöüß", "pl": "ąćęłńóśźż",
            "es": "áíóúñ¿¡", "pt": "ãõç",
        }.items() if any(char in text.casefold() for char in chars)]
        return distinctive[0] if len(distinctive) == 1 else fallback
    return best_language


def matches_output_language(text, language):
    """Reject only clear language mismatches; tolerate proper nouns and loan words."""
    language = normalize_language(language)
    if not isinstance(text, str) or language not in SUPPORTED_LANGUAGES or not text.strip():
        return False
    detected = detect_language(text)
    counts = _script_counts(text)
    if language == "ja" and counts["han"] >= 2 and counts["kana"] == 0:
        # Kanji-only names/short phrases are indistinguishable from Chinese
        # without context, so do not reject them on script evidence alone.
        return True
    # If the short text is ambiguous, prefer showing it to falsely rejecting a
    # correct answer that contains a country name, tag, loan word, or quotation.
    return detected is None or detected == language
