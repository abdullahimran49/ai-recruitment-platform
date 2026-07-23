"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import { API, adminSession, apiGet, apiSend } from "@/lib/api";

function fmt(iso) { return iso ? new Date(iso).toLocaleString() : "—"; }

export default function Applications() {
  const router = useRouter();
  const [session, setSession] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [stages, setStages] = useState([]);
  const [apps, setApps] = useState(null);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState("");   // candidate uuid currently acting on
  const [selected, setSelected] = useState(() => new Set());
  const [bulkBusy, setBulkBusy] = useState(false);
  const [filters, setFilters] = useState({ job_uuid: "", stage_id: "", source: "", q: "" });

  useEffect(() => {
    const s = adminSession();
    if (!s) { router.replace("/admin"); return; }
    setSession(s);
    Promise.all([
      apiGet("/api/admin/jobs", s.token),
      apiGet("/api/admin/pipeline-stages", s.token),
    ]).then(([j, st]) => { setJobs(j); setStages(st); })
      .catch((e) => setError(e.message));
  }, [router]);

  const load = useCallback((s, f) => {
    const qs = new URLSearchParams();
    if (f.job_uuid) qs.set("job_uuid", f.job_uuid);
    if (f.stage_id) qs.set("stage_id", f.stage_id);
    if (f.source) qs.set("source", f.source);
    if (f.q.trim()) qs.set("q", f.q.trim());
    apiGet(`/api/admin/applications?${qs.toString()}`, s.token)
      .then((d) => setApps(d.applications))
      .catch((e) => setError(e.message));
  }, []);

  useEffect(() => {
    if (session) load(session, filters);
  }, [session, filters, load]);

  const set = (k) => (e) => setFilters({ ...filters, [k]: e.target.value });
  const flash = (m) => { setNotice(m); setTimeout(() => setNotice(""), 4000); };

  const sendTest = async (a) => {
    if (!window.confirm(
      `Send an assessment test to ${a.name} (${a.email})?\n\n` +
      `They'll receive an email with a unique, one-time proctored link. ` +
      `Questions are drawn from this job's question bank.`)) return;
    setBusy(a.uuid); setError("");
    try {
      const r = await apiSend(`/api/admin/candidates/${a.uuid}/send-test`,
        "POST", {}, session.token);
      flash(`Test sent to ${a.name}. ${r.email_sent ? "Email delivered." : "Email NOT sent: " + r.email_message}`);
      load(session, filters);
    } catch (e) { setError(e.message); }
    setBusy("");
  };

  const toggle = (uuid) => setSelected((prev) => {
    const n = new Set(prev);
    n.has(uuid) ? n.delete(uuid) : n.add(uuid);
    return n;
  });
  const toggleAll = () => setSelected((prev) => {
    if (!apps) return prev;
    if (prev.size === apps.length) return new Set();
    return new Set(apps.map((a) => a.uuid));
  });

  const bulkSendTest = async () => {
    const chosen = (apps || []).filter((a) => selected.has(a.uuid));
    if (chosen.length === 0) return;
    if (!window.confirm(
      `Send an assessment test to ${chosen.length} selected candidate(s)?\n\n` +
      `Each gets a unique proctored link by email, drawn from their job's ` +
      `question bank. Candidates who already have an active link are skipped.`)) return;
    setBulkBusy(true); setError("");
    // The bulk endpoint is per-job, so group the selection by job first.
    const byJob = {};
    for (const a of chosen) (byJob[a.job_uuid] ||= []).push(a.uuid);
    let sent = 0, skipped = 0, failedJobs = [];
    for (const [jobUuid, uuids] of Object.entries(byJob)) {
      try {
        const r = await apiSend(`/api/admin/jobs/${jobUuid}/send-tests`, "POST",
          { candidate_uuids: uuids }, session.token);
        sent += r.sent_count; skipped += r.skipped_count;
      } catch (e) {
        const title = chosen.find((a) => a.job_uuid === jobUuid)?.job_title || jobUuid;
        failedJobs.push(`${title}: ${e.message}`);
      }
    }
    setBulkBusy(false);
    setSelected(new Set());
    if (failedJobs.length) setError(failedJobs.join(" · "));
    flash(`Sent ${sent} test(s)${skipped ? `, skipped ${skipped} (already invited)` : ""}.`);
    load(session, filters);
  };

  const moveStage = async (a, stageId) => {
    setError("");
    try {
      await apiSend(`/api/admin/candidates/${a.uuid}/stage`, "PATCH",
        { stage_id: Number(stageId) }, session.token);
      setApps((list) => list.map((x) => x.uuid === a.uuid
        ? { ...x, stage_id: Number(stageId),
            stage: stages.find((s) => s.id === Number(stageId))?.name } : x));
    } catch (e) { setError(e.message); }
  };

  if (!session) return null;

  return (
    <main className="container wide">
      <div className="topbar">
        <div>
          <h1>Applications</h1>
          <p className="muted">Every candidate across all roles — review resumes,
            send tests, and track each person's stage.</p>
        </div>
        <div className="links">
          <Link href="/admin/dashboard"><button className="secondary">← Dashboard</button></Link>
        </div>
      </div>

      {error && <p className="error">{error}</p>}
      {notice && <p className="success">{notice}</p>}

      <div className="card">
        <div className="grid">
          <div>
            <label>Role</label>
            <select value={filters.job_uuid} onChange={set("job_uuid")}>
              <option value="">All roles</option>
              {jobs.map((j) => <option key={j.uuid} value={j.uuid}>{j.title}</option>)}
            </select>
          </div>
          <div>
            <label>Stage</label>
            <select value={filters.stage_id} onChange={set("stage_id")}>
              <option value="">All stages</option>
              {stages.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
            </select>
          </div>
          <div>
            <label>Source</label>
            <select value={filters.source} onChange={set("source")}>
              <option value="">All sources</option>
              <option value="portal">Portal</option>
              <option value="upload">Upload</option>
              <option value="manual">Manual</option>
            </select>
          </div>
          <div>
            <label>Search</label>
            <input placeholder="name or email" value={filters.q} onChange={set("q")} />
          </div>
        </div>
      </div>

      <div className="card">
        <div className="section-title">
          <h2 style={{ margin: 0 }}>Results</h2>
          {apps && <span className="count-chip">{apps.length}</span>}
        </div>

        {selected.size > 0 && (
          <div className="topbar" style={{ background: "var(--accent-soft)",
            border: "1px solid var(--border)", borderRadius: 12,
            padding: "10px 14px", marginBottom: 12 }}>
            <strong>{selected.size} selected</strong>
            <div className="actions">
              <button className="mini" disabled={bulkBusy} onClick={bulkSendTest}>
                {bulkBusy ? "Sending…" : `📨 Send test to ${selected.size}`}
              </button>
              <button className="mini secondary" onClick={() => setSelected(new Set())}>
                Clear
              </button>
            </div>
          </div>
        )}

        {apps === null ? <p className="muted">Loading…</p> :
         apps.length === 0 ? <p className="muted">No applications match these filters.</p> : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th style={{ width: 34 }}>
                    <input type="checkbox" style={{ width: "auto" }}
                           checked={selected.size === apps.length && apps.length > 0}
                           onChange={toggleAll} />
                  </th>
                  <th>Candidate</th><th>Role</th><th>Score</th>
                  <th>Stage</th><th>Source</th><th>Applied</th><th>Actions</th></tr>
              </thead>
              <tbody>
                {apps.map((a) => (
                  <tr key={a.uuid}>
                    <td>
                      <input type="checkbox" style={{ width: "auto" }}
                             checked={selected.has(a.uuid)}
                             onChange={() => toggle(a.uuid)} />
                    </td>
                    <td>{a.name}<br /><span className="muted">{a.email}</span></td>
                    <td>{a.job_title}<br /><span className="muted">{a.department}</span></td>
                    <td>{a.resume_score != null ? Math.round(a.resume_score) : "—"}</td>
                    <td>
                      <select value={a.stage_id || ""}
                              onChange={(e) => moveStage(a, e.target.value)}
                              style={{ padding: "4px 6px", fontSize: 13 }}>
                        {!a.stage_id && <option value="">—</option>}
                        {stages.map((s) => <option key={s.id} value={s.id}>{s.name}</option>)}
                      </select>
                    </td>
                    <td><span className={`src-tag ${a.source === "portal" ? "portal" : ""}`}>{a.source}</span></td>
                    <td className="muted">{fmt(a.applied_at)}</td>
                    <td>
                      <div className="actions">
                        {a.has_resume ? (
                          <a href={`${API}/api/admin/candidates/${a.uuid}/resume?token=${encodeURIComponent(session.token)}`}
                             target="_blank" rel="noreferrer">
                            <button className="mini secondary">📄 Resume</button>
                          </a>
                        ) : (
                          <span className="muted" style={{ fontSize: 12 }}>no PDF</span>
                        )}
                        <button className="mini" disabled={busy === a.uuid}
                                onClick={() => sendTest(a)}>
                          {busy === a.uuid ? "Sending…" : "Send test"}
                        </button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </main>
  );
}
