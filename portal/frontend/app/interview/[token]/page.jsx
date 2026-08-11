"use client";

import { use, useCallback, useEffect, useRef, useState } from "react";
import { API, apiGet, apiSend } from "@/lib/api";
import { Proctor } from "@/lib/proctor";

const VIOLATION_LABELS = {
  no_face: "Your face is not visible",
  multiple_faces: "Another person was detected",
  gaze_away: "Eyes off the screen",
  head_turn_away: "Head turned away",
  tab_switch: "You switched tabs",
  window_blur: "You switched to another app",
  fullscreen_exit: "You left fullscreen",
  screen_share_stopped: "Screen sharing was stopped",
  copy_paste: "Copy/paste is not allowed",
  devtools_key: "That shortcut is not allowed",
  camera_off: "Camera or microphone was turned off",
};

export default function InterviewPage({ params }) {
  const { token } = use(params);

  const [phase, setPhase] = useState("loading");
  // loading|email|otp|syscheck|waiting|interview|done|terminated|blocked
  const [info, setInfo] = useState(null);
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [resendIn, setResendIn] = useState(0);

  // proctoring
  const videoRef = useRef(null);
  const proctorRef = useRef(null);
  const [check, setCheck] = useState({ cam: "pending", face: "pending",
                                       screen: "pending" });
  const [checkErr, setCheckErr] = useState("");
  const [warnings, setWarnings] = useState(0);
  const [maxWarnings, setMaxWarnings] = useState(3);
  const [violation, setViolation] = useState(null);
  const [fsLost, setFsLost] = useState(false);
  const [terminatedReason, setTerminatedReason] = useState("");

  // conversation
  const [messages, setMessages] = useState([]); // {role, text}
  const [liveText, setLiveText] = useState("");
  const [remaining, setRemaining] = useState(0);
  const [countdown, setCountdown] = useState("");

  // live interview connection state
  const [roomConnected, setRoomConnected] = useState(false);
  const [agentSpeaking, setAgentSpeaking] = useState(false);

  const authRef = useRef("");
  const phaseRef = useRef(phase);
  phaseRef.current = phase;
  const finishedRef = useRef(false);
  const leavingRef = useRef(false);
  const roomRef = useRef(null);

  const chatEndRef = useRef(null);
  const warningsRef = useRef(0);      // last server-confirmed warning count
  const sessionKey = `ats_interview_${token}`;

  // ---- boot -------------------------------------------------------------------

  useEffect(() => {
    apiGet(`/api/portal/interview/${token}/info`)
      .then((d) => {
        setInfo(d);
        setMaxWarnings(d.max_warnings || 3);
        if (["completed", "terminated", "cancelled", "missed"]
            .includes(d.window)) {
          sessionStorage.removeItem(sessionKey);
          setPhase("blocked");
          setError({
            completed: "This interview has already been completed.",
            terminated: "This interview was terminated due to proctoring "
                        + "violations.",
            cancelled: "This interview was cancelled by the recruiting team.",
            missed: "The join window for this interview has passed.",
          }[d.window]);
          return;
        }
        const saved = sessionStorage.getItem(sessionKey);
        if (saved && d.window === "started") {
          authRef.current = saved;
          setPhase("syscheck");
        } else {
          setPhase("email");
        }
      })
      .catch((e) => { setPhase("blocked"); setError(e.message); });
    return () => {
      proctorRef.current?.destroy();
      try { roomRef.current?.disconnect(); } catch {}
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token]);

  useEffect(() => {
    if (resendIn <= 0) return;
    const t = setTimeout(() => setResendIn((s) => s - 1), 1000);
    return () => clearTimeout(t);
  }, [resendIn]);

  // waiting-room countdown to the slot
  useEffect(() => {
    if (phase !== "waiting" || !info) return;
    const iv = setInterval(() => {
      const opens = new Date(info.opens_at);
      const ms = opens.getTime() - Date.now();
      if (ms <= 0) {
        clearInterval(iv);
        setPhase("syscheck");
        return;
      }
      const m = Math.floor(ms / 60000);
      const s = Math.floor((ms % 60000) / 1000);
      setCountdown(`${m}m ${String(s).padStart(2, "0")}s`);
    }, 500);
    return () => clearInterval(iv);
  }, [phase, info]);

  // ---- ending the interview -------------------------------------------------------
  //
  // Three ways out, with distinct owners:
  //  - normal end: the AGENT closes the room when the questions are done or
  //    time is up -> we get RoomEvent.Disconnected -> show "done". The agent
  //    saves the transcript, evaluates, and emails; we never call /finish.
  //  - proctoring termination: WE call /finish with a reason (server records
  //    it, emails, and deletes the room), then show "terminated".
  //  - candidate leaves early: just disconnect; the agent notices the
  //    departure and finalizes with whatever was said so far.

  const cleanupAndShow = useCallback((finalPhase) => {
    if (finishedRef.current) return;
    finishedRef.current = true;
    proctorRef.current?.stop();
    try { roomRef.current?.close(); } catch {}
    proctorRef.current?.destroy();
    document.exitFullscreen?.().catch(() => {});
    sessionStorage.removeItem(sessionKey);
    setPhase(finalPhase);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const doFinish = useCallback(async (reason) => {
    if (finishedRef.current) return;
    setTerminatedReason(reason);
    try {
      // Record the termination BEFORE dropping the room so the agent's
      // finalize pass sees status=terminated and keeps it.
      await apiSend(`/api/portal/interview/${token}/finish`, "POST",
                    { terminated_reason: reason }, authRef.current);
    } catch { /* server auto-finalizes regardless */ }
    cleanupAndShow("terminated");
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [token, cleanupAndShow]);

  const doLeave = useCallback(async () => {
    if (finishedRef.current || leavingRef.current) return;
    leavingRef.current = true;
    setLiveText("Saving and completing your interview…");
    try {
      const ws = roomRef.current;
      if (ws?.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "finish" }));
      }
      await apiSend(`/api/portal/interview/${token}/finish`, "POST",
                    { terminated_reason: null }, authRef.current);
      cleanupAndShow("done");
    } catch {
      leavingRef.current = false;
      setCheckErr("We could not confirm completion. Your answers are saved; "
                  + "please reconnect and try again.");
    }
  }, [token, cleanupAndShow]);

  const reportViolation = useCallback(async (type, detail, evidence) => {
    try {
      const r = await apiSend(
        `/api/portal/interview/${token}/proctor-event`, "POST",
        { event_type: type, detail, evidence }, authRef.current);
      // The server decides what counts: during a spoken interview, gaze/head
      // glances are logged but NOT warnings, so only flash the banner when the
      // count actually went up. Avoids alarming false positives.
      const counted = r.warnings > warningsRef.current;
      warningsRef.current = r.warnings;
      setWarnings(r.warnings);
      if (counted) {
        setViolation({ type, at: Date.now() });
        setTimeout(() => setViolation((v) =>
          v && Date.now() - v.at >= 4500 ? null : v), 5000);
      }
      if (r.terminate) {
        await doFinish(`${r.warnings} proctoring violations (last: ${type})`);
      }
    } catch { /* never break the interview on logging */ }
  }, [token, doFinish]);

  // countdown timer during the interview. At 0 the AGENT wraps up and closes
  // the room; we only force-disconnect if that never arrives (failsafe).
  useEffect(() => {
    if (phase !== "interview") return;
    let failsafe;
    const iv = setInterval(() => {
      setRemaining((r) => {
        if (r <= 1) {
          clearInterval(iv);
          failsafe = setTimeout(() => doLeave(), 5000);
          doLeave();
          return 0;
        }
        return r - 1;
      });
    }, 1000);
    return () => { clearInterval(iv); clearTimeout(failsafe); };
  }, [phase, doLeave]);

  // fullscreen overlay
  useEffect(() => {
    const fn = () => {
      if (phaseRef.current === "interview") {
        setFsLost(!document.fullscreenElement);
      }
    };
    document.addEventListener("fullscreenchange", fn);
    return () => document.removeEventListener("fullscreenchange", fn);
  }, []);

  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, liveText]);

  // ---- OTP flow -------------------------------------------------------------------

  const requestOtp = async (e) => {
    e.preventDefault(); setBusy(true); setError("");
    try {
      await apiSend(`/api/portal/interview/${token}/request-otp`, "POST",
                    { email });
      setPhase("otp");
      setResendIn(60);
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  const verifyOtp = async (e) => {
    e.preventDefault(); setBusy(true); setError("");
    try {
      const d = await apiSend(`/api/portal/interview/${token}/verify-otp`,
                              "POST", { email, code });
      authRef.current = d.token;
      sessionStorage.setItem(sessionKey, d.token);
      setPhase(info?.window === "too_early" ? "waiting" : "syscheck");
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  // ---- system check ------------------------------------------------------------------

  const runCameraCheck = async () => {
    setCheckErr("");
    try {
      const p = new Proctor({
        videoEl: videoRef.current,
        voiceDetection: false,  // the candidate is supposed to talk
        onViolation: (t, d, ev) => reportViolation(t, d, ev),
        onPeriodicSnapshot: (snap) =>
          apiSend(`/api/portal/interview/${token}/proctor-event`, "POST",
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
      if (!ok) setCheckErr("No face detected — improve lighting, face the "
                           + "camera directly, and retry.");
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

  // ---- start interview (LiveKit) ---------------------------------------------------

  const startInterview = async () => {
    setBusy(true); setCheckErr("");
    try {
      await document.documentElement.requestFullscreen();
      const p = proctorRef.current;
      p.start();
      // Send check_passed proctor event
      apiSend(`/api/portal/interview/${token}/proctor-event`, "POST",
              { event_type: "check_passed",
                detail: "camera+mic+face+screen verified "
                        + "(identity snapshot attached)",
                evidence: p.snapshot() },
              authRef.current).catch(() => {});

      // Start or resume the server-side interview session.
      const r = await apiSend(`/api/portal/interview/${token}/join`, "POST",
                              undefined, authRef.current);
      setRemaining(r.remaining_seconds);
      setWarnings(r.warnings || 0);
      warningsRef.current = r.warnings || 0;

      setMessages([]);
      setLiveText("");
      // Connect to the checkpointed interview WebSocket.
      const baseApi = (typeof API === "string" && API.startsWith("http")) ? API : window.location.origin;
      const wsUrl = new URL(r.ws_url, baseApi);
      wsUrl.protocol = wsUrl.protocol.replace('http', 'ws');
      
      const ws = new WebSocket(wsUrl);
      roomRef.current = ws;

      let synthVoices = window.speechSynthesis.getVoices();
      window.speechSynthesis.onvoiceschanged = () => { 
          synthVoices = window.speechSynthesis.getVoices(); 
      };

      let agentIsSpeakingNow = true;
      let listeningEnabled = false;
      let agentSpeechTimeout = null;
      let listeningTimer = null;
      let stopActiveRecording = () => {};
      let disposeVoiceCapture = () => {};
      let agentTurn = 0;
      let serverCompleted = false;

      // Do not let the microphone VAD run until browser TTS has completed.
      // This is deliberately independent of speechSynthesis.speaking, which
      // can remain true forever in Chromium after an utterance has ended.
      const beginListeningAfterEchoDrain = () => {
        clearTimeout(listeningTimer);
        agentIsSpeakingNow = false;
        setAgentSpeaking(false);
        // Give acoustic echo cancellation a short tail to settle.  Without
        // this, speakers can trigger a recording from Nova's final word.
        listeningTimer = setTimeout(() => { listeningEnabled = true; }, 450);
      };

      const beginAgentTurn = () => {
        agentTurn += 1;
        listeningEnabled = false;
        agentIsSpeakingNow = true;
        setAgentSpeaking(true);
        clearTimeout(listeningTimer);
        stopActiveRecording(true);
      };

      ws.onmessage = (e) => {
        const msg = JSON.parse(e.data);
        if (msg.type === "notice") {
          setLiveText(msg.message || "Please try speaking again.");
          return;
        }
        if (msg.type === "error") {
          setCheckErr(msg.message || "The connection had a problem.");
          return;
        }
        if (msg.type === "complete") {
          serverCompleted = true;
          cleanupAndShow("done");
          return;
        }
        if (msg.type === "transcript") {
          setMessages((prev) => [...prev, { role: msg.role, text: msg.text }]);
          
          if (msg.role === "interviewer" && !msg.historical) {
            beginAgentTurn();
            const thisTurn = agentTurn;
            clearTimeout(agentSpeechTimeout);
            // Failsafe in case TTS never calls onend. This only unlocks the
            // mic; it never consults the unreliable browser `speaking` flag.
            agentSpeechTimeout = setTimeout(() => {
                if (thisTurn === agentTurn) beginListeningAfterEchoDrain();
            }, Math.max(4000, msg.text.length * 85 + 2500));
            
            window.speechSynthesis.cancel();
            const u = new SpeechSynthesisUtterance(msg.text);
            const locale = msg.tts_locale || "en-US";
            const localeLower = locale.toLowerCase();
            const baseLanguage = localeLower.split("-")[0];
            u.lang = locale;
            const matchingVoices = synthVoices.filter((v) => {
              const lang = (v.lang || "").toLowerCase();
              return lang === localeLower || lang.startsWith(`${baseLanguage}-`)
                || lang === baseLanguage;
            });
            const voice = matchingVoices.find((v) =>
              /google|natural|microsoft|neural|nova/i.test(v.name))
              || matchingVoices[0];
            if (voice) u.voice = voice;
            u.rate = 1.05;
            u.onend = () => { if (thisTurn === agentTurn) { clearTimeout(agentSpeechTimeout); beginListeningAfterEchoDrain(); } };
            u.onerror = () => { if (thisTurn === agentTurn) { clearTimeout(agentSpeechTimeout); beginListeningAfterEchoDrain(); } };
            window.speechSynthesis.speak(u);
          }
        }
      };
      
      ws.onclose = () => {
        disposeVoiceCapture();
        clearTimeout(agentSpeechTimeout);
        clearTimeout(listeningTimer);
        window.speechSynthesis.cancel();
        setRoomConnected(false);
        if (!finishedRef.current && !serverCompleted) {
          proctorRef.current?.stop();
          proctorRef.current?.destroy();
          document.exitFullscreen?.().catch(() => {});
          phaseRef.current = "syscheck";
          setCheckErr("Connection interrupted. Your answers were saved. Run "
                      + "the check again to resume where you left off.");
          setPhase("syscheck");
        }
      };
      
      ws.onopen = () => {
        ws.send(JSON.stringify({ type: "authenticate",
                                 token: authRef.current }));
        setRoomConnected(true);
      };

      const camStream = p.camStream;
      if (camStream && camStream.getAudioTracks().length > 0) {
        const audioTrack = camStream.getAudioTracks()[0];
        const audioStream = new MediaStream([audioTrack]);

        const audioContext = new (window.AudioContext || window.webkitAudioContext)();
        const source = audioContext.createMediaStreamSource(audioStream);
        const analyser = audioContext.createAnalyser();
        analyser.fftSize = 512;
        source.connect(analyser);

        await audioContext.resume().catch(() => {});
        let silenceStart = 0;
        let speechHits = 0;
        let vadFrame;
        let activeRecording = null;

        const rmsLevel = () => {
          const samples = new Uint8Array(analyser.fftSize);
          analyser.getByteTimeDomainData(samples);
          let energy = 0;
          for (const sample of samples) {
            const value = (sample - 128) / 128;
            energy += value * value;
          }
          return Math.sqrt(energy / samples.length);
        };

        const finishRecording = (discard = false) => {
          const recording = activeRecording;
          if (!recording) return;
          activeRecording = null;
          recording.discard ||= discard;
          if (recording.recorder.state === "recording") recording.recorder.stop();
        };
        stopActiveRecording = finishRecording;

        const startRecording = () => {
          const recording = {
            startedAt: Date.now(), speechMs: 0, discard: false, recorder: null,
          };
          try {
            const options = MediaRecorder.isTypeSupported("audio/webm;codecs=opus")
              ? { mimeType: "audio/webm;codecs=opus" } : undefined;
            const rec = new MediaRecorder(audioStream, options);
            recording.recorder = rec;
            rec.ondataavailable = (e) => {
              const durationMs = Date.now() - recording.startedAt;
              // Only send a confirmed, candidate turn. Metadata gives the
              // server a second guard against silence hallucinations.
              if (!recording.discard && listeningEnabled
                  && durationMs >= 900 && recording.speechMs >= 350
                  && e.data.size >= 4000 && ws.readyState === WebSocket.OPEN) {
                ws.send(JSON.stringify({ type: "audio", duration_ms: durationMs,
                  speech_ms: recording.speechMs }));
                ws.send(e.data);
              }
            };
            rec.start();
          } catch (e) {
            console.error("MediaRecorder error:", e);
            activeRecording = null;
            return;
          }
          activeRecording = recording;
        };

        const checkAudio = () => {
          if (phaseRef.current !== "interview") return;
          
          if (agentIsSpeakingNow || !listeningEnabled) {
             speechHits = 0;
             silenceStart = 0;
             finishRecording(true);
          } else {
             const level = rmsLevel();
             // Hysteresis avoids starting/stopping on individual noisy frames.
             if (level >= 0.025) {
                speechHits += 1;
                silenceStart = 0;
                if (!activeRecording && speechHits >= 4) startRecording();
                if (activeRecording) activeRecording.speechMs += 16;
             } else {
                speechHits = Math.max(0, speechHits - 1);
                if (activeRecording) {
                  if (!silenceStart) silenceStart = Date.now();
                  if (Date.now() - silenceStart >= 1100) finishRecording();
                }
             }
          }
          vadFrame = requestAnimationFrame(checkAudio);
        };
        disposeVoiceCapture = () => {
          cancelAnimationFrame(vadFrame);
          finishRecording(true);
          audioContext.close().catch(() => {});
        };
        // State updates are asynchronous. Set the ref first so the first VAD
        // frame is not accidentally dropped while React commits the UI.
        phaseRef.current = "interview";
        setPhase("interview");
        checkAudio();
      }
      else {
        throw new Error("No microphone track is available. Please re-run the camera and microphone check.");
      }
    } catch (err) {
      if (err.status === 401) {
        sessionStorage.removeItem(sessionKey);
        setPhase("email");
        setError("Your session expired — verify your email again.");
      } else if (err.status === 410) {
        setPhase("blocked"); setError(err.message);
      } else {
        setCheckErr("Could not start: " + err.message);
      }
    }
    setBusy(false);
  };

  const fmt = (s) =>
    `${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`;

  const Status = ({ s }) => (
    <span className={`badge ${s === "ok" ? "ok" : s === "fail" ? "bad" : ""}`}>
      {s === "ok" ? "✓ ready" : s === "fail" ? "✗ failed" : "…"}
    </span>
  );

  const showPreview = phase === "syscheck" || phase === "interview";

  // ================================ RENDER ==========================================

  return (
    <main className="container" style={phase === "interview"
      ? { userSelect: "none", WebkitUserSelect: "none" } : undefined}>

      <video ref={videoRef} autoPlay muted playsInline
        style={{
          display: showPreview ? "block" : "none",
          position: phase === "interview" ? "fixed" : "static",
          bottom: phase === "interview" ? 12 : undefined,
          right: phase === "interview" ? 12 : undefined,
          width: phase === "interview" ? 180 : "100%",
          maxWidth: 420, borderRadius: 10,
          border: "2px solid var(--border)",
          transform: "scaleX(-1)", zIndex: 50,
          margin: phase === "syscheck" ? "0 auto 16px" : 0,
        }} />

      {phase === "loading" && <div className="card">Loading…</div>}

      {phase === "blocked" && (
        <div className="card narrow" style={{ margin: "0 auto" }}>
          <h1>Interview unavailable</h1>
          <p className="error">{String(error)}</p>
        </div>
      )}

      {(phase === "email" || phase === "otp") && info && (
        <div style={{ maxWidth: 620, margin: "0 auto" }}>
          <div className="hero-panel">
            <span className="pill info">🎙 AI Voice Interview</span>
            <h1 style={{ marginTop: 12 }}>{info.job_title}</h1>
            <p className="muted">
              An AI interviewer will ask you questions out loud — you answer by
              speaking naturally. Verify your email to begin.
            </p>
            <div className="spec-grid">
              <div className="spec">
                <div className="k">📅 Scheduled</div>
                <div className="v" style={{ fontSize: 15 }}>{new Date(info.scheduled_at).toLocaleString()}</div>
              </div>
              <div className="spec">
                <div className="k">⏱ Duration</div>
                <div className="v">~{info.duration_minutes}<span style={{ fontSize: 13, fontWeight: 500 }}> min</span></div>
              </div>
            </div>
            <p className="muted" style={{ marginTop: 12 }}>
              <strong>Languages:</strong>{" "}
              {(info.languages || [{ label: "English" }])
                .map((language) => language.label).join(", ")}
            </p>
            {info.window === "too_early" && (
              <p className="muted" style={{ marginTop: 12 }}>⏳ The room opens 10
                minutes before your slot. Verify your identity now to be ready.</p>
            )}
          </div>

          <div className="card">
            <h2>How it works</h2>
            <ul className="rule-list">
              <li><span className="ico">🗣</span> The AI asks questions by voice; speak your answers naturally — there's no typing.</li>
              <li><span className="ico">📷</span> A working camera, microphone and full-screen screen-share are required.</li>
              <li><span className="ico">👤</span> Sit alone in a quiet, well-lit room. The session is monitored; repeated violations end it early.</li>
              <li><span className="ico">💻</span> Use Chrome or Edge on a laptop/desktop with a stable connection.</li>
            </ul>
          </div>

          <div className="card">
            {phase === "email" ? (
              <form onSubmit={requestOtp}>
                <h2>Step 1 — your email</h2>
                <label>The email address your invitation was sent to</label>
                <input type="email" value={email} required
                       onChange={(e) => setEmail(e.target.value)} />
                <button disabled={busy}>
                  {busy ? "Sending…" : "Send verification code"}</button>
              </form>
            ) : (
              <form onSubmit={verifyOtp}>
                <h2>Step 2 — verification code</h2>
                <p className="muted">We emailed a 6-digit code to {email}.</p>
                <label>Code</label>
                <input value={code} required minLength={6} maxLength={6}
                       inputMode="numeric"
                       onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
                <button disabled={busy || code.length !== 6}>
                  {busy ? "Checking…" : "Continue"}</button>
                <button type="button" className="secondary"
                        disabled={busy || resendIn > 0} onClick={requestOtp}>
                  {resendIn > 0 ? `Resend (${resendIn}s)` : "Resend code"}
                </button>
              </form>
            )}
            {error && <p className="error">{String(error)}</p>}
          </div>
        </div>
      )}

      {phase === "waiting" && info && (
        <div className="card narrow" style={{ margin: "0 auto", textAlign: "center" }}>
          <h1>⏳ You're early</h1>
          <p className="muted">Your interview opens at{" "}
            {new Date(info.opens_at).toLocaleTimeString()}.</p>
          <p style={{ fontSize: 32, fontWeight: 700 }}>{countdown}</p>
          <p className="muted">Keep this tab open — you'll continue to the
            equipment check automatically. Find a quiet room and sit alone.</p>
        </div>
      )}

      {phase === "syscheck" && (
        <div className="narrow" style={{ margin: "0 auto" }}>
          <div className="card">
            <h1>Interview check</h1>
            <p className="muted">
              An AI voice will interview you. Speak your answers naturally.
              You may use {(info?.languages || [{ label: "English" }])
                .map((language) => language.label).join(", ")}.{" "}
              Stay in fullscreen, alone, facing the camera. {maxWarnings}{" "}
              violations end the interview.
            </p>
            <div style={{ display: "grid", gap: 10, marginTop: 12 }}>
              <div className="row" style={{ alignItems: "center" }}>
                <span>1. Camera &amp; microphone</span><Status s={check.cam} />
              </div>
              <div className="row" style={{ alignItems: "center" }}>
                <span>2. Face visible</span><Status s={check.face} />
              </div>
              <div className="row" style={{ alignItems: "center" }}>
                <span>3. Share your entire screen</span><Status s={check.screen} />
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
              <button disabled={busy} onClick={startInterview}>
                {busy ? "Starting…" : "Enter fullscreen & start the interview"}
              </button>
            )}
            {checkErr && <p className="error">{checkErr}</p>}
          </div>
        </div>
      )}

      {phase === "interview" && (
        <>
          <div className={`timer ${remaining < 120 ? "low" : ""}`}>
            ⏱ {fmt(remaining)}
            <span className={`badge ${warnings ? "bad" : ""}`}
                  style={{ marginLeft: 12 }}>
              ⚠ {warnings}/{maxWarnings}
            </span>
            <span className="badge" style={{ marginLeft: 8 }}>
              {agentSpeaking ? "🔊 interviewer speaking"
                : roomConnected ? "🎙 listening…" : "connecting…"}
            </span>
            <button className="secondary"
                    style={{ float: "right", margin: "-4px 0 0 0",
                             padding: "4px 8px", fontSize: "0.8em" }}
                    onClick={() => {
                      if (confirm("End the interview early? Your answers so "
                                  + "far will be evaluated.")) doLeave();
                    }}>Leave Interview</button>
            <button className="primary"
                    style={{ float: "right", margin: "-4px 8px 0 0",
                             padding: "4px 8px", fontSize: "0.8em" }}
                    onClick={() => {
                      if (roomRef.current?.readyState === WebSocket.OPEN) {
                        roomRef.current.send(JSON.stringify({ type: "next_question" }));
                      }
                    }}>Next Question</button>
          </div>

          {violation && (
            <div className="card" style={{
              borderColor: "var(--danger)", color: "var(--danger)",
              position: "sticky", top: 64, zIndex: 40, fontWeight: 600 }}>
              ⚠ {VIOLATION_LABELS[violation.type] || violation.type} — warning
              recorded ({warnings}/{maxWarnings}).
            </div>
          )}

          <div className="card" style={{ minHeight: 300 }}>
            {messages.length === 0 && roomConnected && (
              <div style={{ margin: "40px 0", textAlign: "center", opacity: 0.8 }}>
                <div style={{ fontSize: "2rem", marginBottom: "10px" }}>✨</div>
                <p style={{ fontStyle: "italic", fontSize: "1.1rem" }}>
                  Nova is reviewing your profile and getting ready to speak...<br/>
                  Please wait a moment.
                </p>
              </div>
            )}
            {messages.map((m, i) => (
              <p key={i} style={{ margin: "10px 0" }}>
                <strong>{m.role === "interviewer" ? "🎙 Interviewer" : "🧑 You"}:
                </strong>{" "}{m.text}
              </p>
            ))}
            {liveText && (
              <p style={{ margin: "10px 0", opacity: 0.7 }}>
                <em>{liveText}</em>
              </p>
            )}
            <div ref={chatEndRef} />
          </div>



          {fsLost && (
            <div style={{
              position: "fixed", inset: 0, zIndex: 100,
              background: "rgba(10,10,14,0.96)", display: "flex",
              alignItems: "center", justifyContent: "center" }}>
              <div className="card narrow" style={{ textAlign: "center" }}>
                <h2>⚠ Fullscreen required</h2>
                <button onClick={() =>
                  document.documentElement.requestFullscreen()
                    .then(() => setFsLost(false)).catch(() => {})}>
                  Return to fullscreen</button>
              </div>
            </div>
          )}
        </>
      )}

      {phase === "done" && (
        <div className="card narrow" style={{ margin: "0 auto" }}>
          <h1>✅ Interview complete</h1>
          <p className="muted">Thank you — your interview was recorded and
            will be reviewed by the recruiting team. You'll hear back about
            next steps soon.</p>
        </div>
      )}

      {phase === "terminated" && (
        <div className="card narrow" style={{ margin: "0 auto" }}>
          <h1>⛔ Interview terminated</h1>
          <p className="error">Your interview was ended automatically after
            repeated proctoring violations ({terminatedReason}).</p>
          <p className="muted">Your answers up to this point were recorded and
            the recruiting team has been notified.</p>
        </div>
      )}
    </main>
  );
}
