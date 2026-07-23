"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import { apiGet } from "@/lib/api";

function fmtDeadline(iso) {
  if (!iso) return null;
  const d = new Date(iso);
  const days = Math.ceil((d - new Date()) / 86400000);
  if (days <= 0) return "Closing today";
  if (days === 1) return "1 day left";
  if (days <= 30) return `${days} days left`;
  return `Closes ${d.toLocaleDateString()}`;
}

export default function PortalHome() {
  const [jobs, setJobs] = useState(null);
  const [error, setError] = useState("");
  const [q, setQ] = useState("");

  useEffect(() => {
    apiGet("/api/portal/jobs")
      .then((d) => setJobs(d.jobs))
      .catch((e) => setError(e.message));
  }, []);

  const filtered = (jobs || []).filter((j) => {
    const s = q.trim().toLowerCase();
    if (!s) return true;
    return [j.title, j.department, j.location, j.employment_type]
      .filter(Boolean).some((v) => v.toLowerCase().includes(s));
  });

  return (
    <>
      <section className="portal-hero">
        <h1>Find your next role</h1>
        <p>
          Browse open positions, apply with your resume, and track every stage —
          tests and interviews — from a single place.
        </p>
        <div className="hero-search">
          <span className="icon">⌕</span>
          <input placeholder="Search by title, team, or location…"
                 value={q} onChange={(e) => setQ(e.target.value)} />
        </div>
      </section>

      <main className="portal-main">
        <div className="section-title">
          <h2>Open positions</h2>
          {jobs && <span className="count-chip">{filtered.length} open</span>}
        </div>

        {error && <p className="error">{error}</p>}
        {jobs === null && !error && <p className="muted">Loading jobs…</p>}

        {jobs && filtered.length === 0 && (
          <div className="empty">
            <div className="big">🗂️</div>
            <p>{q ? "No roles match your search." : "No open positions right now — please check back soon."}</p>
          </div>
        )}

        <div className="job-grid">
          {filtered.map((j) => (
            <Link className="job-card" key={j.uuid} href={`/portal/jobs/${j.uuid}`}>
              <div>
                <div className="team">{j.department || "Team"}</div>
                <h3>{j.title}</h3>
              </div>
              <div className="meta-row">
                {j.location && <span className="chip">📍 {j.location}</span>}
                {j.employment_type && <span className="chip">🕑 {j.employment_type}</span>}
                {j.openings > 1 && <span className="chip">👥 {j.openings} openings</span>}
                {j.application_deadline && (
                  <span className="chip accent">⏳ {fmtDeadline(j.application_deadline)}</span>
                )}
              </div>
              <span className="go">View &amp; apply →</span>
            </Link>
          ))}
        </div>
      </main>
    </>
  );
}
