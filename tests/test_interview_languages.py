from core.interview_languages import configured_languages, normalize_languages
from portal.backend.routers.interview import _detected_language_code


def test_language_normalization_and_legacy_default():
    assert normalize_languages(["UR", "en", "ur"]) == ["ur", "en"]
    assert configured_languages(None) == ["en"]


def test_whisper_language_names_and_locales_are_recognized():
    assert _detected_language_code("Spanish") == "es"
    assert _detected_language_code("zh-CN") == "zh"
    assert _detected_language_code("Panjabi") == "pa"
