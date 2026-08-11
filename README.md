# 🤖 AI Resume Screener & Recruitment Portal

> An end-to-end AI-powered recruitment platform that screens resumes, generates assessments, conducts AI voice interviews, and manages the entire hiring pipeline — built with transparent, auditable scoring.

---

## ✨ Features

### 📄 Resume Screening (Streamlit Dashboard)
- **Batch PDF upload** — process multiple resumes simultaneously
- **Criteria builder** — define must-have + nice-to-have requirements, each weighted 1–10
- **0–100 match scoring** — deterministic, weighted aggregation (not a black box)
- **Explanation engine** — per-criterion `met` score with extracted evidence and reasoning
- **Gap analysis** — automatically identifies missing qualifications per candidate
- **Cover letter support** — auto-matched to resumes by filename
- **Scanned-PDF OCR** — fallback for image-based PDFs (Tesseract + Poppler)
- **Penalty system** — configurable deductions for missing certifications, experience gaps, etc.

### 📝 Assessment & Testing
- **AI-generated MCQs** — job-description-aware question generation via LLM
- **Question bank** — reusable question sets across multiple job postings
- **Timed tests** — configurable duration with server-side enforcement (30s grace period)
- **Shuffled options** — per-candidate randomization; correct answers never leave the server
- **Auto-scoring** — immediate results with detailed answer breakdown

### 🎙️ AI Voice Interviews
- **LiveKit integration** — real-time voice interview rooms
- **AI interview agent** — automated interviewer powered by OpenAI
- **Interview evaluation** — AI-scored responses with structured feedback
- **Multi-language support** — configurable interview languages

### 🌐 Candidate Portal (Next.js)
- **Self-service registration** — candidates register, browse jobs, and apply
- **OTP authentication** — email-based one-time passwords (hashed with HMAC-SHA256)
- **Test-taking interface** — clean, proctored assessment experience
- **Face detection proctoring** — MediaPipe-powered browser-side monitoring
- **Application dashboard** — candidates track their application status

### 👔 Admin Portal
- **Department-scoped access** — admins see only their department's data
- **Super admin controls** — full platform management, user/department CRUD
- **Real-time dashboard** — job stats, candidate pipeline, screening results
- **Bulk operations** — batch invite, auto-invite based on score thresholds

### 🔒 Security
- **Bcrypt-hashed passwords** — admin credentials
- **JWT sessions** — signed tokens with 12-hour expiry
- **Hashed OTPs** — HMAC-SHA256 with 10-minute expiry, max 5 attempts
- **Server-side timer enforcement** — prevents client-side time manipulation
- **CORS protection** — configurable origin whitelist

---

## 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                        Three Services                          │
├──────────────────┬──────────────────┬───────────────────────────┤
│  Streamlit App   │  FastAPI Backend │   Next.js Frontend        │
│  :8501           │  :8000           │   :3000                   │
│                  │                  │                           │
│  • Resume screen │  • REST API      │   • Candidate portal      │
│  • MCQ generation│  • OTP auth      │   • Admin dashboard       │
│  • Score review  │  • Test delivery │   • Test-taking UI        │
│  • Email dispatch│  • Interview mgmt│   • Proctoring            │
└────────┬─────────┴────────┬─────────┴──────────┬────────────────┘
         │                  │                    │
         └──────────────────┼────────────────────┘
                            │
              ┌─────────────┴─────────────┐
              │   SQL Server / MySQL      │
              │   (ats_screener)          │
              └─────────────┬─────────────┘
                            │
         ┌──────────────────┼──────────────────┐
         │                  │                  │
    ┌────┴─────┐    ┌───────┴──────┐   ┌──────┴───────┐
    │  Groq    │    │   Ollama     │   │   LiveKit    │
    │  (cloud) │    │   (local)    │   │   (voice)    │
    └──────────┘    └──────────────┘   └──────────────┘
```

### How Scoring Works
Two LLM calls per resume keep token usage inside Groq's free tier:
1. **Structure** — parse the resume PDF text into a structured JSON profile
2. **Score** — rate every criterion in one call, returning evidence per criterion

The final 0–100 score is a **deterministic** weighted aggregation in Python (`scoring.py`), making scoring consistent and fully auditable. A rate limiter in `llm.py` respects Groq's tokens-per-minute cap.

---

## 🚀 Quick Start

### Prerequisites
- **Python 3.10+**
- **Node.js 18+** (for the portal frontend)
- **SQL Server** (default) or **MySQL 8+**
- **Groq API key** ([free tier](https://console.groq.com/keys)) or **Ollama** (local)

### 1. Clone & Install

```bash
git clone https://github.com/mmuneebashraf/Abdullah_Imran_PIA_Intern_KSBL.git
cd Abdullah_Imran_PIA_Intern_KSBL/Proj2_AI_Resume_Screener_Portal

# Python environment
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# Frontend
cd portal/frontend
npm install
cd ../..
```

### 2. Configure Environment

```bash
cp .env.example .env
```

Edit `.env` and fill in your keys:

| Variable | Required | Description |
|---|---|---|
| `LLM_PROVIDER` | ✅ | `groq` (default, free tier) or `ollama` (local) |
| `GROQ_API_KEY` | If using Groq | Get from [console.groq.com/keys](https://console.groq.com/keys) |
| `EMAIL_MODE` | For emails | `brevo` (recommended) or `smtp` |
| `BREVO_API_KEY` | If using Brevo | From [app.brevo.com](https://app.brevo.com/settings/keys/api) |
| `SMTP_USER` / `SMTP_PASSWORD` | If using SMTP | Gmail App Password (enable 2FA first) |
| `DB_PROVIDER` | ✅ | `mssql` (default) or `mysql` |
| `MSSQL_SERVER` | If using MSSQL | e.g. `localhost\SQLEXPRESS` |
| `JWT_SECRET` | ✅ | Any long random string |

### 3. Initialize Database

```bash
python -m core.init_db
```

This creates the `ats_screener` database, all tables, seed departments, and a super admin:
- **Email:** `superadmin@ats.local`
- **Password:** `admin123` ← change this after first login

### 4. Run (Three Terminals)

```bash
# Terminal 1 — Streamlit recruiter dashboard
streamlit run app.py

# Terminal 2 — FastAPI portal backend
uvicorn portal.backend.main:app --reload --port 8000

# Terminal 3 — Next.js portal frontend
cd portal/frontend
npm run dev
```

| Service | URL |
|---|---|
| Recruiter Dashboard | http://localhost:8501 |
| Portal Backend API | http://localhost:8000 |
| Candidate/Admin Portal | http://localhost:3000 |

### Optional: AI Voice Interviews

```bash
# Terminal 4 — LiveKit server
cd livekit-server
./livekit-server.exe --dev
```

Set `OPENAI_API_KEY`, `LIVEKIT_API_KEY`, and `LIVEKIT_API_SECRET` in `.env`.

### Optional: OCR for Scanned PDFs

Install [Tesseract](https://github.com/UB-Mannheim/tesseract/wiki) and [Poppler](https://github.com/oschwartz10612/poppler-windows), and ensure both are on your PATH.

---

## 📋 End-to-End Recruitment Flow

1. **Screen** → Upload resumes in the Streamlit dashboard, configure criteria, score candidates
2. **Save** → Save screening results to the database (select department/job)
3. **Assess** → Generate MCQ questions → review & approve → set test duration & expiry
4. **Dispatch** → Create unique test links and email invitations to candidates
5. **Test** → Candidates open their link → verify email with OTP → take timed, proctored test
6. **Interview** → (Optional) Conduct AI voice interviews via LiveKit rooms
7. **Review** → Admin portal shows ranked results, scores, test answers, and interview evaluations

---

## 📁 Project Structure

```
├── app.py                    # Streamlit recruiter dashboard (main UI)
├── config.py                 # LLM provider config + rate limits
├── llm.py                    # OpenAI-compatible JSON client, rate limiter, retry
├── pdf.py                    # PDF text extraction + OCR fallback
├── scoring.py                # Structure → score → aggregate pipeline
├── schemas.py                # Pydantic models for all data types
├── mcq.py                    # MCQ question generation via LLM
├── gaps.py                   # Gap analysis engine
├── penalties.py              # Configurable penalty/deduction rules
├── charts.py                 # Plotly chart generation
├── emailer.py                # Email dispatch (SMTP / Brevo API)
├── db_bridge.py              # Database read/write bridge layer
├── storage.py                # File-based session storage fallback
├── interview_eval.py         # AI interview response evaluation
├── .env.example              # Environment variable template
├── requirements.txt          # Python dependencies
│
├── core/                     # Core business logic
│   ├── models.py             # SQLAlchemy ORM models (all tables)
│   ├── db.py                 # Database session management
│   ├── init_db.py            # Database initialization + seeding
│   ├── security.py           # Password hashing, JWT, auth helpers
│   ├── mailer.py             # Low-level email sending
│   ├── auto_invite.py        # Score-threshold auto-invitation
│   ├── bank.py               # Question bank management
│   ├── question_sets.py      # Reusable question set logic
│   ├── attempts.py           # Test attempt tracking + limits
│   ├── screening.py          # Screening result persistence
│   ├── cv_brief.py           # Resume summary generation
│   ├── templates.py          # Email & notification templates
│   ├── otps.py               # OTP generation + verification
│   ├── predictability.py     # Score consistency analysis
│   ├── interview_languages.py# Interview language configuration
│   └── job_delete.py         # Cascade job deletion
│
├── portal/
│   ├── backend/
│   │   ├── main.py           # FastAPI application entry point
│   │   ├── deps.py           # Dependency injection (auth, DB)
│   │   └── routers/
│   │       ├── admin.py      # Admin API routes
│   │       ├── candidate.py  # Candidate API routes
│   │       ├── interview.py  # Interview management routes
│   │       └── portal.py     # Portal auth + job listing routes
│   │
│   └── frontend/             # Next.js 14 application
│       ├── app/
│       │   ├── admin/        # Admin dashboard pages
│       │   ├── portal/       # Candidate portal pages
│       │   ├── interview/    # Voice interview room
│       │   └── test/         # Test-taking interface
│       ├── lib/
│       │   ├── api.js        # API client helpers
│       │   └── proctor.js    # Face-detection proctoring
│       └── public/
│           ├── mediapipe-wasm/  # MediaPipe WASM binaries
│           └── models/          # Face landmark model
│
├── tests/                    # Comprehensive test suite
│   ├── conftest.py           # Shared fixtures (in-memory DB)
│   ├── test_portal.py        # Portal API tests
│   ├── test_admin_pipeline.py# Admin workflow tests
│   ├── test_bank.py          # Question bank tests
│   ├── test_papers.py        # Test paper generation tests
│   ├── test_attempts.py      # Attempt limiting tests
│   ├── test_gate_and_auto_invite.py  # Auto-invite tests
│   ├── test_job_delete.py    # Cascade delete tests
│   ├── test_security_boundaries.py   # Auth boundary tests
│   └── ...
│
└── livekit-server/           # LiveKit server binary (voice interviews)
```

---

## 🧪 Running Tests

```bash
pytest                        # Run all tests
pytest tests/ -v              # Verbose output
pytest tests/test_portal.py   # Run specific test file
```

All tests use an **in-memory SQLite database** — no external database needed.

---

## 🛠️ Tech Stack

| Layer | Technology |
|---|---|
| **Recruiter UI** | Streamlit, Plotly |
| **Candidate/Admin Portal** | Next.js 14, React |
| **Backend API** | FastAPI, Uvicorn |
| **Database** | SQL Server (ODBC) / MySQL (PyMySQL) via SQLAlchemy |
| **AI/LLM** | Groq (free tier) / Ollama (local) via OpenAI-compatible API |
| **Voice Interviews** | LiveKit, OpenAI |
| **Proctoring** | MediaPipe Face Landmarker (WASM, browser-side) |
| **Email** | Brevo REST API / SMTP (Gmail) |
| **Auth** | bcrypt, JWT (PyJWT), HMAC-SHA256 OTPs |
| **Testing** | pytest with in-memory SQLite fixtures |

---

## 📜 License

This project was developed as part of an internship at **Pakistan International Airlines (PIA)** through **KSBL**.

---

## 👤 Author

**Abdullah Imran** — [GitHub](https://github.com/abdullahimran49)
