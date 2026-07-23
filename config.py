"""Central configuration for the AI Resume Screener.

Provider is swappable via the LLM_PROVIDER env var:
  - "groq"   (default): free, full-precision llama-3.1-8b-instant, rate-limited
  - "ollama"          : unlimited local model, no rate limits

Both are called through the OpenAI-compatible chat API, so only the base URL,
key, and model name change between them.
"""

import os

from dotenv import load_dotenv

load_dotenv()

PROVIDER = os.getenv("LLM_PROVIDER", "groq").lower()

# Per-provider connection + model settings.
_PROVIDERS = {
    "groq": {
        "base_url": "https://api.groq.com/openai/v1",
        "api_key": os.getenv("GROQ_API_KEY", ""),
        "model": os.getenv("GROQ_MODEL", "llama-3.1-8b-instant"),
        # Free-tier limits for llama-3.1-8b-instant. Cached (identical) system
        # prompts don't count, so we keep those byte-for-byte stable.
        "tpm": 6000,      # tokens per minute
        "rpm": 30,        # requests per minute
    },
    "ollama": {
        "base_url": "http://localhost:11434/v1",
        "api_key": "ollama",  # dummy; Ollama ignores it
        "model": os.getenv("OLLAMA_MODEL", "llama3.1:8b-instruct-q3_K_M"),
        "tpm": 10**9,     # effectively unlimited
        "rpm": 10**9,
    },
}

if PROVIDER not in _PROVIDERS:
    raise ValueError(f"Unknown LLM_PROVIDER={PROVIDER!r}; use 'groq' or 'ollama'.")

_cfg = _PROVIDERS[PROVIDER]
BASE_URL = _cfg["base_url"]
API_KEY = _cfg["api_key"]
MODEL = _cfg["model"]
TPM_LIMIT = _cfg["tpm"]
RPM_LIMIT = _cfg["rpm"]

# Generation options
TEMPERATURE = 0.1        # low -> consistent, repeatable scoring
REQUEST_TIMEOUT = 120
MAX_OUTPUT_TOKENS = 1500

# JSON call: how many times to re-prompt if the model returns invalid JSON
JSON_MAX_RETRIES = 2

# Rate limiter: stay this far under the hard TPM cap to leave headroom
TPM_SAFETY_FRACTION = 0.9

# PDF extraction: pages yielding fewer than this many chars are treated
# as scanned/image pages and sent to OCR (if OCR deps are installed).
SCANNED_PAGE_CHAR_THRESHOLD = 100

# Scoring: a must-have criterion with `met` below this counts as a gap.
MUST_HAVE_THRESHOLD = 0.5

# Weight of the overall job-description fit in the final score. The JD is the
# primary signal, so this is weighted like a strong criterion; explicit
# criteria (weight 1-10 each) refine it. With no criteria, the score is 100%
# JD fit.
OVERALL_FIT_WEIGHT = 12

# Automatic fallback: when the primary provider (Groq) hits rate limits,
# seamlessly retry the request against local Ollama.
FALLBACK_ENABLED = PROVIDER == "groq"
if FALLBACK_ENABLED:
    _fb = _PROVIDERS["ollama"]
    FALLBACK_BASE_URL = _fb["base_url"]
    FALLBACK_API_KEY = _fb["api_key"]
    FALLBACK_MODEL = _fb["model"]
