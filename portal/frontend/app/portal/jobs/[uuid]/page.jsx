"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { apiGet, applicantSession } from "@/lib/api";

export default function JobDetail() {
  const { uuid } = useParams();
  const router = useRouter();
  const [job, setJob] = useState(null);
  const [error, setError] = useState("");
  const [signedIn, setSignedIn] = useState(false);

  useEffect(() => {
    setSignedIn(!!applicantSession());
    apiGet(`/api/portal/jobs/${uuid}`)
      .then(setJob)
      .catch((e) => setError(e.message));
  }, [uuid]);

  const onApply = () => {
    if (signedIn) router.push(`/portal/apply/${uuid}`);
    else router.push(`/portal/login?next=/portal/apply/${uuid}`);
  };

  if (error) return (
    <main className="portal-main">
      <p className="error">{error}</p>
      <Link href="/portal" className="back">← All jobs</Link>
    </main>
  );
  if (!job) return <main className="portal-main"><p className="muted">Loading…</p></main>;

  return (
    <main className="portal-main">
      <Link href="/portal" className="back">← All jobs</Link>
      <div className="surface" style={{ marginTop: 14 }}>
        <div className="team" style={{ color: "var(--accent)", fontWeight: 600, fontSize: 13 }}>
          {job.department}
        </div>
        <h1 style={{ marginTop: 4 }}>{job.title}</h1>
        <div className="meta-row" style={{ marginTop: 12 }}>
          {job.location && <span className="chip">📍 {job.location}</span>}
          {job.employment_type && <span className="chip">🕑 {job.employment_type}</span>}
          {job.openings > 1 && <span className="chip">👥 {job.openings} openings</span>}
          {job.application_deadline && (
            <span className="chip accent">
              ⏳ Closes {new Date(job.application_deadline).toLocaleDateString()}
            </span>
          )}
        </div>

        <h2 style={{ marginTop: 24 }}>About the role</h2>
        <div className="jd-body">{job.jd_text || "No description provided."}</div>

        <div style={{ marginTop: 24 }}>
          {job.is_open ? (
            <button onClick={onApply}>
              {signedIn ? "Apply now" : "Sign in to apply"}
            </button>
          ) : (
            <span className="pill warn">Applications are closed</span>
          )}
        </div>
      </div>
    </main>
  );
}
