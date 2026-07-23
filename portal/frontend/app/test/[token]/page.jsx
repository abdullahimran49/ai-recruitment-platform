"use client";

import { use, useCallback, useEffect, useRef, useState } from "react";
import { apiGet, apiSend } from "@/lib/api";
import { Proctor } from "@/lib/proctor";

const VIOLATION_LABELS = {
  no_face: "Your face is not visible",
  multiple_faces: "Another person was detected",
  gaze_away: "Eyes off the screen",
  head_turn_away: "Head turned away from screen",
  voice_detected: "Voice / loud sound detected",
  tab_switch: "You switched tabs",
  window_blur: "You switched to another app",
  fullscreen_exit: "You left fullscreen",
  screen_share_stopped: "Screen sharing was stopped",
  copy_paste: "Copy/paste is not allowed",
  devtools_key: "That shortcut is not allowed",
};

export default function TestPage({ params }) {
  const { token } = use(params);

  const [phase, setPhase] = useState("loading");
  // loading|email|otp|syscheck|test|done|terminated|blocked
  const [info, setInfo] = useState(null);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [auth, setAuth] = useState("");
  const [test, setTest] = useState(null);
  const [answers, setAnswers] = useState({});
  const [remaining, setRemaining] = useState(0);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  // proctoring state
  const videoRef = useRef(null);
  const proctorRef = useRef(null);
  const [check, setCheck] = useState({ cam: "pending", face: "pending",
                                       screen: "pending" });
  const [checkErr, setCheckErr] = useState("");
  const [warnings, setWarnings] = useState(0);
  const [maxWarnings, setMaxWarnings] = useState(3);
  const [violation, setViolation] = useState(null);   // last violation banner
  const [fsLost, setFsLost] = useState(false);
  const [terminatedReason, setTerminatedReason] = useState("");
  const submittedRef = useRef(false);
  const answersRef = useRef(answers);
  answersRef.current = answers;
  const authRef = useRef("");
  const phaseRef = useRef(phase);
  phaseRef.current = phase;

  const sessionKey = `ats_test_${token}`;

  useEffect(() => {
    apiGet(`/api/portal/assignment/${token}/info`)
      .then((d) => {
        setInfo(d);
        setMaxWarnings(d.max_warnings || 3);
        if (d.status === "submitted") {
          sessionStorage.removeItem(sessionKey);
          setPhase("blocked");
          setError("This assessment has already been submitted.");
        } else if (d.status === "terminated") {
          sessionStorage.removeItem(sessionKey);
          setPhase("blocked");
          setError("This assessment was terminated due to proctoring "
                   + "violations and cannot be retaken.");
        } else if (d.expired) {
          setPhase("blocked"); setError("This assessment link has expired.");
        } else {
          // Session resume: a refresh/crash mid-test must not lock the
          // candidate out. Verified identity is kept for this browser tab;
          // proctored tests still re-arm all hardware checks before resuming.
          const saved = sessionStorage.getItem(sessionKey);
          if (saved && d.status === "started") {
            setAuth(saved); authRef.current = saved;
            if (d.proctored) {
              setError("");
              setPhase("syscheck");
            } else {
              fetchTest(saved).catch(() => {
                sessionStorage.removeItem(sessionKey);
                setPhase("email");
              });
            }
          } else setPhase("email");
        }
      })
      .catch((e) => { setPhase("blocked"); setError(e.message); });
    return () => { proctorRef.current?.destroy(); };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  // ---- termination ----------------------------------------------------------

  const terminate = useCallback(async (reason) => {
    if (submittedRef.current) return;
    submittedRef.current = true;
    proctorRef.current?.stop();
    setTerminatedReason(reason);
    try {
      await apiSend(`/api/portal/assignment/${token}/submit`, "POST", {
        answers: Object.entries(answersRef.current).map(([qid, idx]) => ({
          question_id: Number(qid), selected_index: idx })),
        terminated_reason: reason,
      }, authRef.current);
    } catch { /* status is terminal server-side regardless */ }
    proctorRef.current?.destroy();
    document.exitFullscreen?.().catch(() => {});
    sessionStorage.removeItem(sessionKey);
    setPhase("terminated");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  const reportViolation = useCallback(async (type, detail, evidence) => {
    setViolation({ type, at: Date.now() });
    setTimeout(() => setViolation((v) =>
      v && Date.now() - v.at >= 4500 ? null : v), 5000);
    try {
      const r = await apiSend(
        `/api/portal/assignment/${token}/proctor-event`, "POST",
        { event_type: type, detail, evidence }, authRef.current);
      setWarnings(r.warnings);
      if (r.terminate) {
        await terminate(`${r.warnings} proctoring violations `
                        + `(last: ${type})`);
      }
    } catch { /* never break the test on a logging failure */ }
  }, [token, terminate]);

  // ---- OTP flow --------------------------------------------------------------

  const requestOtp = async (e) => {
    e.preventDefault(); setBusy(true); setError("");
    try {
      await apiSend(`/api/portal/assignment/${token}/request-otp`, "POST",
                    { email });
      setPhase("otp");
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  const verifyOtp = async (e) => {
    e.preventDefault(); setBusy(true); setError("");
    try {
      const d = await apiSend(`/api/portal/assignment/${token}/verify-otp`,
                              "POST", { email, code });
      setAuth(d.token); authRef.current = d.token;
      // Survive refreshes/crashes in this tab (cleared on completion).
      sessionStorage.setItem(sessionKey, d.token);
      if (info?.proctored) setPhase("syscheck");
      else await fetchTest(d.token);
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  const [resendIn, setResendIn] = useState(0);
  useEffect(() => {
    if (resendIn <= 0) return;
    const t = setTimeout(() => setResendIn((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [resendIn]);

  const resendOtp = async () => {
    setBusy(true); setError("");
    try {
      await apiSend(`/api/portal/assignment/${token}/request-otp`, "POST",
                    { email });
      setResendIn(60);
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  const fetchTest = async (authToken) => {
    const t = await apiGet(`/api/portal/assignment/${token}/test`, authToken);
    setTest(t);
    setRemaining(t.remaining_seconds);
    setWarnings(t.warnings || 0);
    // Crash-safe resume: restore autosaved selections.
    if (t.draft && Object.keys(t.draft).length) {
      const restored = {};
      for (const [qid, idx] of Object.entries(t.draft)) {
        restored[Number(qid)] = idx;
      }
      setAnswers(restored);
    }
    setPhase("test");
  };

  // ---- system check -----------------------------------------------------------

  const runCameraCheck = async () => {
    setCheckErr("");
    try {
      const p = new Proctor({
        videoEl: videoRef.current,
        onViolation: (t, d, ev) => reportViolation(t, d, ev),
        // Silent periodic evidence trail (info event — no banner, no warning).
        onPeriodicSnapshot: (snap) =>
          apiSend(`/api/portal/assignment/${token}/proctor-event`, "POST",
                  { event_type: "periodic_snapshot",
                    detail: "Periodic monitoring snapshot", evidence: snap },
                  authRef.current).catch(() => {}),
      });
      proctorRef.current = p;
      await p.initCamera();
      setCheck((c) => ({ ...c, cam: "ok" }));
      await p.initFace();
      const tryDetect = async (attempts) => {
        for (let i = 0; i < attempts; i++) {
          await new Promise((r) => setTimeout(r, 500));
          if (p.detectOnce().faces >= 1) return true;
        }
        return false;
      };
      let ok = await tryDetect(8);
      if (!ok && (await p.rebuildFaceCpu())) {
        // GPU delegate can silently see nothing on some machines — retry on CPU
        ok = await tryDetect(8);
      }
      setCheck((c) => ({ ...c, face: ok ? "ok" : "fail" }));
      if (!ok) setCheckErr("No face detected — improve lighting and look "
                           + "at the camera, then retry.");
    } catch (err) {
      setCheck((c) => ({ ...c, cam: "fail" }));
      setCheckErr("Camera/microphone permission is required: " + err.message);
    }
  };

  const runScreenCheck = async () => {
    setCheckErr("");
    try {
      await proctorRef.current.initScreenShare();
      setCheck((c) => ({ ...c, screen: "ok" }));
    } catch (err) {
      setCheck((c) => ({ ...c, screen: "fail" }));
      setCheckErr(err.message);
    }
  };

  const startProctoredTest = async () => {
    setBusy(true); setCheckErr("");
    try {
      await document.documentElement.requestFullscreen();
      const p = proctorRef.current;
      p.start();
      if (p.multiMonitor()) {
        apiSend(`/api/portal/assignment/${token}/proctor-event`, "POST",
                { event_type: "multi_monitor",
                  detail: "Multiple displays detected at check-in" },
                authRef.current).catch(() => {});
      }
      // Identity snapshot: who actually sat down for this test.
      apiSend(`/api/portal/assignment/${token}/proctor-event`, "POST",
              { event_type: "check_passed",
                detail: "camera+mic+face+screen share verified "
                        + "(identity snapshot attached)",
                evidence: p.snapshot() },
              authRef.current).catch(() => {});
      await fetchTest(authRef.current);
    } catch (err) {
      if (err.status === 401) {
        sessionStorage.removeItem(sessionKey);
        setPhase("email");
        setError("Your session expired — verify your email again.");
      } else {
        setCheckErr("Could not start: " + err.message);
      }
    }
    setBusy(false);
  };

  // fullscreen overlay tracking during the test
  useEffect(() => {
    const fn = () => {
      if (phaseRef.current === "test" && info?.proctored) {
        setFsLost(!document.fullscreenElement);
      }
    };
    document.addEventListener("fullscreenchange", fn);
    return () => document.removeEventListener("fullscreenchange", fn);
  }, [info]);

  // Autosave answers (crash-safe draft), debounced; doubles as heartbeat.
  const saveDraft = useCallback(() => {
    if (phaseRef.current !== "test" || submittedRef.current) return;
    apiSend(`/api/portal/assignment/${token}/draft`, "POST", {
      answers: Object.entries(answersRef.current).map(([qid, idx]) => ({
        question_id: Number(qid), selected_index: idx })),
    }, authRef.current).catch(() => {});
  }, [token]);

  useEffect(() => {
    if (phase !== "test") return;
    const t = setTimeout(saveDraft, 1500);       // debounce after a change
    return () => clearTimeout(t);
  }, [answers, phase, saveDraft]);

  useEffect(() => {
    if (phase !== "test") return;
    const hb = setInterval(saveDraft, 20000);    // heartbeat for live view
    return () => clearInterval(hb);
  }, [phase, saveDraft]);

  // Warn before closing/refreshing mid-test (answers are drafted, but the
  // candidate should know the timer keeps running).
  useEffect(() => {
    if (phase !== "test") return;
    const fn = (e) => { e.preventDefault(); e.returnValue = ""; };
    window.addEventListener("beforeunload", fn);
    return () => window.removeEventListener("beforeunload", fn);
  }, [phase]);

  // ---- submit + timer ----------------------------------------------------------

  const submit = useCallback(async (authToken, current) => {
    if (submittedRef.current) return;
    submittedRef.current = true;
    proctorRef.current?.stop();
    try {
      await apiSend(`/api/portal/assignment/${token}/submit`, "POST", {
        answers: Object.entries(current).map(([qid, idx]) => ({
          question_id: Number(qid), selected_index: idx })),
      }, authToken);
      proctorRef.current?.destroy();
      document.exitFullscreen?.().catch(() => {});
      sessionStorage.removeItem(sessionKey);
      setPhase("done");
    } catch (err) {
      submittedRef.current = false;
      proctorRef.current?.start?.();
      setError(err.message);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  useEffect(() => {
    if (phase !== "test") return;
    const iv = setInterval(() => {
      setRemaining((r) => {
        if (r <= 1) { clearInterval(iv); submit(authRef.current, answersRef.current); return 0; }
        return r - 1;
      });
    }, 1000);
    return () => clearInterval(iv);
  }, [phase, submit]);

  const fmt = (s) =>
    `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

  const Status = ({ s }) => (
    <span className={`badge ${s === "ok" ? "ok" : s === "fail" ? "bad" : ""}`}>
      {s === "ok" ? "✓ ready" : s === "fail" ? "✗ failed" : "…"}
    </span>
  );

  // ============================ RENDER ==========================================

  const showPreview = phase === "syscheck" || phase === "test";

  return (
    <main className="container" style={phase === "test"
      ? { userSelect: "none", WebkitUserSelect: "none" } : undefined}>

      {/* persistent camera element (kept mounted across phases) */}
      <video ref={videoRef} autoPlay muted playsInline
        style={{
          display: showPreview ? "block" : "none",
          position: phase === "test" ? "fixed" : "static",
          bottom: phase === "test" ? 12 : undefined,
          right: phase === "test" ? 12 : undefined,
          width: phase === "test" ? 180 : "100%",
          maxWidth: 420,
          borderRadius: 10,
          border: "2px solid var(--border)",
          transform: "scaleX(-1)",
          zIndex: 50,
          margin: phase === "syscheck" ? "0 auto 16px" : 0,
        }} />

      {phase === "loading" && <div className="card">Loading…</div>}

      {phase === "blocked" && (
        <div className="card narrow" style={{ margin: "0 auto" }}>
          <h1>Assessment unavailable</h1>
          <p className="error">{error}</p>
        </div>
      )}

      {(phase === "email" || phase === "otp") && (
        <div style={{ maxWidth: 620, margin: "0 auto" }}>
          <div className="hero-panel">
            <span className="pill info">Online Assessment</span>
            <h1 style={{ marginTop: 12 }}>{info?.job_title}</h1>
            <p className="muted">
              Read the details below, then verify your email to begin. Your
              answers are saved as you go.
            </p>
            <div className="spec-grid">
              <div className="spec">
                <div className="k">📝 Questions</div>
                <div className="v">{info?.num_questions ?? "—"}</div>
              </div>
              <div className="spec">
                <div className="k">⏱ Time limit</div>
                <div className="v">{info?.duration_minutes}<span style={{ fontSize: 13, fontWeight: 500 }}> min</span></div>
              </div>
              <div className="spec">
                <div className="k">🎯 Format</div>
                <div className="v" style={{ fontSize: 16 }}>Multiple choice</div>
              </div>
              <div className="spec">
                <div className="k">🔒 Proctoring</div>
                <div className="v" style={{ fontSize: 16 }}>{info?.proctored ? "On" : "Off"}</div>
              </div>
            </div>
          </div>

          <div className="card">
            <h2>Before you start</h2>
            <ul className="rule-list">
              <li><span className="ico">⏱</span> The timer ({info?.duration_minutes} minutes) starts only when you begin, and cannot be paused. When it reaches zero, your answers are submitted automatically.</li>
              <li><span className="ico">✔</span> You can answer in any order and change answers until you submit. You can submit only once; unanswered questions count as incorrect.</li>
              {info?.proctored && (
                <>
                  <li><span className="ico">📷</span> This is a <strong>proctored</strong> test: a working camera, microphone and full-screen screen-share are required.</li>
                  <li><span className="ico">👤</span> Stay alone, on camera, in full-screen. No switching tabs or apps, and copy/paste is disabled — <strong>{maxWarnings} violations end the test automatically</strong>.</li>
                  <li><span className="ico">💻</span> Use Chrome or Edge on a laptop/desktop in a quiet, well-lit room.</li>
                </>
              )}
            </ul>
          </div>

          <div className="card">
            {phase === "email" ? (
              <form onSubmit={requestOtp}>
                <h2>Step 1 — your email</h2>
                <label>The email address your invitation was sent to</label>
                <input type="email" value={email} required
                       onChange={(e) => setEmail(e.target.value)}
                       placeholder="you@example.com" />
                <button disabled={busy}>
                  {busy ? "Sending…" : "Send verification code"}</button>
              </form>
            ) : (
              <form onSubmit={verifyOtp}>
                <h2>Step 2 — verification code</h2>
                <p className="muted">We emailed a 6-digit code to {email}.</p>
                <label>Code</label>
                <input value={code} required minLength={6} maxLength={6}
                       inputMode="numeric" pattern="[0-9]*"
                       onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
                <button disabled={busy || code.length !== 6}>
                  {busy ? "Checking…" : "Continue"}</button>
                <button type="button" className="secondary"
                        disabled={busy || resendIn > 0} onClick={resendOtp}>
                  {resendIn > 0 ? `Resend code (${resendIn}s)` : "Resend code"}
                </button>
                <button type="button" className="secondary"
                        onClick={() => { setPhase("email"); setCode(""); }}>
                  Different email</button>
              </form>
            )}
            {error && <p className="error">{error}</p>}
          </div>
        </div>
      )}

      {phase === "syscheck" && (
        <div className="narrow" style={{ margin: "0 auto" }}>
          <div className="card">
            <h1>Proctoring check</h1>
            <p className="muted">
              Complete all steps. During the test you must stay in fullscreen,
              keep your face visible, remain alone and silent, and not switch
              apps or tabs. Copy/paste is disabled. {maxWarnings} violations
              end the test.
            </p>

            <div style={{ display: "grid", gap: 10, marginTop: 12 }}>
              <div className="row" style={{ alignItems: "center" }}>
                <span>1. Camera &amp; microphone</span>
                <Status s={check.cam} />
              </div>
              <div className="row" style={{ alignItems: "center" }}>
                <span>2. Face visible</span>
                <Status s={check.face} />
              </div>
              <div className="row" style={{ alignItems: "center" }}>
                <span>3. Share your entire screen</span>
                <Status s={check.screen} />
              </div>
            </div>

            {check.cam !== "ok" || check.face !== "ok" ? (
              <button onClick={runCameraCheck}>
                {check.cam === "pending" ? "Enable camera & microphone"
                  : "Retry camera / face check"}
              </button>
            ) : check.screen !== "ok" ? (
              <button onClick={runScreenCheck}>Share entire screen</button>
            ) : (
              <button disabled={busy} onClick={startProctoredTest}>
                {busy ? "Starting…" : "Enter fullscreen & start the test"}
              </button>
            )}
            {checkErr && <p className="error">{checkErr}</p>}
            <p className="muted" style={{ marginTop: 12, fontSize: 12 }}>
              The timer starts only after you click start. Your camera feed is
              analysed in your browser; snapshots are stored only when a
              violation occurs.
            </p>
          </div>
        </div>
      )}

      {phase === "test" && test && (
        <>
          <div className={`timer ${remaining < 120 ? "low" : ""}`}>
            ⏱ {fmt(remaining)} — answered {Object.keys(answers).length}/
            {test.questions.length}
            {test.proctored && (
              <span className={`badge ${warnings ? "bad" : ""}`}
                    style={{ marginLeft: 12 }}>
                ⚠ warnings {warnings}/{maxWarnings}
              </span>
            )}
          </div>

          {violation && (
            <div className="card" style={{
              borderColor: "var(--danger)", color: "var(--danger)",
              position: "sticky", top: 64, zIndex: 40, fontWeight: 600 }}>
              ⚠ {VIOLATION_LABELS[violation.type] || violation.type} — warning
              recorded ({warnings}/{maxWarnings}). At {maxWarnings} the test
              ends automatically.
            </div>
          )}

          {test.questions.map((q, qi) => (
            <div className="card" key={q.id}>
              <p className="q">Q{qi + 1}. {q.question}</p>
              {q.options.map((o) => (
                <label key={o.idx}
                       className={`opt ${answers[q.id] === o.idx ? "selected" : ""}`}>
                  <input type="radio" name={`q${q.id}`}
                         checked={answers[q.id] === o.idx}
                         onChange={() => setAnswers((a) => ({ ...a, [q.id]: o.idx }))} />
                  <span>{o.text}</span>
                </label>
              ))}
            </div>
          ))}
          <div className="card">
            <p className="muted">You can submit once. Unanswered questions
              count as incorrect.</p>
            <button disabled={busy}
                    onClick={() => submit(authRef.current, answers)}>
              Submit assessment
            </button>
            {error && <p className="error">{error}</p>}
          </div>

          {fsLost && (
            <div style={{
              position: "fixed", inset: 0, zIndex: 100,
              background: "rgba(10,10,14,0.96)", display: "flex",
              alignItems: "center", justifyContent: "center" }}>
              <div className="card narrow" style={{ textAlign: "center" }}>
                <h2>⚠ Fullscreen required</h2>
                <p className="muted">A warning has been recorded. Return to
                  fullscreen to continue your test.</p>
                <button onClick={() =>
                  document.documentElement.requestFullscreen()
                    .then(() => setFsLost(false)).catch(() => {})}>
                  Return to fullscreen
                </button>
              </div>
            </div>
          )}
        </>
      )}

      {phase === "done" && (
        <div className="card narrow" style={{ margin: "0 auto" }}>
          <h1>✅ Submitted</h1>
          <p className="muted">Thank you — your answers were recorded. The
            recruiting team will be in touch about next steps.</p>
        </div>
      )}

      {phase === "terminated" && (
        <div className="card narrow" style={{ margin: "0 auto" }}>
          <h1>⛔ Test terminated</h1>
          <p className="error">Your assessment was ended automatically after
            repeated proctoring violations ({terminatedReason}).</p>
          <p className="muted">The answers you gave before termination were
            recorded and the recruiting team has been notified with the
            session log.</p>
        </div>
      )}
    </main>
  );
}
