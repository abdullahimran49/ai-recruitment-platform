# AI Resume Screener

Score candidates against your hiring criteria with a local or free-tier LLM,
with transparent per-criterion reasoning — not a black box.

## Features
- Upload **multiple resume PDFs** at once
- **Criteria builder**: must-have + nice-to-have, each weighted 1–10
- **0–100 match score** per candidate (must-haves cap the score when missing)
- **Explanation engine**: per-criterion `met` score + evidence + reasoning
- **Optional cover letters**, auto-matched to resumes by filename
- Scanned-PDF **OCR fallback**
- **Provider-swappable**: Groq free tier (default) or local Ollama

## Setup

```bash
python -m venv .venv
.venv\Scripts\activate           # Windows
pip install -r requirements.txt
cp .env.example .env             # then fill in GROQ_API_KEY
```

Get a free Groq key at https://console.groq.com/keys — or set
`LLM_PROVIDER=ollama` in `.env` to use your local model instead.

Optional OCR (for scanned PDFs): install [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki)
and [Poppler](https://github.com/oschwartz10612/poppler-windows), and ensure both are on PATH.

## Run

```bash
python test_ollama.py            # verify the provider works
streamlit run app.py
```

## How it works
Two LLM calls per resume keep token use inside Groq's free tier:
1. **Structure** — parse the resume PDF text into a JSON profile.
2. **Score** — rate every criterion in one call, returning evidence per criterion.

The final 0–100 score is a **deterministic** weighted aggregation in Python
(`scoring.py`), so scoring is consistent and auditable. A rate limiter in
`llm.py` respects Groq's tokens-per-minute cap; identical system prompts are
cached by Groq and stop counting against it.

## Files
| File | Role |
|---|---|
| `app.py` | Streamlit UI |
| `config.py` | Provider selection + limits |
| `llm.py` | OpenAI-compatible JSON client, rate limiter, retry |
| `pdf.py` | PDF text extraction + OCR fallback |
| `scoring.py` | Structure → score → aggregate pipeline |
| `schemas.py` | Pydantic models |
