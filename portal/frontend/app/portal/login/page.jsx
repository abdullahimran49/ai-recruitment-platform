"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { apiSend, saveApplicantSession } from "@/lib/api";

function LoginForm() {
  const router = useRouter();
  const params = useSearchParams();
  const next = params.get("next") || "/portal/dashboard";

  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true); setError("");
    try {
      const d = await apiSend("/api/portal/login", "POST", { email, password });
      saveApplicantSession(d);
      router.push(next);
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  return (
    <main className="portal-narrow">
      <div className="auth-card">
        <h1>Sign in</h1>
        <p className="muted">Access your applications, tests, and interviews.</p>
        <form onSubmit={submit}>
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
          <Link href="/portal/forgot">Forgot your password?</Link>
        </p>
        <p className="muted" style={{ marginTop: 6 }}>
          New here? <Link href={`/portal/register?next=${encodeURIComponent(next)}`}>
            Create an account
          </Link>
        </p>
      </div>
    </main>
  );
}

export default function Login() {
  return (
    <Suspense fallback={<main className="portal-narrow"><p className="muted">Loading…</p></main>}>
      <LoginForm />
    </Suspense>
  );
}
