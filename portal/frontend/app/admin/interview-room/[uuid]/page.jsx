"use client";

import { useEffect, useRef, useState } from "react";
import { use } from "react";
import { Room, RoomEvent, Track } from "livekit-client";
import { apiGet, adminSession } from "@/lib/api";

export default function InterviewRoomPage({ params }) {
  const { uuid } = use(params);

  const [session, setSession] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [info, setInfo] = useState(null); // { candidate_name, job_title, status, ... }

  // LiveKit
  const [roomConnected, setRoomConnected] = useState(false);
  const [agentSpeaking, setAgentSpeaking] = useState(false);
  const [candidateSpeaking, setCandidateSpeaking] = useState(false);
  const [messages, setMessages] = useState([]); // { role, text }
  const [liveText, setLiveText] = useState("");
  const [muted, setMuted] = useState(true);

  const roomRef = useRef(null);
  const audioBoxRef = useRef(null);   // one <audio> per remote audio track
  const videoRef = useRef(null);
  const chatEndRef = useRef(null);
  const micTrackRef = useRef(null);

  // ---- auth check ---------------------------------------------------------------

  useEffect(() => {
    const s = adminSession();
    if (!s) {
      setLoading(false);
      return;
    }
    setSession(s);

    // Fetch interview info + join token
    apiGet(`/api/admin/ai-interviews/${uuid}`, s.token)
      .then((d) => {
        setInfo(d);
        return apiGet(`/api/admin/ai-interviews/${uuid}/join-token`, s.token);
      })
      .then((joinData) => {
        connectToRoom(joinData.livekit_url, joinData.livekit_token);
      })
      .catch((e) => {
        if (e.status === 401) {
          setSession(null);
          setError("Session expired — please log in again.");
        } else {
          setError(e.message || "Failed to join interview room.");
        }
      })
      .finally(() => setLoading(false));

    return () => {
      try { roomRef.current?.disconnect(); } catch {}
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [uuid]);

  // auto-scroll transcript
  useEffect(() => {
    chatEndRef.current?.scrollIntoView({ behavior: "smooth" });
  }, [messages, liveText]);

  // ---- LiveKit connection -------------------------------------------------------

  const connectToRoom = async (url, token) => {
    try {
      const room = new Room();
      roomRef.current = room;

      // Remote audio: agent voice AND candidate mic each get their own element
      room.on(RoomEvent.TrackSubscribed, (track, _pub, _participant) => {
        if (track.kind === Track.Kind.Audio && audioBoxRef.current) {
          audioBoxRef.current.appendChild(track.attach());
        }
        if (track.kind === Track.Kind.Video) {
          const el = videoRef.current;
          if (el) track.attach(el);
        }
      });

      room.on(RoomEvent.TrackUnsubscribed, (track) => {
        track.detach().forEach((el) => el.remove());
      });

      // Live captions via transcription. The same final segment can be
      // delivered twice (legacy protocol + text streams) — dedupe by id.
      const seenSegs = new Set();
      room.on(RoomEvent.TranscriptionReceived, (segments, participant) => {
        for (const seg of segments) {
          if (!seg.text?.trim()) continue;
          const role = participant?.identity?.startsWith("candidate")
            ? "candidate" : "interviewer";
          if (seg.final) {
            if (seenSegs.has(seg.id)) continue;
            seenSegs.add(seg.id);
            setMessages((prev) => [...prev, { role, text: seg.text }]);
            setLiveText("");
          } else {
            setLiveText(seg.text);
          }
        }
      });

      // Track who is speaking
      room.on(RoomEvent.ActiveSpeakersChanged, (speakers) => {
        const agentActive = speakers.some(
          (s) =>
            !s.identity?.startsWith("candidate") &&
            !s.identity?.startsWith("admin")
        );
        const candidateActive = speakers.some((s) =>
          s.identity?.startsWith("candidate")
        );
        setAgentSpeaking(agentActive);
        setCandidateSpeaking(candidateActive);
      });

      room.on(RoomEvent.Disconnected, () => {
        setRoomConnected(false);
      });

      await room.connect(url, token);
      setRoomConnected(true);
    } catch (err) {
      setError("Could not connect to room: " + err.message);
    }
  };

  // ---- Mic mute/unmute ----------------------------------------------------------

  const toggleMic = async () => {
    const room = roomRef.current;
    if (!room) return;

    if (muted) {
      // Unmute: get mic and publish
      try {
        const stream = await navigator.mediaDevices.getUserMedia({ audio: true });
        const audioTrack = stream.getAudioTracks()[0];
        if (audioTrack) {
          await room.localParticipant.publishTrack(audioTrack, {
            name: "admin-mic",
          });
          micTrackRef.current = audioTrack;
        }
        setMuted(false);
      } catch (err) {
        setError("Microphone access denied: " + err.message);
      }
    } else {
      // Mute: unpublish and stop mic track
      try {
        if (micTrackRef.current) {
          await room.localParticipant.unpublishTrack(micTrackRef.current);
          micTrackRef.current.stop();
          micTrackRef.current = null;
        }
      } catch {}
      setMuted(true);
    }
  };

  // ---- Render -------------------------------------------------------------------

  if (loading) {
    return (
      <main className="container">
        <div className="card">Loading…</div>
      </main>
    );
  }

  if (!session) {
    return (
      <main className="container narrow">
        <div className="card">
          <h1>🔒 Admin login required</h1>
          <p className="muted">
            You must be logged in as an admin to observe interviews.
          </p>
          <a href="/admin">
            <button>Go to login</button>
          </a>
        </div>
      </main>
    );
  }

  return (
    <main className="container">
      {/* Header */}
      <div className="card">
        <div
          className="row"
          style={{ alignItems: "center", justifyContent: "space-between" }}
        >
          <div>
            <h1 style={{ margin: 0 }}>
              🎙 Interview Room
            </h1>
            {info && (
              <p className="muted" style={{ margin: "4px 0 0" }}>
                <strong>{info.candidate_name}</strong> · {info.job_title} ·{" "}
                {new Date(info.scheduled_at).toLocaleString()}
              </p>
            )}
          </div>
          <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
            <span
              className={`badge ${roomConnected ? "ok" : "bad"}`}
            >
              {roomConnected ? "● connected" : "● disconnected"}
            </span>
            {info?.status && (
              <span
                className={`badge ${
                  info.status === "started"
                    ? "ok"
                    : info.status === "completed"
                    ? ""
                    : info.status === "terminated"
                    ? "bad"
                    : ""
                }`}
              >
                {info.status}
              </span>
            )}
          </div>
        </div>
      </div>

      {error && <p className="error">{error}</p>}

      <div
        className="row"
        style={{ gap: 16, alignItems: "flex-start" }}
      >
        {/* Left: candidate video feed */}
        <div style={{ flex: "1 1 50%", minWidth: 300 }}>
          <div className="card" style={{ padding: 0, overflow: "hidden" }}>
            <video
              ref={videoRef}
              autoPlay
              muted
              playsInline
              style={{
                width: "100%",
                display: "block",
                borderRadius: 8,
                background: "#111",
                minHeight: 280,
                transform: "scaleX(-1)",
              }}
            />
            <div
              style={{
                padding: "8px 14px",
                display: "flex",
                alignItems: "center",
                gap: 8,
              }}
            >
              <span className="muted" style={{ fontSize: 13 }}>
                Candidate camera
              </span>
              {candidateSpeaking && (
                <span className="badge ok" style={{ fontSize: 11 }}>
                  🎤 speaking
                </span>
              )}
            </div>
          </div>

          {/* Admin mic controls */}
          <div className="card" style={{ textAlign: "center" }}>
            <button
              className={muted ? "secondary" : "danger"}
              onClick={toggleMic}
              disabled={!roomConnected}
              style={{ minWidth: 180 }}
            >
              {muted ? "🎤 Unmute to speak" : "🔇 Mute microphone"}
            </button>
            <p className="muted" style={{ margin: "8px 0 0", fontSize: 13 }}>
              {muted
                ? "You are muted — the candidate and AI cannot hear you."
                : "⚠ You are live — the room can hear you."}
            </p>
          </div>
        </div>

        {/* Right: live transcript */}
        <div style={{ flex: "1 1 50%", minWidth: 300 }}>
          <div className="card" style={{ minHeight: 400, maxHeight: 600, overflowY: "auto" }}>
            <h2 style={{ margin: "0 0 12px" }}>
              📜 Live transcript
              {agentSpeaking && (
                <span className="badge" style={{ marginLeft: 10, fontSize: 12 }}>
                  🔊 AI speaking
                </span>
              )}
            </h2>

            {messages.length === 0 && !liveText && (
              <p className="muted">
                {roomConnected
                  ? "Waiting for the interview to begin…"
                  : "Connecting to room…"}
              </p>
            )}

            {messages.map((m, i) => (
              <p key={i} style={{ margin: "10px 0" }}>
                <strong>
                  {m.role === "interviewer" ? "🎙 AI" : "🧑 Candidate"}:
                </strong>{" "}
                {m.text}
              </p>
            ))}

            {liveText && (
              <p style={{ margin: "10px 0", opacity: 0.7 }}>
                <em>{liveText}</em>
              </p>
            )}

            <div ref={chatEndRef} />
          </div>
        </div>
      </div>

      {/* Hidden container for remote audio elements */}
      <div ref={audioBoxRef} style={{ display: "none" }} />
    </main>
  );
}
