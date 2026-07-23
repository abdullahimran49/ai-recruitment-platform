"use client";

/**
 * Browser proctoring engine.
 *
 * Monitors: camera face presence / count / gaze, microphone voice level,
 * tab-visibility + window focus, fullscreen state, screen-share lifetime,
 * copy/paste/context-menu and devtools shortcuts.
 *
 * The engine handles debounce/cooldowns and sustained-state logic, then calls
 * onViolation(type, detail, snapshotDataUrl|null) exactly once per incident;
 * the page reports it to the server (which decides about termination).
 *
 * Honest scope: a browser cannot stop other applications from running — this
 * is detect-and-punish proctoring (like standard web assessment platforms),
 * not a native lockdown browser.
 */

import { FaceLandmarker, FilesetResolver } from "@mediapipe/tasks-vision";

// MediaPipe's wasm runtime logs benign startup lines (e.g. "INFO: Created
// TensorFlow Lite XNNPACK delegate for CPU") via console.error, which the
// Next.js dev overlay surfaces as a red error box. Filter ONLY that noise;
// real errors pass through untouched.
let _consoleFiltered = false;
function _filterMediapipeNoise() {
  if (_consoleFiltered) return;
  _consoleFiltered = true;
  const orig = console.error.bind(console);
  console.error = (...args) => {
    const first = typeof args[0] === "string" ? args[0] : "";
    if (/^INFO:|^W\d{4}|Created TensorFlow Lite/.test(first)) {
      console.debug(...args);
      return;
    }
    orig(...args);
  };
}

const COOLDOWN_MS = {
  no_face: 8000,
  multiple_faces: 8000,
  gaze_away: 6000,
  head_turn_away: 6000,
  voice_detected: 6000,
  camera_off: 15000,
  tab_switch: 5000,
  window_blur: 5000,
  fullscreen_exit: 5000,
  screen_share_stopped: 10000,
  copy_paste: 5000,
  devtools_key: 5000,
};

// Periodic evidence snapshot interval (info event, never a warning).
const PERIODIC_SNAPSHOT_MS = 75000;

// Face loop runs every 500ms; sustained thresholds are in ticks.
const TICKS_NO_FACE = 5;      // ~2.5s without a face
const TICKS_MULTI_FACE = 3;   // ~1.5s with 2+ faces
const TICKS_GAZE = 5;         // ~2.5s sustained eye-gaze away
const TICKS_HEAD_TURN = 5;    // ~2.5s sustained head turn
const GAZE_THRESHOLD = 0.50;  // blendshape score 0..1 (horizontal gaze)

// Vertical gaze: looking up or down (at phone, notes, etc.)
const GAZE_VERTICAL_THRESHOLD = 0.45;
const TICKS_GAZE_VERTICAL = 5; // ~2.5s looking up/down

// Head pose: yaw/pitch from face landmarks (radians)
const HEAD_YAW_THRESHOLD = 0.45;   // ~25 degrees horizontal head turn
const HEAD_PITCH_THRESHOLD = 0.40; // ~23 degrees vertical head tilt

const VOICE_RMS = 0.055;      // mic loudness considered "speech"
const VOICE_HITS_NEEDED = 3;  // of the last 6 samples (300ms apart)

export class Proctor {
  constructor({ videoEl, onViolation, onPeriodicSnapshot,
                voiceDetection = true }) {
    this.videoEl = videoEl;
    this.onViolation = onViolation;
    this.onPeriodicSnapshot = onPeriodicSnapshot;
    // Off for voice interviews — the candidate is supposed to speak.
    this.voiceDetection = voiceDetection;
    this.camStream = null;
    this.screenStream = null;
    this.landmarker = null;
    this.audioCtx = null;
    this.analyser = null;
    this.running = false;
    this._lastFired = {};
    this._counters = { noFace: 0, multiFace: 0, gaze: 0, gazeVert: 0, headTurn: 0 };
    this._voiceWindow = [];
    this._timers = [];
    this._listeners = [];
    this._blurTimer = null;
  }

  // ---- setup steps (each throws a user-readable Error on failure) ----------

  async initCamera() {
    this.camStream = await navigator.mediaDevices.getUserMedia({
      video: { width: 640, height: 480, facingMode: "user" },
      audio: true,
    });
    if (this.videoEl) {
      this.videoEl.srcObject = this.camStream;
      await this.videoEl.play().catch(() => {});
    }
    // Mic analyser
    this.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const src = this.audioCtx.createMediaStreamSource(this.camStream);
    this.analyser = this.audioCtx.createAnalyser();
    this.analyser.fftSize = 2048;
    src.connect(this.analyser);

    // Camera/mic revoked, unplugged, or disabled mid-test is a violation.
    for (const track of this.camStream.getTracks()) {
      track.addEventListener("ended", () => {
        if (this.running) {
          this._fire("camera_off",
            `${track.kind === "audio" ? "Microphone" : "Camera"} was `
            + "turned off or disconnected", false);
        }
      });
    }
  }

  async initFace(delegate = "GPU") {
    _filterMediapipeNoise();
    if (!this._fileset) {
      this._fileset = await FilesetResolver.forVisionTasks("/mediapipe-wasm");
    }
    const make = (d) => FaceLandmarker.createFromOptions(this._fileset, {
      baseOptions: {
        modelAssetPath: "/models/face_landmarker.task",
        delegate: d,
      },
      runningMode: "VIDEO",
      numFaces: 2,
      outputFaceBlendshapes: true,
      // Defaults (0.5) miss faces in dim webcam lighting; be lenient — the
      // sustained-ticks logic upstream already smooths out flicker.
      minFaceDetectionConfidence: 0.3,
      minFacePresenceConfidence: 0.3,
      minTrackingConfidence: 0.3,
    });
    try {
      this.landmarker = await make(delegate);
      this._delegate = delegate;
    } catch (e) {
      console.warn(`FaceLandmarker ${delegate} delegate failed, using CPU:`, e);
      this.landmarker = await make("CPU");
      this._delegate = "CPU";
    }
  }

  /** The GPU delegate can initialize fine yet silently detect nothing on
   *  some GPU/driver/browser combos. Rebuild on CPU as a fallback. */
  async rebuildFaceCpu() {
    if (this._delegate === "CPU") return false;
    try { this.landmarker?.close(); } catch {}
    this.landmarker = null;
    await this.initFace("CPU");
    return true;
  }

  /** Quick single-shot check used by the system-check screen. */
  detectOnce() {
    if (!this.landmarker || !this.videoEl || this.videoEl.readyState < 2
        || !this.videoEl.videoWidth) {
      return { faces: 0 };
    }
    try {
      // performance.now() must be monotonically increasing between calls;
      // add a small offset to avoid timestamp-collision errors from MediaPipe.
      const ts = Math.max(performance.now(), (this._lastDetectTs || 0) + 1);
      this._lastDetectTs = ts;
      const res = this.landmarker.detectForVideo(this.videoEl, ts);
      return { faces: res.faceLandmarks?.length ?? 0 };
    } catch (e) {
      console.warn("Face detection error:", e);
      return { faces: 0 };
    }
  }

  async initScreenShare() {
    const stream = await navigator.mediaDevices.getDisplayMedia({
      video: { displaySurface: "monitor" },
      audio: false,
    });
    const track = stream.getVideoTracks()[0];
    const surface = track.getSettings().displaySurface;
    if (surface && surface !== "monitor") {
      track.stop();
      throw new Error(
        "You must share your ENTIRE screen, not a window or tab. "
        + "Please try again and pick the whole screen.");
    }
    this.screenStream = stream;
    track.addEventListener("ended", () => {
      if (this.running) {
        this._fire("screen_share_stopped",
          "Candidate stopped sharing their screen", false);
      }
    });
  }

  multiMonitor() {
    return !!(window.screen && window.screen.isExtended);
  }

  // ---- monitoring -----------------------------------------------------------

  start() {
    this.running = true;
    this._timers.push(setInterval(() => this._faceTick(), 500));
    this._timers.push(setInterval(() => this._audioTick(), 300));
    // Periodic evidence trail (info events, not warnings).
    this._timers.push(setInterval(() => {
      if (this.running) {
        const snap = this.snapshot();
        if (snap) this.onPeriodicSnapshot?.(snap);
      }
    }, PERIODIC_SNAPSHOT_MS));
    this._on(document, "visibilitychange", () => {
      if (document.hidden) {
        this._fire("tab_switch", "Tab hidden / switched away", false);
      }
    });
    this._on(window, "blur", () => {
      clearTimeout(this._blurTimer);
      this._blurTimer = setTimeout(() => {
        // Only a real violation if focus is still gone (ignores quick
        // browser-UI blips like permission prompts).
        if (!document.hasFocus() && !document.hidden) {
          this._fire("window_blur", "Window lost focus (another app?)", false);
        }
      }, 1500);
    });
    this._on(document, "fullscreenchange", () => {
      if (!document.fullscreenElement) {
        this._fire("fullscreen_exit", "Left fullscreen", false);
      }
    });
    const blockClipboard = (e) => {
      e.preventDefault();
      this._fire("copy_paste", `Blocked ${e.type}`, false);
    };
    for (const ev of ["copy", "paste", "cut"]) {
      this._on(document, ev, blockClipboard);
    }
    this._on(document, "contextmenu", (e) => e.preventDefault());
    this._on(document, "keydown", (e) => {
      const k = e.key.toLowerCase();
      const devtools = e.key === "F12"
        || (e.ctrlKey && e.shiftKey && ["i", "j", "c"].includes(k))
        || (e.ctrlKey && k === "u");
      if (devtools) {
        e.preventDefault();
        this._fire("devtools_key", `Blocked ${e.key}`, false);
      }
      if (e.key === "PrintScreen") {
        this._fire("copy_paste", "PrintScreen pressed", false);
      }
    });
  }

  stop() {
    this.running = false;
    this._timers.forEach(clearInterval);
    this._timers = [];
    this._listeners.forEach(([t, ev, fn]) => t.removeEventListener(ev, fn));
    this._listeners = [];
    clearTimeout(this._blurTimer);
  }

  destroy() {
    this.stop();
    this.camStream?.getTracks().forEach((t) => t.stop());
    this.screenStream?.getTracks().forEach((t) => t.stop());
    this.audioCtx?.close().catch(() => {});
    this.landmarker?.close();
    this.camStream = this.screenStream = this.landmarker = null;
  }

  snapshot() {
    try {
      const v = this.videoEl;
      if (!v || v.readyState < 2) return null;
      const c = document.createElement("canvas");
      c.width = 320;
      c.height = 240;
      c.getContext("2d").drawImage(v, 0, 0, 320, 240);
      return c.toDataURL("image/jpeg", 0.6);
    } catch {
      return null;
    }
  }

  // ---- internals -------------------------------------------------------------

  _on(target, ev, fn) {
    target.addEventListener(ev, fn);
    this._listeners.push([target, ev, fn]);
  }

  _fire(type, detail, withSnapshot) {
    const now = Date.now();
    if (now - (this._lastFired[type] || 0) < (COOLDOWN_MS[type] || 5000)) return;
    this._lastFired[type] = now;
    const evidence = withSnapshot ? this.snapshot() : null;
    this.onViolation?.(type, detail, evidence);
  }

  _faceTick() {
    if (!this.running || !this.landmarker || !this.videoEl
        || this.videoEl.readyState < 2 || !this.videoEl.videoWidth) return;
    let res;
    try {
      const ts = Math.max(performance.now(), (this._lastDetectTs || 0) + 1);
      this._lastDetectTs = ts;
      res = this.landmarker.detectForVideo(this.videoEl, ts);
    } catch {
      return;
    }
    const faces = res.faceLandmarks?.length ?? 0;

    // face presence
    if (faces === 0) {
      this._counters.noFace += 1;
      if (this._counters.noFace >= TICKS_NO_FACE) {
        this._counters.noFace = 0;
        this._fire("no_face", "Face not visible in camera", true);
      }
    } else {
      this._counters.noFace = 0;
    }

    // multiple people
    if (faces >= 2) {
      this._counters.multiFace += 1;
      if (this._counters.multiFace >= TICKS_MULTI_FACE) {
        this._counters.multiFace = 0;
        this._fire("multiple_faces", `${faces} people detected in camera`, true);
      }
    } else {
      this._counters.multiFace = 0;
    }

    // === Eye gaze + head pose detection (only when exactly 1 face) ===
    if (faces === 1) {
      const cats = res.faceBlendshapes?.[0]?.categories;
      const landmarks = res.faceLandmarks?.[0];

      // --- Horizontal gaze (eye blendshapes) ---
      if (cats) {
        const g = {};
        for (const c of cats) g[c.categoryName] = c.score;

        const left = ((g.eyeLookOutLeft || 0) + (g.eyeLookInRight || 0)) / 2;
        const right = ((g.eyeLookInLeft || 0) + (g.eyeLookOutRight || 0)) / 2;
        if (Math.max(left, right) > GAZE_THRESHOLD) {
          this._counters.gaze += 1;
          if (this._counters.gaze >= TICKS_GAZE) {
            this._counters.gaze = 0;
            this._fire("gaze_away",
              `Eyes looking ${left > right ? "left" : "right"} away from screen`, true);
          }
        } else {
          this._counters.gaze = 0;
        }

        // --- Vertical gaze (looking up/down — e.g. at phone on desk) ---
        const lookUp = ((g.eyeLookUpLeft || 0) + (g.eyeLookUpRight || 0)) / 2;
        const lookDown = ((g.eyeLookDownLeft || 0) + (g.eyeLookDownRight || 0)) / 2;
        if (Math.max(lookUp, lookDown) > GAZE_VERTICAL_THRESHOLD) {
          this._counters.gazeVert += 1;
          if (this._counters.gazeVert >= TICKS_GAZE_VERTICAL) {
            this._counters.gazeVert = 0;
            this._fire("gaze_away",
              `Eyes looking ${lookDown > lookUp ? "down" : "up"} away from screen`, true);
          }
        } else {
          this._counters.gazeVert = 0;
        }
      }

      // --- Head pose estimation from face landmarks ---
      if (landmarks && landmarks.length > 0) {
        const headPose = this._estimateHeadPose(landmarks);
        if (headPose) {
          const absYaw = Math.abs(headPose.yaw);
          const absPitch = Math.abs(headPose.pitch);
          if (absYaw > HEAD_YAW_THRESHOLD || absPitch > HEAD_PITCH_THRESHOLD) {
            this._counters.headTurn += 1;
            if (this._counters.headTurn >= TICKS_HEAD_TURN) {
              this._counters.headTurn = 0;
              let dir = "";
              if (absYaw > HEAD_YAW_THRESHOLD) {
                dir = headPose.yaw > 0 ? "right" : "left";
              } else {
                dir = headPose.pitch > 0 ? "down" : "up";
              }
              this._fire("head_turn_away",
                `Head turned ${dir} away from screen`, true);
            }
          } else {
            this._counters.headTurn = 0;
          }
        }
      }
    }
  }

  /**
   * Estimate head yaw (horizontal) and pitch (vertical) from MediaPipe face
   * landmarks using the nose tip, forehead, chin, and ear-region landmarks.
   * Returns { yaw, pitch } in radians, or null if landmarks are missing.
   */
  _estimateHeadPose(landmarks) {
    try {
      // Key landmark indices (MediaPipe 468-point mesh):
      //   1   = nose tip
      //  10   = top of forehead (mid-brow)
      // 152   = bottom of chin
      // 234   = right ear region
      // 454   = left ear region
      const nose   = landmarks[1];
      const top    = landmarks[10];
      const chin   = landmarks[152];
      const rEar   = landmarks[234];
      const lEar   = landmarks[454];
      if (!nose || !top || !chin || !rEar || !lEar) return null;

      // Yaw: compare nose-x to midpoint of ears
      const earMidX = (lEar.x + rEar.x) / 2;
      const earSpan = Math.abs(lEar.x - rEar.x) || 0.001;
      const yaw = Math.atan2(nose.x - earMidX, earSpan) * 2;

      // Pitch: compare nose-y to midpoint of forehead & chin
      const faceMidY = (top.y + chin.y) / 2;
      const faceSpan = Math.abs(chin.y - top.y) || 0.001;
      const pitch = Math.atan2(nose.y - faceMidY, faceSpan) * 2;

      return { yaw, pitch };
    } catch {
      return null;
    }
  }

  _audioTick() {
    if (!this.running || !this.analyser || !this.voiceDetection) return;
    const buf = new Uint8Array(this.analyser.fftSize);
    this.analyser.getByteTimeDomainData(buf);
    let sum = 0;
    for (let i = 0; i < buf.length; i++) {
      const x = (buf[i] - 128) / 128;
      sum += x * x;
    }
    const rms = Math.sqrt(sum / buf.length);
    this._voiceWindow.push(rms > VOICE_RMS ? 1 : 0);
    if (this._voiceWindow.length > 6) this._voiceWindow.shift();
    const hits = this._voiceWindow.reduce((a, b) => a + b, 0);
    if (hits >= VOICE_HITS_NEEDED) {
      this._voiceWindow = [];
      // Include the RMS level in the detail for better logging
      this._fire("voice_detected",
        `Talking / loud sound detected (level: ${rms.toFixed(3)})`, false);
    }
  }
}
