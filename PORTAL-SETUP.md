# Test Portal — Setup & Run Guide

Three services work together:

| Service | What | Runs at |
|---|---|---|
| Streamlit recruiter app | screening, MCQ generation, dispatch | http://localhost:8501 |
| FastAPI portal backend | OTP auth, test delivery, admin API | http://localhost:8000 |
| Next.js portal frontend | candidate test page + admin dashboard | http://localhost:3000 |

All three read the same MySQL database.

## 1. One-time setup

### a) Configure `.env` (project root)
Add to your existing `.env`:
```
MYSQL_URL=mysql+pymysql://root:YOUR_MYSQL_PASSWORD@localhost:3306
MYSQL_DB=ats_screener
JWT_SECRET=some-long-random-string-change-me
PORTAL_BASE_URL=http://localhost:3000
PORTAL_FRONTEND_ORIGIN=http://localhost:3000
```
Use your MySQL Workbench root password (or a dedicated MySQL user).

### b) Install Python deps + create the database
```powershell
cd D:\ATSResume
pip install -r requirements.txt
python -m core.init_db
```
This creates the `ats_screener` database, all tables, three seed departments,
and a super admin:
- **email:** `superadmin@ats.local`
- **password:** `admin123`  ← change it (create a new super admin in the
  portal's Manage page, then delete this one)

You can inspect everything in MySQL Workbench afterwards.

### c) Frontend deps (already installed if `node_modules` exists)
```powershell
cd D:\ATSResume\portal\frontend
npm install
```

## 2. Run (three terminals)

```powershell
# 1 — portal backend
cd D:\ATSResume
uvicorn portal.backend.main:app --reload --port 8000

# 2 — portal frontend
cd D:\ATSResume\portal\frontend
npm run dev

# 3 — recruiter app
cd D:\ATSResume
streamlit run app.py
```

## 3. End-to-end flow

1. **Streamlit → Screening tab**: score resumes, then use **🗄️ Save screening
   to database** (pick the department the job belongs to).
2. **Assessment tab**: generate/add questions → approve → in **🔗 Portal
   dispatch** set duration + link expiry → *Create links & email*. Each
   candidate gets a unique `http://localhost:3000/test/<uuid>` link.
3. **Candidate**: opens the link → enters their email (must match the
   invitation) → receives a 6-digit OTP by email → takes the timed, shuffled
   test → submits (or auto-submits at 0:00). One submission only.
4. **Admin portal**: `http://localhost:3000/admin`
   - Department admins see only their department's jobs, candidates, resume
     scores, and test results.
   - The super admin sees everything and manages departments/admins under
     **Manage**.

## Security notes

- OTPs are stored hashed (HMAC-SHA256), expire in 10 minutes, max 5 attempts.
- Correct answers never leave the server; options are shuffled per candidate.
- The timer is enforced server-side (30s grace), not just in the browser.
- Admin passwords are bcrypt-hashed; sessions are signed JWTs (12h).
- Set a real `JWT_SECRET` — without it a random one is used and every
  restart invalidates all logins.

## Database tables (visible in Workbench)

`departments`, `users` (admins), `jobs`, `candidates`, `tests`, `questions`,
`test_assignments` (the uuid is the link token), `candidate_answers`, `otps`.
