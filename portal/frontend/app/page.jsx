import Link from "next/link";

export default function Home() {
  return (
    <main className="pia-home">
      <div className="pia-sky" aria-hidden="true" />
      <nav className="pia-nav" aria-label="Primary">
        <a className="pia-brand" href="#top" aria-label="PIA Careers home">
          <img
            src="/images/pia-logo.png"
            alt="Pakistan International Airlines"
          />
          <span className="pia-brand-divider" />
          <span>Careers</span>
        </a>
        <Link className="pia-admin-link" href="/admin">Employer access <span>↗</span></Link>
      </nav>

      <section className="pia-hero" id="top">
        <div className="pia-hero-copy">
          <p className="pia-eyebrow"><span /> Pakistan International Airlines</p>
          <h1>Let your career<br />take <em>flight.</em></h1>
          <p className="pia-intro">
            Join the people who connect Pakistan with the world. Explore an opportunity
            that moves you forward.
          </p>
          <div className="pia-hero-actions">
            <Link href="/portal" className="pia-button pia-button-primary">
              Explore opportunities <span aria-hidden="true">→</span>
            </Link>
            <a className="pia-text-link" href="#how-it-works">How it works <span aria-hidden="true">↓</span></a>
          </div>
        </div>

        <div className="pia-visual">
          <div className="pia-visual-frame">
            <img
              src="/images/pia-plane-hero.jpg"
              alt="Pakistan International Airlines inspired passenger jet in flight"
            />
          </div>
          <div className="pia-route-card">
            <span className="pia-route-icon">✦</span>
            <div><b>A future in motion</b><small>One team. Every journey.</small></div>
          </div>
          <p className="pia-credit">PIA careers · connecting people and places</p>
        </div>
      </section>

      <section className="pia-trust-strip" aria-label="Careers highlights">
        <div><strong>01</strong><span>National<br />flag carrier</span></div>
        <div><strong>∞</strong><span>Possibilities to<br />grow with us</span></div>
        <div><strong>24/7</strong><span>Connecting people<br />and places</span></div>
      </section>

      <section className="pia-process" id="how-it-works">
        <div><p className="pia-eyebrow"><span /> Your journey starts here</p><h2>A clearer way to apply.</h2></div>
        <ol>
          <li><i>01</i><div><b>Find your role</b><p>Browse current openings across PIA.</p></div></li>
          <li><i>02</i><div><b>Share your profile</b><p>Apply in a few simple steps.</p></div></li>
          <li><i>03</i><div><b>Stay on course</b><p>We’ll guide you through the process.</p></div></li>
        </ol>
      </section>
    </main>
  );
}
