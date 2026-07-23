"use client";

import { useEffect, useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { adminSession, apiGet, apiSend, clearAdminSession } from "@/lib/api";

function fmtTime(seconds) {
  if (seconds == null) return "—";
  if (seconds < 60) return `${seconds}s`;
  const m = Math.floor(seconds / 60);
  const s = seconds % 60;
  if (m < 60) return s ? `${m}m ${s}s` : `${m}m`;
  const h = Math.floor(m / 60);
  const rm = m % 60;
  return rm ? `${h}h ${rm}m` : `${h}h`;
}

function localDate(iso) {
  if (!iso) return "—";
  return new Date(iso + (iso.includes("Z") ? "" : "Z"));
}

const TYPE_LABELS = { in_person: "In-Person", phone: "Phone", video: "Video" };

// Friendly names for the merit components (mirrors the backend part names).
const MIX_LABELS = { resume: "résumé", test: "test", interview: "interview" };

// Merit stage -> { label, pill-class } for the recommendation column.
const STAGE_META = {
  awaiting_test: { label: "Needs to take test", cls: "mute" },
  below_interview_cutoff: { label: "Not yet qualified", cls: "warn" },
  invite_to_interview: { label: "Ready for AI interview", cls: "info" },
  interview_pending: { label: "AI interview scheduled", cls: "info" },
  interviewed_no_test: { label: "Interviewed (no test)", cls: "info" },
  onsite_candidate: { label: "Recommended for onsite", cls: "ok" },
  below_onsite_cutoff: { label: "Below onsite bar", cls: "warn" },
};

function MeritBar({ value }) {
  if (value == null) return <span className="muted">—</span>;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
      <div className="merit-bar" style={{ flex: 1 }}>
        <span style={{ width: `${Math.max(0, Math.min(100, value))}%` }} />
      </div>
      <strong style={{ fontVariantNumeric: "tabular-nums", minWidth: 34,
                       textAlign: "right" }}>{value}</strong>
    </div>
  );
}

// Inline job editor. The jobs list doesn't carry the (long) JD text, so this
// lazily loads the full job detail when opened.
function JobEditForm({ job, setJob, departments, role, token, onSubmit }) {
  useEffect(() => {
    if (job._loaded) return;
    apiGet(`/api/admin/jobs/${job.uuid}`, token)
      .then((d) => setJob((j) => (j && j.uuid === d.uuid)
        ? { ...j, jd_text: d.jd_text || "", pass_threshold: d.pass_threshold,
            department_id: d.department_id ?? j.department_id, _loaded: true }
        : j))
      .catch(() => setJob((j) => (j ? { ...j, _loaded: true } : j)));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [job.uuid]);

  return (
    <div className="card" style={{ marginTop: 12, borderLeft: "4px solid var(--accent)" }}>
      <h3>✏️ Edit job</h3>
      <form onSubmit={onSubmit}>
        <div className="row" style={{ flexWrap: "wrap" }}>
          <div style={{ minWidth: 220 }}>
            <label>Title</label>
            <input value={job.title} required minLength={2}
                   onChange={(e) => setJob({ ...job, title: e.target.value })} />
          </div>
          <div style={{ minWidth: 160 }}>
            <label>Pass threshold (résumé %)</label>
            <input type="number" min="0" max="100" value={job.pass_threshold}
                   onChange={(e) => setJob({ ...job, pass_threshold: e.target.value })} />
          </div>
          <div style={{ minWidth: 200 }}>
            <label>Department{role !== "super_admin" ? " (super admin only)" : ""}</label>
            <select value={job.department_id}
                    disabled={role !== "super_admin"}
                    onChange={(e) => setJob({ ...job, department_id: e.target.value })}>
              {departments.map((d) => (
                <option key={d.id} value={d.id}>{d.name}</option>
              ))}
            </select>
          </div>
        </div>
        <label>Job description (used by screening &amp; the AI interviewer)</label>
        <textarea rows={6} value={job.jd_text}
                  placeholder={job._loaded ? "" : "Loading…"}
                  onChange={(e) => setJob({ ...job, jd_text: e.target.value })} />
        <button>Save changes</button>
        <button type="button" className="secondary" style={{ marginLeft: 8 }}
                onClick={() => setJob(null)}>Cancel</button>
      </form>
    </div>
  );
}

const LETTERS = ["A", "B", "C", "D"];

const EMPTY_Q = { question: "", options: ["", "", "", ""], correct_index: 0,
                  explanation: "", category_id: "" };

// Chance that two candidates share a given question = k/N, so the expected
// overlap between any two papers is k*k/N. Pool exposure after c candidates
// (if they compare notes) = N * (1 - (1 - k/N)^c).
function predictability(poolSize, perCandidate, candidates = 5) {
  const N = Math.max(1, poolSize);
  const k = Math.min(perCandidate, N);
  const overlap = (k * k) / N;
  const exposedFrac = 1 - Math.pow(1 - k / N, candidates);
  return {
    overlap: Math.round(overlap * 10) / 10,
    overlapPct: Math.round((overlap / Math.max(1, k)) * 100),
    exposedPct: Math.round(exposedFrac * 100),
    candidates,
    // Rules of thumb: sharing over half your paper with the next candidate is
    // not meaningfully randomised.
    level: k >= N ? "none" : overlap / k > 0.5 ? "weak"
      : overlap / k > 0.25 ? "ok" : "good",
  };
}

function PredictabilityMeter({ poolSize, perCandidate, candidates = 5 }) {
  const p = predictability(poolSize, perCandidate, candidates);
  const COLORS = { none: "#e66", weak: "#e9a13b", ok: "#5aa9e6", good: "#4caf50" };
  const LABEL = {
    none: "Not randomised at all",
    weak: "Weakly randomised",
    ok: "Reasonably randomised",
    good: "Well randomised",
  };
  const suggested = Math.max(poolSize, perCandidate * 4);
  return (
    <div style={{ borderLeft: `3px solid ${COLORS[p.level]}`,
                  padding: "6px 10px", marginTop: 6, fontSize: 13 }}>
      <strong style={{ color: COLORS[p.level] }}>{LABEL[p.level]}</strong>
      {p.level === "none" ? (
        <div className="muted">
          Every candidate sits the whole pool of {poolSize} — all links are
          identical. Lower “questions per candidate” to randomise.
        </div>
      ) : (
        <div className="muted">
          Any two candidates share about <strong>{p.overlap}</strong> of{" "}
          {perCandidate} questions ({p.overlapPct}%). After{" "}
          {p.candidates} candidates compare notes, roughly{" "}
          <strong>{p.exposedPct}%</strong> of the pool is exposed.
          {(p.level === "weak" || p.exposedPct > 80) && (
            <> Consider growing the pool to <strong>{suggested}+</strong>.</>
          )}
        </div>
      )}
    </div>
  );
}

function QuestionBank({ job, token, onError, onNotice }) {
  const [bank, setBank] = useState(null);
  const [busy, setBusy] = useState("");
  const [newCat, setNewCat] = useState("");
  const [gen, setGen] = useState({ count: 5, difficulty: "medium",
                                   category_id: "" });
  const [draft, setDraft] = useState(EMPTY_Q);
  const [showAdd, setShowAdd] = useState(false);
  const [editing, setEditing] = useState(null);   // {id, ...fields}
  const [showRetired, setShowRetired] = useState(false);

  const load = () =>
    apiGet(`/api/admin/jobs/${job.uuid}/bank?include_retired=${showRetired}`,
           token)
      .then(setBank)
      .catch((e) => onError(e.message));

  useEffect(() => { load(); /* eslint-disable-next-line */ },
            [job.uuid, showRetired]);

  const run = async (label, fn) => {
    setBusy(label);
    try {
      await fn();
      await load();
    } catch (e) {
      onError(e.message);
    } finally {
      setBusy("");
    }
  };

  const addCategory = () => {
    if (!newCat.trim()) return;
    run("cat", async () => {
      await apiSend(`/api/admin/jobs/${job.uuid}/bank/categories`, "POST",
                    { name: newCat.trim() }, token);
      setNewCat("");
    });
  };

  const suggest = () =>
    run("suggest", async () => {
      const r = await apiSend(
        `/api/admin/jobs/${job.uuid}/bank/categories/suggest`, "POST", {},
        token);
      onNotice(`Suggested: ${r.categories.map((c) => c.name).join(", ")}`);
    });

  const generate = () =>
    run("gen", async () => {
      const r = await apiSend(`/api/admin/jobs/${job.uuid}/bank/generate`,
                              "POST",
                              { count: Number(gen.count),
                                difficulty: gen.difficulty,
                                category_id: gen.category_id
                                  ? Number(gen.category_id) : null },
                              token);
      onNotice(`Added ${r.added} of ${r.requested} requested `
               + `(duplicates skipped).`);
    });

  const addItem = () =>
    run("item", async () => {
      await apiSend(`/api/admin/jobs/${job.uuid}/bank/items`, "POST",
                    { question: draft.question, options: draft.options,
                      correct_index: Number(draft.correct_index),
                      explanation: draft.explanation,
                      category_id: draft.category_id
                        ? Number(draft.category_id) : null },
                    token);
      setDraft(EMPTY_Q);
      setShowAdd(false);
    });

  if (!bank) return <p className="muted">Loading the bank…</p>;

  const groups = [...bank.categories.map((c) => ({ ...c })),
                  { id: null, name: "Uncategorised" }];

  return (
    <div className="card" style={{ borderLeft: "4px solid var(--accent)",
                                   marginTop: 8 }}>
      <h3 style={{ marginBottom: 4 }}>🏦 Question bank</h3>
      <p className="muted" style={{ marginBottom: 8 }}>
        The reusable library for <strong>{job.title}</strong>. Build it deep
        once — every test draws from it, and each candidate&apos;s link gets
        its own random selection, so no two candidates sit the same paper.
      </p>

      <div className="actions" style={{ marginBottom: 8 }}>
        <input placeholder="New category (Data, AI, General…)"
               value={newCat} style={{ maxWidth: 260 }}
               onChange={(e) => setNewCat(e.target.value)}
               onKeyDown={(e) => e.key === "Enter" && addCategory()} />
        <button className="mini" disabled={!!busy} onClick={addCategory}>
          ➕ Add category
        </button>
        <button className="mini secondary" disabled={!!busy} onClick={suggest}>
          {busy === "suggest" ? "Reading the JD…" : "🤖 Suggest from JD"}
        </button>
      </div>

      <div className="actions" style={{ marginBottom: 10, flexWrap: "wrap" }}>
        <select value={gen.category_id}
                onChange={(e) => setGen({ ...gen, category_id: e.target.value })}>
          <option value="">Uncategorised</option>
          {bank.categories.map((c) => (
            <option key={c.id} value={c.id}>{c.name}</option>
          ))}
        </select>
        <input type="number" min={1} max={50} value={gen.count}
               style={{ maxWidth: 90 }}
               onChange={(e) => setGen({ ...gen, count: e.target.value })} />
        <select value={gen.difficulty}
                onChange={(e) => setGen({ ...gen, difficulty: e.target.value })}>
          <option value="easy">easy</option>
          <option value="medium">medium</option>
          <option value="hard">hard</option>
        </select>
        <button className="mini" disabled={!!busy} onClick={generate}>
          {busy === "gen" ? "Generating…" : "⚡ Generate into bank"}
        </button>
        <button className="mini secondary" disabled={!!busy}
                onClick={() => setShowAdd((v) => !v)}>
          ✍️ Write one
        </button>
      </div>

      {showAdd && (
        <div className="card" style={{ marginBottom: 10 }}>
          <textarea rows={2} placeholder="Question text"
                    value={draft.question}
                    onChange={(e) => setDraft({ ...draft,
                                                question: e.target.value })} />
          {draft.options.map((o, i) => (
            <div key={i} className="actions" style={{ marginBottom: 4 }}>
              <input type="radio" name="correct"
                     checked={Number(draft.correct_index) === i}
                     onChange={() => setDraft({ ...draft, correct_index: i })}
                     title="Mark as the correct answer" />
              <input placeholder={`Option ${LETTERS[i]}`} value={o}
                     onChange={(e) => {
                       const opts = [...draft.options];
                       opts[i] = e.target.value;
                       setDraft({ ...draft, options: opts });
                     }} />
            </div>
          ))}
          <div className="actions">
            <select value={draft.category_id}
                    onChange={(e) => setDraft({ ...draft,
                                                category_id: e.target.value })}>
              <option value="">Uncategorised</option>
              {bank.categories.map((c) => (
                <option key={c.id} value={c.id}>{c.name}</option>
              ))}
            </select>
            <button className="mini" disabled={!!busy} onClick={addItem}>
              ✅ Save to bank
            </button>
          </div>
        </div>
      )}

      <div className="topbar" style={{ marginBottom: 6 }}>
        <p className="muted" style={{ margin: 0 }}>
          <strong>{bank.items.filter((i) => i.active !== false).length}</strong>
          {" "}active question(s) across {bank.categories.length} categor
          {bank.categories.length === 1 ? "y" : "ies"}.
          {" "}A deeper bank means less predictable tests — a pool of{" "}
          <strong>4×</strong> the paper size is a good target.
        </p>
        <label className="muted" style={{ display: "flex", gap: 6,
                                          alignItems: "center", fontSize: 13 }}>
          <input type="checkbox" style={{ width: "auto" }}
                 checked={showRetired}
                 onChange={(e) => setShowRetired(e.target.checked)} />
          Show retired
        </label>
      </div>

      {groups.map((g) => {
        const rows = bank.items.filter((i) => i.category_id === g.id);
        if (!rows.length && g.id === null) return null;
        return (
          <details key={g.id ?? "none"} style={{ marginBottom: 6 }}>
            <summary style={{ cursor: "pointer" }}>
              {g.name} — {rows.length} question(s)
            </summary>
            <div style={{ padding: "6px 2px" }}>
              {rows.length === 0 && (
                <p className="muted">Empty — generate or write some above.</p>
              )}
              {rows.map((r) => (editing?.id === r.id ? (
                <div key={r.id} className="card" style={{ margin: "6px 0" }}>
                  <label>Question</label>
                  <textarea rows={2} value={editing.question}
                            onChange={(e) => setEditing({ ...editing,
                              question: e.target.value })} />
                  {editing.options.map((o, i) => (
                    <div key={i} className="actions" style={{ marginBottom: 4 }}>
                      <input type="radio" name={`edit-correct-${r.id}`}
                             checked={Number(editing.correct_index) === i}
                             onChange={() => setEditing({ ...editing,
                               correct_index: i })}
                             title="Mark as the correct answer" />
                      <input value={o} placeholder={`Option ${LETTERS[i]}`}
                             onChange={(e) => {
                               const opts = [...editing.options];
                               opts[i] = e.target.value;
                               setEditing({ ...editing, options: opts });
                             }} />
                    </div>
                  ))}
                  <div className="actions">
                    <select value={editing.category_id ?? ""}
                            onChange={(e) => setEditing({ ...editing,
                              category_id: e.target.value })}>
                      <option value="">Uncategorised</option>
                      {bank.categories.map((c) => (
                        <option key={c.id} value={c.id}>{c.name}</option>
                      ))}
                    </select>
                    <select value={editing.difficulty}
                            onChange={(e) => setEditing({ ...editing,
                              difficulty: e.target.value })}>
                      <option value="easy">easy</option>
                      <option value="medium">medium</option>
                      <option value="hard">hard</option>
                    </select>
                    <button className="mini" disabled={!!busy}
                            onClick={() => run("save", async () => {
                              await apiSend(`/api/admin/bank/items/${r.id}`,
                                            "PATCH", {
                                              question: editing.question,
                                              options: editing.options,
                                              correct_index:
                                                Number(editing.correct_index),
                                              difficulty: editing.difficulty,
                                              category_id: editing.category_id
                                                ? Number(editing.category_id)
                                                : null,
                                            }, token);
                              setEditing(null);
                              onNotice("Question updated.");
                            })}>
                      💾 Save
                    </button>
                    <button className="mini secondary"
                            onClick={() => setEditing(null)}>Cancel</button>
                  </div>
                  {r.times_used > 0 && (
                    <p className="muted" style={{ fontSize: 12, marginTop: 6 }}>
                      Used in {r.times_used} test(s) already. Editing changes
                      it for <em>future</em> tests only — papers candidates
                      have already sat keep the original wording.
                    </p>
                  )}
                </div>
              ) : (
                <div key={r.id} className="topbar"
                     style={{ alignItems: "flex-start", gap: 8,
                              padding: "4px 0",
                              opacity: r.active === false ? 0.5 : 1,
                              borderBottom: "1px solid var(--border)" }}>
                  <div style={{ flex: 1 }}>
                    <div>
                      {r.question}
                      {r.active === false && (
                        <span className="muted" style={{ fontSize: 12 }}>
                          {" "}· retired
                        </span>
                      )}
                    </div>
                    <div className="muted" style={{ fontSize: 12 }}>
                      correct <strong>{LETTERS[r.correct_index]}</strong> ·{" "}
                      {r.options[r.correct_index]} · {r.source} · {r.difficulty}
                      {r.times_used ? ` · used ${r.times_used}×` : ""}
                    </div>
                  </div>
                  <div className="actions">
                    <button className="mini secondary" disabled={!!busy}
                            title="Edit this question"
                            onClick={() => setEditing({
                              id: r.id, question: r.question,
                              options: [...r.options],
                              correct_index: r.correct_index,
                              difficulty: r.difficulty,
                              category_id: r.category_id ?? "",
                            })}>
                      ✏️
                    </button>
                    <button className="mini secondary" disabled={!!busy}
                            title={r.active === false
                              ? "Offer this question again"
                              : "Stop offering it in new tests (keeps it, and "
                                + "keeps past papers intact)"}
                            onClick={() => run("retire", () =>
                              apiSend(`/api/admin/bank/items/${r.id}`, "PATCH",
                                      { active: r.active === false }, token))}>
                      {r.active === false ? "♻️" : "🚫"}
                    </button>
                    <button className="mini secondary" disabled={!!busy}
                            title="Delete permanently"
                            onClick={() => {
                              if (!confirm(
                                r.times_used
                                  ? `This question has been used in `
                                    + `${r.times_used} test(s). Deleting `
                                    + `removes it from the bank (past papers `
                                    + `keep it). Retire it instead?`
                                  : "Delete this question from the bank?")) return;
                              run("del", () =>
                                apiSend(`/api/admin/bank/items/${r.id}`,
                                        "DELETE", undefined, token));
                            }}>
                      🗑
                    </button>
                  </div>
                </div>
              )))}
              {g.id !== null && (
                <button className="mini secondary" style={{ marginTop: 6 }}
                        disabled={!!busy}
                        title="The questions stay in the bank as uncategorised"
                        onClick={() => run("delcat", async () => {
                          const r = await apiSend(
                            `/api/admin/bank/categories/${g.id}`, "DELETE",
                            undefined, token);
                          onNotice(`Category deleted — ${r.questions_kept} `
                                   + "question(s) kept as uncategorised.");
                        })}>
                  🗑 Delete &quot;{g.name}&quot; category
                </button>
              )}
            </div>
          </details>
        );
      })}
    </div>
  );
}

function EmailTemplateEditor({ job, token, onError, onNotice }) {
  const KIND = "onsite_interview";
  const [tpl, setTpl] = useState(null);
  const [draft, setDraft] = useState({ subject: "", body: "" });
  const [preview, setPreview] = useState(null);
  const [busy, setBusy] = useState("");

  const load = () =>
    apiGet(`/api/admin/jobs/${job.uuid}/email-templates/${KIND}`, token)
      .then((d) => { setTpl(d); setDraft({ subject: d.subject, body: d.body }); })
      .catch((e) => onError(e.message));

  useEffect(() => { load(); /* eslint-disable-next-line */ }, [job.uuid]);

  const run = async (label, fn) => {
    setBusy(label); onError("");
    try { await fn(); } catch (e) { onError(e.message); } finally { setBusy(""); }
  };

  const doPreview = () =>
    run("preview", async () => {
      setPreview(await apiSend(
        `/api/admin/jobs/${job.uuid}/email-templates/${KIND}/preview`, "POST",
        draft, token));
    });

  const save = () =>
    run("save", async () => {
      await apiSend(`/api/admin/jobs/${job.uuid}/email-templates/${KIND}`,
                    "PUT", draft, token);
      onNotice("Interview email template saved for this job.");
      await load();
    });

  const reset = () =>
    run("reset", async () => {
      if (!confirm("Discard this job's custom wording and go back to the "
                   + "default template?")) return;
      await apiSend(`/api/admin/jobs/${job.uuid}/email-templates/${KIND}`,
                    "DELETE", undefined, token);
      onNotice("Reverted to the default interview email.");
      setPreview(null);
      await load();
    });

  if (!tpl) return <p className="muted">Loading the template…</p>;

  const dirty = draft.subject !== tpl.subject || draft.body !== tpl.body;

  return (
    <div className="card" style={{ borderLeft: "4px solid var(--accent)",
                                   marginTop: 8 }}>
      <h3 style={{ marginBottom: 4 }}>✉️ Interview invitation email</h3>
      <p className="muted" style={{ marginBottom: 8 }}>
        The email sent when you schedule an onsite/phone/video interview for{" "}
        <strong>{job.title}</strong>.{" "}
        {tpl.is_default
          ? "Currently using the default wording."
          : `Customised for this job${tpl.updated_by
              ? ` by ${tpl.updated_by}` : ""}.`}
      </p>

      <div style={{ marginBottom: 8 }}>
        <label>Subject</label>
        <input value={draft.subject}
               onChange={(e) => setDraft({ ...draft, subject: e.target.value })} />
      </div>
      <div>
        <label>Body</label>
        <textarea rows={14} value={draft.body} style={{ fontFamily: "monospace" }}
                  onChange={(e) => setDraft({ ...draft, body: e.target.value })} />
      </div>

      <p className="muted" style={{ fontSize: 13, margin: "6px 0" }}>
        Placeholders (click to copy):{" "}
        {tpl.placeholders.map((p) => (
          <code key={p} style={{ marginRight: 6, cursor: "pointer" }}
                title="Copy"
                onClick={() => navigator.clipboard?.writeText(`{{${p}}}`)}>
            {`{{${p}}}`}
          </code>
        ))}
        <br />
        <em>location_line</em> and <em>notes_block</em> render as whole lines
        and vanish when empty — use them instead of writing conditionals.
      </p>

      <div className="actions">
        <button className="mini secondary" disabled={!!busy} onClick={doPreview}>
          {busy === "preview" ? "Rendering…" : "👁 Preview"}
        </button>
        <button className="mini" disabled={!!busy || !dirty} onClick={save}>
          {busy === "save" ? "Saving…" : "💾 Save for this job"}
        </button>
        {!tpl.is_default && (
          <button className="mini secondary" disabled={!!busy} onClick={reset}>
            ↩️ Revert to default
          </button>
        )}
      </div>

      {preview && (
        <div className="card" style={{ marginTop: 10 }}>
          <strong>Preview</strong>
          <p className="muted" style={{ fontSize: 12 }}>
            Rendered with sample values — this is what a candidate receives.
          </p>
          {preview.unknown_placeholders?.length > 0 && (
            <p style={{ color: "var(--danger, #e66)" }}>
              ⚠️ Unknown placeholder(s):{" "}
              {preview.unknown_placeholders.map((p) => `{{${p}}}`).join(", ")}
              {" "}— these will be emailed literally. Check the spelling.
            </p>
          )}
          <div style={{ fontSize: 13 }}>
            <div><strong>Subject:</strong> {preview.subject}</div>
            <pre style={{ whiteSpace: "pre-wrap", marginTop: 6 }}>
              {preview.body}
            </pre>
          </div>
        </div>
      )}
    </div>
  );
}

export default function Dashboard() {
  const router = useRouter();
  const [session, setSession] = useState(null);
  const [jobs, setJobs] = useState([]);
  const [selected, setSelected] = useState(null);
  const [candidates, setCandidates] = useState([]);
  const [activeTab, setActiveTab] = useState("candidates"); // candidates|ai|human|setup
  const [detail, setDetail] = useState(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");

  // Interview scheduling state
  const [showInterview, setShowInterview] = useState(null); // candidate uuid
  const [interviewForm, setInterviewForm] = useState({
    interview_type: "video", scheduled_at: "", duration_minutes: 30,
    location: "", notes: "",
  });
  const [interviews, setInterviews] = useState([]);

  // Edit expiry state
  const [editingExpiry, setEditingExpiry] = useState(null); // assignment uuid
  const [newExpiry, setNewExpiry] = useState("");

  // AI interview state
  const [aiInterviews, setAiInterviews] = useState([]);
  const [showAIForm, setShowAIForm] = useState(null); // candidate uuid
  const [aiForm, setAiForm] = useState({
    scheduled_at: "", duration_minutes: 20, num_questions: 5,
    focus: "", max_warnings: 3,
  });
  const [aiDetail, setAiDetail] = useState(null);

  // Job edit state
  const [departments, setDepartments] = useState([]);
  const [editingJob, setEditingJob] = useState(null); // {uuid,title,pass_threshold,jd_text,department_id}

  // Attempt history state
  const [attempts, setAttempts] = useState(null); // /candidates/{uuid}/attempts

  // Merit Decider state
  const [merit, setMerit] = useState(null);        // /merit response
  const [meritCfg, setMeritCfg] = useState(null);  // editable config
  const [meritBusy, setMeritBusy] = useState(false);
  const [showAutoInvite, setShowAutoInvite] = useState(false);
  const [autoInviteForm, setAutoInviteForm] = useState({
    scheduled_at: "", duration_minutes: 20, num_questions: 5,
    focus: "", max_warnings: 3,
  });

  useEffect(() => {
    const s = adminSession();
    if (!s) { router.push("/admin"); return; }
    setSession(s);
    apiGet("/api/admin/jobs", s.token)
      .then(setJobs)
      .catch((e) => {
        if (e.status === 401) { clearAdminSession(); router.push("/admin"); }
        else setError(e.message);
      });
    apiGet("/api/admin/departments", s.token)
      .then(setDepartments)
      .catch(() => {});
  }, [router]);

  const reloadJobs = () =>
    apiGet("/api/admin/jobs", session.token).then(setJobs).catch(() => {});

  const saveJobEdit = async (e) => {
    e.preventDefault();
    setError(""); setNotice("");
    try {
      await apiSend(`/api/admin/jobs/${editingJob.uuid}`, "PATCH", {
        title: editingJob.title,
        pass_threshold: Number(editingJob.pass_threshold),
        jd_text: editingJob.jd_text,
        department_id: Number(editingJob.department_id),
      }, session.token);
      setNotice("Job updated.");
      setEditingJob(null);
      await reloadJobs();
      if (selected?.uuid === editingJob.uuid) {
        setSelected((prev) => ({ ...prev, title: editingJob.title }));
      }
    } catch (err) { setError(err.message); }
  };

  const deleteJob = async (job) => {
    const typed = prompt(
      `This permanently deletes "${job.title}" and ALL of its data — `
      + `${job.candidates} candidate(s), their tests, answers, proctoring and `
      + `interviews. This cannot be undone.\n\nType the job title to confirm:`);
    if (typed == null) return;
    if (typed.trim() !== job.title.trim()) {
      setError("Job title did not match — deletion cancelled.");
      return;
    }
    setError(""); setNotice("");
    try {
      const res = await apiSend(`/api/admin/jobs/${job.uuid}`, "DELETE",
                                undefined, session.token);
      const r = res.removed || {};
      setNotice(`Deleted "${res.title}" (${r.candidates || 0} candidates, `
        + `${r.tests || 0} tests, ${r.ai_interviews || 0} AI interviews).`);
      if (selected?.uuid === job.uuid) { setSelected(null); setCandidates([]); }
      await reloadJobs();
    } catch (err) { setError(err.message); }
  };

  const openJob = (job) => {
    setSelected(job);
    setActiveTab("candidates");
    setCandidates([]);
    setDetail(null);
    setAiDetail(null);
    setAttempts(null);
    setEditingExpiry(null);
    setShowAIForm(null);
    setShowInterview(null);
    setInterviews([]);
    setMerit(null);
    setShowAutoInvite(false);
    setNotice("");
    apiGet(`/api/admin/jobs/${job.uuid}/candidates`, session.token)
      .then(setCandidates)
      .catch((e) => setError(e.message));
    apiGet(`/api/admin/jobs/${job.uuid}/interviews`, session.token)
      .then(setInterviews)
      .catch(() => {});
    apiGet(`/api/admin/jobs/${job.uuid}/ai-interviews`, session.token)
      .then(setAiInterviews)
      .catch(() => {});
    loadMerit(job.uuid);
  };

  const refreshJob = () => selected && openJob(selected);

  // ---- Merit Decider --------------------------------------------------------

  const loadMerit = (jobUuid) => {
    apiGet(`/api/admin/jobs/${jobUuid}/merit`, session.token)
      .then((d) => { setMerit(d); setMeritCfg(d.config); })
      .catch(() => {});
  };

  const weightSum = meritCfg
    ? Number(meritCfg.resume_weight) + Number(meritCfg.test_weight)
      + Number(meritCfg.interview_weight)
    : 0;

  const saveMeritConfig = async () => {
    if (weightSum !== 100) return;
    setMeritBusy(true); setError(""); setNotice("");
    try {
      await apiSend(`/api/admin/jobs/${selected.uuid}/merit-config`, "PUT", {
        resume_weight: Number(meritCfg.resume_weight),
        test_weight: Number(meritCfg.test_weight),
        interview_weight: Number(meritCfg.interview_weight),
        invite_threshold: Number(meritCfg.invite_threshold),
        onsite_threshold: Number(meritCfg.onsite_threshold),
        onsite_top_n: Number(meritCfg.onsite_top_n),
        require_test_pass: !!meritCfg.require_test_pass,
        auto_invite_on_pass: !!meritCfg.auto_invite_on_pass,
        auto_invite_delay_hours: Number(meritCfg.auto_invite_delay_hours ?? 48),
        auto_invite_duration_minutes:
          Number(meritCfg.auto_invite_duration_minutes ?? 20),
        auto_invite_num_questions:
          Number(meritCfg.auto_invite_num_questions ?? 5),
      }, session.token);
      setNotice("Merit weights & thresholds saved.");
      loadMerit(selected.uuid);
    } catch (err) { setError(err.message); }
    setMeritBusy(false);
  };

  const runAutoInvite = async (e) => {
    e.preventDefault();
    setMeritBusy(true); setError(""); setNotice("");
    try {
      const local = new Date(autoInviteForm.scheduled_at);
      const label = local.toLocaleString([], {
        weekday: "long", year: "numeric", month: "long", day: "numeric",
        hour: "2-digit", minute: "2-digit" }) + " (Local Time)";
      const res = await apiSend(
        `/api/admin/jobs/${selected.uuid}/merit/auto-invite`, "POST", {
          scheduled_at: local.toISOString(),
          scheduled_time_label: label,
          duration_minutes: Number(autoInviteForm.duration_minutes),
          num_questions: Number(autoInviteForm.num_questions),
          focus: autoInviteForm.focus,
          max_warnings: Number(autoInviteForm.max_warnings),
        }, session.token);
      setNotice(`Auto-invited ${res.count} eligible candidate(s) to the AI `
        + `interview.${res.skipped.length ? ` Skipped ${res.skipped.length} `
        + `(missing email).` : ""}`);
      setShowAutoInvite(false);
      refreshJob();
    } catch (err) { setError(err.message); }
    setMeritBusy(false);
  };

  const runShortlist = async () => {
    if (!confirm(`Shortlist the top ${meritCfg.onsite_top_n} onsite-eligible `
      + `candidate(s)? They will be flagged and emailed a shortlist notice.`))
      return;
    setMeritBusy(true); setError(""); setNotice("");
    try {
      const res = await apiSend(
        `/api/admin/jobs/${selected.uuid}/merit/shortlist-onsite`, "POST",
        { notify: true }, session.token);
      setNotice(res.count
        ? `Shortlisted ${res.count} candidate(s) for onsite and emailed them.`
        : "No onsite-eligible candidates to shortlist yet.");
      refreshJob();
    } catch (err) { setError(err.message); }
    setMeritBusy(false);
  };

  // ---- AI interview (single candidate) --------------------------------------

  const scheduleAI = async (e) => {
    e.preventDefault();
    setError(""); setNotice("");
    try {
      const local = new Date(aiForm.scheduled_at);
      const timeLabel = local.toLocaleString([], {
        weekday: "long", year: "numeric", month: "long", day: "numeric",
        hour: "2-digit", minute: "2-digit" }) + " (Local Time)";
      const res = await apiSend("/api/admin/ai-interviews", "POST", {
        ...aiForm,
        candidate_uuid: showAIForm,
        job_uuid: selected.uuid,
        scheduled_at: local.toISOString(),
        duration_minutes: Number(aiForm.duration_minutes),
        num_questions: Number(aiForm.num_questions),
        max_warnings: Number(aiForm.max_warnings),
        scheduled_time_label: timeLabel,
      }, session.token);
      setNotice(res.sent
        ? "AI interview scheduled — unique link emailed to the candidate."
        : `AI interview scheduled. Email: ${res.message}. Link: ${res.link}`);
      setShowAIForm(null);
      refreshJob();
    } catch (err) { setError(err.message); }
  };

  const openAIDetail = (uuid) => {
    setAiDetail(null);
    apiGet(`/api/admin/ai-interviews/${uuid}`, session.token)
      .then((d) => {
        setAiDetail(d);
        setTimeout(() => document.getElementById("ai-detail")
          ?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
      })
      .catch((e) => setError(e.message));
  };

  const cancelAI = async (uuid) => {
    if (!confirm("Cancel this AI interview? The candidate will be emailed.")) return;
    setError(""); setNotice("");
    try {
      await apiSend(`/api/admin/ai-interviews/${uuid}`, "DELETE",
                    undefined, session.token);
      setNotice("AI interview cancelled.");
      refreshJob();
    } catch (err) { setError(err.message); }
  };

  // Live view: silently refresh the open job's candidates every 15s.
  useEffect(() => {
    if (!selected || !session) return;
    const iv = setInterval(() => {
      apiGet(`/api/admin/jobs/${selected.uuid}/candidates`, session.token)
        .then(setCandidates)
        .catch(() => {});
    }, 15000);
    return () => clearInterval(iv);
  }, [selected, session]);

  const liveNow = (c) =>
    c.test_status === "started" && c.last_seen
    && (Date.now() - new Date(c.last_seen + "Z").getTime()) < 45000;

  const openDetail = (c) => {
    if (!c.assignment_uuid) return;
    setDetail(null);
    setDetailLoading(true);
    apiGet(`/api/admin/assignments/${c.assignment_uuid}`, session.token)
      .then((d) => {
        setDetail(d);
        setTimeout(() => document.getElementById("result-detail")
          ?.scrollIntoView({ behavior: "smooth", block: "start" }), 80);
      })
      .catch((e) => setError(e.message))
      .finally(() => setDetailLoading(false));
  };

  const resetAssignment = async (c) => {
    const reason = prompt(
      `Give ${c.name} a new attempt?\n\n`
      + `• Their current attempt is KEPT as a past attempt — answers, score `
      + `and proctoring log are not deleted.\n`
      + `• A NEW link with a freshly drawn set of questions is emailed to `
      + `${c.email}.\n`
      + `• Their old link stops working.\n\n`
      + `Why are you resetting? (recorded against the voided attempt)`,
      "");
    if (reason === null) return;          // cancelled
    setError(""); setNotice("");
    try {
      const r = await apiSend(
        `/api/admin/assignments/${c.assignment_uuid}/reset`, "PUT",
        { expires_at: null, notify: true, reason }, session.token);
      const mailed = r.emailed === false
        ? ` — but the email failed to send (${r.email_message}); copy the `
          + `link from the row.`
        : ` and emailed to ${c.email}.`;
      setNotice(`${c.name}: attempt ${r.attempt_no} created with `
        + `${r.num_questions} freshly drawn question(s)${mailed} `
        + `Attempt ${r.previous_attempt.attempt_no} is kept as history.`);
      refreshJob();
    } catch (err) { setError(err.message); }
  };

  const openAttempts = (c) => {
    setError("");
    apiGet(`/api/admin/candidates/${c.uuid}/attempts`, session.token)
      .then(setAttempts)
      .catch((e) => setError(e.message));
  };

  const editExpiry = async (assignUuid) => {
    if (!newExpiry) return;
    setError(""); setNotice("");
    try {
      await apiSend(`/api/admin/assignments/${assignUuid}`, "PATCH",
        { expires_at: new Date(newExpiry).toISOString() }, session.token);
      setNotice("Expiry updated.");
      setEditingExpiry(null);
      refreshJob();
    } catch (err) { setError(err.message); }
  };

  const sendResults = async (assignUuid, candidateName) => {
    if (!assignUuid) return;
    if (!confirm(`Email detailed results (score + per-question breakdown) to `
      + `${candidateName}?`)) return;
    setError(""); setNotice("");
    try {
      await apiSend(`/api/admin/assignments/${assignUuid}/send-results`, "POST",
        undefined, session.token);
      setNotice(`Results emailed to ${candidateName}.`);
    } catch (err) { setError(err.message); }
  };

  const scheduleInterview = async (e) => {
    e.preventDefault();
    setError(""); setNotice("");
    try {
      const res = await apiSend("/api/admin/interviews", "POST", {
        ...interviewForm,
        candidate_uuid: showInterview,
        job_uuid: selected.uuid,
        scheduled_at: new Date(interviewForm.scheduled_at).toISOString(),
      }, session.token);
      setNotice(res.sent
        ? "Interview scheduled & invitation sent!"
        : `Interview scheduled. Email: ${res.message}`);
      setShowInterview(null);
      setInterviewForm({
        interview_type: "video", scheduled_at: "", duration_minutes: 30,
        location: "", notes: "",
      });
      refreshJob();
    } catch (err) { setError(err.message); }
  };

  const cancelInterview = async (id) => {
    if (!confirm("Cancel this interview? A cancellation email will be sent.")) return;
    setError(""); setNotice("");
    try {
      await apiSend(`/api/admin/interviews/${id}`, "DELETE", undefined, session.token);
      setNotice("Interview cancelled.");
      refreshJob();
    } catch (err) { setError(err.message); }
  };

  const logout = () => { clearAdminSession(); router.push("/admin"); };

  if (!session) return null;

  return (
    <main className="container wide">
      <div className="topbar">
        <div>
          <h1>Dashboard</h1>
          <p className="muted">
            {session.name} · {session.role === "super_admin"
              ? "Super admin — all departments"
              : `Department: ${session.department}`}
          </p>
        </div>
        <div className="links">
          <Link href="/admin/jobs/new"><button>+ Post a job</button></Link>
          <Link href="/admin/applications"><button className="secondary">Applications</button></Link>
          {session.role === "super_admin" && (
            <Link href="/admin/manage"><button className="secondary">Manage</button></Link>
          )}
          <button className="secondary" onClick={logout}>Log out</button>
        </div>
      </div>
      {error && <p className="error">{error}</p>}
      {notice && <p className="success">{notice}</p>}

      <div className="card">
        <h2>Jobs</h2>
        {jobs.length === 0 ? (
          <p className="muted">No jobs yet — run a screening in the recruiter
            app and save it to the database.</p>
        ) : (
          <div className="table-scroll">
            <table>
              <thead>
                <tr><th>Title</th><th>Department</th><th>Candidates</th>
                  <th>Tests submitted</th><th>Pass ≥</th><th>Actions</th></tr>
              </thead>
              <tbody>
                {jobs.map((j) => (
                  <tr key={j.uuid} className="clickable"
                      onClick={() => openJob(j)}
                      style={selected?.uuid === j.uuid
                        ? { background: "color-mix(in srgb, var(--accent) 8%, transparent)" }
                        : undefined}>
                    <td>{j.title}{" "}
                      {j.is_published
                        ? <span className="pill ok" style={{ marginLeft: 6 }}>Live</span>
                        : <span className="pill mute" style={{ marginLeft: 6 }}>Draft</span>}
                    </td>
                    <td>{j.department}</td>
                    <td>{j.candidates}</td>
                    <td>{j.tests_submitted}</td>
                    <td>{j.pass_threshold}</td>
                    <td onClick={(e) => e.stopPropagation()}>
                      <div className="actions">
                        <button className="mini secondary"
                                onClick={() => setEditingJob({
                                  uuid: j.uuid, title: j.title,
                                  pass_threshold: j.pass_threshold,
                                  jd_text: "",
                                  department_id: (departments.find(
                                    (d) => d.name === j.department) || {}).id || "",
                                  _loaded: false,
                                })}
                                title="Edit job">✏️ Edit</button>
                        <button className="mini danger"
                                onClick={() => deleteJob(j)}
                                title="Delete job and all its data">🗑 Delete</button>
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}

        {editingJob && (
          <JobEditForm job={editingJob} setJob={setEditingJob}
                       departments={departments} role={session.role}
                       token={session.token} onSubmit={saveJobEdit} />
        )}
      </div>

      {selected && !detail && !aiDetail && !attempts && !detailLoading && (
        <div>
          <div className="topbar" style={{ marginBottom: 12 }}>
            <h2 style={{ margin: 0 }}>{selected.title}</h2>
          </div>

          {merit && (
            <div className="funnel">
              <div className="step">
                <span className="n">{merit.counts.total}</span>
                <span className="lbl">candidate{merit.counts.total === 1 ? "" : "s"}</span>
              </div>
              <span className="arrow">→</span>
              <div className="step">
                <span className="n">{merit.counts.tested}</span>
                <span className="lbl">tested</span>
              </div>
              <span className="arrow">→</span>
              <div className="step">
                <span className="n">{merit.counts.interviewed}</span>
                <span className="lbl">interviewed{merit.counts.invite_eligible > 0
                  ? ` · ${merit.counts.invite_eligible} ready to invite` : ""}</span>
              </div>
              <span className="arrow">→</span>
              <div className="step hi">
                <span className="n">{merit.counts.onsite_eligible}</span>
                <span className="lbl">onsite-eligible</span>
              </div>
            </div>
          )}

          <div className="subnav">
            <button className={`tab ${activeTab === "candidates" ? "active" : ""}`}
                    onClick={() => setActiveTab("candidates")}>
              Candidates <span className="count">{candidates.length}</span>
            </button>
            <button className={`tab ${activeTab === "ai" ? "active" : ""}`}
                    onClick={() => setActiveTab("ai")}>
              AI interviews <span className="count">{aiInterviews.length}</span>
            </button>
            <button className={`tab ${activeTab === "human" ? "active" : ""}`}
                    onClick={() => setActiveTab("human")}>
              Human interviews <span className="count">{interviews.length}</span>
            </button>
            <button className={`tab ${activeTab === "setup" ? "active" : ""}`}
                    onClick={() => setActiveTab("setup")}>
              Ranking &amp; setup
            </button>
          </div>

          {/* ===== Ranking & setup tab ===== */}
          {activeTab === "setup" && (
          <>
          <p className="section-help">Set how résumé, test and AI-interview
            scores are weighted and where the cut-offs sit, manage this job&apos;s
            question bank, and edit the interview-invitation email. These settings
            apply to the whole job.</p>
          {meritCfg && (
            <div className="card" style={{ borderLeft: "4px solid var(--accent)",
                                           marginTop: 8 }}>
              <h3 style={{ marginBottom: 4 }}>Ranking &amp; shortlisting rules</h3>
              <p className="muted" style={{ marginBottom: 4 }}>
                Weight the résumé screening, test and AI interview, then set the
                cutoffs. Candidates who clear the <strong>AI-interview cutoff</strong>
                {" "}on résumé + test can be auto-invited to the AI interview;
                those who clear the <strong>onsite cutoff</strong> on all three
                are ranked for onsite selection.
              </p>
              <details style={{ margin: "6px 0 2px" }}>
                <summary className="muted" style={{ cursor: "pointer",
                                                    fontSize: 13 }}>
                  ℹ️ How the numbers are calculated
                </summary>
                <div className="muted" style={{ fontSize: 13, lineHeight: 1.7,
                                                padding: "8px 2px 2px" }}>
                  Every input is on a 0–100 scale: the résumé screening score,
                  the test percentage, and the AI interviewer&apos;s rating.
                  <br />
                  <strong>Screening merit</strong> = résumé ×{" "}
                  {meritCfg.resume_weight}% + test × {meritCfg.test_weight}%
                  (rebalanced over those two) → decides who is invited to the
                  AI interview (cutoff {meritCfg.invite_threshold}).
                  <br />
                  <strong>Final merit</strong> = résumé ×{" "}
                  {meritCfg.resume_weight}% + test × {meritCfg.test_weight}% +
                  interview × {meritCfg.interview_weight}% → decides onsite
                  eligibility (cutoff {meritCfg.onsite_threshold}), top{" "}
                  {meritCfg.onsite_top_n} shortlisted.
                  <br />
                  If a candidate hasn&apos;t completed a stage yet, the missing
                  weight is <strong>rebalanced proportionally</strong> across
                  the stages they did complete — the exact mix used is shown
                  under each score, so the math always adds up.
                </div>
              </details>

              <div className="merit-grid">
                {[
                  ["resume_weight", "Résumé weight %"],
                  ["test_weight", "Test weight %"],
                  ["interview_weight", "Interview weight %"],
                ].map(([k, lbl]) => (
                  <div key={k}>
                    <label>{lbl}</label>
                    <input type="number" min="0" max="100"
                           value={meritCfg[k]}
                           onChange={(e) => setMeritCfg({ ...meritCfg,
                             [k]: e.target.value })} />
                  </div>
                ))}
                <div>
                  <label>Min. score to invite (résumé + test)</label>
                  <input type="number" min="0" max="100"
                         value={meritCfg.invite_threshold}
                         onChange={(e) => setMeritCfg({ ...meritCfg,
                           invite_threshold: e.target.value })} />
                </div>
                <div>
                  <label>Min. score for onsite (all three)</label>
                  <input type="number" min="0" max="100"
                         value={meritCfg.onsite_threshold}
                         onChange={(e) => setMeritCfg({ ...meritCfg,
                           onsite_threshold: e.target.value })} />
                </div>
                <div>
                  <label>Top N for onsite</label>
                  <input type="number" min="1" max="100"
                         value={meritCfg.onsite_top_n}
                         onChange={(e) => setMeritCfg({ ...meritCfg,
                           onsite_top_n: e.target.value })} />
                </div>
              </div>

              <div style={{ marginTop: 12, paddingTop: 10,
                            borderTop: "1px solid var(--border)" }}>
                <label style={{ display: "flex", alignItems: "flex-start",
                                gap: 8, cursor: "pointer" }}>
                  <input type="checkbox" style={{ width: "auto", marginTop: 3 }}
                         checked={!!meritCfg.require_test_pass}
                         onChange={(e) => setMeritCfg({ ...meritCfg,
                           require_test_pass: e.target.checked })} />
                  <span>
                    <strong>Require a passing test before any AI interview</strong>
                    <div className="muted" style={{ fontSize: 13 }}>
                      Nobody can be sent to an AI interview until they have
                      actually sat the test and passed it. Turn this off only
                      for a job that does not use a test — otherwise nobody on
                      it can ever be interviewed.
                    </div>
                  </span>
                </label>

                <label style={{ display: "flex", alignItems: "flex-start",
                                gap: 8, cursor: "pointer", marginTop: 10 }}>
                  <input type="checkbox" style={{ width: "auto", marginTop: 3 }}
                         checked={!!meritCfg.auto_invite_on_pass}
                         onChange={(e) => setMeritCfg({ ...meritCfg,
                           auto_invite_on_pass: e.target.checked })} />
                  <span>
                    <strong>Auto-invite the moment a candidate passes</strong>
                    <div className="muted" style={{ fontSize: 13 }}>
                      When someone passes the test, the system schedules their
                      AI interview and emails them the link <strong>with no
                      human review</strong>. The email is drafted by the LLM.
                      Off by default.
                    </div>
                  </span>
                </label>

                {meritCfg.auto_invite_on_pass && (
                  <div className="grid" style={{ marginTop: 10 }}>
                    <div>
                      <label>Schedule it this many hours out</label>
                      <input type="number" min="0" max="8760"
                             value={meritCfg.auto_invite_delay_hours ?? 48}
                             onChange={(e) => setMeritCfg({ ...meritCfg,
                               auto_invite_delay_hours: e.target.value })} />
                    </div>
                    <div>
                      <label>Interview duration (min)</label>
                      <input type="number" min="1"
                             value={meritCfg.auto_invite_duration_minutes ?? 20}
                             onChange={(e) => setMeritCfg({ ...meritCfg,
                               auto_invite_duration_minutes: e.target.value })} />
                    </div>
                    <div>
                      <label>Questions to ask</label>
                      <input type="number" min="2" max="15"
                             value={meritCfg.auto_invite_num_questions ?? 5}
                             onChange={(e) => setMeritCfg({ ...meritCfg,
                               auto_invite_num_questions: e.target.value })} />
                    </div>
                  </div>
                )}
              </div>

              <div style={{ display: "flex", flexWrap: "wrap", gap: 10,
                            alignItems: "center", marginTop: 14 }}>
                <span className={`pill ${weightSum === 100 ? "ok" : "warn"}`}>
                  Weights sum: {weightSum}{weightSum === 100 ? " ✓" : " — must be 100"}
                </span>
                <button className="mini" disabled={meritBusy || weightSum !== 100}
                        onClick={saveMeritConfig}>Save weights & cutoffs</button>
              </div>

              {/* Pipeline funnel at a glance */}
              {merit && (
                <div style={{ display: "flex", flexWrap: "wrap", gap: 6,
                              alignItems: "center", marginTop: 12 }}>
                  <span className="pill mute">
                    👥 {merit.counts.total} candidate{merit.counts.total === 1 ? "" : "s"}
                  </span>
                  <span className="muted">→</span>
                  <span className="pill mute">
                    📝 {merit.counts.tested} tested
                  </span>
                  <span className="muted">→</span>
                  <span className="pill info">
                    🤖 {merit.counts.interviewed} interviewed
                    {merit.counts.invite_eligible > 0 &&
                      ` · ${merit.counts.invite_eligible} ready to invite`}
                  </span>
                  <span className="muted">→</span>
                  <span className="pill ok">
                    🏆 {merit.counts.onsite_eligible} onsite-eligible
                  </span>
                </div>
              )}

              {/* Bulk actions */}
              <div style={{ display: "flex", flexWrap: "wrap", gap: 8,
                            marginTop: 14 }}>
                <button className="mini"
                        disabled={meritBusy || !merit?.counts.invite_eligible}
                        onClick={() => setShowAutoInvite((v) => !v)}>
                  🤖 Auto-invite eligible to AI interview
                  {merit ? ` (${merit.counts.invite_eligible})` : ""}
                </button>
                <button className="mini secondary"
                        disabled={meritBusy || !merit?.counts.onsite_eligible}
                        onClick={runShortlist}>
                  🏆 Shortlist top {meritCfg.onsite_top_n} for onsite
                </button>
              </div>

              {showAutoInvite && (
                <form onSubmit={runAutoInvite} className="card"
                      style={{ marginTop: 12, background: "var(--bg)" }}>
                  <p className="muted" style={{ marginBottom: 0 }}>
                    Every eligible candidate gets a unique AI-interview link
                    (their own room) for this slot.
                  </p>
                  <div className="merit-grid">
                    <div>
                      <label>Date &amp; time</label>
                      <input type="datetime-local" required
                             value={autoInviteForm.scheduled_at}
                             onChange={(e) => setAutoInviteForm({
                               ...autoInviteForm, scheduled_at: e.target.value })} />
                    </div>
                    <div>
                      <label>Duration (min)</label>
                      <input type="number" min="1" max="90"
                             value={autoInviteForm.duration_minutes}
                             onChange={(e) => setAutoInviteForm({
                               ...autoInviteForm, duration_minutes: e.target.value })} />
                    </div>
                    <div>
                      <label>Main questions</label>
                      <input type="number" min="2" max="15"
                             value={autoInviteForm.num_questions}
                             onChange={(e) => setAutoInviteForm({
                               ...autoInviteForm, num_questions: e.target.value })} />
                    </div>
                    <div>
                      <label>Warnings allowed</label>
                      <input type="number" min="1" max="10"
                             value={autoInviteForm.max_warnings}
                             onChange={(e) => setAutoInviteForm({
                               ...autoInviteForm, max_warnings: e.target.value })} />
                    </div>
                  </div>
                  <label>Focus areas (optional)</label>
                  <input value={autoInviteForm.focus}
                         placeholder="e.g. FastAPI, SQL depth, teamwork"
                         onChange={(e) => setAutoInviteForm({
                           ...autoInviteForm, focus: e.target.value })} />
                  <button disabled={meritBusy}>Schedule &amp; email eligible</button>
                  <button type="button" className="secondary"
                          style={{ marginLeft: 8 }}
                          onClick={() => setShowAutoInvite(false)}>Cancel</button>
                </form>
              )}

              {/* Merit ranking */}
              {merit && merit.candidates.length > 0 && (
                <div className="table-scroll" style={{ marginTop: 14 }}>
                  <table>
                    <thead>
                      <tr><th>#</th><th>Candidate</th><th>Résumé</th><th>Test</th>
                        <th>Interview</th><th style={{ minWidth: 130 }}>Screening score</th>
                        <th style={{ minWidth: 130 }}>Overall score</th>
                        <th>Recommendation</th><th></th></tr>
                    </thead>
                    <tbody>
                      {merit.candidates.map((r) => {
                        const meta = STAGE_META[r.stage]
                          || { label: r.stage, cls: "mute" };
                        return (
                          <tr key={r.candidate_uuid}>
                            <td>{r.rank}</td>
                            <td>
                              {r.name || "—"}
                              {r.onsite_top_n && (
                                <span className="pill ok"
                                      style={{ marginLeft: 6 }}>🏆 top {meritCfg.onsite_top_n}</span>
                              )}
                            </td>
                            <td>{r.resume_score}</td>
                            <td>{r.test_score ?? "—"}</td>
                            <td>{r.interview_score ?? (
                              r.interview_status
                                ? <span className="muted">{r.interview_status}</span>
                                : "—")}</td>
                            <td><MeritBar value={r.screening_merit} /></td>
                            <td>
                              <MeritBar value={r.final_merit} />
                              {r.final_merit != null
                                && r.missing_inputs?.length > 0 && (
                                <div className="muted"
                                     style={{ fontSize: 11, marginTop: 2,
                                              whiteSpace: "nowrap" }}
                                     title={"This stage is missing, so its "
                                       + "weight was rebalanced across the "
                                       + "completed stages."}>
                                  {(r.final_parts || [])
                                    .map((p) => `${MIX_LABELS[p.name] || p.name} ${p.weight_pct}%`)
                                    .join(" + ")}
                                  {" · no "}
                                  {r.missing_inputs
                                    .map((n) => MIX_LABELS[n] || n)
                                    .join(", no ")}
                                  {" yet"}
                                </div>
                              )}
                            </td>
                            <td><span className={`pill ${meta.cls}`}>{meta.label}</span></td>
                            <td onClick={(e) => e.stopPropagation()}>
                              {r.stage === "invite_to_interview" && (
                                <button className="mini"
                                        onClick={() => { setShowAIForm(r.candidate_uuid);
                                          setActiveTab("ai"); }}>
                                  🤖 Invite
                                </button>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </div>
              )}
            </div>
          )}

          <QuestionBank job={selected} token={session.token}
                        onError={setError} onNotice={setNotice} />
          <EmailTemplateEditor job={selected} token={session.token}
                               onError={setError} onNotice={setNotice} />
          </>
          )}

          {/* ===== Candidates tab ===== */}
          {activeTab === "candidates" && (
          <div className="card">
          {candidates.length === 0 ? (
            <p className="muted">Loading candidates…</p>
          ) : (
            <>
              <p className="section-help" style={{ marginTop: 0 }}>
                Click a candidate with a completed test to view their answers,
                score and proctoring log. Use the row actions to reset links,
                edit expiry, email results, or schedule interviews.
              </p>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr><th>Candidate</th><th>Résumé</th><th>Test</th>
                      <th>Time</th><th>Expires</th>
                      <th>Interview</th><th>Actions</th></tr>
                  </thead>
                  <tbody>
                    {candidates.map((c) => {
                      const done = c.test_status === "submitted"
                        || c.test_status === "terminated";
                      return (
                        <tr key={c.uuid} className={done ? "clickable" : ""}
                            onClick={() => done && openDetail(c)}
                            style={{ cursor: done ? "pointer" : "default" }}
                            title={done
                              ? "Click to view answers, score and proctoring log"
                              : "No completed attempt yet"}>
                          <td>
                            <div style={{ fontWeight: 600 }}>{c.name || "—"}</div>
                            {c.email && (
                              <div className="muted" style={{ fontSize: 12,
                                   maxWidth: 220, overflow: "hidden",
                                   textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                                {c.email}</div>
                            )}
                          </td>
                          <td style={{ whiteSpace: "nowrap" }}>
                            {c.resume_score}{" "}
                            <span className={`badge ${c.passed_screening ? "ok" : "bad"}`}>
                              {c.passed_screening ? "PASS" : "FAIL"}
                            </span>
                          </td>
                          <td>
                            {c.test_status === "terminated" ? (
                              <span className="badge bad">⛔ terminated</span>
                            ) : c.test_status === "started" ? (
                              <>
                                {liveNow(c)
                                  ? <span className="badge ok">● live</span>
                                  : "in progress"}
                                {c.answered_so_far != null &&
                                  <span className="muted"> {c.answered_so_far} answered</span>}
                              </>
                            ) : done ? (
                              <>
                                {c.test_score != null && (
                                  <strong style={{ fontVariantNumeric: "tabular-nums" }}>
                                    {c.test_score}%</strong>)}{" "}
                                {c.test_passed != null && (
                                  <span className={`badge ${c.test_passed ? "ok" : "bad"}`}>
                                    {c.test_passed ? "PASS" : "FAIL"}</span>)}
                                {c.test_pass_score != null && (
                                  <span className="muted" style={{ fontSize: "0.82em" }}>
                                    {" "}(pass ≥{c.test_pass_score}%)</span>)}
                              </>
                            ) : (c.test_status || "not invited")}
                            {c.proctor_warnings > 0 &&
                              <span className="muted"> ⚠{c.proctor_warnings}
                                {c.max_warnings ? `/${c.max_warnings}` : ""}</span>}
                            {c.pending_retake_uuid && (
                              <span className="pill info" style={{ marginLeft: 6 }}>
                                retake link active</span>
                            )}
                          </td>
                          <td style={{ whiteSpace: "nowrap" }}>{fmtTime(c.time_taken_seconds)}</td>
                          <td style={{ fontSize: "0.82em", whiteSpace: "nowrap" }}>
                            {c.expires_at
                              ? localDate(c.expires_at).toLocaleDateString()
                              : "—"}
                          </td>
                          <td>
                            {c.interview_status ? (
                              <span className={`badge ${c.interview_status === "scheduled" ? "ok" : ""}`}>
                                {c.interview_status}
                              </span>
                            ) : "—"}
                          </td>
                          <td onClick={(e) => e.stopPropagation()}>
                            <div className="actions">
                              {done && (
                                <button className="mini secondary"
                                        onClick={() => openDetail(c)}
                                        title="View answers, score & proctoring log">
                                  👁 View</button>
                              )}
                              {done && (
                                <button className="mini secondary"
                                        onClick={() => sendResults(c.assignment_uuid, c.name)}
                                        title="Email results to this candidate">
                                  📧 Email</button>
                              )}
                              {c.assignment_uuid && (
                                <>
                                  <button className="mini secondary"
                                          onClick={() => resetAssignment(c)}
                                          title="New attempt: keeps this one as history and emails a new link with different questions">
                                    🔄 Reset</button>
                                  {c.total_attempts > 1 && (
                                    <button className="mini secondary"
                                            onClick={() => openAttempts(c)}
                                            title="View every past attempt">
                                      🗂 {c.total_attempts} attempts</button>
                                  )}
                                  <button className="mini secondary"
                                          onClick={() => { setEditingExpiry(c.assignment_uuid);
                                            setNewExpiry(""); }}
                                          title="Edit link expiry">
                                    📅 Expiry</button>
                                </>
                              )}
                              <span className="sep" />
                              <button className="mini secondary"
                                      onClick={() => { setShowInterview(c.uuid);
                                        setActiveTab("human"); }}
                                      title="Schedule a human interview">
                                🗓️ Human</button>
                              <button className="mini secondary"
                                      onClick={() => { setShowAIForm(c.uuid);
                                        setActiveTab("ai"); }}
                                      title="Schedule an AI voice interview">
                                🤖 AI</button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </>
          )}

          {/* Edit expiry inline */}
          {editingExpiry && (
            <div className="card" style={{ marginTop: 12, borderLeft: "4px solid var(--accent)" }}>
              <h3>Edit link expiry</h3>
              <div className="row">
                <div>
                  <label>New expiry date & time</label>
                  <input type="datetime-local" value={newExpiry}
                         onChange={(e) => setNewExpiry(e.target.value)} />
                </div>
                <div style={{ alignSelf: "end", flex: "0 0 auto" }}>
                  <button onClick={() => editExpiry(editingExpiry)}
                          disabled={!newExpiry}>Save</button>
                  <button className="secondary" style={{ marginLeft: 8 }}
                          onClick={() => setEditingExpiry(null)}>Cancel</button>
                </div>
              </div>
            </div>
          )}
          </div>
          )}

          {/* ===== AI interviews tab ===== */}
          {activeTab === "ai" && (
          <div className="card">
          <p className="section-help" style={{ marginTop: 0 }}>
            Schedule AI voice interviews and review completed ones. To schedule
            one, open the <strong>Candidates</strong> tab and click
            {" "}<strong>AI</strong> on a candidate.
          </p>
          {showAIForm && (
            <div className="card" style={{ marginTop: 12, borderLeft: "4px solid var(--accent)" }}>
              <h3>Schedule AI voice interview</h3>
              <p className="muted">The candidate gets a unique link that only
                opens around the scheduled time. An AI interviewer asks
                questions by voice under full proctoring, then scores the
                transcript.</p>
              <form onSubmit={scheduleAI}>
                <div className="merit-grid">
                  <div>
                    <label>Date &amp; time</label>
                    <input type="datetime-local" required
                           value={aiForm.scheduled_at}
                           onChange={(e) => setAiForm({ ...aiForm, scheduled_at: e.target.value })} />
                  </div>
                  <div>
                    <label>Duration (min)</label>
                    <input type="number" min="1" max="90"
                           value={aiForm.duration_minutes}
                           onChange={(e) => setAiForm({ ...aiForm, duration_minutes: e.target.value })} />
                  </div>
                  <div>
                    <label>Main questions</label>
                    <input type="number" min="2" max="15"
                           value={aiForm.num_questions}
                           onChange={(e) => setAiForm({ ...aiForm, num_questions: e.target.value })} />
                  </div>
                  <div>
                    <label>Warnings allowed</label>
                    <input type="number" min="1" max="10"
                           value={aiForm.max_warnings}
                           onChange={(e) => setAiForm({ ...aiForm, max_warnings: e.target.value })} />
                  </div>
                </div>
                <label>Focus areas (optional — guides the questions)</label>
                <input value={aiForm.focus}
                       placeholder="e.g. FastAPI experience, SQL depth, teamwork"
                       onChange={(e) => setAiForm({ ...aiForm, focus: e.target.value })} />
                <button>Schedule &amp; send invitation</button>
                <button type="button" className="secondary" style={{ marginLeft: 8 }}
                        onClick={() => setShowAIForm(null)}>Cancel</button>
              </form>
            </div>
          )}

          {/* AI interviews list */}
          {aiInterviews.length === 0 ? (
            <p className="muted" style={{ marginTop: 12 }}>
              No AI interviews scheduled yet.</p>
          ) : (
            <div className="card" style={{ marginTop: 12 }}>
              <h3>Completed &amp; scheduled AI interviews</h3>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr><th>Candidate</th><th>Scheduled</th><th>Status</th>
                      <th>AI score</th><th>Questions</th><th>⚠</th><th></th></tr>
                  </thead>
                  <tbody>
                    {aiInterviews.map((iv) => {
                      const reviewable = ["completed", "terminated"].includes(iv.status);
                      return (
                        <tr key={iv.uuid} className={reviewable ? "clickable" : ""}
                            onClick={() => reviewable && openAIDetail(iv.uuid)}>
                          <td>{iv.candidate_name}</td>
                          <td>{localDate(iv.scheduled_at).toLocaleString()}</td>
                          <td>
                            <span className={`badge ${
                              iv.status === "completed" ? "ok"
                              : ["terminated", "missed"].includes(iv.status) ? "bad" : ""}`}>
                              {iv.status}
                            </span>
                          </td>
                          <td>{iv.ai_score != null ? `${iv.ai_score}/100` : "—"}</td>
                          <td>{iv.questions_asked}</td>
                          <td>{iv.proctor_warnings || ""}</td>
                          <td onClick={(e) => e.stopPropagation()}>
                            {reviewable ? (
                              <button className="mini secondary"
                                      onClick={() => openAIDetail(iv.uuid)}>👁 View</button>
                            ) : iv.status === "started" ? (
                              <button className="mini"
                                      onClick={() => window.open(`/admin/interview-room/${iv.uuid}`, "_blank")}>🎙 Join</button>
                            ) : ["scheduled"].includes(iv.status) && (
                              <button className="mini danger"
                                      onClick={() => cancelAI(iv.uuid)}>✕ Cancel</button>
                            )}
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          </div>
          )}

          {/* ===== Human interviews tab ===== */}
          {activeTab === "human" && (
          <div className="card">
          <p className="section-help" style={{ marginTop: 0 }}>
            Schedule phone, video or on-site interviews with a person and track
            them. To schedule one, open the <strong>Candidates</strong> tab and
            click <strong>Human</strong> on a candidate.
          </p>
          {showInterview && (
            <div className="card" style={{ marginTop: 12, borderLeft: "4px solid var(--ok)" }}>
              <h3>Schedule human interview</h3>
              <form onSubmit={scheduleInterview}>
                <div className="row">
                  <div>
                    <label>Type</label>
                    <select value={interviewForm.interview_type}
                            onChange={(e) => setInterviewForm({ ...interviewForm, interview_type: e.target.value })}>
                      <option value="video">Video</option>
                      <option value="phone">Phone</option>
                      <option value="in_person">In-Person</option>
                    </select>
                  </div>
                  <div>
                    <label>Date & Time</label>
                    <input type="datetime-local" required
                           value={interviewForm.scheduled_at}
                           onChange={(e) => setInterviewForm({ ...interviewForm, scheduled_at: e.target.value })} />
                  </div>
                  <div>
                    <label>Duration (minutes)</label>
                    <input type="number" min="5" max="480"
                           value={interviewForm.duration_minutes}
                           onChange={(e) => setInterviewForm({ ...interviewForm, duration_minutes: Number(e.target.value) })} />
                  </div>
                </div>
                <div className="row">
                  <div>
                    <label>{interviewForm.interview_type === "video" ? "Video link" : "Location"}</label>
                    <input value={interviewForm.location}
                           placeholder={interviewForm.interview_type === "video" ? "https://meet.google.com/..." : "Office address"}
                           onChange={(e) => setInterviewForm({ ...interviewForm, location: e.target.value })} />
                  </div>
                  <div>
                    <label>Notes (optional)</label>
                    <input value={interviewForm.notes}
                           placeholder="Bring ID, dress code, etc."
                           onChange={(e) => setInterviewForm({ ...interviewForm, notes: e.target.value })} />
                  </div>
                </div>
                <button type="submit">📨 Send Interview Invitation</button>
                <button type="button" className="secondary" style={{ marginLeft: 8 }}
                        onClick={() => setShowInterview(null)}>Cancel</button>
              </form>
            </div>
          )}

          {/* Interviews list */}
          {interviews.length === 0 ? (
            <p className="muted" style={{ marginTop: 16 }}>
              No human interviews scheduled yet.</p>
          ) : (
            <div style={{ marginTop: 16 }}>
              <h3>Scheduled human interviews</h3>
              <div className="table-scroll">
                <table>
                  <thead>
                    <tr><th>Candidate</th><th>Type</th><th>Date</th>
                      <th>Duration</th><th>Location</th><th>Status</th><th></th></tr>
                  </thead>
                  <tbody>
                    {interviews.map((iv) => (
                      <tr key={iv.id}>
                        <td>{iv.candidate_name}</td>
                        <td>{TYPE_LABELS[iv.interview_type] || iv.interview_type}</td>
                        <td>{localDate(iv.scheduled_at).toLocaleString()}</td>
                        <td>{iv.duration_minutes}m</td>
                        <td style={{ maxWidth: 200, overflow: "hidden", textOverflow: "ellipsis" }}>
                          {iv.location || "—"}</td>
                        <td>
                          <span className={`badge ${iv.status === "scheduled" ? "ok" : iv.status === "cancelled" ? "bad" : ""}`}>
                            {iv.status}
                          </span>
                        </td>
                        <td>
                          {iv.status === "scheduled" && (
                            <button className="mini danger"
                                    onClick={() => cancelInterview(iv.id)}>Cancel</button>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </div>
          )}
          </div>
          )}
        </div>
      )}

      {detailLoading && (
        <div className="card"><p className="muted">Loading result…</p></div>
      )}

      {attempts && (
        <div className="card" id="attempt-history">
          <div className="topbar">
            <div>
              <h2>{attempts.candidate_name} — attempt history</h2>
              <p className="muted">
                {attempts.job_title} · {attempts.attempts.length} attempt(s).
                Nothing here is ever deleted by a reset.
              </p>
            </div>
            <button className="secondary" style={{ marginTop: 0 }}
                    onClick={() => setAttempts(null)}>← Back</button>
          </div>
          <div className="table-scroll">
            <table>
              <thead>
                <tr>
                  <th>#</th><th>STATUS</th><th>SCORE</th><th>QUESTIONS</th>
                  <th>TIME</th><th>WARNINGS</th><th>WHEN</th><th></th>
                </tr>
              </thead>
              <tbody>
                {attempts.attempts.map((a) => (
                  <tr key={a.assignment_uuid}
                      style={a.superseded ? { opacity: 0.65 } : undefined}>
                    <td>{a.attempt_no}</td>
                    <td>
                      {a.status}
                      {a.superseded && (
                        <span className="muted" style={{ fontSize: 12 }}>
                          {" "}· replaced
                        </span>
                      )}
                      {a.terminated_reason && (
                        <div className="muted" style={{ fontSize: 12 }}>
                          {a.terminated_reason}
                        </div>
                      )}
                      {a.superseded && (a.superseded_by || a.reset_reason) && (
                        <div className="muted" style={{ fontSize: 12 }}>
                          reset{a.superseded_by ? ` by ${a.superseded_by}` : ""}
                          {a.reset_reason ? `: “${a.reset_reason}”` : ""}
                        </div>
                      )}
                    </td>
                    <td>
                      {a.test_score === null ? "—" : (
                        <>
                          {a.test_score}%{" "}
                          {a.passed === true && <span title="Passed">✅</span>}
                          {a.passed === false && <span title="Failed">❌</span>}
                        </>
                      )}
                    </td>
                    <td>{a.num_questions} · {a.difficulty}</td>
                    <td>{fmtTime(a.time_taken_seconds)}</td>
                    <td>{a.proctor_warnings || 0}</td>
                    <td className="muted" style={{ fontSize: 12 }}>
                      {localDate(a.submitted_at || a.started_at
                                 || a.created_at).toLocaleString()}
                    </td>
                    <td>
                      {a.status !== "pending" && (
                        <button className="mini secondary"
                                onClick={() => { setAttempts(null);
                                                 openDetail({ assignment_uuid:
                                                   a.assignment_uuid }); }}
                                title="See the exact paper this attempt sat">
                          🔍 View
                        </button>
                      )}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {detail && (
        <div className="card" id="result-detail">
          <div className="topbar">
            <div>
              <h2>{detail.candidate_name} — result</h2>
              <p className="muted">{detail.candidate_email} · {detail.job_title}</p>
            </div>
            <div>
              <button className="secondary" style={{ marginTop: 0, marginRight: 8 }}
                      onClick={() => sendResults(detail.assignment_uuid, detail.candidate_name)}>
                📧 Email results
              </button>
              <button className="secondary" style={{ marginTop: 0 }}
                      onClick={() => setDetail(null)}>← Back</button>
            </div>
          </div>

          <div className="row" style={{ marginBottom: 16, flexWrap: "wrap" }}>
            <div><label>Test score</label>
              <strong>{detail.test_score ?? "—"} / 100</strong>
              <span className="muted"> (pass ≥ {detail.pass_score})</span></div>
            <div><label>Result</label>{" "}
              <span className={`badge ${detail.passed ? "ok" : "bad"}`}>
                {detail.passed ? "PASS" : "FAIL"}
              </span></div>
            <div><label>Correct</label>
              <strong>{detail.correct} / {detail.total}</strong></div>
            <div><label>Time taken</label>
              <strong>{fmtTime(detail.time_taken_seconds)}</strong>
              <span className="muted"> / {detail.duration_minutes} min allowed</span>
            </div>
            <div><label>Résumé score</label>
              <strong>{detail.resume_score}</strong></div>
          </div>

          {detail.status === "terminated" && (
            <div className="card" style={{ borderLeft: "4px solid var(--danger)" }}>
              <strong style={{ color: "var(--danger)" }}>
                ⛔ Test terminated by proctoring
              </strong>
              <p className="muted">{detail.terminated_reason}</p>
            </div>
          )}

          {detail.proctored && (() => {
            const events = detail.proctor_events || [];
            const periodic = events.filter(
              (e) => e.event_type === "periodic_snapshot" && e.evidence);
            const timeline = events.filter(
              (e) => e.event_type !== "periodic_snapshot");
            return (
              <div className="card">
                <h3>🎥 Proctoring log — {detail.proctor_warnings}
                  {detail.max_warnings ? `/${detail.max_warnings}` : ""} warning(s)</h3>
                {timeline.length === 0 ? (
                  <p className="muted">No violations recorded. ✓</p>
                ) : (
                  timeline.map((e, i) => (
                    <div key={i} className="row" style={{
                      alignItems: "flex-start", padding: "8px 0",
                      borderBottom: "1px solid var(--border)" }}>
                      <div style={{ flex: 2 }}>
                        <span className={`badge ${e.is_warning ? "bad" : ""}`}>
                          {e.event_type.replace(/_/g, " ")}
                        </span>
                        <div className="muted" style={{ fontSize: 13 }}>
                          {e.detail} · {localDate(e.at).toLocaleTimeString()}
                        </div>
                      </div>
                      {e.evidence && (
                        <img src={e.evidence} alt={`evidence ${e.event_type}`}
                             style={{ width: 160, borderRadius: 8,
                                      border: "1px solid var(--border)" }} />
                      )}
                    </div>
                  ))
                )}
                {periodic.length > 0 && (
                  <>
                    <h3 style={{ marginTop: 14 }}>
                      📸 Session snapshots ({periodic.length})</h3>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                      {periodic.map((e, i) => (
                        <figure key={i} style={{ margin: 0 }}>
                          <img src={e.evidence} alt={`snapshot ${i + 1}`}
                               style={{ width: 110, borderRadius: 6,
                                        border: "1px solid var(--border)" }} />
                          <figcaption className="muted"
                                      style={{ fontSize: 11, textAlign: "center" }}>
                            {localDate(e.at).toLocaleTimeString()}
                          </figcaption>
                        </figure>
                      ))}
                    </div>
                  </>
                )}
              </div>
            );
          })()}

          {detail.questions.map((q, i) => (
            <div className="card" key={i}
                 style={{ borderLeft: `4px solid var(--${q.is_correct ? "ok" : "danger"})` }}>
              <p className="q">
                <span className={`badge ${q.is_correct ? "ok" : "bad"}`}>
                  {q.is_correct ? "✓ correct" : q.answered ? "✗ wrong" : "✗ blank"}
                </span>{" "}
                Q{i + 1}. {q.question}
              </p>
              {q.options.map((opt, oi) => {
                const isCorrect = oi === q.correct_index;
                const isChosen = oi === q.selected_index;
                let mark = "";
                if (isCorrect) mark = "✓ correct answer";
                else if (isChosen) mark = "← their answer";
                return (
                  <div key={oi} className={`opt ${isChosen ? "selected" : ""}`}
                       style={{ cursor: "default",
                                borderColor: isCorrect ? "var(--ok)" : undefined }}>
                    <span>{"ABCD"[oi]}) {opt}</span>
                    {mark && <span className="muted"
                                   style={{ marginLeft: "auto" }}>{mark}</span>}
                  </div>
                );
              })}
            </div>
          ))}
        </div>
      )}

      {aiDetail && (
        <div className="card" id="ai-detail">
          <div className="topbar">
            <div>
              <h2>🤖 {aiDetail.candidate_name} — AI interview</h2>
              <p className="muted">{aiDetail.candidate_email} ·
                {" "}{aiDetail.job_title} ·
                {" "}{localDate(aiDetail.scheduled_at).toLocaleString()}</p>
            </div>
            <button className="secondary" style={{ marginTop: 0 }}
                    onClick={() => setAiDetail(null)}>← Back</button>
          </div>

          <div className="row" style={{ marginBottom: 16, flexWrap: "wrap" }}>
            <div><label>AI score</label>
              <strong>{aiDetail.ai_score != null
                ? `${aiDetail.ai_score} / 100` : "—"}</strong></div>
            <div><label>Status</label>{" "}
              <span className={`badge ${
                aiDetail.status === "completed" ? "ok" : "bad"}`}>
                {aiDetail.status}</span></div>
            <div><label>Questions</label>
              <strong>{aiDetail.questions_asked} / {aiDetail.num_questions}
              </strong></div>
            <div><label>Warnings</label>
              <strong>{aiDetail.proctor_warnings} / {aiDetail.max_warnings}
              </strong></div>
          </div>

          {aiDetail.terminated_reason && (
            <div className="card" style={{ borderLeft: "4px solid var(--danger)" }}>
              <strong style={{ color: "var(--danger)" }}>
                ⛔ Terminated by proctoring</strong>
              <p className="muted">{aiDetail.terminated_reason}</p>
            </div>
          )}

          {aiDetail.ai_summary && (
            <div className="card">
              <h3>🧠 AI evaluation</h3>
              <p>{aiDetail.ai_summary.summary}</p>
              {aiDetail.ai_summary.strengths?.length > 0 && (
                <p><strong style={{ color: "var(--ok)" }}>Strengths:</strong>{" "}
                  {aiDetail.ai_summary.strengths.join(" · ")}</p>
              )}
              {aiDetail.ai_summary.concerns?.length > 0 && (
                <p><strong style={{ color: "var(--danger)" }}>Concerns:</strong>{" "}
                  {aiDetail.ai_summary.concerns.join(" · ")}</p>
              )}
              {aiDetail.ai_summary.per_question?.length > 0 && (
                <div style={{ marginTop: 8 }}>
                  {aiDetail.ai_summary.per_question.map((pq, i) => (
                    <p key={i} className="muted" style={{ fontSize: 13 }}>
                      <strong>Q:</strong> {pq.question} —{" "}
                      <em>{pq.assessment}</em></p>
                  ))}
                </div>
              )}
            </div>
          )}

          <div className="card">
            <h3>📜 Transcript</h3>
            {(aiDetail.transcript || []).map((m, i) => (
              <p key={i} style={{ margin: "8px 0" }}>
                <strong>{m.role === "interviewer" ? "🎙 AI" : "🧑 Candidate"}:
                </strong>{" "}{m.text}
              </p>
            ))}
            {(!aiDetail.transcript || aiDetail.transcript.length === 0) && (
              <p className="muted">No transcript recorded.</p>
            )}
          </div>

          {(() => {
            const events = aiDetail.events || [];
            const periodic = events.filter(
              (e) => e.event_type === "periodic_snapshot" && e.evidence);
            const timeline = events.filter(
              (e) => e.event_type !== "periodic_snapshot");
            return (
              <div className="card">
                <h3>🎥 Proctoring log</h3>
                {timeline.length === 0 ? (
                  <p className="muted">No violations recorded. ✓</p>
                ) : timeline.map((e, i) => (
                  <div key={i} className="row" style={{
                    alignItems: "flex-start", padding: "8px 0",
                    borderBottom: "1px solid var(--border)" }}>
                    <div style={{ flex: 2 }}>
                      <span className={`badge ${e.is_warning ? "bad" : ""}`}>
                        {e.event_type.replace(/_/g, " ")}</span>
                      <div className="muted" style={{ fontSize: 13 }}>
                        {e.detail} · {localDate(e.at).toLocaleTimeString()}
                      </div>
                    </div>
                    {e.evidence && (
                      <img src={e.evidence} alt="evidence"
                           style={{ width: 160, borderRadius: 8,
                                    border: "1px solid var(--border)" }} />
                    )}
                  </div>
                ))}
                {periodic.length > 0 && (
                  <>
                    <h3 style={{ marginTop: 14 }}>
                      📸 Session snapshots ({periodic.length})</h3>
                    <div style={{ display: "flex", flexWrap: "wrap", gap: 8 }}>
                      {periodic.map((e, i) => (
                        <figure key={i} style={{ margin: 0 }}>
                          <img src={e.evidence} alt={`snap ${i + 1}`}
                               style={{ width: 110, borderRadius: 6,
                                        border: "1px solid var(--border)" }} />
                          <figcaption className="muted"
                                      style={{ fontSize: 11, textAlign: "center" }}>
                            {localDate(e.at).toLocaleTimeString()}
                          </figcaption>
                        </figure>
                      ))}
                    </div>
                  </>
                )}
              </div>
            );
          })()}
        </div>
      )}
    </main>
  );
}
