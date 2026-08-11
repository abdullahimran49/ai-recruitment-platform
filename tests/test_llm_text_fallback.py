from types import SimpleNamespace

import llm


class _ChatCompletions:
    def __init__(self, result=None, error=None):
        self.result = result
        self.error = error
        self.kwargs = None

    def create(self, **kwargs):
        self.kwargs = kwargs
        if self.error:
            raise self.error
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(
                content=self.result))],
            usage=None,
        )


def _client(completions):
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def test_chat_text_returns_primary_plain_text(monkeypatch):
    primary = _ChatCompletions(result="Hello")
    monkeypatch.setattr(llm, "_client", _client(primary))

    assert llm.chat_text([{"role": "user", "content": "Hi"}]) == "Hello"
    assert "response_format" not in primary.kwargs


def test_chat_text_falls_back_to_ollama_on_rate_limit(monkeypatch):
    class FakeRateLimit(Exception):
        pass

    primary = _ChatCompletions(error=FakeRateLimit("free tier exhausted"))
    fallback = _ChatCompletions(result="Local reply")
    monkeypatch.setattr(llm, "RateLimitError", FakeRateLimit)
    monkeypatch.setattr(llm, "_client", _client(primary))
    monkeypatch.setattr(llm, "_fallback_client", _client(fallback))
    monkeypatch.setattr(llm.time, "sleep", lambda _seconds: None)

    result = llm.chat_text([{"role": "user", "content": "Hi"}])

    assert result == "Local reply"
    assert fallback.kwargs["model"] == llm.config.FALLBACK_MODEL
    assert "response_format" not in fallback.kwargs
