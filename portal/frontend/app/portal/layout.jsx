"use client";

import "./portal.css";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useEffect, useState } from "react";
import { applicantSession, clearApplicantSession } from "@/lib/api";

export default function PortalLayout({ children }) {
  const router = useRouter();
  const [session, setSession] = useState(null);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    setSession(applicantSession());
    setReady(true);
    const onStorage = () => setSession(applicantSession());
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, []);

  const logout = () => {
    clearApplicantSession();
    setSession(null);
    router.push("/portal");
  };

  return (
    <div className="portal-scope">
      <nav className="portal-nav">
        <Link href="/portal" className="portal-brand">
          <img src="/images/pia-logo.png" alt="PIA Pakistan International Airlines" />
          <span className="logo">✦</span> Careers
        </Link>
        <div className="links">
          <Link href="/portal">Jobs</Link>
          {ready && session ? (
            <>
              <Link href="/portal/dashboard">My applications</Link>
              <span className="who">{session.applicant?.name}</span>
              <span className="navlink" onClick={logout}>Sign out</span>
            </>
          ) : ready ? (
            <>
              <Link href="/portal/login">Sign in</Link>
              <Link href="/portal/register" className="cta">Register</Link>
            </>
          ) : null}
        </div>
      </nav>
      {children}
    </div>
  );
}
