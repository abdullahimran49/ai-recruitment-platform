"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { adminSession, apiGet, apiSend } from "@/lib/api";

export default function PostJob() {
  const router = useRouter();
  const [session, setSession] = useState(null);
  const [departments, setDepartments] = useState([]);
  const [form, setForm] = useState({
    title: "", department_id: "", jd_text: "", location: "",
    employment_type: "Full-time", openings: 1, pass_threshold: 60,
    application_deadline: "", is_published: false,
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    const s = adminSession();
    if (!s) { router.replace("/admin"); return; }
    setSession(s);
    apiGet("/api/admin/departments", s.token)
      .then((d) => {
        setDepartments(d);
        if (d.length) setForm((f) => ({ ...f, department_id: String(d[0].id) }));
      })
      .catch((e) => setError(e.message));
  }, [router]);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    if (!form.title.trim() || !form.department_id) {
      setError("Title and department are required."); return;
    }
    setBusy(true);
    try {
      await apiSend("/api/admin/jobs", "POST", {
        title: form.title.trim(),
        department_id: Number(form.department_id),
        jd_text: form.jd_text,
        location: form.location.trim(),
        employment_type: form.employment_type.trim(),
        openings: Number(form.openings) || 1,
        pass_threshold: Number(form.pass_threshold),
        application_deadline: form.application_deadline || null,
        is_published: form.is_published,
      }, session.token);
      router.push("/admin/dashboard");
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  if (!session) return null;

  return (
    <main className="container">
      <div className="topbar">
        <h1>Post a job</h1>
        <Link href="/admin/dashboard"><button className="secondary">← Dashboard</button></Link>
      </div>
      <div className="card">
        <form onSubmit={submit}>
          <label>Job title *</label>
          <input value={form.title} onChange={set("title")} required
                 placeholder="e.g. Senior Backend Engineer" />

          <label>Department *</label>
          <select value={form.department_id} onChange={set("department_id")}>
            {departments.map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </select>

          <div className="grid">
            <div>
              <label>Location</label>
              <input value={form.location} onChange={set("location")}
                     placeholder="e.g. Karachi / Remote" />
            </div>
            <div>
              <label>Employment type</label>
              <select value={form.employment_type} onChange={set("employment_type")}>
                {["Full-time", "Part-time", "Contract", "Internship", "Remote"]
                  .map((t) => <option key={t}>{t}</option>)}
              </select>
            </div>
            <div>
              <label>Openings</label>
              <input type="number" min="1" value={form.openings} onChange={set("openings")} />
            </div>
          </div>

          <div className="grid">
            <div>
              <label>Application deadline</label>
              <input type="datetime-local" value={form.application_deadline}
                     onChange={set("application_deadline")} />
            </div>
            <div>
              <label>Screening pass threshold (%)</label>
              <input type="number" min="0" max="100" value={form.pass_threshold}
                     onChange={set("pass_threshold")} />
            </div>
          </div>

          <label>Job description</label>
          <textarea rows={10} value={form.jd_text} onChange={set("jd_text")}
                    placeholder="Responsibilities, requirements, must-haves…" />

          <label style={{ display: "flex", alignItems: "center", gap: 8, marginTop: 16 }}>
            <input type="checkbox" style={{ width: "auto" }}
                   checked={form.is_published}
                   onChange={(e) => setForm({ ...form, is_published: e.target.checked })} />
            Publish to the public careers portal immediately
          </label>

          <button disabled={busy}>{busy ? "Posting…" : "Post job"}</button>
        </form>
        {error && <p className="error">{error}</p>}
        <p className="muted" style={{ marginTop: 12 }}>
          Tip: to define screening criteria and build a test, open the job in the
          dashboard after posting, or use the recruiter app.
        </p>
      </div>
    </main>
  );
}
