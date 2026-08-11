"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useRef, useState } from "react";
import { apiGet, apiSend, apiUpload, applicantSession } from "@/lib/api";

const STAGES = ["Applied", "Screening", "Test", "Interview", "Offer", "Hired"];

function stageIndex(stage) {
  const i = STAGES.indexOf(stage);
  return i < 0 ? 0 : i;
}
function fmt(iso) {
  return iso ? new Date(iso).toLocaleString() : null;
}

function StageTrack({ app }) {
  const rejected = app.stage === "Rejected"
    || ["terminated", "test_terminated", "interview_terminated"].includes(app.status);
  const active = rejected ? -1 : stageIndex(app.stage);
  return (
    <>
      <div className="stage-track">
        {STAGES.map((s, i) => {
          const cls = i < active ? "done" : i === active ? "current" : "";
          return (
            <div key={s} className={`stage-node ${cls}`}>
              <div className="dot">{i < active ? "✓" : i + 1}</div>
              <div className="name">{s}</div>
            </div>
          );
        })}
      </div>
      {rejected && (
        <span className="pill warn" style={{ marginTop: 8 }}>Not moving forward</span>
      )}
    </>
  );
}

export default function Dashboard() {
  const router = useRouter();
  const [apps, setApps] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");        // application_uuid being acted on
  const fileRef = useRef(null);
  const pendingRef = useRef(null);             // app awaiting a chosen file

  const token = () => applicantSession()?.token;

  const reload = () =>
    apiGet("/api/portal/me/applications", token())
      .then((d) => setApps(d.applications))
      .catch((e) => setError(e.message));

  useEffect(() => {
    const s = applicantSession();
    if (!s) { router.replace("/portal/login?next=/portal/dashboard"); return; }
    reload();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [router]);

  const flash = (m) => { setNotice(m); setTimeout(() => setNotice(""), 4000); };

  const withdraw = async (app) => {
    if (!window.confirm(`Withdraw your application for ${app.job_title}? This cannot be undone.`)) return;
    setBusy(app.application_uuid); setError("");
    try {
      await apiSend(`/api/portal/applications/${app.application_uuid}/withdraw`, "POST", {}, token());
      flash("Application withdrawn.");
      await reload();
    } catch (e) { setError(e.message); }
    setBusy("");
  };

  const pickResume = (app) => { pendingRef.current = app; fileRef.current?.click(); };

  const onFileChosen = async (e) => {
    const file = e.target.files?.[0];
    e.target.value = "";
    const app = pendingRef.current;
    if (!file || !app) return;
    if (!file.name.toLowerCase().endsWith(".pdf")) { setError("Résumé must be a PDF."); return; }
    setBusy(app.application_uuid); setError("");
    try {
      await apiUpload(`/api/portal/applications/${app.application_uuid}/resume`,
        file, "resume", token());
      flash("Résumé updated and re-screened.");
      await reload();
    } catch (e2) { setError(e2.message); }
    setBusy("");
  };

  if (error && apps === null) return <main className="portal-main"><p className="error">{error}</p></main>;
  if (apps === null) return <main className="portal-main"><p className="muted">Loading…</p></main>;

  return (
    <main className="portal-main">
      <div className="section-title">
        <h2>My applications</h2>
        {apps.length > 0 && <span className="count-chip">{apps.length} total</span>}
      </div>
      {notice && <p className="success">{notice}</p>}
      {error && <p className="error">{error}</p>}

      {apps.length === 0 && (
        <div className="empty">
          <div className="big">📭</div>
          <p>You haven’t applied to any jobs yet.</p>
          <Link href="/portal"><button>Browse open positions</button></Link>
        </div>
      )}

      {apps.map((app) => (
        <div className="surface" key={app.application_uuid}>
          <div className="track-head">
            <div>
              <h2 style={{ margin: 0 }}>{app.job_title}</h2>
              <span className="muted">Applied {fmt(app.applied_at)}</span>
            </div>
            <span className="pill info">{app.stage || app.status}</span>
          </div>

          <StageTrack app={app} />

          <div className="kv">
            <div>
              <div className="k">Current stage</div>
              <div className="v">{app.stage || "—"}</div>
            </div>
          </div>

          {app.test && (
            <div className="sub-card">
              <div className="track-head">
                <strong>📝 Assessment test</strong>
                <span className={`pill ${app.test.status === "submitted" ? "ok" : "info"}`}>
                  {app.test.status}
                </span>
              </div>
              {app.test.expires_at && (
                <p className="muted" style={{ margin: "8px 0 0" }}>
                  Available until {fmt(app.test.expires_at)}
                </p>
              )}
              {["pending", "started"].includes(app.test.status) && (
                <a href={app.test.link} target="_blank" rel="noreferrer">
                  <button>Open test</button>
                </a>
              )}
            </div>
          )}

          {app.interview && (
            <div className="sub-card">
              <div className="track-head">
                <strong>🎤 Interview</strong>
                <span className="pill info">{app.interview.status}</span>
              </div>
              {app.interview.scheduled_at && (
                <p className="muted" style={{ margin: "8px 0 0" }}>
                  {fmt(app.interview.scheduled_at)}
                  {app.interview.duration_minutes ? ` · ${app.interview.duration_minutes} min` : ""}
                </p>
              )}
              {["scheduled", "started"].includes(app.interview.status) && (
                <a href={app.interview.link} target="_blank" rel="noreferrer">
                  <button>Join interview</button>
                </a>
              )}
            </div>
          )}

          {app.human_interview && (
            <div className="sub-card">
              <div className="track-head">
                <strong>Recruiter interview</strong>
                <span className="pill info">{app.human_interview.status}</span>
              </div>
              <p className="muted" style={{ margin: "8px 0 0" }}>
                {app.human_interview.type?.replace("_", " ")}
                {app.human_interview.scheduled_at
                  ? ` · ${fmt(app.human_interview.scheduled_at)}` : ""}
                {app.human_interview.duration_minutes
                  ? ` · ${app.human_interview.duration_minutes} min` : ""}
              </p>
              {app.human_interview.location && (
                /^https?:\/\//i.test(app.human_interview.location)
                  ? <a href={app.human_interview.location} target="_blank"
                       rel="noopener noreferrer"><button>Open meeting</button></a>
                  : <p><strong>Location:</strong> {app.human_interview.location}</p>
              )}
            </div>
          )}

          {(app.withdrawn || app.can_update_resume || app.can_withdraw) && (
            <div className="actions" style={{ marginTop: 16, paddingTop: 14,
              borderTop: "1px solid var(--border)" }}>
              {app.withdrawn && <span className="pill warn">Application withdrawn</span>}
              {app.can_update_resume && (
                <button className="mini secondary" disabled={busy === app.application_uuid}
                        onClick={() => pickResume(app)}>
                  {busy === app.application_uuid ? "Working…" : "↻ Update résumé"}
                </button>
              )}
              {app.can_withdraw && (
                <button className="mini danger" disabled={busy === app.application_uuid}
                        onClick={() => withdraw(app)}>Withdraw</button>
              )}
            </div>
          )}
        </div>
      ))}

      <input ref={fileRef} type="file" accept="application/pdf,.pdf"
             style={{ display: "none" }} onChange={onFileChosen} />
    </main>
  );
}
