"""Supported languages for automated voice interviews.

The stored value is a short, stable code.  Labels are for prompts/admin UI and
BCP-47 tags are used by the browser's speech-synthesis voice picker.
"""

LANGUAGES = {
    "en": {"label": "English", "tts_locale": "en-US"},
    "ur": {"label": "Urdu", "tts_locale": "ur-PK"},
    "ar": {"label": "Arabic", "tts_locale": "ar-SA"},
    "hi": {"label": "Hindi", "tts_locale": "hi-IN"},
    "pa": {"label": "Punjabi", "tts_locale": "pa-IN"},
    "es": {"label": "Spanish", "tts_locale": "es-ES"},
    "fr": {"label": "French", "tts_locale": "fr-FR"},
    "de": {"label": "German", "tts_locale": "de-DE"},
    "pt": {"label": "Portuguese", "tts_locale": "pt-BR"},
    "zh": {"label": "Mandarin Chinese", "tts_locale": "zh-CN"},
}

DEFAULT_LANGUAGES = ["en"]


def normalize_languages(value) -> list[str]:
    """Validate/deduplicate language codes while preserving admin order."""
    if not isinstance(value, list) or not value:
        raise ValueError("select at least one interview language")
    result = []
    for raw in value:
        code = str(raw).strip().lower()
        if code not in LANGUAGES:
            raise ValueError(f"unsupported interview language: {raw}")
        if code not in result:
            result.append(code)
    return result


def configured_languages(value) -> list[str]:
    """Read persisted data safely; old/null rows remain English interviews."""
    try:
        return normalize_languages(value)
    except ValueError:
        return DEFAULT_LANGUAGES.copy()


def language_payload(value) -> list[dict[str, str]]:
    return [
        {"code": code, **LANGUAGES[code]}
        for code in configured_languages(value)
    ]

