"use client";

import { useState } from "react";
import { useRouter } from "next/navigation";
import Link from "next/link";
import { apiSend, saveAdminSession } from "@/lib/api";

export default function AdminLogin() {
  const router = useRouter();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const login = async (e) => {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      const d = await apiSend("/api/admin/login", "POST", { email, password });
      saveAdminSession(d);
      router.push("/admin/dashboard");
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  return (
    <main className="container narrow">
      <div className="card pia-admin-login">
        <div className="pia-form-brand"><img src="/images/pia-logo.png" alt="PIA Pakistan International Airlines" /><span>Recruitment operations</span></div>
        <h1>Admin login</h1>
        <p className="muted">Recruiting portal administration</p>
        <form onSubmit={login}>
          <label>Email</label>
          <input type="email" value={email} required
                 onChange={(e) => setEmail(e.target.value)} />
          <label>Password</label>
          <input type="password" value={password} required
                 onChange={(e) => setPassword(e.target.value)} />
          <button disabled={busy}>{busy ? "Signing in…" : "Sign in"}</button>
        </form>
        {error && <p className="error">{error}</p>}
        <p className="muted" style={{ marginTop: 14 }}>
          <Link href="/admin/forgot">Forgot your password?</Link>
        </p>
      </div>
    </main>
  );
}
