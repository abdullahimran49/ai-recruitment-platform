import "./globals.css";

export const metadata = {
  title: "PIA Careers — ATS Assessment Portal",
  description: "Candidate assessments and recruiting administration",
};

export default function RootLayout({ children }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
