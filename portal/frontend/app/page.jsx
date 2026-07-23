import Link from "next/link";

export default function Home() {
  return (
    <main className="container narrow">
      <div className="card">
        <h1>ATS Assessment Portal</h1>
        <p className="muted">
          Looking for a job? Browse open positions and apply. Already invited to
          a test or interview? Open the personal link from your email.
        </p>
        <div className="row" style={{ marginTop: 12 }}>
          <Link href="/portal"><button>Browse jobs & apply</button></Link>
          <Link href="/admin"><button className="secondary">Admin login</button></Link>
        </div>
      </div>
    </main>
  );
}
