"use client";

import Link from "next/link";
import { useRouter, useSearchParams } from "next/navigation";
import { Suspense, useState } from "react";
import { apiSend, saveApplicantSession } from "@/lib/api";

function RegisterForm() {
  const router = useRouter();
  const params = useSearchParams();
  const requestedNext = params.get("next") || "";
  const next = (requestedNext.startsWith("/portal/")
                && !requestedNext.startsWith("//"))
    ? requestedNext : "/portal/dashboard";

  const [form, setForm] = useState({
    name: "", email: "", cnic: "", phone: "", password: "", confirm: "",
  });
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });

  const submit = async (e) => {
    e.preventDefault();
    setError("");
    if (form.password !== form.confirm) {
      setError("Passwords do not match."); return;
    }
    if (form.password.length < 8) {
      setError("Password must be at least 8 characters."); return;
    }
    setBusy(true);
    try {
      const d = await apiSend("/api/portal/register", "POST", {
        name: form.name, email: form.email, cnic: form.cnic,
        phone: form.phone, password: form.password,
      });
      saveApplicantSession(d);
      router.push(next);
    } catch (err) { setError(err.message); }
    setBusy(false);
  };

  return (
    <main className="portal-narrow">
      <div className="auth-card">
        <h1>Create your account</h1>
        <p className="muted">
          Your CNIC is your unique identity across all our hiring portals — you
          only ever need one account.
        </p>
        <form onSubmit={submit}>
          <label>Full name</label>
          <input value={form.name} required onChange={set("name")} />
          <label>Email</label>
          <input type="email" value={form.email} required onChange={set("email")} />
          <label>CNIC (13 digits)</label>
          <input value={form.cnic} required placeholder="XXXXX-XXXXXXX-X"
                 onChange={set("cnic")} />
          <label>Phone</label>
          <input value={form.phone} onChange={set("phone")} />
          <div className="row">
            <div>
              <label>Password</label>
              <input type="password" value={form.password} required
                     onChange={set("password")} />
            </div>
            <div>
              <label>Confirm password</label>
              <input type="password" value={form.confirm} required
                     onChange={set("confirm")} />
            </div>
          </div>
          <button disabled={busy}>{busy ? "Creating…" : "Create account"}</button>
        </form>
        {error && <p className="error">{error}</p>}
        <p className="muted" style={{ marginTop: 16 }}>
          Already have an account? <Link href="/portal/login">Sign in</Link>
        </p>
      </div>
    </main>
  );
}

export default function Register() {
  return (
    <Suspense fallback={<main className="portal-narrow"><p className="muted">Loading…</p></main>}>
      <RegisterForm />
    </Suspense>
  );
}
