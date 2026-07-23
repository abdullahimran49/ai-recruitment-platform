"use client";

import Link from "next/link";
import { useParams, useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { apiGet, apiUpload, applicantSession } from "@/lib/api";

export default function Apply() {
  const { uuid } = useParams();
  const router = useRouter();
  const [session, setSession] = useState(null);
  const [job, setJob] = useState(null);
  const [file, setFile] = useState(null);
  const [phone, setPhone] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(null);

  useEffect(() => {
    const s = applicantSession();
    if (!s) { router.replace(`/portal/login?next=/portal/apply/${uuid}`); return; }
    setSession(s);
    setPhone(s.applicant?.phone || "");
    apiGet(`/api/portal/jobs/${uuid}`).then(setJob).catch((e) => setError(e.message));
  }, [uuid, router]);

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    if (!file) { setError("Please attach your resume (PDF)."); return; }
    if (!file.name.toLowerCase().endsWith(".pdf")) {
      setError("Resume must be a PDF file."); return;
    }
    setBusy(true);
    try {
      const d = await apiUpload(`/api/portal/jobs/${uuid}/apply`, file,
        "resume", session.token, { phone });
      setDone(d);
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  if (done) return (
    <main className="portal-narrow">
      <div className="auth-card">
        <h1>Application submitted ✓</h1>
        <p>Your application for <strong>{done.job_title}</strong> has been
          received and sent to our hiring team.</p>
        <p className="muted">
          You can track its progress — including any test or interview links —
          from your applications page.
        </p>
        <Link href="/portal/dashboard"><button>Go to my applications</button></Link>
      </div>
    </main>
  );

  if (!session) return null;
  if (error && !job) return (
    <main className="portal-narrow"><p className="error">{error}</p></main>
  );
  if (!job) return <main className="portal-narrow"><p className="muted">Loading…</p></main>;

  return (
    <main className="portal-narrow">
      <Link href={`/portal/jobs/${uuid}`} className="back">← Back to job</Link>
      <div className="auth-card" style={{ marginTop: 12 }}>
        <h1>Apply: {job.title}</h1>
        <p className="muted">Applying as {session.applicant?.name} ({session.applicant?.email})</p>
        {!job.is_open && <p className="error">Applications for this job are closed.</p>}
        <form onSubmit={submit}>
          <label>Phone</label>
          <input value={phone} onChange={(e) => setPhone(e.target.value)} />
          <label>Resume (PDF)</label>
          <input type="file" accept="application/pdf,.pdf"
                 onChange={(e) => setFile(e.target.files?.[0] || null)} />
          <button disabled={busy || !job.is_open}>
            {busy ? "Submitting…" : "Submit application"}
          </button>
        </form>
        {error && <p className="error">{error}</p>}
      </div>
    </main>
  );
}
