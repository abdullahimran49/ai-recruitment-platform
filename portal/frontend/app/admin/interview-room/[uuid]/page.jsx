"use client";

import { use, useEffect, useState } from "react";
import { apiGet, adminSession } from "@/lib/api";

export default function InterviewMonitorPage({ params }) {
  const { uuid } = use(params);
  const [info, setInfo] = useState(null);
  const [error, setError] = useState("");

  useEffect(() => {
    const session = adminSession();
    if (!session) {
      setError("Admin login required.");
      return undefined;
    }
    let active = true;
    const refresh = async () => {
      try {
        const data = await apiGet(`/api/admin/ai-interviews/${uuid}`,
                                  session.token);
        if (active) { setInfo(data); setError(""); }
      } catch (err) {
        if (active) setError(err.message || "Could not load interview status.");
      }
    };
    refresh();
    const timer = setInterval(refresh, 3000);
    return () => { active = false; clearInterval(timer); };
  }, [uuid]);

  if (error && !info) {
    return <main className="container narrow"><div className="card">
      <h1>Interview monitor</h1><p className="error">{error}</p>
      <a href="/admin"><button>Back to admin</button></a>
    </div></main>;
  }
  if (!info) return <main className="container"><div className="card">
    Loading interview status…
  </div></main>;

  const active = info.status === "started";
  return <main className="container">
    <div className="card">
      <div className="row" style={{ justifyContent: "space-between",
                                     alignItems: "center" }}>
        <div><h1 style={{ marginBottom: 4 }}>Interview monitor</h1>
          <p className="muted" style={{ margin: 0 }}>
            <strong>{info.candidate_name}</strong> · {info.job_title}
          </p></div>
        <span className={`badge ${active ? "ok" : ""}`}>{info.status}</span>
      </div>
      <p className="muted">Read-only live status · refreshes every 3 seconds</p>
      <div className="stat-grid">
        <div><strong>{info.questions_asked || 0}</strong><span>Questions</span></div>
        <div><strong>{info.proctor_warnings || 0}</strong><span>Warnings</span></div>
        <div><strong>{info.ai_score ?? "—"}</strong><span>AI score</span></div>
      </div>
    </div>

    <div className="card">
      <h2>Live transcript</h2>
      {(info.transcript || []).length ? (
        <div className="chat-log" aria-live="polite">
          {info.transcript.map((item, index) => <div key={index}
            className={`chat-msg ${item.role === "candidate" ? "candidate" : "interviewer"}`}>
            <strong>{item.role === "candidate" ? info.candidate_name : "Nova"}</strong>
            <p>{item.text}</p>
          </div>)}
        </div>
      ) : <p className="muted">No transcript has been recorded yet.</p>}
    </div>

    <div className="card">
      <h2>Proctoring events</h2>
      {(info.events || []).length ? <div className="table-scroll"><table>
        <thead><tr><th>Time</th><th>Event</th><th>Detail</th></tr></thead>
        <tbody>{info.events.map((event, index) => <tr key={index}>
          <td>{new Date(`${event.at}Z`).toLocaleTimeString()}</td>
          <td><span className={`badge ${event.is_warning ? "bad" : ""}`}>
            {event.event_type}</span></td><td>{event.detail || "—"}</td>
        </tr>)}</tbody>
      </table></div> : <p className="muted">No proctoring events yet.</p>}
    </div>
  </main>;
}
