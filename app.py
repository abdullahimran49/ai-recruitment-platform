# -*- coding: utf-8 -*-
"""AI Resume Screener — Streamlit UI.

Run with:  streamlit run app.py

Tabs:
  2. Dashboard      — ranked results, per-candidate charts + explanations
  3. Communication  — Top-N selection, LLM-drafted emails, manual edit, send
  4. Assessment     — JD-based MCQ generation, review/approve, dispatch
"""

import difflib
import os
import re

import pandas as pd
import streamlit as st

import charts
import config
import emailer
import llm
import mcq as mcq_mod
import pdf
import storage
from core import predictability
from schemas import Criteria, Criterion, MCQQuestion, MCQTest, PenaltyRule
from scoring import process_resume

# MySQL bridge is optional: without MYSQL_URL (or its deps) the app keeps
# working on file/session storage and portal features are hidden.
try:
    import db_bridge
    _DB_ON = db_bridge.db_enabled()
except Exception:  # noqa: BLE001
    _DB_ON = False

st.set_page_config(page_title="Recruiter Console — ATS", page_icon="🧭",
                   layout="wide")

# ---- Look & feel (HR-friendly polish) ----------------------------------------
# Injected CSS keeps the recruiter console clean and approachable for
# non-technical users: softer cards, rounded controls, clearer headings.
st.markdown("""
<style>
  :root { --acc:#7c74ff; --acc2:#8f88ff; --ink:#eceef3; --mut:#98a1b2;
          --line:#262b35; --soft:#171a21; --card:#1d212a; }
  .block-container { padding-top: 1.4rem; max-width: 1500px; }
  h1, h2, h3 { letter-spacing: -0.02em; }
  /* App header banner */
  .app-hero {
    background: linear-gradient(135deg, #5b54e6, #7c74ff);
    color: #fff; border-radius: 16px; padding: 20px 26px; margin-bottom: 8px;
    box-shadow: 0 16px 40px -20px rgba(124,116,255,.55);
  }
  .app-hero h1 { color:#fff; margin:0; font-size: 26px; }
  .app-hero p { color: rgba(255,255,255,.92); margin: 4px 0 0; font-size: 14px; }
  /* Tabs: pill style */
  .stTabs [data-baseweb="tab-list"] { gap: 4px; border-bottom: 1px solid var(--line); }
  .stTabs [data-baseweb="tab"] {
    height: auto; padding: 10px 18px; border-radius: 10px 10px 0 0;
    font-weight: 600; color: var(--mut);
  }
  .stTabs [aria-selected="true"] { color: var(--acc); background: var(--soft); }
  /* Buttons */
  .stButton > button, .stDownloadButton > button {
    border-radius: 10px; font-weight: 600; border: 1px solid var(--line);
    transition: transform .08s, box-shadow .15s;
  }
  .stButton > button[kind="primary"] {
    background: var(--acc); border-color: var(--acc); color: #fff;
    box-shadow: 0 8px 20px -10px rgba(124,116,255,.7);
  }
  .stButton > button:hover { transform: translateY(-1px); border-color: var(--acc); }
  /* Inputs */
  .stTextInput input, .stNumberInput input, .stTextArea textarea,
  .stSelectbox div[data-baseweb="select"] > div { border-radius: 10px; }
  /* Metric cards */
  [data-testid="stMetric"] {
    background: var(--card); border: 1px solid var(--line); border-radius: 14px;
    padding: 14px 16px;
  }
  /* Expanders softer */
  [data-testid="stExpander"] { border-radius: 12px; border-color: var(--line); }
  [data-testid="stExpander"] summary { border-radius: 12px; }
</style>
""", unsafe_allow_html=True)

st.markdown("""
<div class="app-hero">
  <h1>🧭 Recruiter Console</h1>
  <p>Screen resumes, build assessments, and manage candidates — no technical
  setup required. Work left to right through the tabs below.</p>
</div>
""", unsafe_allow_html=True)


# ---- Helpers ------------------------------------------------------------------

_STOP = {"resume", "cv", "cover", "letter", "coverletter", "final", "v1", "v2", "doc"}


def _stem_tokens(name: str) -> str:
    stem = re.sub(r"\.pdf$", "", name, flags=re.I)
    parts = re.split(r"[^a-z0-9]+", stem.lower())
    return " ".join(p for p in parts if p and p not in _STOP)


def match_cover_letters(resume_names, cover_files):
    mapping = {}
    if not cover_files:
        return mapping
    cover_keys = [(cf, _stem_tokens(cf.name)) for cf in cover_files]
    for rn in resume_names:
        rkey = _stem_tokens(rn)
        best, best_ratio = None, 0.0
        for cf, ckey in cover_keys:
            ratio = difflib.SequenceMatcher(None, rkey, ckey).ratio()
            if ratio > best_ratio:
                best, best_ratio = cf, ratio
        if best is not None and best_ratio >= 0.6:
            try:
                mapping[rn] = pdf.extract_text(best.getvalue())
            except Exception:  # noqa: BLE001
                pass
    return mapping


def fmt_experience(years: float, n_roles: int) -> str:
    if years >= 1:
        return f"{years:g} yrs"
    if years > 0:
        return f"~{max(1, round(years * 12))} mo"
    return "<1 yr" if n_roles else "None listed"


def df_to_records(df):
    out = []
    for _, row in df.iterrows():
        text = str(row.get("criterion", "")).strip()
        if text:
            try:
                w = int(row.get("weight", 5))
            except (TypeError, ValueError):
                w = 5
            out.append({"criterion": text, "weight": max(1, min(10, w))})
    return out


def build_criteria(must_records, nice_records) -> Criteria:
    must, nice, cid = [], [], 1
    for r in must_records:
        must.append(Criterion(id=cid, text=r["criterion"], weight=r["weight"]))
        cid += 1
    for r in nice_records:
        nice.append(Criterion(id=cid, text=r["criterion"], weight=r["weight"]))
        cid += 1
    return Criteria(must_have=must, nice_to_have=nice)


def build_penalty_rules(penalty_df) -> list[PenaltyRule]:
    """Convert the penalty editor DataFrame into a list of PenaltyRule objects."""
    rules = []
    rid = 1
    for _, row in penalty_df.iterrows():
        cat = str(row.get("category", "")).strip().lower()
        cond = str(row.get("condition", "")).strip()
        val = str(row.get("value", "")).strip()
        if not (cat and cond and val):
            continue
        try:
            pts = float(row.get("points", 5))
        except (TypeError, ValueError):
            pts = 5.0
        enabled = bool(row.get("enabled", True))
        rules.append(PenaltyRule(
            id=rid, category=cat, condition=cond,
            field_value=val, points=max(0, min(50, pts)),
            enabled=enabled,
        ))
        rid += 1
    return rules


def clean_intro(text: str) -> str:
    """Strip any greeting ('Dear ...') and sign-off the LLM added, since the
    email template supplies its own personalized greeting + signature."""
    lines = text.strip().splitlines()
    # Drop a leading salutation line.
    while lines and re.match(r"^\s*(dear|hi|hello|greetings)\b", lines[0], re.I):
        lines.pop(0)
    # Drop a trailing sign-off block (e.g. "Best regards," / "The ... Team").
    _signoff = re.compile(
        r"^\s*(best|kind|warm)\s+regards|^\s*(sincerely|thanks|thank you|regards|"
        r"cheers)\b|team\s*$|recruit", re.I)
    while lines and (not lines[-1].strip() or _signoff.search(lines[-1])):
        lines.pop()
    return "\n".join(lines).strip()


def ok_results():
    return sorted(
        [r for r in st.session_state.get("results", []) if not r.error],
        key=lambda r: r.score, reverse=True,
    )


def top_n_results(n: int):
    return ok_results()[:n]


# ---- Session defaults ----------------------------------------------------------

_defaults = {
    "editor_version": 0,
    "pass_threshold": 60,
    "penalize_gaps": False,
    "gap_min_months": 6,
    "gap_points": 5.0,
    "gap_max": 15.0,
    "penalty_max": 30.0,
    "drafts": {},          # filename -> {"to","subject","body"}
    "mcq_test": None,      # MCQTest
    "mcq_intro": "",       # editable intro paragraph for the assessment email
    "custom_questions": [],  # list of dicts for user-defined MCQs
    "custom_ratio": 50,      # percentage slider: % of custom questions
}
for k, v in _defaults.items():
    st.session_state.setdefault(k, v)
if "must_df" not in st.session_state:
    st.session_state.must_df = pd.DataFrame(
        [{"criterion": "3+ years Python", "weight": 9}])
    st.session_state.nice_df = pd.DataFrame(
        [{"criterion": "Kubernetes", "weight": 4}])
if "penalty_df" not in st.session_state:
    st.session_state.penalty_df = pd.DataFrame(
        [{"category": "education", "condition": "No Bachelor's degree",
          "value": "bachelor", "points": 10.0, "enabled": True}])


# ---- Sidebar -------------------------------------------------------------------

with st.sidebar:
    st.header("⚙️ AI Engine")
    st.write(f"**{config.PROVIDER}** · `{config.MODEL}`")
    if config.PROVIDER == "groq" and not config.API_KEY:
        st.warning("No GROQ_API_KEY set. Add it to `.env` or switch "
                   "`LLM_PROVIDER=ollama`.")
    if st.button("Check connection", width="stretch"):
        ok, msg = llm.ping()
        (st.success if ok else st.error)(msg)
    if llm.fallback_was_used():
        st.info("⚠️ Some calls fell back to local Ollama (Groq rate limit).")
    st.caption("✉️ SMTP: " + ("configured ✓" if emailer.smtp_configured()
                              else "not configured — drafts only"))
    if not pdf.ocr_available():
        st.caption("OCR unavailable — scanned PDFs may extract poorly.")

    st.divider()
    st.header("📁 Saved Job Templates")
    # Source of truth is MySQL when it's configured; otherwise fall back to
    # the local jobs.json store.
    _jobs_from_db = _DB_ON
    try:
        jobs = db_bridge.list_saved_jobs() if _DB_ON else storage.load_jobs()
    except Exception as e:  # noqa: BLE001
        jobs = storage.load_jobs()
        _jobs_from_db = False
        st.caption(f"⚠️ Falling back to local jobs (DB read failed: {e})")
    st.caption("Source: " + ("MySQL database" if _jobs_from_db
                             else "local jobs.json"))
    if jobs:
        picked = st.selectbox("Load a saved job", list(jobs.keys()),
                              index=None, placeholder="Choose…")
        c_load, c_del = st.columns(2)
        if c_load.button("Load", width="stretch", disabled=not picked):
            job = jobs[picked]
            # Autofill the job name so it doesn't have to be re-entered.
            st.session_state.job_name = picked
            st.session_state.jd = job.get("jd", "")
            st.session_state.must_df = pd.DataFrame(
                job.get("must", []) or [{"criterion": "", "weight": 5}])
            st.session_state.nice_df = pd.DataFrame(
                job.get("nice", []) or [{"criterion": "", "weight": 5}])
            pen_data = job.get("penalties", [])
            if pen_data:
                st.session_state.penalty_df = pd.DataFrame(pen_data)
            else:
                st.session_state.penalty_df = pd.DataFrame(
                    columns=["category", "condition", "value", "points", "enabled"])
            s = job.get("settings", {})
            st.session_state.pass_threshold = s.get(
                "pass_threshold", job.get("pass_threshold", 60))
            st.session_state.penalize_gaps = s.get("penalize_gaps", False)
            st.session_state.gap_min_months = s.get("gap_min_months", 6)
            st.session_state.gap_points = s.get("gap_points", 5.0)
            st.session_state.gap_max = s.get("gap_max", 15.0)
            st.session_state.penalty_max = s.get("penalty_max", 30.0)
            # Preselect the job's department for a subsequent Save to MySQL.
            if job.get("department_id"):
                st.session_state.loaded_department_id = job["department_id"]
            # Pull the ACTUAL resume PDFs of everyone who applied through the
            # job portal into the Screen Resumes tab, so HR screens/reviews/
            # dispatches them by hand exactly like a manual upload. Applicants
            # already sent a test (or withdrawn) are hidden.
            st.session_state.pop("loaded_candidates_note", None)
            st.session_state.portal_resumes = []
            st.session_state.portal_uuid_by_email = {}
            if _jobs_from_db and job.get("uuid"):
                try:
                    files, hidden = db_bridge.load_job_resume_files(job["uuid"])
                    st.session_state.portal_resumes = files
                    st.session_state.portal_uuid_by_email = {
                        (f["email"] or "").lower(): f["candidate_uuid"]
                        for f in files if f.get("email")}
                    st.session_state.db_job_uuid = job["uuid"]
                    st.session_state.loaded_candidates_note = (len(files), hidden)
                except Exception as e:  # noqa: BLE001
                    st.session_state.loaded_candidates_note = ("error", str(e))
            st.session_state.editor_version += 1
            st.rerun()
        if c_del.button("Delete", width="stretch", disabled=not picked):
            if _jobs_from_db:
                db_bridge.delete_job_by_title(picked)
            else:
                storage.delete_job(picked)
            st.rerun()
    else:
        st.caption("No saved jobs yet.")


# ---- Header --------------------------------------------------------------------

# Feedback after loading a saved job's candidates from the portal.
_note = st.session_state.pop("loaded_candidates_note", None)
if _note:
    if _note[0] == "error":
        st.warning(f"Couldn't load this job's résumés: {_note[1]}")
    else:
        n_loaded, n_hidden = _note
        msg = (f"✅ Loaded **{n_loaded}** portal résumé(s) into the **Screen "
               f"Resumes** tab — press **Evaluate Candidates** to score them, "
               f"then dispatch a test in **Send Tests**.")
        if n_hidden:
            msg += f" ({n_hidden} already invited to a test are hidden.)"
        if n_loaded == 0:
            msg = ("No new portal résumés to load for this job"
                   + (f" ({n_hidden} already invited are hidden)." if n_hidden
                      else " yet. New applications appear here when you reload "
                           "the job."))
        st.success(msg)

tab_screen, tab_dash, tab_comm, tab_mcq = st.tabs(
    ["1️⃣ Screen Resumes", "2️⃣ Review Results", "3️⃣ Email Candidates", "4️⃣ Send Tests"])


# ================================================================================
# TAB 1 — SCREENING
# ================================================================================

with tab_screen:
    st.subheader("1. Define the Job Description")
    st.text_area(
        "This is the primary input — the AI uses this to evaluate all candidates.",
        key="jd", height=220,
        placeholder="Senior Python Engineer\n\nWe're looking for someone to…",
    )

    c_name, c_save = st.columns([3, 1])
    job_name = c_name.text_input("Save this job as", key="job_name",
                                 placeholder="e.g. Senior Python Engineer")
    if c_save.button("💾 Save job", width="stretch"):
        name = (job_name or "").strip()
        if not name:
            st.warning("Give the job a name to save it.")
        else:
            pen_records = st.session_state.penalty_df.to_dict(orient="records")
            storage.save_job(
                name, st.session_state.get("jd", ""),
                df_to_records(st.session_state.must_df),
                df_to_records(st.session_state.nice_df),
                penalties=pen_records,
                settings={
                    "pass_threshold": st.session_state.pass_threshold,
                    "penalize_gaps": st.session_state.penalize_gaps,
                    "gap_min_months": st.session_state.gap_min_months,
                    "gap_points": st.session_state.gap_points,
                    "gap_max": st.session_state.gap_max,
                    "penalty_max": st.session_state.penalty_max,
                },
            )
            st.success(f"Saved “{name}”.")

    with st.expander("🎛️ Screening settings", expanded=True):
        c1, c2 = st.columns(2)
        with c1:
            st.slider(
                "Minimum passing score", 0, 100, key="pass_threshold",
                help="Candidates below this score are marked as not passing. "
                     "E.g. 40 for an intern role, 80 for a senior role.")
        with c2:
            st.toggle("Penalize employment gaps", key="penalize_gaps",
                      help="Subtract points for gaps between roles, detected "
                           "deterministically from resume dates.")
            if st.session_state.penalize_gaps:
                g1, g2, g3 = st.columns(3)
                g1.number_input("Min gap (months)", 2, 24, key="gap_min_months")
                g2.number_input("Points per gap", 1.0, 20.0, key="gap_points", step=1.0)
                g3.number_input("Max penalty", 5.0, 50.0, key="gap_max", step=5.0)

    with st.expander("🎯 Bonus Criteria (Optional — look for specific skills)"):
        st.caption("Leave empty to score purely on the job description. "
                   "Weight 1â€“10. Missing must-haves cap the score. "
                   "To delete a row: tick its checkbox and press Delete.")
        v = st.session_state.editor_version
        st.markdown("**Must-have**")
        must_df = st.data_editor(
            st.session_state.must_df, num_rows="dynamic", key=f"must_editor_{v}",
            width="stretch", hide_index=True,
            column_config={"weight": st.column_config.NumberColumn(
                min_value=1, max_value=10, step=1)},
        )
        st.markdown("**Nice-to-have**")
        nice_df = st.data_editor(
            st.session_state.nice_df, num_rows="dynamic", key=f"nice_editor_{v}",
            width="stretch", hide_index=True,
            column_config={"weight": st.column_config.NumberColumn(
                min_value=1, max_value=10, step=1)},
        )
        st.session_state.must_df = must_df
        st.session_state.nice_df = nice_df

    with st.expander("⚖️ Hard Requirements / Penalties (Optional)"):
        st.caption(
            "Define rules to penalize candidates who lack specific qualifications. "
            "**Category**: what to check (education level, experience years, "
            "skill, certification). **Value**: what to look for (e.g. 'bachelor', "
            "'3', 'Python', 'AWS'). **Points**: how much to deduct.")
        v = st.session_state.editor_version
        penalty_df = st.data_editor(
            st.session_state.penalty_df, num_rows="dynamic",
            key=f"penalty_editor_{v}",
            width="stretch", hide_index=True,
            column_config={
                "category": st.column_config.SelectboxColumn(
                    "Category",
                    options=["education", "experience", "skills", "certifications"],
                    required=True),
                "condition": st.column_config.TextColumn(
                    "Condition", help="Human-readable description, e.g. 'No Bachelor's degree'"),
                "value": st.column_config.TextColumn(
                    "Value", help="What to check: 'bachelor', '3', 'Python', 'AWS'"),
                "points": st.column_config.NumberColumn(
                    "Points", min_value=0.0, max_value=50.0, step=1.0,
                    help="Points to deduct if condition is triggered"),
                "enabled": st.column_config.CheckboxColumn(
                    "Enabled", default=True),
            },
        )
        st.session_state.penalty_df = penalty_df
        st.number_input(
            "Max total penalty (cap)", 5.0, 100.0, key="penalty_max", step=5.0,
            help="Total penalty from all rules is capped at this value.")

    st.subheader("2. Upload Resumes")
    resume_files = st.file_uploader(
        "Upload one or more resume PDFs", type=["pdf"], accept_multiple_files=True)

    _portal_resumes = st.session_state.get("portal_resumes", [])
    if _portal_resumes:
        with st.container(border=True):
            st.markdown(f"**📥 {len(_portal_resumes)} résumé(s) from job-portal "
                        "applications** are loaded and will be screened together "
                        "with anything you upload above.")
            st.caption("These are the actual PDFs candidates submitted online. "
                       "Applicants already sent a test are not included.")
            st.dataframe(
                pd.DataFrame([{"Candidate": f["name"] or "(from résumé)",
                               "Email": f["email"], "File": f["filename"]}
                             for f in _portal_resumes]),
                hide_index=True, width="stretch")
            if st.button("Clear loaded portal résumés"):
                st.session_state.portal_resumes = []
                st.rerun()

    with st.expander("➕ Add cover letters (optional)"):
        st.caption("Matched to resumes by filename (e.g. `jane_resume.pdf` ↔ "
                   "`jane_cover.pdf`).")
        cover_files = st.file_uploader(
            "Upload cover letter PDFs", type=["pdf"],
            accept_multiple_files=True, key="covers")

    _has_inputs = bool(resume_files) or bool(_portal_resumes)
    if st.button("🚀 3. Evaluate Candidates", type="primary",
                 disabled=not _has_inputs, width="stretch"):
        jd = st.session_state.get("jd", "").strip()
        if not jd:
            st.error("Add a job description before scoring — it's the main input.")
            st.stop()

        criteria = build_criteria(df_to_records(st.session_state.must_df),
                                  df_to_records(st.session_state.nice_df))
        st.session_state.criteria_weights = {c.id: c.weight for c in criteria.all()}
        penalty_rules = build_penalty_rules(st.session_state.penalty_df)

        # One combined worklist: (filename, pdf_bytes, portal_candidate_uuid,
        # portal_account_email). Uploaded files have no uuid/email.
        sources = [(f.name, f.getvalue(), None, None) for f in (resume_files or [])]
        sources += [(pf["filename"], pf["bytes"], pf["candidate_uuid"],
                     pf["email"]) for pf in _portal_resumes]

        covers = match_cover_letters([s[0] for s in sources], cover_files)
        if covers:
            st.info(f"Matched {len(covers)} cover letter(s): "
                    + ", ".join(covers.keys()))

        results = []
        portal_map = {}   # filename -> candidate_uuid, for direct dispatch
        progress = st.progress(0.0, text="Starting…")
        for i, (fname, fbytes, cand_uuid, acct_email) in enumerate(sources):
            progress.progress(
                i / len(sources),
                text=f"Processing {fname} ({i + 1}/{len(sources)})…")
            try:
                text = pdf.extract_text(fbytes)
            except Exception as e:  # noqa: BLE001
                r = process_resume(fname, "", jd, criteria)
                r.error = f"PDF read failed: {e}"
                results.append(r)
                continue
            if not text.strip():
                r = process_resume(fname, "", jd, criteria)
                r.error = "No extractable text (scanned PDF without OCR?)."
                results.append(r)
                continue
            r = process_resume(
                fname, text, jd, criteria, covers.get(fname, ""),
                penalize_gaps=st.session_state.penalize_gaps,
                gap_min_months=st.session_state.gap_min_months,
                gap_points=st.session_state.gap_points,
                gap_max_penalty=st.session_state.gap_max,
                penalty_rules=penalty_rules,
                penalty_max=st.session_state.penalty_max,
            )
            # For portal résumés, pin the delivery email to the registered
            # account (so a Save matches the existing row instead of creating a
            # duplicate) and remember the candidate for direct dispatch.
            if cand_uuid:
                if r.structured and acct_email:
                    r.structured.email = acct_email
                portal_map[fname] = cand_uuid
            results.append(r)
        progress.progress(1.0, text="Done.")
        st.session_state.results = results
        st.session_state.drafts = {}
        # Portal candidates already exist in the DB, so their links can be
        # dispatched without re-saving — seed the dispatch map now.
        if portal_map:
            st.session_state.db_candidate_map = {
                **st.session_state.get("db_candidate_map", {}), **portal_map}
        st.success(f"Scored {len([r for r in results if not r.error])} candidate(s). "
                   "See the **Review Results** tab, then **Send Tests**.")

    if _DB_ON and st.session_state.get("results"):
        st.divider()
        st.subheader("🗄️ Save screening to database")
        st.caption("Required before dispatching portal test links. Re-saving "
                   "updates existing candidates (matched by email).")
        try:
            departments = db_bridge.list_departments()
        except Exception as e:  # noqa: BLE001
            departments = []
            st.error(f"Could not load departments from MySQL: {e}")
        if departments:
            s1, s2 = st.columns([2, 1])
            _loaded_dept = st.session_state.get("loaded_department_id")
            _dept_idx = next(
                (i for i, d in enumerate(departments) if d[0] == _loaded_dept),
                0)
            dept = s1.selectbox(
                "Department this job belongs to", departments,
                index=_dept_idx,
                format_func=lambda d: d[1], key="db_dept")
            s2.write("")
            if s2.button("💾 Save to MySQL", type="secondary", width="stretch"):
                title = (st.session_state.get("job_name") or "").strip()
                if not title:
                    st.error("Set a job title in “Save this job as” above first.")
                else:
                    try:
                        full_config = {
                            "must": df_to_records(st.session_state.must_df),
                            "nice": df_to_records(st.session_state.nice_df),
                            "penalties": st.session_state.penalty_df.to_dict(
                                orient="records"),
                            "settings": {
                                "pass_threshold": st.session_state.pass_threshold,
                                "penalize_gaps": st.session_state.penalize_gaps,
                                "gap_min_months": st.session_state.gap_min_months,
                                "gap_points": st.session_state.gap_points,
                                "gap_max": st.session_state.gap_max,
                                "penalty_max": st.session_state.penalty_max,
                            },
                        }
                        job_uuid, cand_map = db_bridge.save_screening(
                            title, dept[0],
                            st.session_state.get("jd", ""),
                            full_config,
                            int(st.session_state.pass_threshold),
                            st.session_state.results,
                        )
                        st.session_state.db_job_uuid = job_uuid
                        st.session_state.db_candidate_map = cand_map
                        st.success(f"Saved job “{title}” with "
                                   f"{len(cand_map)} candidate(s) to MySQL.")
                    except Exception as e:  # noqa: BLE001
                        st.error(f"Database save failed: {e}")


# ================================================================================
# TAB 2 — DASHBOARD
# ================================================================================

with tab_dash:
    ok = ok_results()
    failed = [r for r in st.session_state.get("results", []) if r.error]
    threshold = st.session_state.pass_threshold

    if not ok and not failed:
        st.info("No results yet — run a screening first.")
    if ok:
        n_pass = sum(1 for r in ok if r.score >= threshold)
        m1, m2, m3, m4 = st.columns(4)
        m1.metric("Candidates", len(ok))
        m2.metric(f"Passing (≥{threshold})", n_pass)
        m3.metric("Top score", f"{ok[0].score:g}")
        m4.metric("Average", f"{sum(r.score for r in ok) / len(ok):.1f}")

        table = pd.DataFrame([{
            "Rank": i + 1,
            "Candidate": r.candidate_name,
            "Score": r.score,
            "Pass": "✅" if r.score >= threshold else "❌",
            "Email": r.email or "—",
            "Gap penalty": f"-{r.gap_penalty:g}" if r.gap_penalty else "",
            "Rule penalty": f"-{r.criteria_penalty:g}" if r.criteria_penalty else "",
            "Must-have gaps": len(r.must_have_gaps),
            "File": r.filename,
        } for i, r in enumerate(ok)])
        st.dataframe(
            table, hide_index=True, width="stretch",
            column_config={"Score": st.column_config.ProgressColumn(
                "Score", min_value=0, max_value=100, format="%d")},
        )

        st.divider()
        st.subheader("Deep Dive: Candidate Analysis")
        pick = st.selectbox(
            "Candidate", [r.filename for r in ok],
            format_func=lambda fn: next(
                (f"{r.candidate_name} — {r.score:g}/100" for r in ok
                 if r.filename == fn), fn),
        )
        r = next((x for x in ok if x.filename == pick), None)
        if r:
            s = r.structured
            c1, c2, c3, c4 = st.columns(4)
            total_pen = r.gap_penalty + r.criteria_penalty
            delta_parts = []
            if r.gap_penalty:
                delta_parts.append(f"-{r.gap_penalty:g} gap")
            if r.criteria_penalty:
                delta_parts.append(f"-{r.criteria_penalty:g} rules")
            delta_str = ", ".join(delta_parts) if delta_parts else None
            c1.metric("Score", f"{r.score:g}/100",
                      delta=delta_str,
                      delta_color="inverse" if total_pen else "off")
            c2.metric("Verdict", "PASS" if r.score >= threshold else "FAIL")
            if s:
                c3.metric("Experience",
                          fmt_experience(s.years_experience, len(s.experience)))
                c4.metric("Email", s.email or "—")

            if r.must_have_gaps:
                st.error("Missing must-haves: " + "; ".join(r.must_have_gaps))
            if r.employment_gaps:
                st.warning("Employment gaps: " + "; ".join(r.employment_gaps))

            ch1, ch2 = st.columns(2)
            radar = charts.radar_chart(r)
            with ch1:
                st.markdown("**Skill match radar**")
                if radar:
                    st.plotly_chart(radar, width="stretch")
                else:
                    st.caption("Needs 3+ scored criteria for a radar.")
            with ch2:
                st.markdown("**Score breakdown (earned vs available)**")
                st.plotly_chart(
                    charts.breakdown_chart(
                        r, st.session_state.get("criteria_weights", {})),
                    width="stretch")

            tl = charts.timeline_chart(r)
            if tl:
                st.markdown("**Experience timeline**")
                st.plotly_chart(tl, width="stretch")

            if s:
                st.write(f"**Skills:** {', '.join(s.skills[:20]) or '—'}")

            st.markdown("**Why this score**")
            _KIND_LABEL = {"overall": "overall", "must_have": "must",
                           "nice_to_have": "nice"}
            expl = pd.DataFrame([{
                "Criterion": cs.criterion_text,
                "Type": _KIND_LABEL.get(cs.kind, cs.kind),
                "Met": round(cs.met, 2),
                "Evidence": cs.evidence,
                "Reasoning": cs.reasoning,
            } for cs in sorted(
                r.criterion_scores,
                key=lambda x: ({"overall": 0, "must_have": 1}.get(x.kind, 2),
                               -x.met))])
            st.dataframe(
                expl, hide_index=True, width="stretch",
                column_config={"Met": st.column_config.ProgressColumn(
                    "Met", min_value=0.0, max_value=1.0, format="%.2f")},
            )

            if r.penalty_results:
                st.markdown("**Penalty breakdown**")
                pen_df = pd.DataFrame([{
                    "Category": p.category.title(),
                    "Condition": p.condition,
                    "Applied": "✅ Yes" if p.applied else "❌ No",
                    "Deduction": f"-{p.points_deducted:g}" if p.applied else "—",
                    "Reason": p.reason,
                } for p in r.penalty_results])
                st.dataframe(pen_df, hide_index=True, width="stretch")

    if failed:
        st.markdown("**Could not process**")
        for r in failed:
            st.warning(f"{r.filename}: {r.error}")


# ================================================================================
# TAB 3 — COMMUNICATION
# ================================================================================

with tab_comm:
    ok = ok_results()
    if not ok:
        st.info("No results yet — run a screening first.")
    else:
        st.subheader("1. Choose who to email")
        mode = st.radio(
            "Selection mode", ["Top N", "Pick manually"], horizontal=True,
            key="comm_mode",
            help="Top N for batch emails (e.g. invite the top 5); pick "
                 "manually to email specific people (e.g. one rejection).")
        c1, c2 = st.columns([1, 2])
        if mode == "Top N":
            top_n = c1.number_input("Top N candidates", 1, len(ok),
                                    min(3, len(ok)), key="top_n")
            selected = top_n_results(int(top_n))
        else:
            threshold = st.session_state.pass_threshold
            label_by_file = {
                r.filename: (f"{r.candidate_name} — {r.score:g} "
                             f"({'PASS' if r.score >= threshold else 'FAIL'})")
                for r in ok}
            picked_files = c1.multiselect(
                "Candidates", list(label_by_file),
                format_func=lambda fn: label_by_file[fn],
                key="comm_manual_pick",
                placeholder="Choose one or more…")
            selected = [r for r in ok if r.filename in picked_files]
        missing_email = [r.candidate_name for r in selected if not r.email]

        if selected:
            c2.dataframe(pd.DataFrame([{
                "Candidate": r.candidate_name, "Score": r.score,
                "Email": r.email or "⚠️ none found",
            } for r in selected]), hide_index=True, width="stretch")
        else:
            c2.info("No candidates selected.")
        if missing_email:
            st.warning("No email parsed for: " + ", ".join(missing_email)
                       + ". Sending will be skipped for them.")

        st.subheader("2. Draft and Send")
        d1, d2, d3, d4 = st.columns(4)
        kind = d1.selectbox("Email type", list(emailer.EMAIL_KINDS),
                            format_func=lambda k: k.replace("_", " ").title())
        job_title = d2.text_input("Job title",
                                  st.session_state.get("job_name", "") or "the role")
        company = d3.text_input("Company", "our company")
        sign_off = d4.text_input("Sign off as", "The Recruiting Team")

        if st.button("🤖 Auto-draft for selected", type="secondary",
                     disabled=not selected):
            with st.spinner("Drafting…"):
                for r in selected:
                    strengths = ""
                    if r.criterion_scores:
                        best = max(r.criterion_scores, key=lambda s: s.met)
                        strengths = best.evidence[:300]
                    draft = emailer.draft_email(
                        kind, r.candidate_name, job_title, company,
                        strengths=strengths, sign_off=sign_off)
                    # Write straight into the widget keys (widgets render
                    # below this handler): a widget's default value is
                    # ignored once the key exists, so setting the key is the
                    # only way an auto-draft shows up in the fields.
                    st.session_state[f"to_{r.filename}"] = r.email
                    st.session_state[f"subj_{r.filename}"] = draft["subject"]
                    st.session_state[f"body_{r.filename}"] = draft["body"]
            st.success(f"Drafted {len(selected)} email(s). Review below.")

        for r in selected:
            st.session_state.setdefault(f"to_{r.filename}", r.email)
            st.session_state.setdefault(f"subj_{r.filename}", "")
            st.session_state.setdefault(f"body_{r.filename}", "")
            drafted = bool(st.session_state[f"body_{r.filename}"])
            with st.expander(f"✉️ {r.candidate_name} "
                             f"({r.email or 'no email'})", expanded=drafted):
                to = st.text_input("To", key=f"to_{r.filename}")
                subject = st.text_input("Subject", key=f"subj_{r.filename}")
                body = st.text_area(
                    "Body", height=220, key=f"body_{r.filename}",
                    placeholder="Auto-draft above, or write from scratch…")
                st.session_state.drafts[r.filename] = {
                    "to": to, "subject": subject, "body": body}
                if st.button("Send", key=f"send_{r.filename}",
                             disabled=not (to and subject and body)):
                    ok_send, msg = emailer.send_email(to, subject, body)
                    (st.success if ok_send else st.error)(msg)

        ready = [r for r in selected
                 if (dr := st.session_state.drafts.get(r.filename))
                 and dr["to"] and dr["subject"] and dr["body"]]
        if ready and st.button(f"📤 Send all ({len(ready)})", type="primary"):
            for r in ready:
                dr = st.session_state.drafts[r.filename]
                ok_send, msg = emailer.send_email(
                    dr["to"], dr["subject"], dr["body"])
                (st.success if ok_send else st.error)(msg)

        if not emailer.smtp_configured():
            st.info("SMTP not configured — you can draft and edit, but not "
                    "send. Add SMTP_HOST/SMTP_USER/SMTP_PASSWORD to `.env`.")


# ================================================================================
# TAB 4 — MCQ ASSESSMENT
# ================================================================================

with tab_mcq:
    jd = st.session_state.get("jd", "").strip()
    if not jd:
        st.info("Add a job description in the Screening tab first — the test "
                "is generated from it.")
    else:
        # ---- Step 1: Define the pool size -------------------------------------
        st.subheader("📊 Step 1 — Define the question pool")
        st.caption(
            "This is the **pool**, not what one candidate sits. Build it "
            "bigger than the paper (e.g. a pool of 30 for a 10-question test) "
            "and every candidate's link draws its own random selection.")
        ts1, ts2 = st.columns(2)
        total_questions = ts1.number_input(
            "Questions in the pool",
            1, 200, 10, key="total_test_questions",
            help="Pick from the bank below and/or let the LLM generate the "
                 "rest. You choose how many of these each candidate sits at "
                 "dispatch time.")
        difficulty = ts2.selectbox("Difficulty", ["easy", "medium", "hard"],
                                    index=1, key="mcq_difficulty")

        # Initialize custom questions list in session state
        if "custom_qs" not in st.session_state:
            st.session_state.custom_qs = []
        st.session_state.setdefault("bank_picks", [])

        _bank_job = (st.session_state.get("db_job_uuid")
                     if _DB_ON else None)

        st.divider()

        # ---- Step 1b: The job's question bank ---------------------------------
        st.subheader("🏦 Question bank for this job")
        if not _bank_job:
            st.info(
                "Save the screening to the database (Screening tab) to unlock "
                "the question bank — it is stored per job, so questions you "
                "write once are reusable for every future test on this role.")
        else:
            show_retired = st.checkbox(
                "Show retired questions", key="bank_show_retired",
                help="Retired questions stay in the bank and in every paper "
                     "already sat, but are no longer offered for new tests.")
            ov = db_bridge.bank_overview(_bank_job,
                                         include_retired=show_retired)
            cats = ov["categories"]
            items = ov["items"]

            if not cats:
                st.caption("No categories yet. Let the LLM read the job "
                           "description and propose some, then edit freely.")
            cc1, cc2 = st.columns([2, 1])
            new_cat = cc1.text_input(
                "Add a category", key="bank_new_cat",
                placeholder="Data, AI, General…", label_visibility="collapsed")
            if cc1.button("➕ Add category", width="stretch"):
                if new_cat.strip():
                    db_bridge.bank_add_category(_bank_job, new_cat.strip())
                    st.rerun()
                else:
                    st.warning("Type a category name first.")
            if cc2.button("🤖 Suggest from JD", width="stretch"):
                with st.spinner("Reading the job description…"):
                    for name in mcq_mod.suggest_categories(jd):
                        db_bridge.bank_add_category(_bank_job, name)
                st.rerun()

            if cats:
                by_cat = {c["id"]: [] for c in cats}
                by_cat[None] = []
                for it in items:
                    by_cat.setdefault(it["category_id"], []).append(it)

                st.caption(f"**{len(items)}** question(s) in the bank. "
                           "Tick the ones to put in this test's pool.")
                letters = "ABCD"
                groups = [(c["id"], c["name"]) for c in cats]
                if by_cat.get(None):
                    groups.append((None, "Uncategorised"))
                for cid, cname in groups:
                    rows = by_cat.get(cid, [])
                    picked_here = sum(
                        1 for r in rows
                        if r["id"] in st.session_state.bank_picks)
                    with st.expander(
                            f"{cname} — {len(rows)} question(s)"
                            + (f", {picked_here} picked" if picked_here else "")):
                        if not rows:
                            st.caption("Empty — add or generate questions "
                                       "below.")
                        for r in rows:
                            k = f"bankpick_{r['id']}"
                            col_a, col_e, col_r, col_b = st.columns([7, 1, 1, 1])
                            retired = not r.get("active", True)
                            checked = col_a.checkbox(
                                f"{'🚫 ' if retired else ''}"
                                f"{r['question'][:100]}"
                                f"{'…' if len(r['question']) > 100 else ''}  "
                                f"·  correct **{letters[r['correct_index']]}**"
                                f"  ·  {r['source']}"
                                + (f"  ·  used {r['times_used']}×"
                                   if r["times_used"] else ""),
                                value=r["id"] in st.session_state.bank_picks,
                                key=k, disabled=retired)
                            if checked and r["id"] not in st.session_state.bank_picks:
                                st.session_state.bank_picks.append(r["id"])
                            elif not checked and r["id"] in st.session_state.bank_picks:
                                st.session_state.bank_picks.remove(r["id"])
                            if col_e.button("✏️", key=f"bankedit_{r['id']}",
                                            help="Edit this question"):
                                st.session_state["bank_editing"] = r["id"]
                                st.rerun()
                            if col_r.button("♻️" if retired else "🚫",
                                            key=f"bankret_{r['id']}",
                                            help="Offer again" if retired else
                                            "Retire — stop offering it in new "
                                            "tests, keep past papers intact"):
                                db_bridge.bank_update_item(r["id"],
                                                           active=retired)
                                st.rerun()
                            if col_b.button("🗑️", key=f"bankdel_{r['id']}",
                                            help="Delete permanently"):
                                db_bridge.bank_delete_item(r["id"])
                                st.rerun()

                            if st.session_state.get("bank_editing") == r["id"]:
                                with st.form(f"bankeditform_{r['id']}"):
                                    st.caption(
                                        "Editing changes FUTURE tests only — "
                                        "papers already sat keep the original "
                                        "wording."
                                        if r["times_used"] else
                                        "Fix the wording or the answer key.")
                                    e_q = st.text_area(
                                        "Question", r["question"], height=80)
                                    e_cols = st.columns(2)
                                    e_opts = [
                                        e_cols[i % 2].text_input(
                                            f"Option {letters[i]}",
                                            r["options"][i],
                                            key=f"be_o_{r['id']}_{i}")
                                        for i in range(4)]
                                    e_correct = st.radio(
                                        "Correct answer", list(letters),
                                        index=r["correct_index"],
                                        horizontal=True,
                                        key=f"be_c_{r['id']}")
                                    e_cat = st.selectbox(
                                        "Category",
                                        ["(uncategorised)"]
                                        + [c["name"] for c in cats],
                                        index=([c["name"] for c in cats]
                                               .index(r["category"]) + 1)
                                        if r["category"] else 0,
                                        key=f"be_cat_{r['id']}")
                                    s1, s2 = st.columns(2)
                                    if s1.form_submit_button("💾 Save",
                                                             type="primary"):
                                        if not all(o.strip() for o in e_opts):
                                            st.error("All four options are "
                                                     "required.")
                                        else:
                                            cid = next(
                                                (c["id"] for c in cats
                                                 if c["name"] == e_cat), None)
                                            db_bridge.bank_update_item(
                                                r["id"], question=e_q.strip(),
                                                options=e_opts,
                                                correct_index=letters.index(
                                                    e_correct),
                                                category_id=cid)
                                            st.session_state.pop("bank_editing")
                                            st.success("Question updated.")
                                            st.rerun()
                                    if s2.form_submit_button("Cancel"):
                                        st.session_state.pop("bank_editing")
                                        st.rerun()
                        if cid is not None and st.button(
                                f"🗑️ Delete '{cname}' category",
                                key=f"bankdelcat_{cid}"):
                            n = db_bridge.bank_delete_category(cid)
                            st.info(f"Category deleted; {n} question(s) kept "
                                    "as uncategorised.")
                            st.rerun()

                cat_choices = {c["name"]: c["id"] for c in cats}

                with st.expander("⚡ Generate questions into the bank"):
                    g1, g2, g3 = st.columns(3)
                    g_cat = g1.selectbox("Category",
                                         list(cat_choices.keys()),
                                         key="bank_gen_cat")
                    g_n = g2.number_input("How many", 1, 50, 5,
                                          key="bank_gen_n")
                    g_diff = g3.selectbox("Difficulty",
                                          ["easy", "medium", "hard"],
                                          index=1, key="bank_gen_diff")
                    st.caption(
                        "Generate a deep bank once (say 20 per category); "
                        "every test afterwards draws from it, so candidates "
                        "cannot predict the paper.")
                    if st.button(f"⚡ Generate {int(g_n)} into '{g_cat}'",
                                 type="secondary"):
                        existing = [i["question"] for i in items]
                        with st.spinner(f"Generating {int(g_n)} {g_diff} "
                                        f"{g_cat} question(s)…"):
                            gen = mcq_mod.generate_mcqs(
                                jd, g_diff, int(g_n),
                                avoid=existing, category=g_cat)
                        if not gen.questions:
                            st.error("Generation failed — try again.")
                        else:
                            n_added = db_bridge.bank_add_questions(
                                _bank_job, gen.questions,
                                cat_choices[g_cat], g_diff, source="llm")
                            st.success(f"Added {n_added} question(s) to "
                                       f"'{g_cat}'.")
                            st.rerun()

                with st.expander("➕ Write a question into the bank"):
                    bq_text = st.text_area("Question text", key="bq_text",
                                           height=80)
                    bq_cols = st.columns(2)
                    bq_opts = [
                        bq_cols[0].text_input("Option A", key="bq_a"),
                        bq_cols[1].text_input("Option B", key="bq_b"),
                        bq_cols[0].text_input("Option C", key="bq_c"),
                        bq_cols[1].text_input("Option D", key="bq_d"),
                    ]
                    bq_correct = st.radio("Correct answer", list("ABCD"),
                                          horizontal=True, key="bq_correct")
                    bq_cat = st.selectbox("Category",
                                          list(cat_choices.keys()),
                                          key="bq_cat")
                    if st.button("✅ Save to bank"):
                        if not bq_text.strip():
                            st.error("Question text is required.")
                        elif not all(o.strip() for o in bq_opts):
                            st.error("All four options are required.")
                        else:
                            db_bridge.bank_add_questions(
                                _bank_job,
                                [MCQQuestion(
                                    question=bq_text.strip(),
                                    options=bq_opts,
                                    correct_index=list("ABCD").index(bq_correct))],
                                cat_choices[bq_cat], difficulty,
                                source="custom")
                            st.success("Saved to the bank.")
                            st.rerun()

            n_picked = len(st.session_state.bank_picks)
            if n_picked:
                st.success(f"✅ **{n_picked}** question(s) picked from the "
                           f"bank for this pool.")

        st.divider()

        # ---- Step 2: One-off custom questions ---------------------------------
        n_picked = len(st.session_state.bank_picks) if _bank_job else 0
        n_custom = len(st.session_state.custom_qs)
        n_llm_needed = max(0, int(total_questions) - n_custom - n_picked)

        st.subheader(f"📝 Step 2 — One-off questions ({n_custom})")
        st.caption(
            "For questions you want in *this* pool only. Anything reusable "
            "belongs in the bank above."
            if _bank_job else
            "Add your own questions here; the LLM generates the rest.")
        if n_llm_needed == 0:
            st.success(
                f"✅ {n_picked} from the bank + {n_custom} one-off = "
                f"{n_picked + n_custom} — the pool of "
                f"{int(total_questions)} is covered. No LLM needed.")
        else:
            st.caption(
                f"Pool so far: **{n_picked}** from the bank + **{n_custom}** "
                f"one-off. The LLM will generate the remaining "
                f"**{n_llm_needed}** to reach **{int(total_questions)}**.")

        with st.expander("➕ Add a custom question", expanded=False):
            cq_text = st.text_area("Question text", key="cq_text", height=80,
                                   placeholder="What is the output of...")
            cq_cols = st.columns(2)
            cq_a = cq_cols[0].text_input("Option A", key="cq_opt_a")
            cq_b = cq_cols[1].text_input("Option B", key="cq_opt_b")
            cq_c = cq_cols[0].text_input("Option C", key="cq_opt_c")
            cq_d = cq_cols[1].text_input("Option D", key="cq_opt_d")
            cq_correct = st.radio(
                "Correct answer", ["A", "B", "C", "D"], horizontal=True,
                key="cq_correct")
            cq_explanation = st.text_input(
                "Explanation (optional)", key="cq_explanation",
                placeholder="Why this is the correct answer...")

            if st.button("✅ Add question"):
                opts = [cq_a, cq_b, cq_c, cq_d]
                if not cq_text.strip():
                    st.error("Question text is required.")
                elif not all(o.strip() for o in opts):
                    st.error("All four options are required.")
                else:
                    st.session_state.custom_qs.append(MCQQuestion(
                        question=cq_text.strip(),
                        options=opts,
                        correct_index=["A", "B", "C", "D"].index(cq_correct),
                        explanation=cq_explanation.strip(),
                    ))
                    st.success(f"Added! You now have "
                               f"{len(st.session_state.custom_qs)} custom "
                               f"question(s).")
                    st.rerun()

        # Show existing custom questions
        if st.session_state.custom_qs:
            letters = "ABCD"
            st.markdown(f"**Your custom questions "
                        f"({len(st.session_state.custom_qs)})**")
            for idx, cq in enumerate(st.session_state.custom_qs):
                col_q, col_del = st.columns([6, 1])
                col_q.markdown(
                    f"**{idx + 1}.** {cq.question[:100]}"
                    f"{'…' if len(cq.question) > 100 else ''} "
                    f"— correct: **{letters[cq.correct_index]}**")
                if col_del.button("🗑️", key=f"del_cq_{idx}"):
                    st.session_state.custom_qs.pop(idx)
                    st.rerun()
        else:
            st.caption("No custom questions yet.")

        st.divider()

        # ---- Step 3: Build the pool ------------------------------------------
        n_custom = len(st.session_state.custom_qs)
        n_llm_needed = max(0, int(total_questions) - n_custom - n_picked)

        st.subheader("🧱 Step 3 — Build the pool")
        sources = []
        if n_picked:
            sources.append(f"**{n_picked}** from the bank")
        if n_custom:
            sources.append(f"**{n_custom}** one-off")
        if n_llm_needed:
            sources.append(f"**{n_llm_needed}** generated at {difficulty}")
        st.caption("Pool = " + (" + ".join(sources) if sources
                                else "nothing yet — pick or add questions above")
                   + f" → **{int(total_questions)}** total.")

        build_label = (f"⚡ Generate {n_llm_needed} & build pool"
                       if n_llm_needed else "🔀 Build pool")
        if st.button(build_label, type="secondary", width="stretch",
                     disabled=not (n_picked or n_custom or n_llm_needed)):
            from_bank = (db_bridge.bank_pick(_bank_job,
                                             st.session_state.bank_picks)
                         if (_bank_job and n_picked) else [])
            pool = from_bank + list(st.session_state.custom_qs)
            if n_llm_needed:
                with st.spinner(f"Generating {n_llm_needed} {difficulty} "
                                "question(s)…"):
                    gen_test = mcq_mod.generate_mcqs(
                        jd, difficulty, int(n_llm_needed),
                        avoid=[q.question for q in pool])
                if not gen_test.questions and not pool:
                    st.error("Generation failed — try again.")
                    gen_test = None
                if gen_test:
                    if len(gen_test.questions) < n_llm_needed:
                        st.warning(
                            f"Only {len(gen_test.questions)} unique questions "
                            "generated. Building with what we have.")
                    pool += gen_test.questions
            if pool:
                import random as _rand
                _rand.shuffle(pool)
                st.session_state.mcq_test = MCQTest(
                    difficulty=difficulty,
                    questions=pool[:int(total_questions)],
                    custom_questions=list(st.session_state.custom_qs),
                    custom_ratio=(n_custom + n_picked)
                    / max(1, int(total_questions)),
                    approved=False,
                )
                # Fresh pool, fresh keep flags.
                st.session_state.pop("keep_flags", None)
                st.success(f"✅ Pool built: {len(pool[:int(total_questions)])} "
                           "question(s). Review below.")
                st.rerun()

        # ---- Review, keep/regenerate & approve --------------------------------
        test = st.session_state.mcq_test
        if test and test.questions:
            st.divider()
            st.subheader(f"Review & edit ({len(test.questions)} questions, "
                         f"{test.difficulty})")
            st.caption(
                "Edit anything below, then approve. **Keep** marks a question "
                "as settled — *Regenerate the rest* rewrites only the unkept "
                "ones, and the kept ones are sent to the LLM as "
                "don't-repeat context so the replacements come back genuinely "
                "different. The correct answer radio is for your reference — "
                "it is never emailed.")
            letters = "ABCD"

            # Keep flags live alongside the pool, indexed by position.
            keeps = st.session_state.setdefault(
                "keep_flags", [False] * len(test.questions))
            if len(keeps) != len(test.questions):
                keeps = [False] * len(test.questions)
                st.session_state.keep_flags = keeps

            custom_texts = {cq.question for cq in st.session_state.custom_qs}

            k1, k2, k3 = st.columns([1, 1, 2])
            if k1.button("✅ Keep all", width="stretch"):
                st.session_state.keep_flags = [True] * len(test.questions)
                st.rerun()
            if k2.button("✖️ Keep none", width="stretch"):
                st.session_state.keep_flags = [False] * len(test.questions)
                st.rerun()
            n_keep = sum(keeps)
            n_regen = len(test.questions) - n_keep
            k3.caption(f"**{n_keep}** kept · **{n_regen}** would be "
                       "regenerated")

            for i, q in enumerate(test.questions):
                if q.category:
                    source = f"🏦 {q.category}"
                elif q.question in custom_texts:
                    source = "📝 Custom"
                else:
                    source = "🤖 LLM"
                flag = "🔒" if keeps[i] else "🔄"
                with st.expander(f"{flag} Q{i + 1}. [{source}] "
                                 f"{q.question[:70]}"):
                    keeps[i] = st.checkbox(
                        "Keep this question (exclude from regeneration)",
                        value=keeps[i], key=f"mcq_keep_{i}")
                    q.question = st.text_area(
                        "Question", q.question, key=f"mcq_q_{i}", height=80)
                    for j in range(4):
                        q.options[j] = st.text_input(
                            f"Option {letters[j]}", q.options[j],
                            key=f"mcq_o_{i}_{j}")
                    q.correct_index = letters.index(st.radio(
                        "Correct answer", list(letters),
                        index=q.correct_index, horizontal=True,
                        key=f"mcq_a_{i}"))
                    if q.explanation:
                        st.caption(f"Why: {q.explanation}")
            st.session_state.keep_flags = keeps

            r1, r2 = st.columns([1, 1])
            if r1.button(f"🔄 Regenerate the {n_regen} unkept",
                         width="stretch", disabled=n_regen == 0,
                         help="Kept questions stay exactly as they are."):
                kept = [q for q, k in zip(test.questions, keeps) if k]
                with st.spinner(f"Regenerating {n_regen} question(s)…"):
                    gen = mcq_mod.generate_mcqs(
                        jd, test.difficulty, n_regen,
                        avoid=[q.question for q in kept])
                if not gen.questions:
                    st.error("Regeneration failed — the kept questions are "
                             "untouched. Try again.")
                else:
                    if len(gen.questions) < n_regen:
                        st.warning(
                            f"Only {len(gen.questions)} of {n_regen} came "
                            "back unique — the pool is that much smaller.")
                    test.questions = kept + gen.questions
                    test.approved = False   # re-approve after a change
                    st.session_state.mcq_test = test
                    st.session_state.keep_flags = (
                        [True] * len(kept) + [False] * len(gen.questions))
                    st.success(f"Kept {len(kept)}, regenerated "
                               f"{len(gen.questions)}. Review again.")
                    st.rerun()
            if _bank_job and r2.button(
                    "🏦 Save this pool to the bank", width="stretch",
                    help="Keeps these questions for future tests on this job."):
                n_saved = db_bridge.bank_add_questions(
                    _bank_job, test.questions, None, test.difficulty,
                    source="custom")
                st.success(f"Saved {n_saved} new question(s) to the bank "
                           "(duplicates skipped). Assign them to categories "
                           "in the bank section above.")

            a1, a2 = st.columns([1, 3])
            if a1.button("✅ Approve test", type="primary", width="stretch"):
                test.approved = True
                st.session_state.mcq_test = test
            if test.approved:
                a2.success("Test approved — dispatch below.")

            if test.approved:
                st.divider()
                st.subheader("Dispatch to shortlisted candidates")
                ok = ok_results()
                if not ok:
                    st.info("Run a screening first to get candidates.")
                else:
                    n = st.number_input("Send to Top N", 1, len(ok),
                                        min(3, len(ok)), key="mcq_top_n")
                    recipients = [r for r in top_n_results(int(n)) if r.email]
                    skipped = [r.candidate_name for r in top_n_results(int(n))
                               if not r.email]
                    st.dataframe(pd.DataFrame([{
                        "Candidate": r.candidate_name, "Score": r.score,
                        "Email": r.email,
                    } for r in recipients]), hide_index=True, width="stretch")
                    if skipped:
                        st.warning("Skipped (no email): " + ", ".join(skipped))

                    dj1, dj2 = st.columns(2)
                    mcq_job = dj1.text_input(
                        "Job title for the email",
                        st.session_state.get("job_name", "") or "the role",
                        key="mcq_job_title")
                    mcq_company = dj2.text_input("Company", "our company",
                                                 key="mcq_company")
                    if st.button("🤖 Draft assessment email intro"):
                        draft = emailer.draft_email(
                            "assessment", "Candidate", mcq_job, mcq_company,
                            extra="Do NOT include any greeting/salutation "
                                  "(no 'Dear ...') or sign-off/signature (no "
                                  "'Best regards', no team name) — the email "
                                  "template adds those. Do NOT mention the "
                                  "number of questions, time limit, or deadline "
                                  "— those are listed separately. Write only 1-2 "
                                  "warm body paragraphs.")
                        st.session_state["mcq_intro_edit"] = draft["body"]
                    st.session_state.setdefault("mcq_intro_edit", "")
                    intro = st.text_area(
                        "Email intro (the link + exact time limit and expiry are "
                        "appended automatically — don't repeat them here)",
                        height=180, key="mcq_intro_edit")

                    if _DB_ON and st.session_state.get("db_job_uuid"):
                        st.markdown("**🔗 Portal dispatch — unique test link "
                                    "per candidate**")
                        p1, p2 = st.columns(2)
                        duration = p1.number_input(
                            "Test duration (minutes)", min_value=1,
                            max_value=None, value=20,
                            key="portal_duration",
                            help="Any length — no minimum or maximum. The "
                                 "timer starts when the candidate opens the "
                                 "link and is enforced server-side.")
                        test_pass = p2.number_input(
                            "Test pass score (%)", 0, 100, 60,
                            key="portal_pass_score",
                            help="Candidates scoring at least this on the test "
                                 "are marked PASS on the admin portal.")

                        pool_size = len(test.questions)
                        per_cand = st.number_input(
                            "Questions per candidate",
                            min_value=1, max_value=pool_size,
                            value=pool_size, key="portal_per_candidate",
                            help=f"Drawn from the {pool_size}-question pool "
                                 "above. Set this below the pool size and "
                                 "every candidate's link gets a different, "
                                 "randomly drawn paper — so a leaked test "
                                 "does not predict the next one.")
                        _pred = predictability.assess(pool_size, int(per_cand))
                        _msg = predictability.summary(pool_size, int(per_cand))
                        if _pred["level"] in ("none", "weak"):
                            st.warning(f"⚠️ {_msg}")
                        elif _pred["level"] == "ok":
                            st.info(f"🎲 {_msg}")
                        else:
                            st.success(f"🎲 {_msg}")

                        # Blueprint: guarantee the mix of each drawn paper,
                        # e.g. 1 Data + 1 AI + 1 General. Only categories
                        # actually present in the pool can be quota'd.
                        pool_cats: dict[str, int] = {}
                        for q in test.questions:
                            if q.category:
                                pool_cats[q.category] = pool_cats.get(
                                    q.category, 0) + 1
                        blueprint: dict = {}
                        if pool_cats:
                            with st.expander(
                                    "🎯 Category mix per candidate "
                                    "(optional blueprint)"):
                                st.caption(
                                    "Guarantee every candidate's paper "
                                    "contains a set number from each "
                                    "category. Leave all at 0 to draw purely "
                                    "at random. Any remainder is filled at "
                                    "random from the rest of the pool.")
                                bp_cols = st.columns(min(4, len(pool_cats)))
                                for bi, (cname, avail) in enumerate(
                                        sorted(pool_cats.items())):
                                    want = bp_cols[bi % len(bp_cols)].number_input(
                                        f"{cname} (of {avail})",
                                        min_value=0,
                                        max_value=min(avail, int(per_cand)),
                                        value=0, key=f"bp_{cname}")
                                    if int(want):
                                        blueprint[cname] = int(want)
                                bp_total = sum(blueprint.values())
                                if bp_total > int(per_cand):
                                    st.error(
                                        f"The blueprint asks for {bp_total} "
                                        f"questions but each candidate only "
                                        f"sits {int(per_cand)}. Lower a "
                                        "quota or raise questions per "
                                        "candidate.")
                                elif bp_total:
                                    rest = int(per_cand) - bp_total
                                    st.caption(
                                        f"Each paper: "
                                        + " + ".join(f"{v} {k}" for k, v
                                                     in blueprint.items())
                                        + (f" + {rest} random"
                                           if rest else "")
                                        + f" = {int(per_cand)}.")
                        pr1, pr2 = st.columns([3, 1])
                        proctored = pr1.toggle(
                            "🎥 Proctored test (camera, mic, screen share, "
                            "fullscreen lock)", value=True,
                            key="portal_proctored",
                            help="Candidates must grant camera + microphone + "
                                 "entire-screen share and stay in fullscreen. "
                                 "Face/voice/tab-switch violations give "
                                 "warnings; at the limit the test "
                                 "auto-terminates with evidence for admin "
                                 "review.")
                        max_warn = pr2.number_input(
                            "Warnings allowed", 1, 10, 3,
                            key="portal_max_warnings",
                            disabled=not st.session_state.get(
                                "portal_proctored", True),
                            help="Violations before the test is automatically "
                                 "terminated.")

                        # Calendar date-time range for test availability
                        st.markdown("**📅 Test link availability window**")
                        from datetime import datetime as _dt, timedelta as _td
                        dc1, dc2 = st.columns(2)
                        default_from = _dt.now()
                        default_to = _dt.now() + _td(days=7)
                        avail_from_date = dc1.date_input(
                            "Available from (date)",
                            value=default_from.date(),
                            key="test_avail_from_date")
                        avail_from_time = dc1.time_input(
                            "Available from (time)",
                            value=default_from.time(),
                            key="test_avail_from_time")
                        avail_to_date = dc2.date_input(
                            "Available until (date)",
                            value=default_to.date(),
                            key="test_avail_to_date")
                        avail_to_time = dc2.time_input(
                            "Available until (time)",
                            value=default_to.time(),
                            key="test_avail_to_time")

                        expires_at = _dt.combine(avail_to_date, avail_to_time)
                        st.caption(
                            f"Links will expire on "
                            f"**{expires_at.strftime('%b %d, %Y at %I:%M %p')}**")

                        cand_map = st.session_state.get("db_candidate_map", {})
                        dispatchable = [r for r in recipients
                                        if r.filename in cand_map]
                        not_in_db = [r.candidate_name for r in recipients
                                     if r.filename not in cand_map]
                        if not_in_db:
                            st.warning("Not in the database (re-save the "
                                       "screening first): "
                                       + ", ".join(not_in_db))

                        if st.button(
                            f"🚀 Create links & email {len(dispatchable)} "
                            "candidate(s)",
                            type="primary",
                            disabled=not (dispatchable and intro
                                          and emailer.smtp_configured())):
                            try:
                                entries, already_tested = \
                                    db_bridge.create_test_with_assignments(
                                        st.session_state.db_job_uuid, test,
                                        [cand_map[r.filename]
                                         for r in dispatchable],
                                        int(duration),
                                        expires_at=expires_at,
                                        pass_score=int(test_pass),
                                        proctored=bool(proctored),
                                        max_warnings=int(max_warn),
                                        questions_per_candidate=int(per_cand),
                                        blueprint=blueprint or None)
                            except Exception as e:  # noqa: BLE001
                                entries = []
                                already_tested = []
                                st.error(f"Could not create test links: {e}")
                            if already_tested:
                                st.warning(
                                    "⚠️ Skipped (already tested for this "
                                    "job): "
                                    + ", ".join(already_tested))
                            body_intro = clean_intro(intro)
                            for e in entries:
                                body = (
                                    f"Dear {e['name']},\n\n{body_intro}\n\n"
                                    f"Start your assessment here:\n{e['link']}\n\n"
                                    f"Details:\n"
                                    f"- {e['num_questions']} multiple-choice "
                                    f"questions\n"
                                    f"- Time limit: {int(duration)} minutes "
                                    f"(starts when you open the test)\n"
                                    f"- The link expires on "
                                    f"{expires_at.strftime('%b %d, %Y at %I:%M %p')}"
                                    f" and works only once\n\n"
                                    f"You will verify your identity with a "
                                    f"one-time code sent to this email "
                                    f"address.\n\n"
                                    f"Best regards,\n{mcq_company}")
                                ok_send, msg = emailer.send_email(
                                    e["email"],
                                    f"{mcq_job} — Online Assessment "
                                    "Invitation", body)
                                (st.success if ok_send else st.error)(msg)
                    elif _DB_ON:
                        st.info("To send portal test links, first save the "
                                "screening to MySQL in the Screening tab.")

                    with st.expander(
                            "✉️ Fallback: email questions inline (no portal)"):
                        st.text(
                            (intro or "(intro)")
                            + "\n\n--- ASSESSMENT ---\n\n"
                            + mcq_mod.test_to_email_block(test)
                            + "\nReply to this email with your answers "
                              "(e.g. 1-A, 2-C, ...)."
                        )
                        if st.button(
                            f"📤 Send inline to {len(recipients)} candidate(s)",
                            disabled=not (recipients and intro
                                          and emailer.smtp_configured())):
                            block = mcq_mod.test_to_email_block(test)
                            for r in recipients:
                                body = (
                                    f"Dear {r.candidate_name},\n\n{intro}\n\n"
                                    "--- ASSESSMENT ---\n\n" + block
                                    + "\nReply to this email with your answers "
                                      "(e.g. 1-A, 2-C, ...).\n"
                                )
                                ok_send, msg = emailer.send_email(
                                    r.email, f"{mcq_job} — Online Assessment",
                                    body)
                                (st.success if ok_send else st.error)(msg)
                    if not emailer.smtp_configured():
                        st.info("SMTP not configured — add SMTP_* to `.env` "
                                "to enable sending.")
