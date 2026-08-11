"use client";

import Link from "next/link";
import { useRouter } from "next/navigation";
import { useState } from "react";
import { apiSend, saveAdminSession } from "@/lib/api";

export default function Forgot() {
  const router = useRouter();
  const [step, setStep] = useState("request"); // request | reset
  const [email, setEmail] = useState("");
  const [code, setCode] = useState("");
  const [pw, setPw] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const [busy, setBusy] = useState(false);

  const request = async (e) => {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      await apiSend("/api/admin/forgot-password", "POST", { email: email.trim() });
      setNotice(`If an account exists for ${email}, a 6-digit reset code is on its way.`);
      setStep("reset");
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  const reset = async (e) => {
    e.preventDefault();
    setError("");
    if (pw !== confirm) { setError("Passwords do not match."); return; }
    if (pw.length < 8) { setError("Password must be at least 8 characters."); return; }
    setBusy(true);
    try {
      const d = await apiSend("/api/admin/reset-password", "POST", {
        email: email.trim(), code: code.trim(), new_password: pw });
      saveAdminSession(d);
      router.push("/admin/dashboard");
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  return (
    <main className="container narrow">
      <div className="auth-card">
        <h1>Reset your password</h1>
        {step === "request" ? (
          <>
            <p className="muted">Enter your account email and we'll send you a
              6-digit code to set a new password.</p>
            <form onSubmit={request}>
              <label>Email</label>
              <input type="email" value={email} required
                     onChange={(e) => setEmail(e.target.value)} />
              <button disabled={busy}>{busy ? "Sending…" : "Send reset code"}</button>
            </form>
          </>
        ) : (
          <>
            {notice && <p className="success">{notice}</p>}
            <form onSubmit={reset}>
              <label>6-digit code</label>
              <input value={code} required inputMode="numeric" maxLength={6}
                     placeholder="000000"
                     onChange={(e) => setCode(e.target.value.replace(/\D/g, ""))} />
              <label>New password</label>
              <input type="password" value={pw} required
                     onChange={(e) => setPw(e.target.value)} />
              <label>Confirm new password</label>
              <input type="password" value={confirm} required
                     onChange={(e) => setConfirm(e.target.value)} />
              <button disabled={busy || code.length !== 6}>
                {busy ? "Resetting…" : "Set new password"}</button>
              <button type="button" className="secondary" style={{ marginLeft: 8 }}
                      onClick={() => { setStep("request"); setNotice(""); }}>
                Use a different email</button>
            </form>
          </>
        )}
        {error && <p className="error">{error}</p>}
        <p className="muted" style={{ marginTop: 16 }}>
          Remembered it? <Link href="/admin">Back to sign in</Link>
        </p>
      </div>
    </main>
  );
}
