"""LLM client: OpenAI-compatible JSON calls with rate limiting + retry.

Works against Groq (free tier) or a local Ollama, selected in config.py.
When the primary provider is Groq and its rate limit is exhausted, the
client automatically falls back to a local Ollama instance.

A q3-quantized 8B model — and a rate-limited free tier — are both only
reliable on small, single-purpose calls that return structured JSON, so
every call here forces JSON output, re-prompts on parse failure, and is
throttled to respect the provider's tokens-per-minute cap.
"""

import json
import time
from collections import deque

from openai import OpenAI, APIError, RateLimitError

import config

_client = OpenAI(base_url=config.BASE_URL, api_key=config.API_KEY or "none")

# Fallback client: local Ollama, used when Groq rate limits are exhausted.
_fallback_client = None
if config.FALLBACK_ENABLED:
    _fallback_client = OpenAI(
        base_url=config.FALLBACK_BASE_URL,
        api_key=config.FALLBACK_API_KEY,
    )

_fallback_was_used = False  # Tracks whether fallback was triggered this session


class LLMError(Exception):
    """Raised when the model can't be reached or won't return valid JSON."""


# ---- Rate limiter -----------------------------------------------------------
# Rolling 60s windows for both tokens and requests. Before each call we wait
# until the estimated cost fits under the (safety-scaled) caps.

class _RateLimiter:
    def __init__(self):
        self._tokens = deque()   # (timestamp, token_count)
        self._reqs = deque()     # timestamps
        self.tpm = int(config.TPM_LIMIT * config.TPM_SAFETY_FRACTION)
        self.rpm = config.RPM_LIMIT

    def _prune(self, now):
        while self._tokens and now - self._tokens[0][0] > 60:
            self._tokens.popleft()
        while self._reqs and now - self._reqs[0] > 60:
            self._reqs.popleft()

    def acquire(self, est_tokens):
        while True:
            now = time.time()
            self._prune(now)
            used = sum(t for _, t in self._tokens)
            if used + est_tokens <= self.tpm and len(self._reqs) < self.rpm:
                return
            # Sleep until the oldest relevant entry ages out of the window.
            oldest = self._tokens[0][0] if self._tokens else now
            if self._reqs:
                oldest = min(oldest, self._reqs[0])
            time.sleep(max(0.5, 60 - (now - oldest) + 0.1))

    def record(self, tokens):
        now = time.time()
        self._tokens.append((now, tokens))
        self._reqs.append(now)


_limiter = _RateLimiter()


def estimate_tokens(text: str) -> int:
    """Rough token estimate (~4 chars/token) for rate-limit budgeting."""
    return max(1, len(text) // 4)


# ---- Core JSON call ---------------------------------------------------------

def chat_json(system: str, user: str, temperature: float | None = None,
              max_tokens: int | None = None) -> dict:
    """Send system+user prompts and return parsed JSON as a dict.

    Keep `system` byte-for-byte stable across calls so Groq caches it and
    those tokens stop counting against the rate limit.

    `temperature` overrides the low scoring default — use it for generative
    tasks (e.g. MCQ writing) where variety matters more than repeatability.
    `max_tokens` overrides the output budget — raise it for large batched
    JSON (e.g. several MCQs at once) so the response isn't truncated mid-object.
    """
    out_budget = max_tokens or config.MAX_OUTPUT_TOKENS
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    est = estimate_tokens(system + user) + out_budget
    last_raw = ""

    for attempt in range(config.JSON_MAX_RETRIES + 1):
        _limiter.acquire(est)
        content, used = _call_with_backoff(
            messages, temperature=temperature, max_tokens=out_budget)
        _limiter.record(used or est)
        last_raw = content
        try:
            return json.loads(content)
        except json.JSONDecodeError:
            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": "That was not valid JSON. Reply with ONLY the JSON "
                           "object — no prose, no markdown fences.",
            })
            est = estimate_tokens(content) + 200

    raise LLMError(
        f"Model returned invalid JSON after {config.JSON_MAX_RETRIES + 1} "
        f"attempts. Last output:\n{last_raw[:500]}"
    )


def _call_with_backoff(messages, max_retries=4, temperature=None,
                       max_tokens=None):
    """Call the chat endpoint, retrying with backoff on 429/transient errors.

    If Groq rate limits are exhausted and a fallback client is available,
    automatically reroutes to local Ollama.

    Returns (content, total_tokens_used).
    """
    temp = config.TEMPERATURE if temperature is None else temperature
    out_budget = max_tokens or config.MAX_OUTPUT_TOKENS
    delay = 2.0
    for i in range(max_retries):
        try:
            resp = _client.chat.completions.create(
                model=config.MODEL,
                messages=messages,
                temperature=temp,
                max_tokens=out_budget,
                response_format={"type": "json_object"},
                timeout=config.REQUEST_TIMEOUT,
            )
            content = resp.choices[0].message.content or ""
            used = getattr(resp, "usage", None)
            total = used.total_tokens if used else 0
            return content, total
        except RateLimitError:
            if i == max_retries - 1:
                if _fallback_client:
                    return _try_fallback(messages, temperature=temp,
                                         max_tokens=out_budget)
                raise LLMError("Rate limit hit; retries exhausted. Try again "
                               "later or switch LLM_PROVIDER=ollama.")
            time.sleep(delay)
            delay *= 2
        except APIError as e:
            if i == max_retries - 1:
                raise LLMError(f"LLM API error: {e}") from e
            time.sleep(delay)
            delay *= 2
    raise LLMError("Unreachable")


def _try_fallback(messages, temperature=None, max_tokens=None):
    """Attempt a single call via the local Ollama fallback client."""
    global _fallback_was_used
    temp = config.TEMPERATURE if temperature is None else temperature
    out_budget = max_tokens or config.MAX_OUTPUT_TOKENS
    try:
        resp = _fallback_client.chat.completions.create(
            model=config.FALLBACK_MODEL,
            messages=messages,
            temperature=temp,
            max_tokens=out_budget,
            response_format={"type": "json_object"},
            timeout=config.REQUEST_TIMEOUT,
        )
        content = resp.choices[0].message.content or ""
        used = getattr(resp, "usage", None)
        total = used.total_tokens if used else 0
        _fallback_was_used = True
        return content, total
    except Exception as e:  # noqa: BLE001
        raise LLMError(
            f"Groq rate limit exceeded and Ollama fallback failed: {e}. "
            "Ensure Ollama is running (ollama serve) with the model pulled."
        ) from e


def fallback_was_used() -> bool:
    """Return True if any call fell back to Ollama this session."""
    return _fallback_was_used


def ping():
    """Return (ok, message) describing whether the provider is reachable."""
    try:
        models = {m.id for m in _client.models.list().data}
    except Exception as e:  # noqa: BLE001 - surface any connection issue
        return False, f"Cannot reach {config.PROVIDER} at {config.BASE_URL}: {e}"

    if config.MODEL not in models:
        hint = (f"Run: ollama pull {config.MODEL}"
                if config.PROVIDER == "ollama"
                else "Check the model id / your GROQ_API_KEY.")
        return False, f"Connected, but model '{config.MODEL}' not found. {hint}"
    return True, f"{config.PROVIDER} reachable — model '{config.MODEL}' ready."
