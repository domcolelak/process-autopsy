import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "Process Autopsy",
  description:
    "Reconstructs how work actually moves through a company and quantifies where time is lost.",
};

const NAV = [
  { href: "/", label: "Overview" },
  { href: "/processes", label: "Processes" },
  { href: "/findings", label: "Findings" },
  { href: "/opportunities", label: "Opportunities" },
  { href: "/import", label: "Import data" },
];

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>
        <div className="shell">
          <aside className="sidebar">
            <div className="brand">
              <span className="brand-mark" aria-hidden />
              <span>Process Autopsy</span>
            </div>
            <nav>
              {NAV.map((item) => (
                <Link key={item.href} href={item.href}>
                  {item.label}
                </Link>
              ))}
            </nav>
            <p className="sidebar-note">
              Every figure shown here is computed from the event log. Narrative text is
              generated only from those computed values.
            </p>
          </aside>
          <main className="content">{children}</main>
        </div>
      </body>
    </html>
  );
}
